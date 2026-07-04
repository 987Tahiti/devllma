"""
Enrichissement de la base de connaissance semantique de DevLLMA.

Injecte dans la table embeddings (RAG, nomic-embed-text) un corpus soigne :
- "lesson"    : lecons tirees des bugs reellement rencontres sur ce poste
                (chaque projet de demo a revele au moins une faille, cf. HANDOFF.md)
- "system"    : faits sur CE poste precis (chemins, services, materiels)
- "knowledge" : bonnes pratiques que le modele local oublie regulierement

Ces souvenirs sont retrouves par similarite cosinus a chaque demande pertinente :
- webui.py handle_prompt (phase 0) les injecte dans le plan du brain,
- l'agent generaliste peut les chercher via son outil memory_search.

Relancable sans risque : mem_index deduplique les chunks identiques.
Usage : python seed_knowledge.py
"""
import sys
sys.path.insert(0, r"C:\Devllma")
from db import init, mem_index

init()

CORPUS = [
    # ── Leçons des bugs réellement rencontrés ────────────────────────────────
    ("lesson", "serveur-fastapi-demarrage",
     "Piege FastAPI recurrent sur ce poste : un fichier serveur genere sans le bloc "
     "`if __name__ == \"__main__\": uvicorn.run(app, host=\"0.0.0.0\", port=...)` se termine "
     "immediatement avec exit 0. Ca RESSEMBLE a un succes (pas d'erreur) mais le serveur n'a "
     "jamais demarre. Toujours inclure ce bloc, et verifier le succes en sondant reellement le "
     "port TCP + une requete HTTP, jamais en se fiant au code retour seul."),

    ("lesson", "sqlite-fastapi-thread",
     "SQLite + FastAPI : toujours ouvrir la connexion avec "
     "sqlite3.connect(chemin, check_same_thread=False), sinon TOUTES les routes plantent avec "
     "'SQLite objects created in a thread can only be used in that same thread'. Ce bug a bloque "
     "toutes les pages du projet blog multi-utilisateurs avant correction."),

    ("lesson", "xss-html-fstring",
     "XSS recurrent dans le code genere : quand du HTML est construit en f-string avec des donnees "
     "utilisateur (titre, commentaire, nom...), toujours passer chaque valeur par html.escape(). "
     "Constate sur les projets gestionnaire de depenses, kanban et blog."),

    ("lesson", "imports-sous-dossier",
     "Projets Python multi-fichiers en sous-dossier : le point d'entree est lance directement "
     "(python app/main.py), donc les imports doivent etre PLATS et sans prefixe de package : "
     "`from database import get_db` et PAS `from app.database import get_db`. Les imports "
     "relatifs (`from .database import ...`) cassent aussi au lancement direct."),

    ("lesson", "port-fantome",
     "Sur Windows, tuer un serveur web de test avec un simple .kill() ne libere pas toujours le "
     "port : les process enfants survivent. Utiliser taskkill /F /T /PID pour tuer l'arbre entier, "
     "sinon le projet suivant echoue avec 'address already in use'."),

    ("lesson", "chemins-windows-hallucines",
     "Ne JAMAIS deviner un chemin utilisateur Windows. Sur ce poste le compte est Admin : le bureau "
     "est C:\\Users\\Admin\\Desktop. Un chemin generique invente comme C:\\Users\\Utilisateur\\... "
     "cree silencieusement un dossier fantome et le fichier atterrit au mauvais endroit (faux succes). "
     "En cas de doute, verifier avec list_dir avant d'ecrire."),

    ("lesson", "reecriture-fichier-tronquee",
     "Les modeles locaux tronquent souvent les gros fichiers quand on leur demande de les reecrire "
     "en entier (le chat temps reel a du etre reecrit a la main pour ca). Pour une petite "
     "modification, preferer une edition ciblee (remplacer un extrait precis) plutot que regenerer "
     "tout le fichier ; et ne jamais remplacer un fichier substantiel par un contenu beaucoup plus court."),

    ("lesson", "succes-fonctionnel-vs-demarrage",
     "'Le process ne plante pas' ne veut PAS dire 'l'application marche'. Plusieurs projets (kanban "
     "vide, serveur jamais demarre, IndexError sur donnees reelles) ont ete declares reussis a tort. "
     "Un vrai test verifie le comportement : requete HTTP sur les endpoints, presence des "
     "fonctionnalites demandees, donnees reellement persistees."),

    # ── Faits sur ce poste précis ────────────────────────────────────────────
    ("system", "poste-chemins",
     "Poste Windows 11 Home, compte utilisateur : Admin. Chemins importants : "
     "bureau C:\\Users\\Admin\\Desktop ; projets DevLLMA C:\\Devllma\\workspace ; "
     "Python 3.11 : C:\\Users\\Admin\\AppData\\Local\\Programs\\Python\\Python311\\python.exe ; "
     "base DevLLMA : C:\\Devllma\\database\\devllma.db. L'interface web tourne sur "
     "http://192.168.1.30:8080/ (tache planifiee DevLLMAWeb), Ollama sur localhost:11434 "
     "(tache planifiee OllamaLLM)."),

    ("system", "poste-modeles",
     "Modeles Ollama installes sur ce poste (CPU uniquement, pas de GPU) : qwen3-coder:30b "
     "(MoE, ~13 tok/s, le meilleur rapport vitesse/qualite mesure, modele par defaut), "
     "qwen2.5-coder:7b (conversation), nomic-embed-text (embeddings memoire), et plus lents : "
     "devstral-small-2:24b, qwen3:14b, deepseek-r1:32b. Resultats de benchmark dans "
     "C:\\Devllma\\bench_results.json."),

    ("system", "poste-sqlserver",
     "Acces bases de donnees sur ce poste : pyodbc est installe avec le driver ODBC 'SQL Server'. "
     "Chaine de connexion type : DRIVER={SQL Server};SERVER=localhost;DATABASE=nom;"
     "Trusted_Connection=yes (authentification Windows) ou UID/PWD pour SQL. "
     "SQLite est disponible nativement pour les fichiers .db locaux."),

    ("system", "poste-services-redemarrage",
     "Le service web DevLLMA tourne en Session 0 via la tache planifiee DevLLMAWeb : impossible de "
     "le tuer par PID direct. Pour redeployer apres modification de webui.py ou agent_core.py : "
     "Stop-ScheduledTask -TaskName 'DevLLMAWeb' puis Start-ScheduledTask -TaskName 'DevLLMAWeb'. "
     "Les fichiers crees par ce service appartiennent a SYSTEM."),

    ("system", "poste-documents",
     "Ce poste peut lire et ecrire des documents bureautiques via C:\\Devllma\\documents.py : "
     "Word .docx (python-docx), Excel .xlsx (openpyxl), PDF (pypdf en lecture, reportlab en "
     "generation). L'agent generaliste les manipule via ses outils read_file/write_file qui "
     "detectent l'extension automatiquement."),

    # ── Bonnes pratiques que le modèle local oublie ──────────────────────────
    ("knowledge", "calculs-precis",
     "Pour tout calcul numerique non trivial (multiplication a plusieurs chiffres, pourcentages, "
     "dates...), ne jamais calculer de tete : utiliser l'outil execute_python avec print(). "
     "Un modele de langage se trompe frequemment en arithmetique mentale."),

    ("knowledge", "heure-date-reelles",
     "L'heure et la date actuelles ne sont jamais connues par le modele : toujours utiliser l'outil "
     "get_datetime. Pour les actualites ou toute information recente, toujours utiliser web_search "
     "plutot que repondre de memoire."),

    ("knowledge", "powershell-francais",
     "Ce Windows est en francais : certains outils en ligne de commande ont des reponses localisees "
     "(ex: takeown /D attend O/N et non Y/N). PowerShell 5.1 : pas d'operateurs && et ||, chainer "
     "avec ; ou if ($?) {...}. Preferer les cmdlets (Remove-Item -Recurse -Force, New-Item, "
     "Get-ChildItem) aux commandes DOS."),

    ("knowledge", "encodage-utf8",
     "Toujours ecrire et lire les fichiers en encoding='utf-8' explicite (Windows utilise cp1252 par "
     "defaut, ce qui casse les accents francais). Pour les scripts Python executes en sous-process, "
     "definir PYTHONIOENCODING=utf-8 si la sortie contient des accents ou emojis."),

    ("knowledge", "verification-apres-action",
     "Apres chaque action sur le poste (creation de fichier, commande, requete SQL), verifier le "
     "resultat reel avant d'annoncer un succes : relire le fichier cree, tester la presence du "
     "dossier, compter les lignes inserees. Annoncer un succes non verifie est pire qu'une erreur "
     "annoncee clairement."),

    ("knowledge", "requetes-sql-securisees",
     "Toujours utiliser des requetes SQL parametrees (placeholders ? ou %s) au lieu de concatener "
     "des valeurs dans la chaine SQL — obligatoire des que la valeur vient d'un utilisateur ou d'un "
     "fichier. Pour SQL Server via pyodbc, cursor.execute('... WHERE nom=?', (valeur,))."),
]

if __name__ == "__main__":
    ok, skipped = 0, 0
    for kind, name, text in CORPUS:
        rid = mem_index(kind, name, text)
        if rid:
            ok += 1
            print(f"  [{kind:9}] {name} -> id {rid}")
        else:
            skipped += 1
            print(f"  [{kind:9}] {name} -> deja present ou embedding indisponible")
    print(f"\n{ok} souvenirs indexes, {skipped} ignores (doublons/indisponibles).")
