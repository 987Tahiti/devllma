import requests, json, sys, re, unicodedata

OLLAMA = "http://localhost:11434"
_http = requests.Session()  # connexion reutilisee (keep-alive) pour les appels Ollama

def strip_accents(s):
    """Retire les accents (é->e, à->a...) pour que le matching de mots-cles fonctionne
    quelle que soit la conjugaison/orthographe tapee par l'utilisateur (Cree/Crée/Créé...).
    Les listes de mots-cles ci-dessous sont volontairement ecrites sans accents."""
    return ''.join(c for c in unicodedata.normalize('NFKD', s) if not unicodedata.combining(c))

def _kw_match(keyword, low):
    """Match un mot-cle en PREFIXE de mot (frontiere seulement au debut).
    - Evite les faux positifs type 'api' trouve au milieu de 'capitale' (aucune
      frontiere avant le 'a' de 'api' puisque precede par un caractere de mot).
    - Accepte quand meme les conjugaisons/variantes: 'develop' matche 'developpe',
      'developper', 'developpement'... (une frontiere de mot APRES le prefixe
      empecherait ces cas legitimes, donc on ne l'exige pas).
    `low` doit deja etre normalise via strip_accents()+lower()."""
    if " " in keyword:
        return keyword in low
    return re.search(r'\b' + re.escape(keyword), low) is not None

DIRECT_RULE = """
Si la tache demande une ACTION sur le PC (creer fichier/dossier, lancer programme, etc.),
reponds UNIQUEMENT avec le bloc de code PowerShell a executer, sans explication.
Format obligatoire pour les actions PC:
```powershell
New-Item ...
```

REGLE ABSOLUE: Ne pose JAMAIS de questions. Ne demande JAMAIS de details.
Produis IMMEDIATEMENT le code ou la reponse demandee.
Si des informations manquent, fais des choix raisonnables et code directement.
Sois concis, technique, et efficace. Pas d introduction, pas de conclusion inutile.
"""

AGENTS = {
  "brain": {
    "model": "qwen2.5-coder:7b",
    "desc": "Orchestrateur central",
    "system": """Tu es le BRAIN de DevLLMA. Tu orchestres 10 agents specialises.
Reponds en 1-2 phrases max pour indiquer ce que tu fais. Pas de blabla.
Si c est une salutation, reponds juste "Pret. Decris ton projet."
""" + DIRECT_RULE
  },
  "architect": {
    "model": "qwen3-coder:30b",
    "desc": "Architecture et conception",
    "system": """Tu es ARCHITECT. Tu fournis des schemas d architecture clairs et concis.
Format: structure en ASCII ou liste bulletee, choix techniques justifies en 1 ligne.
""" + DIRECT_RULE
  },
  "coder": {
    "model": "qwen3-coder:30b",
    "desc": "Ecriture de code",
    "system": """Tu es CODER. Tu ecris du code complet et fonctionnel immediatement.
- Code dans un bloc ```langage
- Imports inclus
- Commentaires seulement si indispensable
- Pas d explication avant le code, juste le code puis 2-3 lignes max d explication apres
""" + DIRECT_RULE
  },
  "debugger": {
    "model": "qwen3-coder:30b",
    "desc": "Detection et correction de bugs",
    "system": """Tu es DEBUGGER. Format: CAUSE: X / FIX: code corrige / PREVENTION: 1 ligne.
""" + DIRECT_RULE
  },
  "reviewer": {
    "model": "qwen3-coder:30b",
    "desc": "Revue de code",
    "system": """Tu es REVIEWER. Format: NOTE: X/10 / POINTS FORTS: liste / PROBLEMES: liste / CODE CORRIGE si necessaire.
""" + DIRECT_RULE
  },
  "tester": {
    "model": "qwen3-coder:30b",
    "desc": "Tests unitaires",
    "system": """Tu es TESTER. Ecris directement les tests avec pytest. Couvre les cas nominaux et les erreurs.
""" + DIRECT_RULE
  },
  "devops": {
    "model": "qwen3-coder:30b",
    "desc": "Docker et deploiement",
    "system": """Tu es DEVOPS. Fournis directement Dockerfile, docker-compose.yml ou scripts CI/CD complets.
""" + DIRECT_RULE
  },
  "database": {
    "model": "qwen3-coder:30b",
    "desc": "Base de donnees et SQL",
    "system": """Tu es DATABASE. Ecris directement le schema SQL, les migrations ou les requetes optimisees.
""" + DIRECT_RULE
  },
  "frontend": {
    "model": "qwen3-coder:30b",
    "desc": "Interface utilisateur",
    "system": """Tu es FRONTEND. Produis directement le code HTML/CSS/JS ou React/Vue complet et fonctionnel.
""" + DIRECT_RULE
  },
  "backend": {
    "model": "qwen3-coder:30b",
    "desc": "API et serveur",
    "system": """Tu es BACKEND. Ecris directement l API complete avec tous les endpoints, validation et gestion d erreurs.
""" + DIRECT_RULE
  },
  "security": {
    "model": "qwen3-coder:30b",
    "desc": "Securite",
    "system": """Tu es SECURITY. Format: VULNERABILITES: liste avec severite / CODE SECURISE: code corrige.
""" + DIRECT_RULE
  },
  "researcher": {
    "model": "qwen2.5-coder:7b",
    "desc": "Recherche internet et reponses factuelles",
    "system": """Tu es RESEARCHER, l agent de recherche de DevLLMA. Tu reponds aux questions generales,
factuelles ou d actualite en te basant sur les resultats de recherche web fournis dans le prompt.
Reponds de maniere claire, structuree, en francais. Cite les sources (URLs) utilisees a la fin sous
"SOURCES:". Si les resultats fournis ne permettent pas de repondre, dis le clairement plutot que d inventer.
Ne pose jamais de question, ne demande jamais de details."""
  },
}

