# DevLLMA — Résumé de session (à l'attention d'un autre assistant IA)

Ce document résume l'état du projet **DevLLMA**, un système d'IA locale multi-agents pour le
développement et la recherche, tournant sur Ollama (CPU uniquement, pas de GPU). Il a été
massivement révisé et testé au cours de cette session. Objectif du document : permettre à un
autre modèle (Fable ou autre) de reprendre le travail avec le contexte nécessaire.

## Architecture du système

- **Dossier racine** : `C:\Devllma\`
- **Interface principale** : `webui.py` — serveur FastAPI + WebSocket, servi sur
  `http://192.168.1.30:8080/`. C'est **le seul point d'entrée voulu par l'utilisateur** — toute
  nouvelle fonctionnalité doit passer par cette interface web, pas par des scripts terminal.
- **Lancement** : géré par une tâche planifiée Windows `DevLLMAWeb` (démarre au boot, tourne en
  Session 0 — d'où l'impossibilité de la tuer par PID direct ; utiliser
  `Stop-ScheduledTask`/`Start-ScheduledTask -TaskName 'DevLLMAWeb'` pour redéployer après une
  modification de `webui.py`).
- **Ollama** : tâche planifiée `OllamaLLM`, sert sur `localhost:11434`.
- **Modèles** : `qwen3-coder:30b` (MoE, ~13 tok/s mesuré, le plus rapide/précis testé) est le
  modèle par défaut pour les agents codeurs. `qwen2.5-coder:7b` est utilisé spécifiquement pour
  le rôle **brain/conversation** (qwen3-coder ignore les consignes de simple discussion et
  génère du code même quand on lui demande de juste discuter). D'autres modèles installés mais
  volontairement pas utilisés par défaut (trop lents sur ce CPU) : `devstral-small-2:24b`,
  `qwen3:14b`, `deepseek-r1:32b`. Résultats de benchmark dans `bench_results.json`.
- **Base de données** : SQLite `database\devllma.db` (`db.py`). Contient sessions/messages/
  tâches/artefacts + une table `embeddings` (mémoire sémantique RAG via `nomic-embed-text`,
  recherche par similarité cosinus vectorisée avec numpy).
- **Agents** (`agents.py`) : brain, architect, coder, debugger, reviewer, tester, devops,
  database, frontend, backend, security, researcher (11+1). Router par mots-clés avec
  normalisation d'accents (`strip_accents`) et correspondance par préfixe de mot.
- **Second projet découvert sur la machine** : `C:\qwen-sdec-project\` — une plateforme plus
  large (FastAPI+JWT+RBAC+PostgreSQL/Docker) sans lien direct avec DevLLMA. A été sécurisée
  (identifiants par défaut remplacés) sur demande explicite de l'utilisateur.

## Routage des requêtes (webui.py `handle_prompt`)

Quatre chemins possibles, dans cet ordre :
1. **Salutation/message très court** → réponse rapide sans pipeline.
2. **Aucun mot-clé de dev détecté** → soit `handle_research` (question factuelle, fait une vraie
   recherche web) soit `handle_chat` (discussion libre, modèle `qwen2.5-coder:7b`).
3. **Mot-clé de dev détecté** (`has_dev_keywords`) OU projet existant référencé OU demande
   d'édition → pipeline complet : plan (brain) → génération de fichiers (coder, format strict
   `###FILE:.../###ENDFILE`) → écriture → scan sécurité → exécution réelle → boucle
   d'auto-correction (jusqu'à 3 itérations, avec détection de blocage/escalade de température si
   la même erreur persiste) → commit git si succès.
4. **Action système directe** (créer un dossier, lancer une app) → PowerShell direct.

## Bugs corrigés dans DevLLMA lui-même pendant cette session

1. Bouton Stop inopérant → l'appel HTTP bloquant à Ollama gelait toute la boucle asyncio ;
   déplacé dans un thread séparé.
2. `execute_project` ne cherchait le point d'entrée qu'à la racine du projet → un projet
   structuré en sous-dossier n'était jamais exécuté ni testé (faux "succès" silencieux).
