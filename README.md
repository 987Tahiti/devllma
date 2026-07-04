# DevLLMA

Assistant IA local et autonome, tournant entièrement sur des modèles [Ollama](https://ollama.com/)
en local (CPU, sans GPU requis) — développement logiciel, bureautique et recherche, piloté depuis
une unique interface web (FastAPI + WebSocket).

Aucune donnée n'est envoyée à un service tiers (hors recherche web explicite) : tout le
raisonnement, la génération de code et l'exécution ont lieu sur le poste qui héberge le projet.

## Ce que ça fait

DevLLMA route chaque demande vers le traitement adapté :

- **Question / discussion** → réponse directe, avec recherche web si l'information doit être vérifiée.
- **Tâche ponctuelle** (lire/écrire un fichier, exécuter une commande, interroger une base SQL,
  analyser un CSV, lire une image par OCR, appeler une API...) → un agent généraliste à outils
  (boucle ReAct, tool-calling natif Ollama) décide lui-même des actions à effectuer sur le poste.
- **Génération de projet** ("crée une API FastAPI avec authentification"...) → pipeline dédié :
  planification → génération multi-fichiers → écriture → scan de sécurité → **exécution réelle**
  → boucle d'auto-correction (jusqu'à escalade de température si la même erreur persiste) →
  commit Git automatique d'une version qui fonctionne.

L'interface expose aussi : sessions multiples avec recherche plein texte, mémoire sémantique
(RAG via `nomic-embed-text`) qui retient les projets passés et les leçons tirées de ses propres
erreurs, gestion des modèles Ollama installés, visionneuse de fichiers avec coloration syntaxique,
tableaux de résultats SQL, bibliothèque de prompts personnalisables, glisser-déposer de documents
Word/Excel/PDF pour analyse.

## Architecture

| Fichier | Rôle |
|---|---|
| `webui.py` | Serveur FastAPI + WebSocket, interface web complète, pipeline de génération de projet |
| `agent_core.py` | Agent généraliste à outils (lecture/édition de fichiers, PowerShell, SQL, HTTP, OCR, CSV...) |
| `agents.py` | Définition des agents spécialisés (coder, debugger, reviewer, architecte...) et routage par mots-clés |
| `documents.py` | Lecture/écriture de documents Word (.docx), Excel (.xlsx), PDF |
| `skills.py` | Extraction de fichiers, snapshots/rollback, scan de sécurité, garde-fous anti-destruction |
| `db.py` | Persistance SQLite (sessions, messages) et mémoire sémantique (embeddings + similarité cosinus) |
| `tools.py` | Recherche web, OCR, vérification de syntaxe, détection d'erreurs |
| `dev_agent.py` | Variante autonome en ligne de commande du pipeline de génération, avec validation fonctionnelle |
| `eval_agent.py` | Banc de tests de bout en bout (vérifie des résultats réels, pas juste "le modèle a répondu") |
| `seed_knowledge.py` | Amorce la mémoire sémantique avec des leçons/bonnes pratiques |

## Modèles utilisés

Optimisé pour tourner sur CPU sans carte graphique. Modèle par défaut pour la génération de code :
`qwen3-coder:30b` (architecture MoE, ~3.3 milliards de paramètres actifs par token — aussi rapide
qu'un modèle dense 7B mais bien plus capable). `qwen2.5-coder:7b` est utilisé pour la conversation
libre. `nomic-embed-text` pour la mémoire sémantique. D'autres modèles (14b à 32b) sont supportés
mais nettement plus lents sans GPU.

## Lancer le projet

Prérequis : [Ollama](https://ollama.com/) installé avec au moins un modèle de code, Python 3.11+.

```bash
pip install fastapi uvicorn requests python-docx openpyxl pypdf reportlab beautifulsoup4 pyodbc
python webui.py
```

L'interface est servie sur `http://0.0.0.0:8080/` (accessible depuis tout le réseau local).

> ⚠️ Aucune authentification n'est implémentée sur les endpoints HTTP/WebSocket — l'agent peut
> lire/écrire des fichiers et exécuter des commandes PowerShell sur la machine hôte. À réserver à
> un réseau local de confiance, ou à protéger derrière un reverse proxy avec authentification
> avant toute exposition plus large.

## Limites connues

- Sans supervision, la génération de projets multi-fichiers complexes n'est pas fiable à 100 % —
  le pipeline vérifie autant que possible (exécution réelle, sonde HTTP sur les serveurs générés,
  boucle d'auto-correction) mais une intervention manuelle reste parfois nécessaire.
- Les performances dépendent fortement du CPU hôte ; testé et mesuré sans GPU.
- Conçu à l'origine pour un usage personnel/local — la portabilité vers d'autres environnements
  peut nécessiter d'ajuster des chemins codés en dur (compte utilisateur, emplacement Python).