GREETINGS = {"hello","bonjour","hi","salut","hey","allo","test","ok","oui","non"}

# Mots/tournures qui signalent une QUESTION/RECHERCHE plutot qu'une demande de dev
_QUESTION_STARTERS = (
    "qui ","quoi","quel ","quelle","quels","quelles","quand","ou est","où est","pourquoi",
    "comment","combien","est-ce que","qu'est-ce","qu est ce","c'est quoi","c est quoi",
    "explique","resume","compare","cherche","recherche","trouve moi","trouve-moi",
    "actualite","derniere version","derniere nouvelle","quelle est","quel est","donne moi des infos",
    "parle moi de","parle-moi de","qui gagne","quel est le prix","quelle heure","quelle date",
)

def is_research_question(prompt):
    """Heuristique: est-ce une question/recherche generale plutot qu'une demande de dev ?"""
    low = strip_accents(prompt.lower().strip())
    if len(low) < 3:
        return False
    if low.endswith("?"):
        return True
    return any(low.startswith(k) for k in _QUESTION_STARTERS)

def has_dev_keywords(prompt):
    """Vrai si le prompt contient un mot-cle explicite de dev (architecture/code/bug/sql/...)."""
    low = strip_accents(prompt.lower())
    return any(any(_kw_match(k, low) for k in kws) for kws in ROUTING.values())

ROUTING = {
  "architect": ["architecture","conception","design","structure","schema","diagramme","uml"],
  "coder":     ["code","ecris","implemente","fonction","script","programme","cree","fais","genere","develop","python","javascript","java"],
  "debugger":  ["bug","erreur","error","debug","corrige","plante","crash","exception","traceback","fix"],
  "reviewer":  ["revue","review","qualite","ameliore","optimise","refactore","analyse ce code"],
  "tester":    ["test","unittest","pytest","qa","coverage","spec"],
  "devops":    ["deploy","docker","ci","cd","pipeline","nginx","kubernetes","server"],
  "database":  ["sql","base","table","requete","donnees","migration","orm","bdd","sqlite","postgres","mysql"],
  "frontend":  ["html","css","react","vue","interface","page","composant","ui","ux","tailwind","dashboard"],
  "backend":   ["api","rest","endpoint","flask","fastapi","django","route","middleware","authentification"],
  "security":  ["securite","vulnerabilite","auth","token","injection","xss","csrf","faille"],
}

def route(prompt):
    low = strip_accents(prompt.lower().strip())
    if low in GREETINGS or len(low) < 5:
        return []  # Brain repond seul
    found = [a for a,kws in ROUTING.items() if any(_kw_match(k, low) for k in kws)]
    return found[:2] if found else ["coder"]

def call(agent_name, prompt, stream=True):
    cfg = AGENTS.get(agent_name, AGENTS["coder"])
    payload = {
        "model": cfg["model"],
        "system": cfg["system"],
        "prompt": prompt,
        "stream": stream,
        "options": {"temperature": 0.3, "num_predict": 2048}
    }
    if stream:
        print(f"\n\033[1;36m[{agent_name.upper()}]\033[0m ", end="", flush=True)
        full = ""
        try:
            r = _http.post(f"{OLLAMA}/api/generate", json=payload, stream=True, timeout=300)
            for line in r.iter_lines():
                if line:
                    d = json.loads(line)
                    t = d.get("response","")
                    print(t, end="", flush=True)
                    full += t
                    if d.get("done"): break
        except Exception as e:
            print(f"Erreur: {e}")
        print()
        return full
    else:
        try:
            r = _http.post(f"{OLLAMA}/api/generate", json=payload, timeout=300)
            return r.json().get("response","")
        except Exception as e:
            return f"Erreur: {e}"