3. Les serveurs web lancés pour tester un projet n'étaient jamais vraiment tués → fuite de port,
   bloquait les tests des projets suivants (`address already in use`). Corrigé avec
   `taskkill /F /T` (kill d'arbre de process) au lieu d'un simple `.kill()`.
4. Retry automatique (3 tentatives, backoff) ajouté sur les appels Ollama en cas de coupure
   transitoire (l'app Ollama redémarre parfois seule).
5. Le modèle emet parfois un artefact `<code>...</code>` au lieu du format `###FILE`, ce qui
   polluait les fichiers extraits (SyntaxError immédiate). `extract_files()` (`skills.py`)
   nettoie maintenant aussi ce format en plus des fences markdown.
6. **Bug le plus important** : le routage dev/chat comparait les mots-clés (ex: "cree") au texte
   tel quel, sans retirer les accents. "Créé"/"Développe"/"Écris" (accentués, comme on écrit
   naturellement en français) ne matchaient pas "cree"/"develop"/"ecris" (sans accent) → toute
   demande de dev formulée avec des accents partait en simple discussion. Corrigé avec
   normalisation d'accents (`strip_accents` dans `agents.py`) + matching par préfixe de mot
   (garde la protection contre les faux positifs type "api" trouvé dans "capitale", tout en
   acceptant les conjugaisons comme "developpe").
7. Coloration syntaxique JS cassée : le mot-clé "class" (du langage Python/JS) se
   re-surlignait lui-même dans le HTML `<span class="tok-num">` que le highlighter venait
   d'injecter, cassant complètement l'affichage du code. Réécrit en tokenizer single-pass
   (une seule regex avec alternance, pas de passes séquentielles qui s'interfèrent).
8. Détection de blocage ajoutée à la boucle d'auto-correction (webui.py, absente à l'origine
   contrairement à `dev_agent.py` qui l'avait déjà) : si la même erreur persiste, escalade la
   température et instruit explicitement le modèle de changer d'approche au lieu de boucler
   indéfiniment sur la même correction ratée.
9. Prompt CODER_SYSTEM renforcé contre 3 classes de bugs récurrentes constatées sur plusieurs
   projets généré : (a) bloc `if __name__=="__main__": uvicorn.run(...)` manquant (le serveur ne
   démarre jamais mais "exit 0 sans erreur" ressemble à un succès), (b) `sqlite3.connect(...,
   check_same_thread=False)` nécessaire avec FastAPI, (c) échapper le HTML généré en f-string
   (`html.escape()`) pour éviter le XSS, (d) imports simples sans préfixe de package pour les
   projets multi-fichiers en sous-dossier.

## Nouvelles fonctionnalités ajoutées à l'interface

Bouton Stop fonctionnel, vitesse tok/s en direct, mémoire sémantique RAG (recherche de
projets/conversations passés pertinents), agent **researcher** (recherche web + réponse
factuelle sourcée), coloration syntaxique + bouton copier le code, régénérer, restaurer une
sauvegarde (snapshot déjà existant côté serveur, exposé côté UI), export markdown, thème
clair/sombre, sessions dans la barre latérale avec suppression, fenêtre "écran distant"
déplaçable (glisser-déposer), panneau de gestion des modèles Ollama (lister/télécharger sans
terminal), vérification de syntaxe avant exécution (économise un cycle de test complet si le
code ne compile même pas).

## Projets de démonstration générés, testés et corrigés à la main

Chacun a révélé au moins une vraie faille (voir historique de conversation pour le détail).
Tous fonctionnent et tournent en parallèle sur le réseau local :

| Projet | Taille | URL | Point notable trouvé/corrigé |
|---|---|---|---|
| Site vitrine (HTML/CSS/JS) | petit | :8081 | Accessibilité clavier, bug validation HTML5 natif |
| Vérif/génération mots de passe (CLI) | petit | — | Imports relatifs cassés au lancement direct |
| Gestionnaire de dépenses (FastAPI) | moyen | :8082 | JSON brut au lieu de redirection, XSS |
| Mini Kanban (FastAPI) | moyen | :8083 | Fonctionnalités manquantes malgré "succès" déclaré, XSS, IndexError latent, serveur qui ne démarrait jamais |
| Blog multi-utilisateurs (FastAPI+bcrypt) | lourd | :8084 | Bug SQLite thread bloquant TOUTES les pages, bug datetime/string, XSS, fonctionnalités auth manquantes |
| Chat temps réel (FastAPI+WebSockets) | lourd | :8085 | Fichier tronqué par la boucle de correction, réécrit à la main |

**Constat important à transmettre** : DevLLMA seul (sans supervision humaine/IA) n'est pas
encore fiable à 100% sur des projets multi-fichiers complexes — plusieurs échecs en cascade ont
nécessité une intervention manuelle directe plutôt qu'une simple re-génération. Le test de
DevLLMA ("le process ne plante pas au démarrage") ne vérifie jamais que les fonctionnalités
marchent réellement ; plusieurs bugs (Kanban vide, serveur jamais démarré, IndexError sur
données réelles) ont été déclarés "réussis" à tort pour cette raison.

## Ce qu'il reste à faire / points d'attention

- Le "projet lourd" a été fait en 2 exemplaires (blog + chat) à la demande de l'utilisateur ;
  aucun 3e projet lourd n'a été planifié pour l'instant.
- Les imports multi-fichiers en sous-dossier restent un point faible malgré le renforcement du
  prompt — à surveiller sur les prochaines générations.
- `match_existing_project()` (webui.py) ne normalise pas encore les accents contrairement au
  reste du routage — laissé de côté faute de temps, risque de faux négatif mineur sur les noms
  de projets accentués.
- Fichiers de test/diagnostic (`_dispatch_project.py` à la racine de `C:\Devllma`) sont des
  scripts de test que j'ai créés pour piloter le site via WebSocket depuis le terminal — utiles
  pour du diagnostic futur, pas une fonctionnalité livrée à l'utilisateur.
