import sys, os, json, requests, re, subprocess, asyncio, time, threading, queue as _queue
sys.path.insert(0, r"C:\Devllma")
os.chdir(r"C:\Devllma")

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Request
from fastapi.responses import HTMLResponse, Response, PlainTextResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from db import init, new_session, msg, history, list_sessions, mem_search, mem_index, mem_get, mem_set, stats as db_stats, delete_session, mem_list, mem_delete, mem_purge
from agents import AGENTS, route, OLLAMA, has_dev_keywords, is_research_question, GREETINGS, _kw_match, strip_accents, is_trivial_snippet
from skills import SnapshotManager, Skills, safety_check, security_scan, BrainMemory, extract_files as extract_files_robust, static_self_check, lint_check
try:
    from tools import read_image_text
except Exception:
    read_image_text = lambda p: "(OCR indisponible)"
try:
    from tools import syntax_check
except Exception:
    syntax_check = lambda project_dir: []
try:
    from tools import extract_error_line
except Exception:
    extract_error_line = lambda output: (output or "")[:200]
try:
    from tools import web_search as _web_search
except Exception:
    _web_search = lambda q, n=3: []
from agent_core import run_agent_sync
from ollama_client import (
    KEEP_ALIVE, BRAIN_MODEL, call_brain, preload_models, stream_agent,
    _fetch_ollama_tags, handle_pull_model, check_ollama_ready, _http, _opts,
    cloud_llm, reason_step, ARCHITECTE_SYS, CRITIQUE_SYS,
)
import uvicorn

init()

app = FastAPI(title="DevLLMA")
# CORS restreint aux origines privees (LAN + Tailscale + localhost) plutot que "*" :
# un site web malveillant visite dans le meme navigateur ne doit pas pouvoir piloter
# l'agent (lecture/ecriture de fichiers, PowerShell...) via une requete cross-origin.
# localhost/127.0.0.1, 192.168.0.0/16, 10.0.0.0/8, 172.16.0.0/12 (LAN prive),
# 100.64.0.0/10 (plage CGNAT utilisee par Tailscale, ex: 100.112.22.79).
_PRIVATE_ORIGIN_RE = (
    r"^https?://("
    r"localhost|127\.0\.0\.1|"
    r"192\.168\.\d{1,3}\.\d{1,3}|"
    r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
    r"172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|"
    r"100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}"
    r")(:\d+)?$"
)
app.add_middleware(CORSMiddleware, allow_origin_regex=_PRIVATE_ORIGIN_RE,
                    allow_methods=["*"], allow_headers=["*"])
os.makedirs(r"C:\Devllma\static", exist_ok=True)
app.mount("/static", StaticFiles(directory=r"C:\Devllma\static"), name="static")

SESSION_ID  = new_session("Web Session")
WORKSPACE   = r"C:\Devllma\workspace"
PYTHON      = r"C:\Users\Admin\AppData\Local\Programs\Python\Python311\python.exe"
# Interpreteurs multi-langages (chemins absolus : le service tourne en tache planifiee
# SYSTEM, dont le PATH peut differer de celui d'une session utilisateur normale).
NODE_EXE       = r"C:\Program Files\nodejs\node.exe"
NPM_CMD        = r"C:\Program Files\nodejs\npm.cmd"
BASH_EXE       = r"C:\Program Files\Git\usr\bin\bash.exe"
POWERSHELL_EXE = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
GO_EXE         = r"C:\Program Files\Go\bin\go.exe"
# Cache Go dedie, hors profil utilisateur : le service tourne en tache SYSTEM dont le profil
# (systemprofile) n'est PAS celui deja rechauffe manuellement -> le tout premier "go run" sur ce
# compte aurait subi le meme cout de compilation a froid (constate : 18s, contre 0.4s une fois le
# cache chaud) et declenche un faux echec via le timeout. Un chemin fixe partage, rechauffe une
# fois pour toutes, evite ce piege quel que soit le compte qui execute le service.
GO_CACHE_DIR   = r"C:\Devllma\.gocache"
os.makedirs(WORKSPACE, exist_ok=True)

# ─── Mémoire globale du Brain ────────────────────────────────────────────────
# Chargée une fois au démarrage, mise à jour après chaque projet terminé
BRAIN_MEMORY = {
    "projects": [],       # [{name, path, files, date}]
    "session_todos": [],  # tâches accomplies cette session
    "context": ""         # résumé texte pour le brain
}

# -inf : garantit l'execution du scan initial meme juste apres le boot
# (time.monotonic() ~ uptime Windows, proche de 0 au demarrage de la machine).
_ws_mem_ts = -1e9

# Compteur global de generations en cours (toutes connexions WebSocket confondues),
# lu par _self_watchdog() qui tourne dans un thread separe sans acces aux variables
# locales de ws_handler. Simple int protege par le GIL (incr/decr atomiques en pratique).
_ACTIVE_GEN = {"n": 0}

# Notifications globales vers TOUTES les UI connectees (etat du worker Colab, etc.).
# _colab_keepalive() tourne dans un thread separe : il n'a pas de WebSocket a lui, il
# diffuse donc via _broadcast_threadsafe() en planifiant l'envoi sur _MAIN_LOOP.
_WS_CLIENTS = set()                          # WebSocket actifs (ajoutes/retires par ws_handler)
_MAIN_LOOP = None                            # boucle asyncio principale, capturee au 1er /ws
_COLAB_STATE = {"up": None, "configured": False}  # dernier etat connu du worker Colab

def _broadcast_threadsafe(payload):
    """Diffuse un evenement JSON a toutes les UI connectees, depuis N'IMPORTE quel
    thread : planifie l'envoi sur la boucle asyncio principale. Sans effet si aucune
    UI n'est connectee ou si la boucle n'est pas encore prete."""
    loop = _MAIN_LOOP
    if loop is None or not _WS_CLIENTS:
        return
    async def _send_all():
        for ws in list(_WS_CLIENTS):
            try:
                await ws.send_json(payload)
            except Exception:
                _WS_CLIENTS.discard(ws)
    try:
        asyncio.run_coroutine_threadsafe(_send_all(), loop)
    except Exception:
        pass

def load_workspace_memory(force=False):
    """Scanne le workspace et charge la mémoire des projets existants.
    TTL 5 s : le scan complet (os.walk de chaque projet + SQLite) etait refait a
    CHAQUE prompt dev et get_projects ; on le rejoue au plus toutes les 5 s, sauf
    force=True apres une ecriture de fichiers (la liste projets envoyee a l'UI
    doit refleter le nouveau projet immediatement). Rebind atomique de
    BRAIN_MEMORY["projects"] (GIL) -> pas de verrou, pire cas un scan redondant."""
    global _ws_mem_ts
    if not force and time.monotonic() - _ws_mem_ts < 5:
        return
    projects = []
    if os.path.isdir(WORKSPACE):
        for d in sorted(os.listdir(WORKSPACE)):
            pd = os.path.join(WORKSPACE, d)
            if os.path.isdir(pd):
                flist = []
                for root, dirs, files in os.walk(pd):
                    dirs[:] = [x for x in dirs if x not in {"__pycache__",".git","node_modules",".venv"}]
                    for f in files:
                        rel = os.path.relpath(os.path.join(root, f), pd)
                        flist.append(rel)
                if flist:
                    projects.append({"name":d,"path":pd,"files":flist})
    BRAIN_MEMORY["projects"] = projects
    # Recharger l'historique persistant du brain (survit aux redémarrages)
    BRAIN_MEMORY["session_todos"] = [
        e["event"] for e in BrainMemory.recent_events(8)
    ]
    if projects:
        summary = f"{len(projects)} projet(s) existants: " + ", ".join(p["name"] for p in projects[-5:])
        BRAIN_MEMORY["context"] = summary
    else:
        BRAIN_MEMORY["context"] = "Aucun projet encore créé."
    _ws_mem_ts = time.monotonic()

load_workspace_memory()

def bootstrap_memory():
    """Indexe une fois les projets deja presents dans le workspace pour la memoire semantique."""
    if mem_get("mem_bootstrap_done"):
        return
    for p in BRAIN_MEMORY["projects"]:
        files = read_project(p["path"], max_chars=800, total_budget=3000, max_files=6)
        if files:
            mem_index("project", p["name"], format_context(files)[:2500])
    mem_set("mem_bootstrap_done", "1")

# ─── Prompts ─────────────────────────────────────────────────────────────────

def make_brain_system():
    ctx = BRAIN_MEMORY["context"]
    todos = BRAIN_MEMORY["session_todos"]
    todo_str = "\n".join(f"✓ {t}" for t in todos[-5:]) if todos else "(rien encore)"
    return f"""Tu es le BRAIN de DevLLMA — cerveau central qui planifie chaque projet.

MÉMOIRE WORKSPACE: {ctx}
RÉALISÉ CETTE SESSION:
{todo_str}

CIBLE MATÉRIELLE: poste LOCAL sans carte graphique (CPU uniquement). Choisis des dépendances
LÉGÈRES et qui tournent en pratique sur ce poste. ÉVITE torch/tensorflow/CUDA/diffusers et les
gros modèles ML sauf si l'utilisateur le demande EXPLICITEMENT — préfère une API/bibliothèque
légère, et si une tâche exige vraiment du lourd (génération d'image...), dis-le clairement dans
POINTS CLÉS au lieu de produire un projet qui ne tournera pas ici.

Pour chaque demande produis un plan structuré:
1. PROJET: nom exact du projet
2. STACK: technologies choisies (légères, compatibles CPU)
3. FICHIERS: liste exacte à créer (avec chemins relatifs)
4. ARCHITECTURE: structure en 3-5 lignes
5. POINTS CLÉS: ce qu'il ne faut pas oublier (dont: limites sur ce poste CPU)
6. TODOS: 3-5 tâches numérotées que le codeur va accomplir

Sois précis, concis. Si un projet similaire existe déjà dans le workspace, mentionne-le."""

BRAIN_FIX_SYSTEM = """Tu es le BRAIN. Du code vient d'échouer.
Analyse l'erreur, identifie la cause EXACTE en 2 phrases.
Dis quel fichier corriger et comment (modification précise)."""

CODER_SYSTEM = """Tu es CODER, expert Python/JS/PowerShell/Bash/Go/web. Tu CODES directement sans questions,
quel que soit le langage demande (PowerShell, Bash et Go sont des langages a part entiere, traite-les avec
le meme serieux que Python/JS — jamais de reponse "explicative" a la place du code).
ECRIRE du code PowerShell/.ps1 est TOTALEMENT SANS DANGER, exactement comme ecrire du Python ou du JS —
ce n'est QUE du texte source, personne ne l'execute pendant que tu l'ecris. Ne refuse JAMAIS en pretextant
que "les scripts .ps1 sont bloques pour raisons de securite" (constate : refus errone deja produit sur une
demande de script de sauvegarde parfaitement legitime) — un fichier .ps1 EST un livrable attendu comme
n'importe quel .py/.js.

FORMAT DE SORTIE OBLIGATOIRE. Chaque fichier commence par ###FILE: et finit par ###ENDFILE.
EXEMPLE (à respecter EXACTEMENT, sans ``` markdown):
###FILE: main.py
import outil
print(outil.f())
###ENDFILE
###FILE: outil.py
def f():
    return 42
###ENDFILE

RÈGLES ABSOLUES:
- Application Android / APK -> traite EXACTEMENT comme un site web statique ci-dessous : produis
  UNIQUEMENT index.html/style.css/main.js (interface complete, responsive, pensee pour un ecran
  de telephone). INTERDICTION ABSOLUE de Kivy, Buildozer, buildozer.spec, Kotlin, Java ou Gradle
  meme si c'est ta premiere idee pour "app Android" — le pipeline ne sait PAS les compiler, seul
  le HTML/CSS/JS est automatiquement empaquete en APK reel (squelette Android WebView deja teste
  et fonctionnel). TOUJOURS produire un fichier nomme EXACTEMENT "index.html" (sinon l'empaquetage
  echoue) — AUCUN autre fichier Python/Kivy ne doit etre cree pour cette demande.
- Site web statique (HTML/CSS/JS, pas de backend demandé) -> produis DIRECTEMENT index.html, style.css, main.js etc.
  EN CLAIR (pas de balises ``` a l'interieur du bloc ###FILE). N'ECRIS JAMAIS de script Python qui genere/ecrit
  ces fichiers a la place — les fichiers eux-memes sont le livrable.
- Projet Go -> UN SEUL fichier nomme EXACTEMENT "main.go", package main, fonction func main().
  N'IMPORTE JAMAIS de package tiers (ex: github.com/...) : il n'y a PAS de go.mod dans ce pipeline
  (execute avec "go run main.go" tel quel) -> tout import hors stdlib echoue immediatement avec
  "no required module provides package X: go.mod file not found". Utilise UNIQUEMENT la bibliotheque
  standard (fmt, os, time, net/http, encoding/json, strings, strconv, bufio, errors, sync...), qui
  couvre deja CLI, fichiers, JSON, et meme un petit serveur HTTP.
- Projet Python -> produis TOUS les fichiers, main.py (point d'entrée) en PREMIER
- RÈGLE CRITIQUE: tout module Python importé (ex: import downloader) DOIT avoir son fichier créé (downloader.py)
- requirements.txt = UNIQUEMENT les packages pip externes (ex: requests). JAMAIS la stdlib (os, sys, time, threading, argparse, json, re, queue...)
- Respecte les contraintes: si "en parallèle" est demandé, utilise threading.Thread (start puis join), pas une boucle séquentielle
- Code complet, jamais tronqué. Aucun texte ni ``` hors des blocs ###FILE
- Ne JAMAIS dire "je vais faire...", "je vais vous expliquer...", "voici comment utiliser..." ni aucune
  autre phrase d'INTRODUCTION ou de MODE D'EMPLOI a la place du code — ta toute premiere ligne de reponse
  DOIT etre "###FILE: nom". Constate (PowerShell) : le modele repond parfois une notice d'utilisation
  ("Le script a ete cree dans...", suivi d'instructions Set-ExecutionPolicy) SANS jamais emettre le bloc
  ###FILE -> aucun fichier ecrit, projet vide. Le mode d'emploi n'a AUCUNE valeur sans le code lui-meme.
- Reste concis dans le HTML/CSS (pas de commentaires superflus, pas de contenu redondant) pour ne pas depasser la limite de sortie
- Serveur web (FastAPI/Flask/etc.) -> le fichier principal DOIT se terminer par un bloc
  if __name__ == "__main__": qui lance reellement le serveur (ex: uvicorn.run(app, host="0.0.0.0", port=8000)).
  SANS ce bloc, "python main.py" ne demarre rien et se termine immediatement sans erreur.
- N'IMPORTE QUEL serveur genere (Python/Node/Go/autre), QUEL QUE SOIT le framework -> N'ECOUTE
  JAMAIS sur le port 8080, meme si c'est l'exemple le plus courant dans la doc du langage (ex: Go
  net/http). Le port 8080 est deja occupe par DevLLMA lui-meme -> un serveur genere qui l'utilise
  est signale a tort comme en echec (le port surveille est exclu par securite, jamais verifie) alors
  que le code est correct. Utilise 8000 (Python/Go), 3000 (Node) ou tout port libre >1024 different
  de 8080.
- SQLite utilise avec FastAPI -> sqlite3.connect(..., check_same_thread=False), sinon erreur
  "SQLite objects created in a thread can only be used in that same thread" des la premiere requete.
- Toute donnee saisie par l'utilisateur affichee dans du HTML genere en f-string DOIT etre
  echappee avec html.escape() (ou utiliser Jinja2 dont l'auto-echappement est natif) pour eviter le XSS.
- Projet multi-fichiers reparti dans un sous-dossier (ex: app/main.py, app/database.py) -> les
  imports entre ces fichiers DOIVENT etre en imports simples SANS prefixe de package et SANS point
  (ex: "from database import get_db", PAS "from app.database import get_db" ni "from .database import get_db").
  Le point d'entree est execute directement (python app/main.py), donc "app" n'est PAS un package
  importable — seul l'import simple par nom de fichier fonctionne.
- FastAPI + SQLAlchemy -> NE JAMAIS utiliser un modele SQLAlchemy (classe heritant de Base/declarative_base)
  directement comme response_model d'une route (ex: @app.get("/x", response_model=ModeleSQLAlchemy) ou
  response_model=List[ModeleSQLAlchemy]) : FastAPI plante AU DEMARRAGE avec "Invalid args for response
  field, ... is a valid Pydantic field type ?" (constate de facon repetee). TOUJOURS definir un schema
  Pydantic SEPARE (souvent dans schemas.py) avec `class Config: orm_mode = True` (Pydantic v1) ou
  `model_config = ConfigDict(from_attributes=True)` (Pydantic v2), et utiliser CE schema comme response_model,
  jamais la classe SQLAlchemy elle-meme.
- RÈGLE CRITIQUE (import manquant — bug constate a repetition, sur des fichiers differents dans des
  projets differents) : CHAQUE fichier Python DOIT importer LUI-MEME tout ce qu'il utilise, y compris
  la stdlib (time, io, csv, json, re, datetime, contextlib...). Ne JAMAIS supposer qu'un import present
  dans un AUTRE fichier du meme projet est disponible ici — chaque fichier est un module independant
  avec son propre espace de noms. Avant de finir un fichier, relis-le et verifie que chaque nom utilise
  (fonction, classe, module) est soit defini dans ce fichier, soit importe en tete de CE fichier.
- FastAPI + SQLAlchemy -> la fonction de dependance utilisee avec Depends() (typiquement get_db()) DOIT
  etre un generateur SIMPLE, jamais decore par @contextmanager :
  def get_db():
      db = SessionLocal()
      try:
          yield db
      finally:
          db.close()
  Depends() appelle lui-meme la fonction generatrice ; si elle est decoree @contextmanager, Depends()
  recoit un objet gestionnaire de contexte au lieu de la session SQLAlchemy, et TOUTES les routes qui
  en dependent echouent (constate).
- RÈGLE CRITIQUE (collision de nom — constate a 2 reprises independantes, formes differentes, mais
  meme cause racine) : un nom LOCAL (fonction de route FastAPI, parametre, variable) NE DOIT JAMAIS
  porter EXACTEMENT le meme nom qu'une fonction importee que ce code appelle — le nom local ECRASE
  l'import dans cette portee, et l'appel invoque alors le mauvais objet.
  Cas 1 (route ecrase un import de crud.py) :
      from crud import calculer_amende
      @app.get("/x")
      def calculer_amende(...):        # <- ecrase l'import au niveau module !
          return calculer_amende(...)  # <- s'appelle ELLE-MEME -> RecursionError
  Cas 2 (parametre/flag ecrase un import de fonction) :
      from link_checker import check_links
      def cli_main(check_links: bool):     # <- le PARAMETRE ecrase l'import !
          if check_links:
              check_links(html, path)      # <- appelle le booleen -> TypeError: 'bool' object is not callable
  Ceci passe souvent inapercu car le reste du fichier semble normal. Avant de finir un fichier,
  verifie qu'AUCUN nom de fonction/parametre/variable local ne reutilise le nom d'une fonction
  importee qu'il appelle par ailleurs. Si besoin, renomme le local (ex: suffixe _route, _flag,
  ou prefixe route_/do_) ou importe le module entier ("import crud"/"import link_checker") et
  appelle-le via crud.xxx()/link_checker.xxx().
- RÈGLE CRITIQUE (parametre/option declare mais jamais utilise — constate : un script de sauvegarde
  Bash acceptait --level <niveau> et le stockait dans une variable, mais la commande tar qui cree
  l'archive n'utilisait JAMAIS cette variable -> l'option semblait fonctionner cote CLI mais n'avait
  AUCUN effet reel). Toute option de ligne de commande (argparse/click/param CLI), tout parametre de
  fonction, DOIT etre effectivement UTILISE dans la logique qui suit, pas seulement lu/stocke dans
  une variable. Avant de finir un fichier, pour chaque parametre/option ajoute, verifie qu'il
  influence reellement au moins une commande/decision/calcul en aval — sinon retire-le ou branche-le
  correctement.
- Bash/tar : `tar` N'A PAS d'option `--level` pour le niveau de compression gzip (constate : tar
  l'accepte SANS ERREUR mais l'ignore silencieusement avec l'avertissement "--level is meaningless
  without --listed-incremental" — l'archive est creee avec la compression PAR DEFAUT, le niveau
  demande par l'utilisateur n'a AUCUN effet, et le script rapporte quand meme "succes"). Pour un
  niveau de compression gzip configurable avec tar, utilise soit la variable d'environnement
  `GZIP=-N tar -czf archive.tar.gz dossier/` (N = niveau, AVANT la commande tar), soit un pipe
  explicite `tar -cf - dossier/ | gzip -N > archive.tar.gz`.
- Bash sur Windows (Git Bash, l'environnement d'execution de ce pipeline) : la variable `$USER`
  (convention POSIX/Linux) est VIDE/non definie ici — constate : un script utilisant
  `"/c/Users/$USER/.ssh"` a produit le chemin casse `/c/Users//.ssh` (double slash, $USER vide) et
  a echoue en pretendant que le dossier n'existe pas. Utilise `$USERNAME` (definie par Windows et
  heritee par Git Bash) pour recuperer le nom de l'utilisateur courant, jamais `$USER`.
- Bash sur Windows (meme environnement) : les commandes Linux `uptime`, `free`, et le systeme de
  fichiers `/proc` (`/proc/uptime`, `/proc/loadavg`...) N'EXISTENT PAS (constate : "uptime: command
  not found", verifie qu'aucune n'est installee avec Git Bash sur ce poste). Pour du temps de
  fonctionnement / charge systeme / memoire sur Windows, appelle PowerShell DEPUIS le script Bash
  (powershell.exe est bien dans le PATH, lui) : ex. `powershell.exe -Command
  "(Get-CimInstance Win32_OperatingSystem).LastBootUpTime"` pour l'heure de demarrage, ou
  `Get-Counter '\\Processor(_Total)\\% Processor Time'` pour la charge CPU.
- Bash sur Windows : `ps` est une version Cygwin LIMITEE (options : -a/-e/-f/-h/-l/-p/-s/-u/-V/-W
  UNIQUEMENT — verifie directement), PAS le `ps` GNU/Linux complet. Aucune option `-o`/`--sort`
  n'existe, et AUCUNE colonne memoire (%mem/RSS) n'est jamais affichee, meme avec `ps aux` (erreur
  constatee : "ps: unknown option -- o"). Pour lister les processus par consommation memoire sous
  Windows, appelle PowerShell DEPUIS le script Bash : `powershell.exe -Command "Get-Process |
  Sort-Object WorkingSet -Descending | Select-Object -First 5 Name,WorkingSet"`.
- Bash : NE JAMAIS faire `$(basename $VARIABLE)` quand `$VARIABLE` peut contenir PLUSIEURS chemins
  separes par des espaces (ex: resultat de `find ... -name "*.log"` non guillemete) — `basename`
  n'accepte qu'UN SEUL nom (+ suffixe optionnel), pas une liste (constate : avec 3 fichiers .log,
  `tar -czf archive.tar.gz -C "$DOSSIER" $(basename $LOGS)` a echoue avec "basename: extra operand",
  et l'archive n'a jamais ete creee — le script marchait seulement avec 1 seul fichier trouve). Pour
  compresser/traiter plusieurs fichiers trouves par `find`, utilise soit une boucle
  (`while IFS= read -r f; do ...; done < <(find "$DOSSIER" -maxdepth 1 -name "*.log")`), soit un
  tableau bash (`mapfile -t LOGS < <(find ...)` puis `tar -czf archive.tar.gz -C "$DOSSIER"
  "${LOGS[@]##*/}"`), soit `find "$DOSSIER" -maxdepth 1 -name "*.log" -printf "%f\\n"` pour obtenir
  directement les noms sans chemin (pas besoin de basename)."""

CODER_FIX_SYSTEM = """Tu es CODER. Tu corriges du code en erreur.
Réécris UNIQUEMENT les fichiers à corriger, format strict:
###FILE: nom.ext
<code corrigé complet>
###ENDFILE
Pas de texte hors des blocs.

RÈGLE PRIORITAIRE : si l'erreur est un NameError ("name 'X' is not defined"), l'HYPOTHESE PAR DEFAUT
est un import manquant en tete du fichier concerne (ex: NameError: name 'time' is not defined -> ajouter
"import time"). Verifie et corrige CE point EN PRIORITE avant toute autre hypothese — c'est la cause la
plus frequente constatee, et une correction qui ne fait qu'ajouter la ligne d'import manquante suffit
generalement (pas besoin de reecrire le reste du fichier)."""

# ════════════════════════════════════════════════════════════════════════════
#  Auto-apprentissage — DevLLMA distille ses propres echecs/corrections en
#  regles reutilisables, indexees en memoire semantique (kind="lesson") et
#  reinjectees dynamiquement dans CODER_SYSTEM/CODER_FIX_SYSTEM pour les
#  PROCHAINS projets. Boucle de correction fermee : plus besoin d'un humain
#  qui repere un bug au banc de tests et patch le prompt a la main (ce qui
#  a ete fait manuellement ~6 fois le 14/07/2026 avant la mise en place de
#  ce mecanisme).
# ════════════════════════════════════════════════════════════════════════════
LESSON_SYSTEM = """Tu es un analyste qui distille UN bug corrige en UNE regle GENERALE
reutilisable pour des projets FUTURS et DIFFERENTS. Reponds en 1-2 phrases courtes,
sans nommer de variables/fichiers/projets specifiques a ce cas precis — generalise le
principe (ex: "un parametre de fonction ne doit jamais porter le meme nom qu'une
fonction importee et appelee dans le meme corps" plutot que de citer "check_links").
Si le bug est trop specifique a ce projet pour donner une lecon generalisable utile,
reponds exactement: AUCUNE LECON."""

def record_lesson(initial_error, fix_summary, resolved, final_error):
    """Distille un cycle echec+correction (resolu ou non) en regle generale et l'indexe
    (kind='lesson') pour injection future dans CODER_SYSTEM/CODER_FIX_SYSTEM. Appele en
    fire-and-forget (run_in_executor, jamais awaite) apres la boucle d'auto-correction :
    ne doit JAMAIS lever d'exception ni ralentir la reponse a l'utilisateur."""
    try:
        # NE distiller une lecon QUE si le probleme a REELLEMENT ete resolu. Distiller un
        # echec jamais corrige produisait une "regle" sans signal de justesse (le modele
        # invente une explication a une erreur qu'il n'a pas su reparer), ensuite reinjectee
        # dans tous les futurs projets similaires -> pollution du prompt avec de faux conseils
        # (defaut identifie en revue de code : resolved=run_ok pouvait etre False).
        if not resolved:
            return
        ctx = (f"ERREUR RENCONTREE:\n{initial_error[:800]}\n\n"
               f"CORRECTION QUI A RESOLU LE PROBLEME:\n{fix_summary[:800]}")
        lesson = call_brain(ctx, system=LESSON_SYSTEM, max_tokens=220)
        lesson = (lesson or "").strip()
        # call_brain() ne leve JAMAIS d'exception sur panne Ollama : il renvoie une CHAINE
        # descriptive ("(modele 'X' indisponible sur Ollama : ...)", "(brain indisponible
        # apres 3 tentatives: ...)") qui passait tous les filtres suivants (non vide, pas le
        # sentinel, >15 car) et etait stockee comme si c'etait une vraie lecon de code, puis
        # reinjectee dans TOUS les projets futurs (constate en revue de code independante).
        if lesson.startswith("(") and ("indisponible" in lesson.lower() or "erreur" in lesson.lower()):
            return
        # Constate empiriquement : le modele repond parfois la VRAIE lecon PUIS ajoute quand
        # meme "AUCUNE LECON" a la suite (n'obeit pas toujours a "reponds EXACTEMENT X"). Un
        # simple "in" aurait jete une lecon par ailleurs utile -> on ne rejette que si le
        # sentinel est la reponse ENTIERE (une fois nettoyee), pas juste present quelque part.
        cleaned = lesson.strip(" \n\t.\"'")
        if not lesson or cleaned.upper() == "AUCUNE LECON" or len(lesson) < 15:
            return
        # Si le sentinel traine EN PLUS d'une vraie lecon, on le retire avant stockage.
        lesson = re.sub(r'\n*AUCUNE LECON\.?\s*$', '', lesson, flags=re.IGNORECASE).strip()
        if len(lesson) < 15:
            return
        # Filet de securite complementaire au sentinel : un refus PARAPHRASE (le modele
        # n'obeit pas toujours a "reponds EXACTEMENT AUCUNE LECON") ne contient pas la
        # chaine exacte mais reste reconnaissable a ces tournures habituelles de refus.
        _REFUSAL_HINTS = ("trop specifique", "pas de lecon", "aucune regle generale",
                          "ne s'applique pas", "cas particulier", "n'est pas generalisable",
                          "pas generalisable")
        if any(h in strip_accents(lesson.lower()) for h in _REFUSAL_HINTS):
            return
        # Securite anti-troncature : max_tokens coupe parfois en plein milieu d'une phrase ;
        # on tronque a la derniere phrase complete plutot que de stocker un fragment.
        if lesson[-1] not in ".!?":
            last_punct = max(lesson.rfind("."), lesson.rfind("!"), lesson.rfind("?"))
            if last_punct >= 15:
                lesson = lesson[:last_punct + 1]
            else:
                return
        # Dedup semantique : ne pas re-stocker une lecon deja tres proche d'une existante
        # (seuil HAUT expres : on veut eviter les quasi-doublons, pas rater une nuance).
        if mem_search(lesson, k=1, kind="lesson", min_score=0.80):
            return
        mem_index("lesson", lesson[:60], lesson)
    except Exception:
        pass  # best-effort : ne doit jamais impacter le pipeline principal

def _record_lesson_tracked(initial_error, fix_summary, resolved, final_error):
    """Wrapper qui compte cet appel dans _ACTIVE_GEN pendant toute sa duree. record_lesson
    est lance en fire-and-forget (jamais awaite) juste avant que handle_prompt() ne
    retourne — sans ce compteur, _ACTIVE_GEN retombe a 0 des le retour de handle_prompt
    alors que ce thread d'arriere-plan peut encore solliciter Ollama (call_brain, jusqu'a
    ~15 min via 3 tentatives x 300s) : le watchdog reperdrait la tolerance qui vient
    justement d'etre ajoutee pour tolerer une generation active (constate en revue de
    code independante — reouvrait exactement le bug de faux-redemarrage du jour meme,
    dans un angle mort different)."""
    _ACTIVE_GEN["n"] += 1
    try:
        record_lesson(initial_error, fix_summary, resolved, final_error)
    finally:
        _ACTIVE_GEN["n"] = max(0, _ACTIVE_GEN["n"] - 1)

SHELL_SYSTEM = """Tu es SHELL. Tu génères des commandes PowerShell pour accomplir des tâches système.
Format: ```powershell\ncommande\n```
Commandes sûres uniquement. Pas de rm -rf, pas de suppression système."""

# Appliquer CODER_SYSTEM à tous les agents codeurs
for a in ["coder","backend","frontend","architect","database"]:
    AGENTS[a]["system"] = CODER_SYSTEM

# ─── Outils fichiers ─────────────────────────────────────────────────────────
CODE_EXTS = {'.py','.js','.ts','.html','.css','.json','.yml','.yaml',
             '.txt','.md','.sql','.sh','.env','.toml','.ini','.cfg','.jsx','.tsx'}

def read_project(project_dir, max_chars=1600, total_budget=9000, max_files=8):
    """Lit le code source d'un projet, en bornant la taille (sinon prompt trop gros -> timeout CPU).
    Exclut les dossiers de build/sortie, ignore les fichiers internes (_*) et le code volumineux."""
    files = {}
    if not os.path.isdir(project_dir): return files
    used = 0
    for root, dirs, fnames in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in {"__pycache__",".git","node_modules",".venv","build","dist"}]
        for fname in sorted(fnames):
            if fname.startswith("_"):            # fichiers internes (logs, tests harnais)
                continue
            if os.path.splitext(fname)[1].lower() in CODE_EXTS:
                fpath = os.path.join(root, fname)
                rel = os.path.relpath(fpath, project_dir)
                try:
                    with open(fpath, encoding="utf-8", errors="replace") as f:
                        content = f.read()[:max_chars]
                    files[rel] = content
                    used += len(content)
                    if used >= total_budget or len(files) >= max_files:
                        return files
                except Exception:
                    pass
    return files

def format_context(files):
    parts = []
    for name, content in files.items():
        ext = os.path.splitext(name)[1].lstrip(".")
        parts.append(f"**{name}**\n```{ext}\n{content}\n```")
    return "\n\n".join(parts)

# ─── Extraction de fichiers ───────────────────────────────────────────────────
def extract_files(text):
    # Extracteur robuste universel partagé (skills.py) — gère **f**, ### N. `f`, etc.
    return extract_files_robust(text)

_TEMP_SUFFIX_RE = re.compile(r'(\.[A-Za-z0-9]{1,8})[._-]?temp[._-]?\d+$', re.IGNORECASE)

def _sanitize_filename(fname):
    """Retire un suffixe de fichier temporaire parasite (ex: 'main.py_temp_4' -> 'main.py',
    'utils.py.devllma_tmp' -> 'utils.py'). Le modele emet parfois des noms en '<nom>.<ext>_temp_N'
    (strategie ecriture-atomique jamais finalisee) : le fichier reel n'a alors PAS la bonne
    extension et les imports entre modules echouent (from cli import ... alors que seul
    'cli.py_temp_1' existe). Constate sur un projet livre (renommeur de fichiers)."""
    fname = _TEMP_SUFFIX_RE.sub(r'\1', fname)
    fname = re.sub(r'\.devllma_tmp$', '', fname, flags=re.IGNORECASE)
    return fname

_SHEBANG_EXT = {"bash": ".sh", "sh": ".sh", "zsh": ".sh",
                "python": ".py", "python3": ".py", "python2": ".py",
                "node": ".js", "nodejs": ".js",
                "pwsh": ".ps1"}
_SHEBANG_RE = re.compile(r'^#!\s*\S*/(?:env\s+)?(\w+)')

def _infer_missing_extension(fname, code):
    """Si le modele ecrit un nom de fichier SANS AUCUNE extension (constate : ###FILE:
    script_bash_qui_compresse — confusion avec le nom du DOSSIER projet vu dans le prompt enrichi
    'Projet: C:\\Devllma\\workspace\\script_bash_qui_compresse\\', reutilise par erreur comme nom de
    fichier), deduit l'extension depuis la ligne shebang (#!/bin/bash, #!/usr/bin/env python3...)
    si presente. Sans ca : find_entry_point() ne reconnait aucune extension -> AUCUN point d'entree
    trouve -> echec SILENCIEUX (meme famille que _single_script_entry, mais pour un nom SANS
    extension du tout plutot qu'un nom descriptif avec la bonne extension)."""
    if os.path.splitext(fname)[1]:
        return fname
    first_line = (code or "").splitlines()[0] if code else ""
    m = _SHEBANG_RE.match(first_line)
    if m:
        ext = _SHEBANG_EXT.get(m.group(1).lower())
        if ext:
            return fname + ext
    return fname

def write_files(project_dir, files):
    os.makedirs(project_dir, exist_ok=True)
    created = []
    for fname, code in files:
        fname = _infer_missing_extension(fname, code)
        fname = _sanitize_filename(fname)
        fpath = os.path.join(project_dir, fname.replace("/", os.sep))
        os.makedirs(os.path.dirname(fpath), exist_ok=True)
        with open(fpath, "w", encoding="utf-8") as f: f.write(code)
        created.append(fname)
    return created

def install_deps(project_dir):
    req = os.path.join(project_dir, "requirements.txt")
    pkg = os.path.join(project_dir, "package.json")
    if os.path.exists(pkg):
        # Projet Node.js : npm install (node_modules quasi jamais fourni par le modele).
        try:
            r = subprocess.run([NPM_CMD, "install", "--no-audit", "--no-fund"],
                               capture_output=True, text=True, timeout=600, cwd=project_dir,
                               encoding="utf-8", errors="replace")
            return r.returncode == 0, (r.stdout + r.stderr).strip()[:200]
        except subprocess.TimeoutExpired:
            return False, "npm install trop long (>10 min) — projet créé ; lance 'npm install' à la main si nécessaire"
        except Exception as e:
            return False, f"npm install interrompu : {str(e)[:150]}"
    if not os.path.exists(req): return None, None
    try:
        r = subprocess.run([PYTHON,"-m","pip","install","-r",req,"-q","--no-warn-script-location"],
                           capture_output=True, text=True, timeout=600,
                           encoding="utf-8", errors="replace")
        return r.returncode==0, (r.stdout+r.stderr).strip()[:200]
    except subprocess.TimeoutExpired:
        # Des paquets lourds (torch, diffusers, transformers...) peuvent depasser meme 600s.
        # Ne PAS laisser l'exception faire ECHOUER TOUT le pipeline (constate : un projet
        # correct affiche en "Erreur ... timed out") : le projet est cree, on informe et on
        # continue. L'utilisateur installera manuellement si besoin.
        return False, "dépendances trop longues à installer (>10 min) — projet créé ; lance 'pip install -r requirements.txt' à la main si nécessaire"
    except Exception as e:
        return False, f"installation interrompue : {str(e)[:150]}"

# Nom d'import != nom du paquet PyPI (les cas courants ou pip install <import> echouerait)
_PIP_ALIAS = {"cv2":"opencv-python","PIL":"pillow","yaml":"pyyaml","bs4":"beautifulsoup4",
              "sklearn":"scikit-learn","dotenv":"python-dotenv","dateutil":"python-dateutil",
              "serial":"pyserial","Crypto":"pycryptodome","OpenGL":"PyOpenGL"}

def _pypi_resolve(pkgs):
    """Pour chaque paquet, interroge l'API JSON PyPI (gratuite) : renvoie 'pkg==derniere_version'
    s'il EXISTE, et l'IGNORE s'il est introuvable (404 = nom probablement hallucine par le modele
    -> evite un 'pip install' qui echoue sur tout le fichier). Reseau KO -> on garde le nom nu
    (comportement d'origine). Active par l'interrupteur 'Resolveur PyPI' du rail Capacites."""
    out = []
    for p in pkgs:
        try:
            r = requests.get(f"https://pypi.org/pypi/{p}/json", timeout=6)
            if r.status_code == 200:
                ver = (r.json().get("info") or {}).get("version")
                out.append(f"{p}=={ver}" if ver else p)
            elif r.status_code == 404:
                continue  # paquet inexistant sur PyPI -> on ne l'ecrit pas
            else:
                out.append(p)
        except Exception:
            out.append(p)  # reseau indisponible -> nom nu, comme avant
    return out

_NODE_BUILTINS = {
    "fs","path","http","https","url","crypto","os","util","events","stream",
    "child_process","net","dns","zlib","buffer","querystring","readline","cluster",
    "worker_threads","assert","timers","tty","dgram","v8","vm","perf_hooks","punycode",
    "string_decoder","tls","dns/promises","fs/promises","node:fs","node:path","node:http",
    "node:https","node:os","node:util","node:crypto","node:events","node:stream","node:url",
}

# ── Generation d'APK Android (WebView) ────────────────────────────────────────
# Approche : plutot que d'esperer que le modele ecrive du Kotlin/Java Android natif
# correct (jamais teste, tres peu fiable sur un modele CPU local), on reutilise sa
# vraie force (HTML/CSS/JS) dans un squelette Android FIXE (WebView) deja teste et
# valide manuellement (build reussi, cf. C:\Devllma\templates\android_webview).
ANDROID_JAVA_HOME = r"C:\Program Files\Eclipse Adoptium\jdk-17.0.19.10-hotspot"
ANDROID_TEMPLATE_DIR = r"C:\Devllma\templates\android_webview"
ANDROID_SDK_ROOT = r"C:\Android\Sdk"

_APK_KEYWORDS = re.compile(
    r"\bapk\b|application\s+android|app\s+android|application\s+mobile|"
    r"application\s+pour\s+(?:android|smartphone|t[ée]l[ée]phone)",
    re.IGNORECASE)

def wants_android_apk(prompt):
    """Detecte une demande d'application Android/APK dans le prompt utilisateur."""
    return bool(_APK_KEYWORDS.search(prompt or ""))

def build_android_apk(project_dir, html_files):
    """Copie le squelette Android WebView, y injecte le contenu web genere (HTML/CSS/JS),
    compile via le wrapper Gradle du template (gradlew assembleDebug), et copie l'APK
    resultant dans project_dir. Retourne (ok, message, apk_path|None)."""
    import shutil
    if not os.path.isdir(ANDROID_TEMPLATE_DIR):
        return False, "Squelette Android introuvable (ANDROID_TEMPLATE_DIR manquant).", None
    android_dir = os.path.join(project_dir, "android")
    try:
        if os.path.isdir(android_dir):
            shutil.rmtree(android_dir)
        shutil.copytree(ANDROID_TEMPLATE_DIR, android_dir,
                        ignore=shutil.ignore_patterns(".gradle", "build", "*.apk"))
    except Exception as e:
        return False, f"Copie du squelette Android impossible : {e}", None

    www_dir = os.path.join(android_dir, "app", "src", "main", "assets", "www")
    try:
        # Vider le placeholder par defaut avant d'ecrire le vrai contenu genere.
        for f in os.listdir(www_dir):
            os.remove(os.path.join(www_dir, f))
        for fname, code in html_files:
            fpath = os.path.join(www_dir, fname.replace("/", os.sep))
            os.makedirs(os.path.dirname(fpath), exist_ok=True)
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(code)
        if not any(f.lower() == "index.html" for f, _ in html_files):
            return False, "Aucun index.html parmi les fichiers generes — impossible de construire l'APK.", None
    except Exception as e:
        return False, f"Ecriture du contenu web dans le squelette impossible : {e}", None

    child_env = dict(os.environ)
    child_env["JAVA_HOME"] = ANDROID_JAVA_HOME
    # SDK fourni via variable d'environnement (pas de local.properties dans le template :
    # ce fichier contient un chemin absolu specifique a la machine, ne doit pas etre versionne).
    child_env["ANDROID_HOME"] = ANDROID_SDK_ROOT
    child_env["ANDROID_SDK_ROOT"] = ANDROID_SDK_ROOT
    gradlew = os.path.join(android_dir, "gradlew.bat")
    try:
        r = subprocess.run([gradlew, "assembleDebug"], cwd=android_dir, capture_output=True,
                           text=True, timeout=600, encoding="utf-8", errors="replace", env=child_env)
    except subprocess.TimeoutExpired:
        return False, "Compilation Gradle trop longue (>10 min).", None
    except Exception as e:
        return False, f"Lancement de Gradle impossible : {e}", None

    out = (r.stdout + r.stderr).strip()
    apk_src = os.path.join(android_dir, "app", "build", "outputs", "apk", "debug", "app-debug.apk")
    if r.returncode != 0 or not os.path.exists(apk_src):
        return False, out[-1200:], None

    apk_name = (os.path.basename(project_dir.rstrip("\\/")) or "application") + ".apk"
    apk_dst = os.path.join(project_dir, apk_name)
    try:
        shutil.copy2(apk_src, apk_dst)
    except Exception as e:
        return True, f"APK compile mais copie impossible ({e}) : {apk_src}", apk_src
    mb = round(os.path.getsize(apk_dst) / 1e6, 2)
    return True, f"APK genere avec succes : {apk_name} ({mb} Mo)", apk_dst

def derive_node_requirements(project_dir):
    """Equivalent Node.js de derive_requirements : deduit package.json des require()/import
    presents dans les .js, SANS appel LLM. Sans ca, un script utilisant axios/node-fetch/etc.
    n'a JAMAIS de package.json -> install_deps ne lance aucun 'npm install' (il ne se declenche
    QUE si package.json existe) -> 'Cannot find module' au premier lancement (constate, projet
    convertisseur de devises : le modele a laisse un require('axios') sans jamais creer de
    package.json). On ne deduit que les paquets EXTERNES (retire les modules Node natifs et les
    imports relatifs ./x) et on n'AJOUTE que les manquants (jamais d'ecrasement d'un package.json
    existant deja complet)."""
    ignore = {"__pycache__", ".git", "node_modules", ".venv", "build", "dist"}
    js_files = []
    for root, dirs, names in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in ignore]
        for n in names:
            if n.endswith((".js", ".mjs", ".cjs")):
                js_files.append(os.path.join(root, n))
    if not js_files:
        return []
    found = set()
    for fp in js_files:
        try:
            src = open(fp, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        for m in re.finditer(r'require\(\s*[\'"]([^\'"]+)[\'"]\s*\)'
                              r'|(?:^|\n)\s*import\s+.*?\s+from\s+[\'"]([^\'"]+)[\'"]', src):
            name = m.group(1) or m.group(2)
            if not name or name.startswith((".", "/")):
                continue  # import relatif entre fichiers du projet, pas un paquet npm
            pkg = name.split("/")[0] if not name.startswith("@") else "/".join(name.split("/")[:2])
            found.add(pkg)
    external = sorted(p for p in found if p not in _NODE_BUILTINS)
    if not external:
        return []
    pkg_path = os.path.join(project_dir, "package.json")
    data = {"name": os.path.basename(project_dir.rstrip("\\/")) or "projet", "version": "1.0.0",
            "private": True, "dependencies": {}}
    if os.path.exists(pkg_path):
        try:
            data = json.load(open(pkg_path, encoding="utf-8"))
        except Exception:
            pass
    data.setdefault("dependencies", {})
    missing = [p for p in external if p not in data["dependencies"]]
    if not missing:
        return []
    for p in missing:
        data["dependencies"][p] = "latest"
    try:
        with open(pkg_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception:
        return []
    return missing

def derive_requirements(project_dir):
    """Complete requirements.txt en analysant les imports (AST), SANS appel LLM : le modele
    oublie souvent ce fichier -> ModuleNotFoundError au lancement. On ne deduit que les
    paquets pip EXTERNES (retire la stdlib et les modules locaux du projet) et on n'AJOUTE
    que les manquants (jamais d'ecrasement d'un requirements.txt existant). Retourne la liste
    ajoutee."""
    import ast
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    ignore = {"__pycache__", ".git", "node_modules", ".venv", "build", "dist"}
    local, py_files = set(), []
    for root, dirs, names in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in ignore]
        for n in names:
            if n.endswith(".py"):
                py_files.append(os.path.join(root, n)); local.add(n[:-3])
        if root == project_dir:
            local.update(dirs)  # dossiers-paquets a la racine = modules locaux
    found = set()
    for fp in py_files:
        try:
            tree = ast.parse(open(fp, encoding="utf-8", errors="replace").read())
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names: found.add(a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])  # level==0 => ignore les imports relatifs
    external = [_PIP_ALIAS.get(m, m) for m in sorted(found)
                if m and m not in stdlib and m not in local]
    if not external:
        return []
    req_path = os.path.join(project_dir, "requirements.txt")
    existing_text = ""
    if os.path.exists(req_path):
        try: existing_text = open(req_path, encoding="utf-8").read()
        except Exception: existing_text = ""
    have = {re.split(r'[<>=!~ ]', l.strip())[0].lower() for l in existing_text.splitlines() if l.strip()}
    missing = [p for p in external if p.lower() not in have]
    if not missing:
        return []
    # Resolveur PyPI (rail Capacites) : verifie l'existence + epingle la version.
    if cfg_on("pypi"):
        resolved = _pypi_resolve(missing)
        if resolved:
            missing = resolved
    sep = "" if (not existing_text or existing_text.endswith("\n")) else "\n"
    try:
        with open(req_path, "a", encoding="utf-8") as f:
            f.write(sep + "\n".join(missing) + "\n")
    except Exception:
        return []
    return missing

bootstrap_memory()

# ─── Exécution de code ────────────────────────────────────────────────────────
ENTRY_POINTS = ["main.py","app.py","run.py","server.py","start.py","index.py",
                "main.js","app.js","server.js","index.js",
                "main.ps1","script.ps1","run.ps1","backup.ps1",
                "main.sh","script.sh","run.sh","backup.sh",
                "main.go"]

def _interpreter_cmd(fpath):
    """Choisit l'interpreteur/executable selon l'extension du point d'entree — le pipeline
    ne generait/executait QUE du Python (execute_project lancait toujours [PYTHON, fpath],
    meme pour un .js/.ps1/.sh correct -> faux echec garanti). Chemins absolus (cf. constantes
    NODE_EXE/BASH_EXE/POWERSHELL_EXE/GO_EXE) : le service tourne en tache SYSTEM, PATH pas fiable.
    Go : 'go run <fichier>' compile+execute en une etape (verification suffisante) ; ne gere
    correctement qu'un programme en UN SEUL fichier .go (limitation connue — un package Go
    reparti sur plusieurs fichiers necessiterait 'go run .' avec le bon cwd, pas encore cable)."""
    ext = os.path.splitext(fpath)[1].lower()
    if ext in (".js", ".mjs", ".cjs"):
        return [NODE_EXE, fpath]
    if ext == ".ps1":
        return [POWERSHELL_EXE, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", fpath]
    if ext == ".sh":
        return [BASH_EXE, fpath]
    if ext == ".go":
        return [GO_EXE, "run", fpath]
    return [PYTHON, fpath]

_ENTRY_IGNORE_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "build", "dist", "tests"}

def _main_block_entry(dir_path):
    """Parmi les .py d'UN dossier (pas recursif), trouve celui qui contient un bloc
    `if __name__ == "__main__"` — repli quand aucun nom standard (main.py, app.py...)
    n'est present. Retourne le NOM DE FICHIER SEUL (pas le chemin), ou None."""
    try:
        py_files = [f for f in sorted(os.listdir(dir_path))
                    if f.endswith(".py") and os.path.isfile(os.path.join(dir_path, f))]
    except Exception:
        return None
    if len(py_files) == 1:
        return py_files[0]
    if len(py_files) <= 1:
        return None
    entry_candidates = []
    for f in py_files:
        try:
            src = open(os.path.join(dir_path, f), encoding="utf-8").read()
        except Exception:
            continue
        if re.search(r'if\s+__name__\s*==\s*["\']__main__["\']', src):
            entry_candidates.append(f)
    if len(entry_candidates) == 1:
        return entry_candidates[0]
    if entry_candidates:
        for pref in ("main.py", "app.py", "run.py", "cli.py", "__main__.py"):
            if pref in entry_candidates:
                return pref
        return entry_candidates[0]
    return None

_SCRIPT_EXTS = (".sh", ".ps1", ".js", ".mjs", ".cjs", ".go")

def _single_script_entry(dir_path):
    """Repli pour Bash/PowerShell/JS/Go (equivalent non-Python de _main_block_entry) : si UN
    SEUL fichier de script existe dans ce dossier, c'est forcement lui le point d'entree —
    meme s'il ne porte pas un des noms conventionnels de ENTRY_POINTS. Sans ca, un script
    nomme de facon descriptive (ex: rotation_logs.sh au lieu de main.sh/script.sh/run.sh/
    backup.sh) faisait echouer find_entry_point() completement -> execute_project() ne
    trouvait AUCUN point d'entree, run_ok restait None, et AUCUN message run_result n'etait
    jamais envoye au client (echec totalement SILENCIEUX, constate : projet 'rotation_logs.sh'
    au code pourtant correct, jamais execute ni valide).
    Si PLUSIEURS fichiers de script coexistent (constate : deux projets Bash SANS RAPPORT — liste
    des ports ecoute + liste des fichiers modifies — regroupes dans le MEME dossier a cause d'un
    nom de projet generique partage, ex 'script_bash_qui_liste'), on prend le PLUS RECEMMENT
    MODIFIE : c'est presque toujours celui vise par la demande la plus recente (creation/edition),
    l'autre etant un residu d'une demande anterieure sans rapport. Repli imparfait mais bien
    meilleur que l'echec silencieux precedent (aucun des deux n'etait jamais execute/valide)."""
    try:
        candidates = [f for f in sorted(os.listdir(dir_path))
                      if os.path.splitext(f)[1].lower() in _SCRIPT_EXTS
                      and not f.startswith("_")
                      and os.path.isfile(os.path.join(dir_path, f))]
    except Exception:
        return None
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        return max(candidates, key=lambda f: os.path.getmtime(os.path.join(dir_path, f)))
    return None

def find_entry_point(project_dir):
    """Cherche un point d'entree a la racine, PUIS dans un sous-dossier direct
    (layout package Python du type monpaquet/main.py). Sans ce fallback, un projet
    structuré en package n'est jamais exécuté ni testé (faux "succès" silencieux)."""
    for ep in ENTRY_POINTS:
        if os.path.exists(os.path.join(project_dir, ep)):
            return ep
    subdirs = [e for e in sorted(os.listdir(project_dir))
               if os.path.isdir(os.path.join(project_dir, e)) and e not in _ENTRY_IGNORE_DIRS]
    for entry in subdirs:
        sub = os.path.join(project_dir, entry)
        for ep in ENTRY_POINTS:
            if os.path.exists(os.path.join(sub, ep)):
                return os.path.join(entry, ep)
    # Dernier recours (racine) : un script unique nomme autrement (ex: calculatrice.py seul
    # a la racine), ou plusieurs fichiers .py sans nom standard -> on identifie le point
    # d'entree reel comme celui qui contient un bloc `if __name__ == "__main__"` (constate,
    # data_05 : le modele a produit des modules sans main.py -> projet jamais execute).
    # Meme idee pour Bash/PowerShell/JS/Go via _single_script_entry (pas de bloc __main__
    # dans ces langages, mais un seul fichier de script = sans ambiguite le point d'entree).
    root_entry = _main_block_entry(project_dir) or _single_script_entry(project_dir)
    if root_entry:
        return root_entry
    # Meme repli, mais DANS un sous-dossier (layout package avec noms non standards, ex:
    # backup_tool/cli.py, backup_tool/backup.py... aucun ne s'appelle main.py). Sans ce
    # deuxieme niveau de repli, ces projets ne sont jamais executes -> run_ok reste None
    # indefiniment (constate, script_python_sauvegar_compression : entree reelle = cli.py
    # dans un sous-dossier, aucun fichier racine, donc find_entry_point rendait None).
    for entry in subdirs:
        sub = os.path.join(project_dir, entry)
        sub_entry = _main_block_entry(sub) or _single_script_entry(sub)
        if sub_entry:
            return os.path.join(entry, sub_entry)
    return None

def _kill_process_tree(pid):
    """Tue un process ET tous ses enfants (Windows). Un simple .kill() ne suffit pas
    toujours a liberer un port occupe par un serveur (uvicorn, etc.), ce qui bloquait
    les tests d'execution des projets suivants avec "address already in use"."""
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, timeout=5)
    except Exception:
        pass

SERVER_MARKERS = ("uvicorn", "flask", "app.run(", "socketserver", "http.server",
                   "runserver", "waitress", "gunicorn", "websockets.serve",
                   "express()", "app.listen(", "createserver", "http.createserver",
                   "listenandserve(")

def _detect_server_port(project_dir):
    """Devine le(s) port(s) qu'un projet serveur va ouvrir en scannant son code.
    Retourne (is_server, [ports candidats]). On collecte TOUS les `port=` (pas juste
    le premier) + des defauts par framework, car un projet peut definir le port
    ailleurs que dans une 1re ligne evidente. 8080 (PROD) est exclu par securite."""
    is_server = False
    ports, frameworks = [], set()
    for root, dirs, names in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in {"__pycache__",".git","node_modules",".venv","build","dist"}]
        for n in names:
            if not n.endswith((".py", ".js", ".mjs", ".cjs", ".go")):
                continue
            try:
                content = open(os.path.join(root, n), encoding="utf-8", errors="replace").read()
            except Exception:
                continue
            low = content.lower()
            for mk in SERVER_MARKERS:
                if mk in low:
                    is_server = True; frameworks.add(mk)
            for m in re.findall(r'\.listen\(\s*(\d{2,5})|port\s*[=:]\s*(\d{2,5})', content):
                p = int(m[0] or m[1])
                if p != 8080 and p not in ports:  # 8080 = PROD, jamais un projet genere
                    ports.append(p)
            # Go idiomatique : http.ListenAndServe(":8000", ...) — le port est un argument
            # string ":NNNN", pas un "port=" ni ".listen(" -> les regex ci-dessus le ratent.
            for p in re.findall(r'ListenAndServe\(\s*"?:(\d{2,5})', content, re.IGNORECASE):
                p = int(p)
                if p != 8080 and p not in ports:
                    ports.append(p)
    # Defauts par framework (le port explicite prime, mais sert de repli s'il manque)
    if frameworks & {"flask", "app.run("} and 5000 not in ports:
        ports.append(5000)
    if frameworks & {"uvicorn", "http.server", "runserver", "waitress", "gunicorn",
                     "express()", "app.listen(", "createserver", "http.createserver",
                     "listenandserve("} and 8000 not in ports:
        ports.append(8000)
    if is_server and not ports:
        ports.append(8000)
    return is_server, ports

def _wait_port_open(port, max_wait=8):
    """Sonde reellement le port en local — remplace la supposition 'le process n'a
    pas plante donc ca marche' par une verification effective que quelque chose ecoute."""
    import socket as _socket
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            with _socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.4)
    return False

def _wait_any_port_open(ports, max_wait=12):
    """Sonde plusieurs ports candidats jusqu'a ce que l'UN accepte une connexion TCP.
    Retourne le port ouvert, ou None. L'ouverture TCP est le signal PRINCIPAL 'un serveur
    ecoute' — plus fiable que HTTP (websockets/socketserver ne repondent pas sur / en HTTP)."""
    import socket as _socket
    if not ports:
        return None
    deadline = time.time() + max_wait
    while time.time() < deadline:
        for p in ports:
            try:
                with _socket.create_connection(("127.0.0.1", p), timeout=0.4):
                    return p
            except OSError:
                continue
        time.sleep(0.4)
    return None

def _http_probe(port):
    """Requete HTTP reelle sur la racine du serveur — un statut (meme 404) prouve
    qu'une vraie appli repond, pas juste qu'un port TCP est ouvert par autre chose.
    Retourne (etat, detail) avec etat in {True, False, None} :
      None  -> pas de reponse HTTP du tout (serveur websocket/socket brut probable,
               PAS une erreur : purement informatif, cf. appelant).
      True  -> HTTP confirme, et si FastAPI/openapi.json dispo, une vraie route a
               ete testee avec succes.
      False -> HTTP confirme MAIS une vraie route retourne une erreur serveur —
               echec REEL, pas juste "pas de HTTP" (constate, projet "api_05":
               "/" repond 200 alors que TOUTES les routes qui touchent la DB
               plantent, get_db() decore par erreur avec @contextmanager,
               incompatible avec Depends())."""
    try:
        r = requests.get(f"http://127.0.0.1:{port}/", timeout=3)
        root_status = r.status_code
    except Exception as e:
        return None, str(e)
    try:
        o = requests.get(f"http://127.0.0.1:{port}/openapi.json", timeout=3)
        if o.status_code == 200:
            paths = o.json().get("paths", {})
            for path, methods in paths.items():
                if "{" in path or "get" not in methods:
                    continue
                try:
                    rr = requests.get(f"http://127.0.0.1:{port}{path}", timeout=5)
                except Exception as e:
                    return False, f"route {path} injoignable: {e}"
                if rr.status_code >= 500:
                    return False, f"route {path} renvoie {rr.status_code} (dependance/DB probablement cassee)"
                break  # une route GET fonctionnelle suffit a valider que get_db()/deps marchent
    except Exception:
        pass  # pas de FastAPI/openapi.json exploitable -> on garde le check racine seul
    return True, root_status

def execute_project(project_dir, timeout=15):
    # Site web statique (index.html a la racine, AUCUN serveur backend detecte) : le/les
    # fichier(s) .js associes (script.js/main.js...) sont du code COTE NAVIGATEUR (utilisent
    # document/window/DOM), jamais un script a executer via node -> execute_project le
    # lancerait quand meme et provoquerait a coup sur un "ReferenceError: document is not
    # defined", sur un site par ailleurs parfaitement correct une fois ouvert dans un
    # navigateur (constate : 2 projets sur 2 dans un meme lot, page_web_htmlcssjs_*). Un site
    # statique se VERIFIE en l'ouvrant dans un navigateur, pas en executant son JS hors
    # navigateur. Si un VRAI serveur est detecte (Express/http.createServer/...), on ne
    # court-circuite rien : le flux normal ci-dessous gere deja ce cas correctement.
    if os.path.exists(os.path.join(project_dir, "index.html")):
        is_static_check, _ = _detect_server_port(project_dir)
        if not is_static_check:
            return True, "(site web statique — à vérifier en ouvrant index.html dans un navigateur, pas d'exécution JS hors navigateur)", "index.html"
    ep = find_entry_point(project_dir)
    if not ep:
        return None, "Aucun fichier principal trouvé (main.py, app.py…)", None
    fpath = os.path.join(project_dir, ep)
    is_server, server_ports = _detect_server_port(project_dir)
    # Force les E/S du projet enfant en UTF-8 : sur Windows la console par defaut est en
    # cp1252, et un simple print() contenant un emoji ou un caractere hors cp1252 (fleche
    # de tendance, symbole meteo...) crashe avec UnicodeEncodeError — alors que le code est
    # CORRECT (constate, projet meteo). PYTHONIOENCODING+PYTHONUTF8 alignent l'enfant sur
    # l'UTF-8 (ce que fait tout deploiement moderne) et suppriment cette classe de faux echecs.
    child_env = dict(os.environ)
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUTF8"] = "1"
    # Un script Bash appelle souvent des utilitaires GNU (awk, sed, grep, cut...) qui vivent
    # a cote de bash.exe dans Git\usr\bin — PAS forcement sur le PATH herite par le service
    # SYSTEM. Sans ca : "awk: command not found" / "sed: command not found" en plein milieu
    # d'un script Bash par ailleurs correct (constate sur un script de verification d'espace
    # disque). On les ajoute devant le PATH existant (ne retire rien).
    _git_bin = r"C:\Program Files\Git\usr\bin"
    if os.path.isdir(_git_bin) and _git_bin not in child_env.get("PATH", ""):
        child_env["PATH"] = _git_bin + os.pathsep + child_env.get("PATH", "")
    # Go : GOCACHE par defaut vit sous le profil du compte qui execute go.exe. Le service
    # tourne en tache SYSTEM -> son propre cache serait vierge et rechargerait TOUT a froid
    # (constate manuellement : 18s pour un hello-world, contre 0.4s cache chaud) alors que le
    # code genere est correct -> faux echec garanti sur le timeout par defaut. Un chemin fixe
    # hors profil (deja rechauffe une fois) elimine ce cout quel que soit le compte executant.
    is_go = fpath.lower().endswith(".go")
    if is_go:
        os.makedirs(GO_CACHE_DIR, exist_ok=True)
        child_env["GOCACHE"] = GO_CACHE_DIR
    eff_timeout = 40 if is_go else timeout
    proc = None
    try:
        proc = subprocess.Popen(_interpreter_cmd(fpath), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, cwd=project_dir, encoding="utf-8", errors="replace",
                                env=child_env)
        try:
            out, _ = proc.communicate(timeout=eff_timeout)
            combined = (out or "").strip()[:800]
            low = combined.lower()
            cli_usage_exit = proc.returncode != 0 and (
                # Signatures explicites argparse / click / typer
                re.search(r'the following arguments are required'
                          r"|Error:\s*Missing (?:option|argument)"
                          r"|Missing (?:option|argument)\b", combined, re.IGNORECASE)
                # Heuristique GENERALE : sortie d'usage CLI (argparse SystemExit(2) ou click) =
                # une ligne "usage:" + une ligne "... error: ..." et AUCUN Traceback. Couvre les
                # messages d'erreur PERSONNALISES via parser.error("...") (constate, data_10 :
                # "main.py: error: Veuillez specifier --config") que les regex ci-dessus ratent.
                # L'absence de Traceback distingue "mauvais usage CLI" (code sain) d'un vrai crash.
                or ("traceback" not in low
                    and re.search(r'(?m)^usage:', combined, re.IGNORECASE)
                    and re.search(r'\berror:', combined, re.IGNORECASE))
                # GROUPE de sous-commandes (click.Group / argparse subparsers) lance SANS
                # sous-commande : il affiche son aide ("Usage: ... COMMAND [ARGS]" + liste
                # "Commands:") et sort en code != 0, mais SANS ligne "error:" -> les regex
                # ci-dessus le rataient et le pipeline gaspillait 3 cycles de correction (voire
                # timeout) sur du code parfaitement sain (constate, suivi_temps_travail #12).
                # Signature = usage + (liste Commands: OU placeholder COMMAND), sans Traceback.
                or ("traceback" not in low
                    and re.search(r'(?m)^\s*usage:', combined, re.IGNORECASE)
                    and (re.search(r'(?m)^\s*Commands:', combined)
                         or re.search(r'\[OPTIONS\]\s+COMMAND', combined, re.IGNORECASE)
                         or re.search(r'\{[\w-]+(?:,[\w-]+)+\}', combined)))
                # GENERIQUE multi-langage (Node/PowerShell/Bash — pas de convention argparse/click) :
                # un script qui exige un argument et s'arrete PROPREMENT avec un message court est
                # un CLI sain lance sans ses arguments, pas un bug. Deux cas constates sur le MEME
                # script Node.js selon la generation : "Veuillez fournir un chemin de dossier en
                # argument." (mot-cle mais pas de "usage:"), PUIS sur une regeneration suivante
                # juste "Usage: node main.js <dossier>" (mot "usage:" seul, sans "error:"/"Commands:"
                # donc rate par les regles Python ci-dessus). D'ou 2 sous-cas independants :
                # (a) mot-cle argument+requis peu importe la forme, (b) simple ligne "usage:" isolee.
                # Dans les deux cas, on exige l'ABSENCE de toute signature de plantage (Python/JS/
                # PowerShell/Bash confondus) et une sortie courte -> un vrai crash produit toujours
                # une trace plus longue et reconnaissable, jamais juste une ligne d'usage propre.
                # Seuil releve de 300 a 600 (constate : un CLI a PLUSIEURS options nommees, ex.
                # -u/-n/-d avec une ligne de description chacune, produit un bloc "usage:" bien
                # forme mais legitimement >300 caracteres — 498 caracteres mesures sur un script
                # de healthcheck sain, faux echec garanti par le seuil trop strict). 600 reste tres
                # en dessous d'une vraie trace d'erreur (Traceback/Exception font typiquement
                # plusieurs centaines de caracteres PAR frame), donc ne relache pas la detection
                # de vrais plantages — ceux-ci sont de toute facon deja exclus par la liste de
                # signatures ci-dessous.
                or (len(combined) < 600
                    and not re.search(r'traceback|exception|error:.*\bat\b|at Object\.|at Module\.|'
                                      r'\.js:\d+|\.ps1:\d+|line \d+:.*(?:syntax error|command not found)|'
                                      r'referenceerror|typeerror|is not recognized as',
                                      low)
                    and (
                        re.search(r'\b(argument|argv|param(?:etre|eter)?|option)s?\b.*'
                                  r'\b(requis|required|manquant|missing|fournir|provide|sp[ée]cifi)',
                                  low)
                        or re.search(r'\b(veuillez|please)\b.*\b(fournir|provide|sp[ée]cifi|indiqu)', low)
                        # "Aucun motif/chemin/nom... fourni/donne/specifie" : tournure francaise
                        # courante pour "entree obligatoire manquante", constate sur un script de
                        # recherche de processus ("Aucun motif de recherche fourni.") qui ne
                        # contient ni "argument/parametre/option" ni "veuillez" -> ratee par les
                        # 2 regles ci-dessus, faux echec sur un CLI par ailleurs parfaitement sain.
                        or re.search(r'\baucun[e]?\b.*\b(fourni[e]?|donn[ée][e]?|sp[ée]cifi[ée]?)\b', low)
                        # "usage:" ET son equivalent francais "utilisation:" (tres frequent dans
                        # les scripts generes par ce pipeline, qui est entierement en francais) —
                        # rate par les regles ci-dessus qui ne cherchaient QUE le mot anglais.
                        or re.search(r'(?m)^\s*(?:usage|utilisation)\s*:', low)
                    ))
            )
            if cli_usage_exit:
                # CLI avec argument(s) obligatoire(s) : "python main.py" SANS argument echoue
                # TOUJOURS par construction, meme quand le code est correct (constate sur argparse
                # required=True, click/typer Missing option, ET parser.error personnalise — chaque
                # fois 4 cycles d'auto-correction gaspilles sur du code sain, avec RISQUE de le
                # casser). Exiger des arguments en CLI est une bonne pratique, pas un bug.
                return True, f"(outil CLI avec arguments obligatoires — usage normal sans arguments) {combined[:200]}", ep
            return proc.returncode == 0, combined, ep
        except subprocess.TimeoutExpired:
            # Process encore vivant apres le timeout : si c'est un serveur, on VERIFIE
            # qu'il repond reellement au lieu de supposer un succes (cf. HANDOFF.md —
            # plusieurs projets "reussis" avaient en realite un serveur jamais demarre).
            if is_server:
                # Ouverture TCP sur N'IMPORTE lequel des ports candidats = serveur demarre.
                # HTTP n'est qu'un detail informatif : un faux echec HTTP ne doit PAS declencher
                # 3 cycles d'auto-correction sur un serveur (websocket/socket) qui marche.
                opened = _wait_any_port_open(server_ports, max_wait=min(timeout, 12))
                if opened is not None:
                    ok_http, detail = _http_probe(opened)
                    _kill_process_tree(proc.pid)
                    try: proc.communicate(timeout=3)
                    except Exception: pass
                    if ok_http is False:
                        # HTTP confirme mais une vraie route echoue -> echec REEL, pas un
                        # simple "pas de HTTP" (serveur websocket/socket brut) a ignorer.
                        return False, f"(serveur demarre sur le port {opened} mais {detail})", ep
                    http_note = f" — répond en HTTP {detail}" if ok_http else ""
                    return True, f"(serveur actif — écoute sur le port {opened}{http_note})", ep
                _kill_process_tree(proc.pid)
                try: proc.communicate(timeout=3)
                except Exception: pass
                return False, f"(le serveur n'a ouvert aucun port {server_ports} après {min(timeout,12)}s — démarrage probablement en échec silencieux)", ep
            # Pas un serveur détecté (calcul long, script CLI...) : on garde l'ancien
            # comportement, un timeout de process encore vivant reste ambigu mais
            # n'est pas un échec certain.
            _kill_process_tree(proc.pid)
            try:
                proc.communicate(timeout=3)
            except Exception:
                pass
            return True, f"(lancé en arrière-plan — timeout {timeout}s atteint)", ep
    except Exception as e:
        if proc is not None:
            _kill_process_tree(proc.pid)
        return False, str(e), ep

def _explicit_project_name(text):
    """Nom de projet donne EXPLICITEMENT par l'utilisateur, sinon None.
    Sans ca, deux demandes aux 4 premiers mots identiques (ex: "Cree une application
    Windows de bureau : projet 'X'") produisent le meme slug et le second projet
    ECRASE le premier (bug constate: 3 apps GUI ecrites dans un seul dossier).
    On ne prend les guillemets SIMPLES qu'apres un marqueur (projet/appele/nomme...) :
    un ' isole est le plus souvent une apostrophe francaise (l'appli, d'unites)."""
    m = re.search(r"(?:projets?|programmes?|applications?|appli|app|logiciels?|jeux?|"
                  r"nomm[ee]+|appel[ee]+|intitul[ee]+)\s*[:=]?\s*['\"«“]([^'\"»”\n]{2,40})['\"»”]",
                  text, re.IGNORECASE)
    if not m:  # repli : uniquement guillemets doubles / francais (jamais l'apostrophe)
        m = re.search(r"[\"«“]([^\"»”\n]{2,40})[\"»”]", text)
    return m.group(1).strip() if m else None

_SLUG_GENERIC_VEHICLE = {"outil","outils","script","scripts","programme","programmes",
                          "application","applications","logiciel","logiciels","app","utilitaire",
                          "python","javascript","js","typescript","bash","powershell","ps1",
                          "go","golang","java","html","css","cli","ligne","commande"}

def slug(text):
    name = _explicit_project_name(text)
    if name:
        s = re.sub(r"[^\w\s-]", "", strip_accents(name).lower())
        s = re.sub(r"[\s-]+", "_", s).strip("_")
        if s:
            return s[:40]
    text=re.sub(r"(?:crée|cree|créer|creer|fais|fait|génère|genere|développe|developpe|écris|ecris)\s+","",text.lower())
    text=re.sub(r"(?:moi|un|une|le|la|les|des|du|avec|pour|en|et|ou|de)\s+"," ",text)
    text=re.sub(r"[^\w\s]","",text).strip()
    words = text.split()
    # Retire les mots "vehicule" generiques (outil/script/langage/CLI) qui rendent le slug
    # IDENTIQUE pour des demandes totalement DIFFERENTES partageant juste la meme introduction
    # (ex: "Cree un outil Python en ligne de commande qui X" -> memes 4 premiers mots "outil
    # python ligne commande" quel que soit X). Constate : 7 projets SANS RAPPORT regroupes dans
    # un seul dossier ("outil_python_ligne_comman"), au point qu'un run_result d'un VIEUX projet
    # a valide a tort un NOUVEAU projet sans jamais executer son vrai code (find_entry_point
    # priorise main.py, deja present de l'ancien projet, avant tout repli). Garde-fou : si le
    # filtrage ne laisse presque rien, revenir aux mots bruts plutot que produire un slug vide.
    filtered = [w for w in words if w not in _SLUG_GENERIC_VEHICLE]
    use_words = filtered if len(filtered) >= 3 else words
    return "_".join(use_words[:4]) or "projet"

def is_edit(prompt):
    low = strip_accents(prompt.lower())
    return any(_kw_match(k, low) for k in ["modifie","modifier","corrige","corriger","ajoute","ajouter",
                                             "change","ameliore","refactore","fixe","fix",
                                             "mets a jour","met a jour","reprend","reprends","continue",
                                             "continuer","complete","termine","terminer"])

_FILE_OR_DOC_RE = re.compile(
    r'\b(fichier|dossier|document|note|classeur|feuille excel|tableau excel|fichier word|fichier pdf|word|excel|pdf)\b')
_STRONG_DEV_RE = re.compile(
    r'\b(api|application|app|site|projet|programme|logiciel|jeu|jeux|quiz|bot|fonction|classe|serveur|'
    r'backend|frontend|base de donnees|interface|dashboard|tableau de bord|'
    r'outil|ligne de commande|\bcli\b|'
    r'python|javascript|typescript|\bjs\b|\bjava\b|react|vue|angular|node|flask|fastapi|django|html|css|'
    r'bash|powershell|\bgo\b)\b')
# Ajoute "outil"/"ligne de commande"/"cli" PUIS les noms de langage/framework + "quiz"/"jeux"
# (constate, banc de tests, 2 categories touchees) : un projet de dev qui MANIPULE des
# fichiers (CLI d'organisation de fichiers ; quiz qui sauvegarde les scores "en fichier")
# matchait _FILE_OR_DOC_RE sur "fichier"/"dossier" SANS matcher l'ancienne liste ->
# is_file_or_doc_action() le classait a tort comme "action fichier ponctuelle" et partait
# vers l'agent generaliste au lieu du pipeline de dev. Un nom de LANGAGE (python, js...) est
# un signal de dev en beton : "un quiz EN PYTHON qui sauvegarde les scores en fichier" doit
# aller au pipeline. NB : "script python" retire car "python" seul le couvre desormais.
# Ajoute bash/powershell/go (constate : un script Bash/PowerShell de rotation de logs ou de
# rapport disque mentionne quasi TOUJOURS "fichier"/"dossier" — c'est son metier — mais
# aucun de ces 3 langages n'etait dans la liste, donc AUCUN mot-cle de langage ne matchait
# jamais -> is_file_or_doc_action() renvoyait a tort True, et la demande partait vers l'agent
# generaliste (write_file direct, SANS execution/validation/auto-correction) au lieu du
# pipeline CODER durci. Repere en testant le pipeline Go/PowerShell/Bash de bout en bout :
# les MEMES prompts avec "programme" au lieu de "script" (qui matche deja "programme")
# passaient bien par le pipeline, ce qui masquait le trou pour "script" seul.

def is_file_or_doc_action(prompt):
    """Vrai si la demande vise un FICHIER/DOCUMENT ponctuel (dossier, note, Word/Excel/PDF...)
    plutot qu'un vrai projet de dev — sinon "crée un fichier Word" (mot-cle "crée" = dev par
    defaut) partirait a tort dans le pipeline lourd de generation de projet multi-fichiers au
    lieu de l'agent generaliste, qui sait vraiment lire/ecrire ce type de document."""
    low = strip_accents(prompt.lower())
    return bool(_FILE_OR_DOC_RE.search(low)) and not bool(_STRONG_DEV_RE.search(low))

# Generation de MEDIA lourd (image/video) : tache que ce poste CPU ne peut pas faire ->
# on l'envoie a l'agent, qui la delegue AUTOMATIQUEMENT au GPU Colab (outil colab_run).
# Ce n'est pas un "projet" a scaffolder ; router vers l'agent est aussi sans risque car
# l'agent gere images (lecture ET generation) mieux que le pipeline.
_HEAVY_MEDIA_RE = re.compile(
    r'\b(gener\w*|cree\w*|creer|dessine\w*|fais|produis|fabrique\w*)\b[^.?!\n]{0,40}'
    r'\b(image|images|photo|photos|illustration|dessin|logo|visuel|banniere|avatar|'
    r'video|videos|clip|animation|rendu)\b')
# Signal ETROIT "construire un programme" (pour desamorcer un faux positif media sur un
# outil qui TRAITE des images) : uniquement des mots qui signifient sans ambiguite "ecris
# du code", volontairement SANS site/app/api/interface (contexte design possible).
_BUILD_PROGRAM_RE = re.compile(
    r'\b(outil|script|programme|logiciel|cli|ligne de commande|'
    r'python|javascript|typescript|node|module|package|bibliotheque|librairie)\b')
def is_heavy_media_request(prompt):
    low = strip_accents(prompt.lower())
    # "jeu video"/"mini-jeu video"/"jeu de..." = un JEU a developper (projet), PAS une
    # generation de video : le mot "jeu" desamorce le faux positif sur "video".
    if re.search(r'\bjeu', low):
        return False
    # Un OUTIL/SCRIPT/programme qui MANIPULE des images (redimensionner, convertir, compresser...)
    # est un projet de DEV, pas une demande de GENERATION d'image a deleguer au GPU (constate,
    # banc de tests : "outil Python de redimensionnement d'images en lot" partait a tort vers
    # l'agent de generation media). On utilise un signal "construire un programme" ETROIT (pas
    # _STRONG_DEV_RE entier, qui contient site/app/api et capterait a tort "genere une image
    # pour mon site").
    if _BUILD_PROGRAM_RE.search(low):
        return False
    return bool(_HEAVY_MEDIA_RE.search(low))

def match_existing_project(prompt):
    """Retrouve le projet EXISTANT visé par la demande (nom entre « », ou nom present dans le texte).
    Retourne le nom exact du dossier workspace, ou None."""
    candidates = [p["name"] for p in BRAIN_MEMORY.get("projects", [])]
    if not candidates:
        return None
    # 1) nom explicite entre guillemets « » ou " " ou “ ”
    m = re.search(r'[«"“]\s*([^»"”]+?)\s*[»"”]', prompt)
    if m:
        target = strip_accents(m.group(1).strip().lower())
        for name in candidates:
            nl = strip_accents(name.lower())
            if nl == target or target in nl or nl in target:
                return name
    # 2) nom de projet present tel quel dans le texte (le plus long d'abord)
    # normalisation d'accents : sinon "Créé Mon Blog" ne matche jamais le dossier
    # workspace "cree_mon_blog" (cf. HANDOFF.md) car .lower() seul ne suffit pas.
    # Bornes de mot explicites (alnum uniquement, underscore autorise comme frontiere) :
    # sans ca, un projet nomme "_eval" matchait a tort dans "calculatrice_eval" (constate
    # au banc de tests), redirigeant une nouvelle demande vers un dossier existant sans rapport.
    low = strip_accents(prompt.lower())
    for name in sorted(candidates, key=len, reverse=True):
        nl = strip_accents(name.lower())
        if re.search(r'(?<![a-z0-9])' + re.escape(nl) + r'(?![a-z0-9])', low):
            return name
    return None

# ─── Appels IA : voir ollama_client.py (call_brain, stream_agent, preload_models...) ──

# ─── HTML (interface web complete, voir templates.py) ─────────────────────────
from templates import HTML


# ─── WebSocket ────────────────────────────────────────────────────────────────

def parse_todos(plan_text):
    todos = []
    for m in re.finditer(r'(?:TODO|TODOS?|TÂCHES?)[:\s]*\n?((?:\d+[\.\)]\s*.+\n?)+)', plan_text, re.IGNORECASE):
        for line in m.group(1).strip().split("\n"):
            txt = re.sub(r'^\d+[\.\)]\s*','',line).strip()
            if txt: todos.append({"text":txt,"done":False,"active":False})
    if not todos:
        for m in re.finditer(r'\d+[\.\)]\s+([A-ZÉÈÀÙ][^\.]+)', plan_text):
            todos.append({"text":m.group(1).strip(),"done":False,"active":False})
    return todos[:6]

@app.get("/", response_class=HTMLResponse)
async def home(): return HTML

# Libellés conviviaux pour les modèles connus
MODEL_LABELS = {
    "qwen2.5-coder:1.5b": "Qwen2.5 1.5b — ultra rapide",
    "qwen2.5-coder:3b":   "Qwen2.5 3b — rapide",
    "qwen2.5-coder:7b":   "Qwen2.5 7b — équilibré (ancien défaut)",
    "qwen2.5-coder:14b":  "Qwen2.5 14b — précis mais lent",
    "qwen3-coder:30b":    "Qwen3-Coder 30b (MoE) — rapide et précis ★",
    "devstral-small-2:24b": "Devstral Small 2 24b — precis, lent sur CPU",
    "qwen3:14b":          "Qwen3 14b — raisonnement, tres verbeux/lent",
    "deepseek-r1:32b":    "DeepSeek-R1 32b — raisonnement profond, tres lent",
    "deepseek-coder-v2:16b": "DeepSeek-V2 16b — MoE rapide",
    "deepseek-coder:6.7b":   "DeepSeek 6.7b",
}

@app.get("/models")
async def models():
    """Liste les modèles de code disponibles dans Ollama (pour le sélecteur)."""
    # run_in_executor : un requests.get direct dans une coroutine async gele TOUTE
    # la boucle asyncio (donc tous les WebSocket actifs) pendant l'appel reseau.
    raw = await asyncio.get_event_loop().run_in_executor(None, _fetch_ollama_tags)
    names = [m["name"] for m in raw]
    # Garder les modèles de code propres (exclure embeddings et les *-llma internes)
    coders = [n for n in names
              if any(k in n.lower() for k in ("coder","deepseek","code","qwen","devstral"))
              and "embed" not in n.lower()
              and not n.lower().endswith("-llma:latest")]
    coders.sort()
    active = AGENTS["coder"]["model"]
    out = [{"name": n, "label": MODEL_LABELS.get(n, n), "active": (n == active)} for n in coders]
    return {"models": out, "active": active}

@app.get("/models_detail")
async def models_detail():
    """Liste TOUS les modeles Ollama installes (taille, actif), pour le panneau de gestion."""
    models_raw = await asyncio.get_event_loop().run_in_executor(None, _fetch_ollama_tags)
    active = AGENTS["coder"]["model"]
    out = []
    for m in models_raw:
        name = m.get("name", "")
        if not name or name.lower().endswith("-llma:latest"):
            continue
        out.append({
            "name": name,
            "label": MODEL_LABELS.get(name, name),
            "size_gb": round(m.get("size", 0) / 1024**3, 1),
            "active": name == active,
            "embed": "embed" in name.lower(),
        })
    out.sort(key=lambda x: (x["embed"], x["name"]))
    return {"models": out}

BENCH_RESULTS_PATH = r"C:\Devllma\bench_results.json"

# NB : les handlers "def" (sans async) de ce fichier sont executes par Starlette
# dans son threadpool — leurs I/O bloquantes (SQLite, disque, subprocess) ne gelent
# donc plus la boucle asyncio ni le streaming des WebSockets, comme /models via
# run_in_executor. Ne pas les repasser en "async def" sans envelopper leurs appels
# bloquants dans un executor.
@app.get("/bench_results")
def bench_results():
    """Derniers resultats du banc d'essai comparatif (bench_models.py), si disponibles."""
    if os.path.exists(BENCH_RESULTS_PATH):
        try:
            return {"results": json.load(open(BENCH_RESULTS_PATH, encoding="utf-8"))}
        except Exception:
            pass
    return {"results": {}}

try:
    import psutil
    psutil.cpu_percent(interval=None)  # amorce la mesure
except Exception:
    psutil = None

def _read_coretemp():
    """Lit la memoire partagee de Core Temp (logiciel deja installe et actif sur ce
    poste) : source BEAUCOUP plus fiable que la zone ACPI WMI, qui renvoie une
    valeur figee sur ce materiel (cf. historique). Structure documentee du SDK
    Core Temp (mapping 'CoreTempMappingObjectEx') : uiLoad[256] + uiTjMax[128] +
    uiCoreCnt + uiCPUCnt + fTemp[256]. Renvoie None si Core Temp ne tourne pas."""
    import mmap, struct
    data = None
    # DevLLMA tourne en Session 0 (tache planifiee SYSTEM) alors que Core Temp
    # tourne dans la session interactive : un mapping nomme sans prefixe est
    # isole par session sous Windows, d'ou le prefixe Global\ en 1er essai.
    for name in ("Global\\CoreTempMappingObjectEx", "CoreTempMappingObjectEx"):
        try:
            mm = mmap.mmap(-1, 4260, name, access=mmap.ACCESS_READ)
            data = mm.read(4260)
            mm.close()
            break
        except OSError:
            continue
    if data is None:
        return None
    try:
        core_cnt, = struct.unpack_from("<I", data, 256 * 4 + 128 * 4)
        if not (0 < core_cnt <= 32):
            return None
        temps = struct.unpack_from(f"<{core_cnt}f", data, (256 + 128 + 2) * 4)
        return round(max(temps), 1)
    except (struct.error, ValueError):
        return None

_temp_cache = {"v": None, "t": 0.0, "history": [], "unreliable": False, "last_check": 0.0}
def get_temp():
    """Température (°C) — Core Temp (memoire partagee) en priorite si actif, sinon
    repli sur WMI MSAcpi_ThermalZoneTemperature. Sur beaucoup de cartes meres
    desktop, cette zone ACPI n'est pas reellement cablee et renvoie une valeur
    figee (constate sur ce poste: 27.9°C en boucle, identique a 0% comme a 4% de
    charge CPU) — dans ce cas de repli, on detecte le gel (6 lectures fraiches
    consecutives ~30s identiques) et on bascule sur N/A plutot qu'un chiffre
    trompeur, avec une nouvelle tentative toutes les 5 min."""
    now = time.time()
    if _temp_cache["v"] is not None and now - _temp_cache["t"] < 5:
        return _temp_cache["v"]

    ct = _read_coretemp()
    if ct is not None:
        _temp_cache["v"] = ct; _temp_cache["t"] = now
        _temp_cache["unreliable"] = False; _temp_cache["history"] = []
        return ct

    if _temp_cache["unreliable"] and now - _temp_cache["last_check"] < 300:
        return None
    raw = None
    try:
        r = subprocess.run(["powershell","-NonInteractive","-Command",
            "(Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature).CurrentTemperature"],
            capture_output=True, text=True, timeout=5, encoding="utf-8", errors="replace")
        vals = [float(x)/10-273.15 for x in (r.stdout or "").split() if x.strip().isdigit()]
        raw = round(max(vals), 1) if vals else None
    except Exception:
        raw = None
    _temp_cache["last_check"] = now
    if raw is None:
        _temp_cache["v"] = None; _temp_cache["t"] = now
        return None
    hist = _temp_cache["history"]
    hist.append(raw)
    del hist[:-6]
    if len(hist) == 6 and len(set(hist)) == 1:
        _temp_cache["unreliable"] = True
        _temp_cache["v"] = None; _temp_cache["t"] = now
        return None
    _temp_cache["v"] = raw; _temp_cache["t"] = now
    return raw

@app.get("/stats")
def stats_endpoint():
    """Usage temps réel du poste : CPU %, RAM, température."""
    if psutil is None:
        try:
            mc = db_stats().get("memories", 0)
        except Exception:
            mc = 0
        return {"cpu": None, "ram_pct": None, "temp": get_temp(), "memories": mc}
    vm = psutil.virtual_memory()
    try:
        mem_count = db_stats().get("memories", 0)
    except Exception:
        mem_count = 0
    return {
        "cpu": round(psutil.cpu_percent(interval=None), 0),
        "ram_pct": round(vm.percent, 0),
        "ram_used": round(vm.used / 1024**3, 1),
        "ram_total": round(vm.total / 1024**3, 1),
        "temp": get_temp(),
        "memories": mem_count,
    }

@app.get("/snapshots/{project}")
def get_snapshots(project: str):
    """Liste les sauvegardes restaurables d'un projet."""
    rows = SnapshotManager.list_snapshots(project)
    return {"snapshots": [{"id": r[0], "label": r[1], "files": r[2], "ts": r[3]} for r in rows]}

_WORKSPACE_REAL = os.path.realpath(WORKSPACE)
_TREE_IGNORE = {"__pycache__", ".git", "node_modules", ".venv", "build", "dist"}

def _safe_workspace_path(relpath):
    """Resout un chemin relatif au workspace et verifie qu'il ne s'en echappe pas
    (meme logique que agent_core._guard_path) — obligatoire, ces endpoints sont
    accessibles a quiconque sur le reseau local sans authentification."""
    relpath = (relpath or "").replace("\\", "/").lstrip("/")
    full = os.path.realpath(os.path.join(_WORKSPACE_REAL, relpath))
    if full != _WORKSPACE_REAL and not full.startswith(_WORKSPACE_REAL + os.sep):
        return None
    return full

def _build_tree(path):
    entries = []
    try:
        names = sorted(os.listdir(path))
    except OSError:
        return entries
    for name in names:
        if name in _TREE_IGNORE:
            continue
        full = os.path.join(path, name)
        if os.path.isdir(full):
            entries.append({"name": name, "type": "dir", "children": _build_tree(full)})
        else:
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            entries.append({"name": name, "type": "file", "size": size})
    return entries

@app.get("/tree/{project}")
def get_tree(project: str):
    """Arborescence complete d'un projet du workspace, pour la visionneuse sidebar."""
    full = _safe_workspace_path(project)
    if not full or not os.path.isdir(full):
        return Response(status_code=404)
    return {"name": project, "children": _build_tree(full)}

@app.get("/file")
def get_file(p: str = ""):
    """Contenu texte d'un fichier du workspace (apercu dans la visionneuse)."""
    full = _safe_workspace_path(p)
    if not full or not os.path.isfile(full):
        return Response(status_code=404)
    if os.path.splitext(full)[1].lower() in (".docx", ".xlsx", ".pdf"):
        try:
            from documents import read_document
            return {"content": read_document(full)[:100_000], "kind": "document"}
        except Exception as e:
            return {"content": f"(document illisible: {e})", "kind": "document"}
    try:
        content = open(full, encoding="utf-8", errors="replace").read(100_000)
    except Exception as e:
        return {"content": f"(fichier illisible: {e})", "kind": "text"}
    return {"content": content, "kind": "text"}

@app.get("/dl")
async def download_file(p: str = ""):
    """Telechargement direct d'un fichier du workspace (binaire ou texte)."""
    full = _safe_workspace_path(p)
    if not full or not os.path.isfile(full):
        return Response(status_code=404)
    return FileResponse(full, filename=os.path.basename(full), media_type="application/octet-stream")

@app.get("/memories")
def get_memories(kind: str = "", q: str = "", offset: int = 0):
    """Liste des souvenirs, ou test de recherche semantique si q est fourni
    (score abaisse a 0.3 pour le mode 'test' — but different de la vraie injection)."""
    if q.strip():
        hits = mem_search(q, 20, kind=kind or None, min_score=0.3)
        return {"mode": "search", "items": hits}
    data = mem_list(kind=kind or None, limit=50, offset=offset)
    return {"mode": "list", **data}

@app.delete("/memories/{mem_id}")
def del_memory(mem_id: int):
    mem_delete(mem_id)
    return {"ok": True}

@app.delete("/memories")
def purge_memories(kind: str = ""):
    mem_purge(kind or None)
    return {"ok": True}

_PIPELINE_LOG = os.path.join(os.path.dirname(WORKSPACE), "logs", "pipeline_runs.jsonl")

def _log_pipeline_run(row):
    """Append-only, best-effort : une ligne JSONL par run du pipeline de dev. Ne doit
    JAMAIS lever (sinon casserait la reponse). Lu par /pipeline_stats."""
    try:
        os.makedirs(os.path.dirname(_PIPELINE_LOG), exist_ok=True)
        row = {**row, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
        with open(_PIPELINE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass

@app.get("/pipeline_stats")
def pipeline_stats(limit: int = 500):
    """Agrege la telemetrie des runs de dev : taux run_ok, iterations moyennes, part Colab.
    Repond au besoin 'quel est mon taux de reussite reel maintenant' sans rejouer un banc."""
    if not os.path.exists(_PIPELINE_LOG):
        return {"runs": 0, "note": "aucun run enregistre pour l'instant"}
    rows = []
    try:
        with open(_PIPELINE_LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try: rows.append(json.loads(line))
                    except Exception: pass
    except Exception as e:
        return {"error": str(e)}
    rows = rows[-limit:]
    n = len(rows)
    if not n:
        return {"runs": 0}
    ok = sum(1 for r in rows if r.get("run_ok"))
    with_files = sum(1 for r in rows if r.get("files_written"))
    iters = [r.get("iterations", 0) for r in rows]
    return {
        "runs": n,
        "run_ok": ok,
        "run_ok_pct": round(100 * ok / n, 1),
        "with_files": with_files,
        "avg_iterations": round(sum(iters) / n, 2),
        "colab_runs": sum(1 for r in rows if r.get("colab")),
        "edits": sum(1 for r in rows if r.get("editing")),
        "last_10": rows[-10:],
    }

# ── Configuration des capacites (interrupteurs du rail "Capacites" de l'UI) ──
# Cles UI booleennes stockees en base (mem_set) et lues par le pipeline pour activer/couper
# une capacite sans redemarrage : recherche web, auto-debug web, passe de verification,
# tests auto, resolveur PyPI. "brain" pilote brain_mode (auto=multi-cerveaux / local).
_UI_CFG_KEYS = {"verify", "tests", "web", "pypi", "autodebug"}

def cfg_on(key, default=False):
    """Un flag de capacite est-il actif ? (lu par le pipeline, cf. rail Capacites)."""
    v = mem_get("cfg_" + key)
    if v is None:
        return default
    return v == "1"

@app.get("/config")
def get_config():
    bm = mem_get("brain_mode") or "auto"
    out = {"brain": bm != "local", "brain_mode": bm}
    # valeurs par defaut alignees sur l'UI (verify ON par defaut, le reste OFF)
    defaults = {"verify": True, "tests": False, "web": False, "pypi": False, "autodebug": False}
    for k in _UI_CFG_KEYS:
        out[k] = cfg_on(k, defaults.get(k, False))
    return out

@app.post("/config")
async def set_config(request: Request):
    try:
        data = await request.json()
    except Exception:
        return {"ok": False, "error": "corps JSON invalide"}
    key = data.get("set")
    val = bool(data.get("value"))
    if key in ("brain", "brain_mode"):
        mem_set("brain_mode", "auto" if val else "local")
    elif key in _UI_CFG_KEYS:
        mem_set("cfg_" + key, "1" if val else "0")
    else:
        return {"ok": False, "error": f"cle inconnue: {key}"}
    return {"ok": True, "set": key, "value": val}

# ── Compilation .exe (bouton "Compiler en .exe" du rail Capacites) ──
def _user_desktop():
    """Vrai bureau de l'utilisateur. Le serveur tourne en SYSTEM -> expanduser('~') pointe
    vers le profil systeme (system32\\config\\systemprofile) et l'exe serait invisible.
    On resout le profil d'un utilisateur reel de C:\\Users."""
    up = os.environ.get("USERPROFILE", "")
    if up and "systemprofile" not in up.lower() and os.path.isdir(os.path.join(up, "Desktop")):
        return os.path.join(up, "Desktop")
    users = r"C:\Users"
    for cand in ("Admin",) + tuple(os.listdir(users) if os.path.isdir(users) else ()):
        if cand.lower() in ("default", "public", "all users", "default user"):
            continue
        dsk = os.path.join(users, cand, "Desktop")
        if os.path.isdir(dsk):
            return dsk
    return os.path.join(os.path.expanduser("~"), "Desktop")

_EXE_OUT = os.path.join(_user_desktop(), "Projets-DevLLMA-EXE")
_HEAVY_MODULES = ["torch", "tensorflow", "scipy", "matplotlib", "pandas", "numpy", "cv2",
                  "sklearn", "diffusers", "transformers", "PIL", "IPython", "notebook",
                  "jupyter", "sympy", "numba"]
_EXE_ENTRIES = ["main.py", "app.py", "cli.py", "run.py", "server.py", "start.py", "index.py", "__main__.py"]
_EXE_IGN_SUB = {"__pycache__", ".git", ".venv", "venv", "node_modules", ".backups",
                ".ruff_cache", "tests", "build", "dist", "logs"}

def _compile_go_exe(project, d):
    """Compile un projet Go (main.go a la racine) en .exe natif via 'go build' — pas besoin de
    PyInstaller ni d'exclusions de libs lourdes : le compilateur Go produit directement un binaire
    autonome, plus petit et plus fiable qu'un empaquetage PyInstaller. Meme GOCACHE dedie que
    l'execution (cf. execute_project) pour eviter tout cout de compilation a froid."""
    os.makedirs(_EXE_OUT, exist_ok=True)
    exe = os.path.join(_EXE_OUT, project + ".exe")
    env = dict(os.environ)
    env["GOCACHE"] = GO_CACHE_DIR
    try:
        r = subprocess.run([GO_EXE, "build", "-o", exe, "main.go"], cwd=d,
                           capture_output=True, text=True, timeout=120,
                           encoding="utf-8", errors="replace", env=env)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if os.path.exists(exe):
        return {"ok": True, "path": exe, "mb": round(os.path.getsize(exe) / 1e6, 1)}
    return {"ok": False, "error": (r.stderr or r.stdout or "echec inconnu")[-300:]}

def _compile_project_exe(project):
    """Compile un projet du workspace en .exe autonome (PyInstaller --onefile), en excluant
    les libs lourdes NON importees (torch/scipy/... -> exe leger). Detecte le point d'entree
    (racine puis sous-dossier). Sortie: Bureau\\DevLLMA-EXE\\<projet>.exe."""
    import shutil, tempfile
    d = os.path.join(WORKSPACE, project)
    if not os.path.isdir(d):
        return {"ok": False, "error": "projet introuvable dans le workspace"}
    if os.path.exists(os.path.join(d, "main.go")):
        return _compile_go_exe(project, d)
    ep = None
    for e in _EXE_ENTRIES:
        if os.path.exists(os.path.join(d, e)):
            ep = e; break
    if not ep:
        for sub in sorted(os.listdir(d)):
            sd = os.path.join(d, sub)
            if os.path.isdir(sd) and sub not in _EXE_IGN_SUB:
                for e in _EXE_ENTRIES:
                    if os.path.exists(os.path.join(sd, e)):
                        ep = os.path.join(sub, e); break
            if ep:
                break
    if not ep:
        pys = [f for f in os.listdir(d) if f.endswith(".py") and not f.startswith("_")]
        ep = pys[0] if pys else None
    if not ep:
        return {"ok": False, "error": "aucun point d'entree Python trouve"}
    txt = ""
    for root, dirs, fs in os.walk(d):
        dirs[:] = [x for x in dirs if x not in _EXE_IGN_SUB]
        for f in fs:
            if f.endswith(".py"):
                try:
                    txt += open(os.path.join(root, f), encoding="utf-8", errors="ignore").read()
                except Exception:
                    pass
    excludes = []
    for m in _HEAVY_MODULES:
        if not re.search(r'(?:^|\n)\s*(?:import|from)\s+' + re.escape(m) + r'\b', txt):
            excludes += ["--exclude-module", m]
    is_web = ("fastapi" in txt.lower() or "uvicorn" in txt.lower())
    os.makedirs(_EXE_OUT, exist_ok=True)
    work = os.path.join(tempfile.gettempdir(), "pyi_web", project)
    shutil.rmtree(work, ignore_errors=True); os.makedirs(work, exist_ok=True)
    cmd = [PYTHON, "-m", "PyInstaller", "--onefile", "--noconfirm", "--clean",
           "--name", project, "--distpath", _EXE_OUT, "--workpath", work, "--specpath", work] + excludes
    if is_web:
        cmd += ["--collect-all", "uvicorn", "--collect-all", "fastapi", "--hidden-import", "anyio"]
    cmd.append(ep)
    try:
        r = subprocess.run(cmd, cwd=d, capture_output=True, text=True, timeout=1200,
                           encoding="utf-8", errors="replace")
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    exe = os.path.join(_EXE_OUT, project + ".exe")
    if os.path.exists(exe):
        return {"ok": True, "path": exe, "mb": round(os.path.getsize(exe) / 1e6, 1)}
    return {"ok": False, "error": (r.stderr or r.stdout or "echec inconnu")[-300:]}

@app.post("/compile_exe/{project}")
async def compile_exe_route(project: str):
    return await asyncio.get_event_loop().run_in_executor(None, _compile_project_exe, project)

@app.post("/restore/{snapshot_id}")
def restore_snapshot(snapshot_id: int):
    """Restaure un snapshot precedent (rollback) d'un projet."""
    from skills import _cx
    with _cx() as c:
        row = c.execute("SELECT project FROM backups WHERE id=?", (snapshot_id,)).fetchone()
    if not row:
        return {"ok": False, "msg": "Snapshot introuvable"}
    project_dir = os.path.join(WORKSPACE, row[0])
    ok, out = SnapshotManager.restore(snapshot_id, project_dir)
    return {"ok": ok, "msg": out}

@app.get("/export/{sid}")
def export_session(sid: int):
    """Exporte une session en markdown telechargeable."""
    rows = history(sid, 500)
    lines = [f"# DevLLMA — Session #{sid}\n"]
    for agent, role, content in rows:
        who = "Utilisateur" if role == "user" else agent.upper()
        lines.append(f"## {who}\n\n{content}\n")
    md_text = "\n".join(lines)
    return Response(md_text, media_type="text/markdown",
                     headers={"Content-Disposition": f'attachment; filename="session_{sid}.md"'})

@app.post("/ocr")
async def ocr(file: UploadFile = File(...)):
    """Reçoit une image (collée/déposée dans le chat) et en extrait le texte (OCR)."""
    try:
        data = await file.read()
        p = r"C:\Devllma\_chat_ocr_upload.png"
        with open(p, "wb") as f:
            f.write(data)
        # executor : l'OCR = plusieurs secondes de CPU, gelerait tous les WebSockets
        txt = await asyncio.get_event_loop().run_in_executor(None, read_image_text, p)
        return {"text": txt}
    except Exception as e:
        return {"text": f"(erreur OCR: {e})"}

@app.post("/upload_doc")
async def upload_doc(file: UploadFile = File(...)):
    """Reçoit un document Word/Excel/PDF déposé dans le chat et en extrait le texte
    (documents.read_document route sur l'extension, d'où le fichier temporaire suffixé)."""
    try:
        from documents import read_document
        ext = os.path.splitext(file.filename or "")[1].lower()
        if ext not in (".docx", ".xlsx", ".pdf"):
            return {"error": f"format non pris en charge : {ext or '(sans extension)'}"}
        data = await file.read()
        p = rf"C:\Devllma\_chat_doc_upload{ext}"
        with open(p, "wb") as f:
            f.write(data)
        # executor : parse DOCX/XLSX/PDF = plusieurs secondes, gelerait tous les WebSockets
        txt = await asyncio.get_event_loop().run_in_executor(None, read_document, p)
        truncated = len(txt) > 15000
        if truncated:
            txt = txt[:15000] + "\n[... document tronqué ...]"
        return {"name": file.filename, "text": txt, "truncated": truncated}
    except Exception as e:
        return {"error": f"lecture du document impossible : {e}"}

@app.get("/search")
def search_sessions(q: str = ""):
    """Recherche plein texte dans l'historique des messages, groupée par session."""
    q = (q or "").strip()
    if len(q) < 2:
        return {"results": []}
    try:
        from db import search_messages
        return {"results": search_messages(q)}
    except Exception:
        return {"results": []}

@app.get("/sessions")
def sessions():
    """Liste les sessions récentes (pour reprendre une session)."""
    try:
        rows = list_sessions()  # (id, title, created_at)
        return {"sessions": [{"id": r[0], "title": r[1], "created_at": r[2]} for r in rows]}
    except Exception:
        return {"sessions": []}

@app.delete("/sessions/{sid}")
def remove_session(sid: int):
    """Supprime une session (et ses messages/taches)."""
    try:
        delete_session(sid)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

TOOL_LABELS = {
    "read_file": "lecture fichier", "write_file": "écriture fichier",
    "list_dir": "liste dossier", "run_powershell": "commande système",
    "web_search": "recherche web", "memory_search": "recherche mémoire",
    "memory_save": "mémorisation", "get_datetime": "horloge système",
    "fetch_url": "lecture page web", "execute_python": "exécution python",
    "run_sql": "requête SQL", "edit_file": "édition de fichier",
    "grep_search": "recherche dans les fichiers", "find_files": "recherche de fichiers",
    "read_lines": "lecture par lignes", "read_image": "lecture d'image (OCR)",
    "http_request": "requête API", "csv_analyze": "analyse CSV",
    "open_path": "ouverture",
}

CONV_WINDOW = 12  # nb de messages recents passes au modele comme contexte conversationnel.
                  # 12 (~1500-1800 car tronques -> ~500 tokens) : assez pour suivre un fil
                  # ("ajoute un mode sombre a ce que tu viens de faire") sans peser lourd dans
                  # num_ctx=32768 (cout d'eval marginal sur ce CPU, mesure).

def _history_text(sid, current_prompt, n=CONV_WINDOW):
    """Derniers n echanges (HORS message courant) formates pour donner au modele le fil de la
    conversation. Le message courant vient d'etre enregistre par msg() -> on le retire pour
    ne pas le dupliquer avec la DEMANDE. Chaque message tronque a 150 car (borne le cout)."""
    rows = history(sid, n + 1)
    if rows and rows[-1][1] == "user" and rows[-1][2][:150] == current_prompt[:150]:
        rows = rows[:-1]
    return "\n".join(f"[{r[0].upper()}]: {r[2][:150]}" for r in rows) if rows else ""

async def handle_agent(websocket, sid_box, prompt, cancel_event):
    """Agent generaliste a outils (type Claude Code) : gere tout ce qui n'est pas une grosse
    tache de generation de projet (questions, recherche, actions systeme, fichiers ponctuels,
    memoire) en decidant lui-meme, etape par etape, quel outil utiliser. Remplace les anciens
    chemins figes handle_chat/handle_research/action-systeme."""
    sid = sid_box["sid"]
    history_text = _history_text(sid, prompt)

    await websocket.send_json({"type":"thinking"})
    await websocket.send_json({"type":"agent_start","agent":"agent"})

    loop = asyncio.get_event_loop()
    out_q = _queue.Queue()

    def notify(kind, payload):
        out_q.put((kind, payload))

    fut = loop.run_in_executor(None, lambda: run_agent_sync(
        prompt, history_text, notify, should_stop=cancel_event.is_set))

    final_text = None
    step_id = 0
    step_times = {}
    while final_text is None:
        if cancel_event.is_set():
            break
        try:
            kind, payload = await loop.run_in_executor(None, out_q.get, True, 0.3)
        except _queue.Empty:
            if fut.done():
                break
            continue
        if kind == "tool_call":
            step_id += 1
            step_times[step_id] = time.monotonic()
            label = TOOL_LABELS.get(payload["name"], payload["name"])
            detail = next(iter(payload.get("args", {}).values()), "")
            await websocket.send_json({
                "type":"tool_step","id":step_id,"phase":"start",
                "name":payload["name"],"label":label,
                "args_preview":str(detail)[:120],
                "args_full":json.dumps(payload.get("args", {}), ensure_ascii=False)[:600],
            })
        elif kind == "tool_result":
            res = payload.get("result", {})
            ok = not (isinstance(res, dict) and res.get("error"))
            t0 = step_times.pop(step_id, None)
            await websocket.send_json({
                "type":"tool_step","id":step_id,"phase":"end","ok":ok,
                "ms":int((time.monotonic() - t0) * 1000) if t0 else None,
                "result_preview":json.dumps(res, ensure_ascii=False, default=str)[:400],
            })
            # Resultat SQL tabulaire -> tableau interactif cote client (donnees EXACTES
            # de la base, sans passer par une reformulation du modele).
            if payload.get("name") == "run_sql" and isinstance(res, dict) and res.get("columns"):
                await websocket.send_json({
                    "type":"sql_result","columns":res["columns"],
                    "rows":res.get("rows", [])[:200],
                    "truncated":bool(res.get("truncated")),
                })
        elif kind == "final":
            final_text = payload

    if cancel_event.is_set():
        await websocket.send_json({"type":"stopped"})
        await websocket.send_json({"type":"done"})
        return

    if final_text is None:
        final_text = await fut

    await websocket.send_json({"type":"token","text":final_text})
    msg(sid, "agent", "assistant", final_text)
    await asyncio.get_event_loop().run_in_executor(
        None, lambda: mem_index("qa", prompt[:80], f"Q: {prompt}\nR: {final_text[:600]}")
    )
    await websocket.send_json({"type":"done"})

MAX_PROMPT_CHARS = 40000  # ~11k tokens : genereux pour un vrai collage de code, mais borne les
                          # collages pathologiques. Au-dela de num_ctx (32768) Ollama tronque
                          # silencieusement et un prompt geant gele la generation CPU sans retour.

# ── Delegation du CODE au GPU Colab (qwen2.5-coder:14b) pour les cas lourds ──────
# Le modele local (30b sur CPU) rame sur les gros projets / quand il tourne en rond.
# On envoie alors la generation au worker Colab (endpoint /llm, GPU) — bien plus rapide,
# et modele different = second avis quand le local bloque. Repli local si Colab eteint.
_COLAB_ASK_RE = re.compile(r'\b(utilise|via|avec|sur|passe par)\s+colab\b')

def _colab_llm(system, prompt, timeout=(10, 180)):
    """Generation de code par le LLM GPU du worker Colab (/llm).
    Retourne (texte, motif) : texte=None si indisponible, motif = raison lisible du repli
    ('worker injoignable', 'route /llm absente (worker sans cellule LLM)', 'token invalide',
    'erreur HTTP N', 'reponse vide') pour afficher un statut Colab HONNETE a l'utilisateur.
    timeout=(connexion, silence-lecture) : 10s pour joindre le tunnel + jusqu'a 3 min de
    silence avant repli local (un notebook qui accepte la connexion puis bloque gelait la
    tache jusqu'a 10 min avant)."""
    base = mem_get("colab_url")
    if not base:
        return None, "aucun worker Colab configure"
    # ngrok-skip-browser-warning : evite la page d'avertissement HTML de ngrok gratuit.
    headers = {"Content-Type": "application/json", "ngrok-skip-browser-warning": "1"}
    tok = mem_get("colab_token")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    # 1 re-essai sur erreur de CONNEXION uniquement (blip transitoire du tunnel ngrok /
    # reconnexion en cours) — PAS sur 404/401/500 (definitifs, inutile d'insister). Rend la
    # delegation resiliente a une micro-coupure pendant un gros traitement.
    last_exc = None
    for attempt in range(2):
        try:
            r = _http.post(base + "/llm", headers=headers,
                           json={"system": system or "", "prompt": prompt}, timeout=timeout)
            if r.status_code == 404:
                return None, "route /llm absente du worker (cellule LLM non lancee)"
            if r.status_code == 401:
                return None, "token Colab invalide"
            if r.status_code != 200:
                return None, f"erreur HTTP {r.status_code}"
            txt = (r.json().get("response") or "").strip()
            return (txt, None) if txt else (None, "reponse vide du worker")
        except Exception as e:
            last_exc = e
            if attempt == 0:
                time.sleep(3)  # laisse le tunnel se reconnecter avant le 2e essai
    return None, f"worker injoignable ({type(last_exc).__name__})"

async def _gen_code(websocket, agent_name, prompt, system, cancel_event, temperature=0.2, via_colab=False):
    """Genere du code via le GPU Colab si via_colab ET Colab dispo, sinon via le modele local
    (stream). Renvoie le texte complet. Repli local automatique si Colab ne repond pas."""
    if via_colab and mem_get("colab_url"):
        await websocket.send_json({"type":"agent_start","agent":"colab-gpu"})
        # Statut CLAIR et VERIFIABLE : on annonce une TENTATIVE (pas un succes premature),
        # puis on confirme reellement selon le resultat. L'evenement colab_task permet a
        # l'UI d'afficher un bandeau distinct ; le token reste visible dans le fil.
        await websocket.send_json({"type":"colab_task","state":"start"})
        await websocket.send_json({"type":"token","text":"⚡ Gros projet → envoi au GPU Colab (Tesla T4)…\n"})
        txt, motif = await asyncio.get_event_loop().run_in_executor(None, _colab_llm, system, prompt)
        if txt is not None:
            await websocket.send_json({"type":"colab_task","state":"ok"})
            await websocket.send_json({"type":"token","text":"✅ Code généré par le GPU Colab.\n"})
            await websocket.send_json({"type":"token","text":txt})
            return txt
        await websocket.send_json({"type":"colab_task","state":"fallback","reason":motif})
        await websocket.send_json({"type":"token","text":f"⚠️ GPU Colab non utilisé ({motif}) → génération en local (CPU).\n"})
    return await stream_agent(websocket, agent_name, prompt, system, cancel_event=cancel_event, temperature=temperature)

async def handle_prompt(websocket, sid_box, prompt, cancel_event):
    sid = sid_box["sid"]
    if len(prompt) > MAX_PROMPT_CHARS:  # tronque AVANT msg() -> l'utilisateur voit la coupe,
        prompt = prompt[:MAX_PROMPT_CHARS] + "\n[... demande tronquée ...]"  # sans nouveau type WS
    msg(sid, "user", "user", prompt)

    # ── Salutation / message tres court -> reponse rapide, pas de pipeline ──
    low = prompt.lower().strip()
    if low in GREETINGS or len(low) < 5:
        reply = "Prêt. Décris ton projet, ou pose-moi une question."
        await websocket.send_json({"type":"agent_start","agent":"brain"})
        await websocket.send_json({"type":"token","text":reply})
        msg(sid, "brain", "assistant", reply)
        await websocket.send_json({"type":"done"})
        return

    # ── Aucun signal de dev EXPLICITE (mot-cle/edition/projet existant) -> l'agent
    # generaliste a outils prend le relais (questions, recherche, actions systeme,
    # fichiers ponctuels...). Le pipeline de generation de projet ci-dessous reste
    # reserve aux demandes de dev clairement identifiees.
    # Un message avec document joint (prefixe par l'UI) va TOUJOURS a l'agent : le contenu
    # du document contiendrait souvent des mots-cles de dev qui declencheraient a tort le
    # pipeline de generation de projet.
    is_doc_message = prompt.startswith("Contenu du document")
    # Une QUESTION ("Explique...", "Comment...", "...?") reste une question meme si elle
    # mentionne python/api/sql : sans ce garde-fou, "Explique-moi les listes en Python"
    # generait un projet complet au lieu de repondre (constate au banc de tests).
    is_question = is_research_question(prompt) and not match_existing_project(prompt) and not is_edit(prompt)
    # Generer une image/video = tache lourde -> agent (delegue au GPU Colab), pas le pipeline projet
    heavy_media = is_heavy_media_request(prompt) and not match_existing_project(prompt)
    dev_signal = (has_dev_keywords(prompt) or match_existing_project(prompt) or is_edit(prompt)) \
                 and not is_file_or_doc_action(prompt) and not is_doc_message and not is_question \
                 and not heavy_media
    if not dev_signal:
        await handle_agent(websocket, sid_box, prompt, cancel_event)
        return

    # ── FAST-PATH snippet : une simple fonction/regex demandee dans le chat ne
    # justifie pas le pipeline projet (plan brain + fichiers + scan + execution
    # ≈ 90 s mesurees) : on streame directement le coder (~15-30 s). Jamais pour
    # un projet existant ni une edition — le pipeline garde ces cas.
    if is_trivial_snippet(prompt) and not match_existing_project(prompt) and not is_edit(prompt):
        await websocket.send_json({"type":"thinking"})
        resp = await stream_agent(websocket, "coder", prompt, cancel_event=cancel_event)
        msg(sid, "coder", "assistant", resp)
        if not cancel_event.is_set():
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: mem_index("qa", prompt[:80], f"Q: {prompt}\nR: {resp[:600]}"))
        await websocket.send_json({"type":"done"})
        return

    # ── Charger mémoire brain ─────────────────────────────────────────
    # executor : sur cache TTL froid le scan disque gelerait la boucle asyncio
    await asyncio.get_event_loop().run_in_executor(None, load_workspace_memory)
    agents_needed = route(prompt) or ["coder"]
    # Si la demande vise un projet EXISTANT -> on cible SON dossier (pas un nouveau slug)
    matched = match_existing_project(prompt)
    if matched:
        project_name = matched
        editing = True
    else:
        project_name = slug(prompt)
        editing = is_edit(prompt)
    project_dir = os.path.join(WORKSPACE, project_name)

    # ── Anti-ecrasement : une CREATION neuve (ni projet existant cible, ni edition)
    # ne doit jamais reutiliser un dossier deja peuple -> suffixe _2, _3... Sans ca,
    # deux demandes au meme slug s'ecrasent silencieusement (perte de donnees).
    if not matched and not editing and os.path.isdir(project_dir) and os.listdir(project_dir):
        base, i = project_name, 2
        while os.path.isdir(project_dir) and os.listdir(project_dir):
            project_name = f"{base}_{i}"
            project_dir = os.path.join(WORKSPACE, project_name)
            i += 1

    # ── Phase 0 : Mémoire sémantique — souvenirs pertinents (RAG) ────────
    memories = await asyncio.get_event_loop().run_in_executor(None, lambda: mem_search(prompt, 3))
    mem_note = ""
    if memories:
        mem_note = "\n\nSOUVENIRS PERTINENTS (projets/taches passees proches de cette demande):\n" + "\n".join(
            f"- [{m['kind']}] {m['ref_name']}: {m['chunk'][:200]}" for m in memories)
        await websocket.send_json({"type":"memory","items":[{"ref":m["ref_name"],"score":m["score"]} for m in memories]})

    # ── Lecons auto-apprises (kind="lesson", cf. record_lesson) — auto-correction :
    # DevLLMA reinjecte ici ce qu'il a lui-meme distille de ses echecs passes, sans
    # qu'un humain ait besoin de repatcher CODER_SYSTEM a la main a chaque bug trouve.
    # min_score 0.50 (au lieu de 0.35) + k=3 : ne reinjecter que des lecons VRAIMENT proches
    # de la demande. Un seuil trop bas ajoutait des conseils peu pertinents qui gonflaient le
    # prompt (couteux en prompt-eval CPU) et pouvaient egarer le modele.
    lessons = await asyncio.get_event_loop().run_in_executor(None, lambda: mem_search(prompt, 3, kind="lesson", min_score=0.50))
    lessons_note = ""
    if lessons:
        lessons_note = "\n\nLECONS AUTO-APPRISES (bugs deja rencontres et corriges sur des projets similaires — EVITE-LES) :\n" + "\n".join(
            f"- {l['chunk']}" for l in lessons)
    coder_system_dyn = CODER_SYSTEM + lessons_note
    coder_fix_system_dyn = CODER_FIX_SYSTEM + lessons_note

    # ── Contexte conversationnel : le pipeline projet suit maintenant le fil du chat
    # (avant, seul l'agent generaliste l'avait) -> "ajoute X au projet precedent",
    # "reprends l'idee d'avant" fonctionnent sans re-decrire tout le contexte.
    conv = _history_text(sid, prompt)
    conv_note = f"\n\nCONVERSATION RECENTE (contexte ; la demande a traiter est ci-dessus):\n{conv}" if conv else ""

    # ── Phase 1 : Cerveau de reflexion (multi-sous-cerveaux) ────────────
    # Pour une demande SUBSTANTIELLE, on ne fait plus une seule passe : on enchaine trois
    # sous-cerveaux — ARCHITECTE (conçoit) -> CRITIQUE (relit, trouve les manques) -> le BRAIN
    # (synthetise le plan+todos final en integrant les deux). Les deux premieres passes sont
    # DELEGUEES a un LLM cloud GRATUIT quand il repond (ne bloque pas le CPU) ; sinon locales.
    # La synthese finale reste sur le brain LOCAL (format plan+todos attendu par le pipeline).
    await websocket.send_json({"type":"thinking"})
    loop = asyncio.get_event_loop()
    brain_mode = mem_get("brain_mode") or "auto"
    deep = len(prompt) > 180 and not is_edit(prompt)   # reflexion approfondie sur les vrais projets

    async def _emit_subbrain(agent, icon, src, text):
        await websocket.send_json({"type":"agent_start","agent":agent})
        await websocket.send_json({"type":"brain_think","text":f"{icon} {agent.capitalize()} ({src}) :\n{text}"})

    plan = None
    if deep and brain_mode == "local":
        # SOUS-CERVEAUX 100% LOCAUX (hors-ligne, plus lent sur CPU) — pour evaluer leur utilite.
        archi = await loop.run_in_executor(None, call_brain, f"DEMANDE:\n{prompt}{mem_note}", ARCHITECTE_SYS, 450)
        if cancel_event.is_set(): await websocket.send_json({"type":"done"}); return
        await _emit_subbrain("architecte","🏛️","local",archi)
        crit = await loop.run_in_executor(None, call_brain, f"DEMANDE:\n{prompt}\n\nPLAN PROPOSE:\n{archi}", CRITIQUE_SYS, 400)
        if cancel_event.is_set(): await websocket.send_json({"type":"done"}); return
        await _emit_subbrain("critique","🔍","local",crit)
        synth_prompt = (f"{prompt}{conv_note}{mem_note}\n\nARCHITECTURE :\n{archi}\n\nCORRECTIONS (critique) :\n{crit}\n\n"
                        f"Produis le PLAN FINAL et les TODOS en integrant l'architecture et ces corrections.")
        plan = await loop.run_in_executor(None, call_brain, synth_prompt)
    elif deep:
        # CERVEAU MULTI-SOUS-CERVEAUX delegue au CLOUD GRATUIT (rapide, ne bloque pas le CPU) :
        # ARCHITECTE (cloud) -> CRITIQUE (cloud) -> SYNTHESE (brain LOCAL, format todos fiable).
        # On teste d'abord l'architecte en cloud PUR : s'il repond, on fait tout le cerveau ;
        # s'il echoue (cloud indispo), on retombe direct sur le plan LOCAL UNIQUE (pas de triple
        # passe locale lente) -> aucune regression de vitesse quand le cloud est absent.
        archi = await loop.run_in_executor(None, lambda: cloud_llm(ARCHITECTE_SYS, f"DEMANDE:\n{prompt}{mem_note}", 500))
        if cancel_event.is_set(): await websocket.send_json({"type":"done"}); return
        if archi:
            await _emit_subbrain("architecte","🏛️","cloud",archi)
            crit = await loop.run_in_executor(None, lambda: cloud_llm(CRITIQUE_SYS, f"DEMANDE:\n{prompt}\n\nPLAN PROPOSE:\n{archi}", 450)) or ""
            if cancel_event.is_set(): await websocket.send_json({"type":"done"}); return
            if crit:
                await _emit_subbrain("critique","🔍","cloud",crit)
            synth_prompt = (f"{prompt}{conv_note}{mem_note}\n\nARCHITECTURE :\n{archi}\n\nCORRECTIONS (critique) :\n{crit}\n\n"
                            f"Produis le PLAN FINAL et les TODOS en integrant l'architecture et ces corrections.")
            plan = await loop.run_in_executor(None, call_brain, synth_prompt)
    if plan is None:
        plan = await loop.run_in_executor(None, call_brain, prompt + conv_note + mem_note)
    if cancel_event.is_set():
        await websocket.send_json({"type":"stopped"}); await websocket.send_json({"type":"done"}); return
    await websocket.send_json({"type":"agent_start","agent":"brain"})
    await websocket.send_json({"type":"brain_think","text":plan})

    # Extraire et envoyer les todos
    todos = parse_todos(plan)
    if todos:
        todos[0]["active"] = True
        await websocket.send_json({"type":"todos","items":todos})

    # ── Déléguer au GPU Colab ? sur demande explicite, OU gros projet (plan long /
    # beaucoup de tâches). Le local (CPU) rame sur ces cas ; le GPU Colab est bien
    # plus rapide. Repli local automatique si Colab est éteint (géré dans _gen_code).
    use_colab_code = bool(_COLAB_ASK_RE.search(strip_accents(prompt.lower())))
    big_project = use_colab_code or len(todos) >= 5 or len(plan) > 1500
    if big_project and mem_get("colab_url"):
        await websocket.send_json({"type":"file_created",
            "name":"⚡ Gros projet détecté — tentative GPU Colab (statut confirmé ci-dessous)","size":""})

    # ── Phase 2 : Lire le code existant si c'est une modif/reprise ───
    existing = {}
    if editing and os.path.isdir(project_dir):
        # executor : lecture disque de tout le projet, ne pas geler la boucle asyncio.
        # Sur la voie EDITION la consigne est "reecris les fichiers changes EN ENTIER" :
        # lire avec le cap serre par defaut (1600 car) tronquait les fichiers >1600 car,
        # que le modele reecrivait alors depuis une version amputee -> perte silencieuse
        # de la fin du fichier. On lit donc les corps COMPLETS (cap large) sur ce chemin.
        existing = await asyncio.get_event_loop().run_in_executor(
            None, lambda: read_project(project_dir, max_chars=12000, total_budget=48000, max_files=20))
    if existing:
        ctx_note = ("\n\nCODE EXISTANT DU PROJET (tu DOIS partir de ce code et le MODIFIER, "
                    "PAS repartir de zéro ; conserve ce qui marche, applique seulement la demande) :\n"
                    + format_context(existing))
        await websocket.send_json({"type":"file_created","name":f"↻ {project_name} chargé ({len(existing)} fichiers)","size":"reprise"})
    else:
        ctx_note = ""
        if matched:
            await websocket.send_json({"type":"file_created","name":f"⚠ {project_name} ciblé mais vide","size":""})

    # ── Phase 3 : Générer le code ─────────────────────────────────────
    agent_name = agents_needed[0]
    consigne = ("MODIFIE le code existant ci-dessus selon la demande (réécris les fichiers changés en entier, garde les autres)."
                if existing else "Crée TOUS les fichiers nécessaires (main.py en premier). Code prêt à lancer.")
    # Rappel renforce et repete pres de la demande (pas seulement dans le system prompt) : le
    # modele a un fort reflexe "Kivy/Buildozer" pour "application Android" malgre l'interdiction
    # dans CODER_SYSTEM (constate : a produit main.py+buildozer.spec au lieu de index.html au
    # premier essai). Repeter la contrainte juste avant la generation reduit ce biais.
    apk_reminder = (
        "\n\nRAPPEL CRITIQUE : ceci est une demande d'APPLICATION ANDROID. Produis UNIQUEMENT "
        "index.html, style.css, main.js (interface mobile complete). N'utilise JAMAIS Kivy, "
        "Buildozer ou Kotlin/Java — ils ne seront PAS compiles par ce pipeline."
        if wants_android_apk(prompt) else ""
    )
    enriched = (
        f"PLAN DU BRAIN:\n{plan}\n\n"
        f"DEMANDE: {prompt}{ctx_note}{conv_note}{mem_note}\n\n"
        f"Projet: C:\\Devllma\\workspace\\{project_name}\\\n"
        f"Produis chaque fichier au format strict:\n###FILE: nom.ext\n<code>\n###ENDFILE\n"
        f"{consigne}{apk_reminder}"
    )
    code_resp = await _gen_code(websocket, agent_name, enriched, coder_system_dyn, cancel_event,
                                via_colab=big_project)
    msg(sid, agent_name, "assistant", code_resp)
    if cancel_event.is_set():
        await websocket.send_json({"type":"done"}); return

    # Marquer todos au fur et à mesure
    for i, t in enumerate(todos):
        if i < len(todos)-1:
            todos[i]["done"] = True; todos[i]["active"] = False
            if i+1 < len(todos): todos[i+1]["active"] = True
            await websocket.send_json({"type":"todo_done","index":i})

    # ── Phase 4 : Écrire les fichiers (avec sauvegarde) ──────────────
    files = extract_files(code_resp)

    # Correction ciblee : demande APK mais pas d'index.html (constate : le modele a un reflexe
    # Kivy/Buildozer tres fort sur "application Android", au point d'ignorer a la fois la regle
    # CODER_SYSTEM et le rappel renforce dans le prompt). Plutot que d'echouer directement, ON
    # REDEMANDE UNE FOIS en pointant precisement ce qui a ete ecrit a la place — un rappel
    # ABSTRAIT ne suffit pas, mais citer les noms de fichiers concrets deja produits (preuve
    # qu'il a devie) est plus efficace pour corriger le cap.
    if wants_android_apk(prompt) and not any(fn.lower() == "index.html" for fn, _ in files):
        bad_names = ", ".join(fn for fn, _ in files[:6]) or "(aucun fichier)"
        apk_fix_p = (
            f"Tu as ecrit ces fichiers : {bad_names} — c'est INTERDIT pour cette demande "
            f"(pas de Kivy/Buildozer/Kotlin/Java, le pipeline ne peut PAS les compiler). "
            f"Reponds UNIQUEMENT avec index.html (+ style.css/main.js si besoin), interface "
            f"mobile complete pour : {prompt[:400]}\n"
            f"Format strict:\n###FILE: index.html\n<code>\n###ENDFILE"
        )
        await websocket.send_json({"type":"agent_start","agent":agent_name})
        apk_fix_resp = await stream_agent(websocket, agent_name, apk_fix_p, coder_system_dyn, cancel_event=cancel_event)
        apk_fixed = extract_files(apk_fix_resp)
        if any(fn.lower() == "index.html" for fn, _ in apk_fixed):
            files = apk_fixed
            code_resp = apk_fix_resp

    run_ok = None
    # Telemetrie du run (une ligne JSONL en fin de pipeline -> /pipeline_stats). Permet de
    # connaitre le taux de reussite reel EN PROD sans rejouer le banc de 60 tests.
    _telemetry = {"project": project_name, "editing": bool(editing),
                  "files_written": len(files), "big_project": bool(big_project),
                  "colab": bool(big_project and mem_get("colab_url")), "iterations": 0}
    if not files and code_resp and code_resp.strip():
        # Retry cible : le codeur a REPONDU (souvent une narration/mode d'emploi — constate a
        # plusieurs reprises sur des scripts PowerShell/Bash, ex: "J'ai cree le script...
        # Pour l'utiliser : chmod +x...") mais SANS AUCUN bloc ###FILE exploitable -> AUCUN
        # fichier ecrit, meme avec la regle anti-narration deja presente dans CODER_SYSTEM.
        # Meme remede que pour la derive Kivy/Buildozer (Android) et la derive de langage
        # (Go) : citer LA PREUVE concrete (le debut de sa propre reponse fautive) plutot qu'un
        # rappel abstrait suffit generalement a corriger le cap en un seul essai.
        narration_fix_p = (
            f"Ta reponse precedente etait UNIQUEMENT du texte explicatif (\"{code_resp.strip()[:150]}\"), "
            f"SANS AUCUN bloc ###FILE -> AUCUN fichier n'a ete ecrit. Ne decris JAMAIS ce que tu vas "
            f"faire ou comment l'utiliser : ta toute premiere ligne DOIT etre '###FILE: nom'. Reponds "
            f"MAINTENANT avec le code complet pour : {prompt[:400]}\n"
            f"Format strict:\n###FILE: nom.ext\n<code>\n###ENDFILE"
        )
        await websocket.send_json({"type":"agent_start","agent":agent_name})
        narration_fix_resp = await stream_agent(websocket, agent_name, narration_fix_p,
                                                 coder_system_dyn, cancel_event=cancel_event)
        narration_fixed = extract_files(narration_fix_resp)
        if narration_fixed:
            files = narration_fixed
            code_resp = narration_fix_resp
            _telemetry["files_written"] = len(files)
        else:
            # Constate (projet "api_04") : le codeur a repondu (souvent une reponse
            # tronquee/incomplete) mais AUCUN fichier n'a pu en etre extrait -> jusqu'ici
            # le pipeline se terminait en silence, sans que l'utilisateur sache que rien
            # n'a ete ecrit. On le rend visible explicitement.
            await websocket.send_json({
                "type": "token",
                "text": "\n⚠ Aucun fichier n'a pu être extrait de la réponse du codeur "
                        "(réponse probablement tronquée ou format inattendu). Rien n'a été écrit."
            })
    # Retry cible : stub/scaffold generique au lieu d'implementer la demande — constate a
    # PLUSIEURS reprises INDEPENDANTES sur Go, prompts completement differents et sous TROIS
    # FORMES DIFFERENTES : (1) un simple "Hello, World!" court (raccourcisseur d'URL par hash,
    # conversion binaire/decimal — 1er essai), (2) un squelette fichier+gestion d'erreur avec
    # un commentaire "// Your code here" (meme prompt binaire/decimal, apres correction
    # automatique), (3) un serveur HTTP complet dont le handler affiche juste "Hello, World!"
    # (verification d'un numero de carte par l'algorithme de Luhn — le modele a ecrit tout un
    # squelette de serveur web au lieu de l'algorithme demande). Le programme s'execute
    # proprement (exit 0, ou meme "serveur actif" pour le cas 3) donc ok=True, aucun signal
    # d'echec visible malgre une fonctionnalite jamais implementee. Restriction de longueur
    # <200 caracteres RETIREE pour "hello world" (initialement pensee pour eviter un faux
    # positif sur un programme complet mentionnant incidemment "hello world") : le cas 3 prouve
    # qu'un stub peut etre PADDE avec du code de scaffolding (imports/serveur/timeouts) sans
    # jamais implementer la logique demandee, restant NETTEMENT au-dessus de 200 caracteres.
    _HELLO_STUB_RE = re.compile(r'hello,?\s*world!?', re.IGNORECASE)
    _PLACEHOLDER_STUB_RE = re.compile(
        r'(?://|#)\s*(?:your|write your|add your|insert your|implement your)\s+(?:code|logic)\s*here|'
        r'(?://|#)\s*todo:?\s*implement', re.IGNORECASE)
    def _is_generic_stub(fs):
        if len(fs) != 1:
            return False
        code = fs[0][1]
        return bool(_PLACEHOLDER_STUB_RE.search(code) or _HELLO_STUB_RE.search(code))
    if _is_generic_stub(files):
        _stub_code = files[0][1]
        stub_fix_p = (
            f"Ta reponse precedente est un stub/squelette generique qui NE REPOND PAS a la demande "
            f"— tu as ecrit exactement : {_stub_code.strip()[:200]!r}. Implemente REELLEMENT la "
            f"demande suivante, sans placeholder ni commentaire \"TODO\"/\"your code here\" : "
            f"{prompt[:400]}\n"
            f"Format strict:\n###FILE: nom.ext\n<code>\n###ENDFILE"
        )
        await websocket.send_json({"type":"agent_start","agent":agent_name})
        stub_fix_resp = await stream_agent(websocket, agent_name, stub_fix_p, coder_system_dyn, cancel_event=cancel_event)
        stub_fixed = extract_files(stub_fix_resp)
        if stub_fixed and not _is_generic_stub(stub_fixed):
            files = stub_fixed
            code_resp = stub_fix_resp
            _telemetry["files_written"] = len(files)
    if files:
        # Snapshot AVANT d'écraser un projet existant (rollback possible)
        if os.path.isdir(project_dir):
            # executor : la copie de tout le projet peut prendre des secondes
            snap_id, snap_n = await asyncio.get_event_loop().run_in_executor(
                None, lambda: SnapshotManager.snapshot(project_dir, label="avant-modif"))
            if snap_id:
                await websocket.send_json({"type":"snapshot","id":snap_id,"files":snap_n})

        created = await asyncio.get_event_loop().run_in_executor(
            None, write_files, project_dir, files)
        for fname in created:
            fp = os.path.join(project_dir, fname)
            kb = round(os.path.getsize(fp)/1024,1) if os.path.exists(fp) else 0
            rel = (project_name + "/" + fname.replace(os.sep, "/"))
            await websocket.send_json({"type":"file_created","name":fname,"size":f"{kb} KB","path":rel})

        await websocket.send_json({
            "type":"project_done","count":len(created),
            "path":project_dir,"files":created,"project_name":project_name
        })

        # Indexer ce projet dans la mémoire sémantique pour les demandes futures
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: mem_index("project", project_name, f"{prompt}\n\n{plan[:600]}")
        )

        # ── Scan de sécurité du code généré ──────────────────────────
        file_map = {f: c for f, c in files}
        findings = security_scan(file_map)
        if findings:
            await websocket.send_json({"type":"security","findings":findings[:8]})

        # Mettre à jour la mémoire persistante du brain
        BrainMemory.append_event(f"Créé {project_name} ({len(created)} fichiers)")
        BRAIN_MEMORY["session_todos"].append(f"Créé {project_name} ({len(created)} fichiers)")
        # force=True : on vient d'ecrire des fichiers, le cache TTL doit etre contourne
        await asyncio.get_event_loop().run_in_executor(None, lambda: load_workspace_memory(force=True))

        # ── Pre-verification syntaxe (rapide) avant d'installer/executer ────
        # Evite de gaspiller un cycle complet d'install+execution sur du code qui ne compile meme pas.
        syntax_errs = await asyncio.get_event_loop().run_in_executor(None, syntax_check, project_dir)
        if syntax_errs and not cancel_event.is_set():
            await websocket.send_json({"type":"file_created","name":"⚠ Erreur de syntaxe — correction rapide","size":""})
            cur = await asyncio.get_event_loop().run_in_executor(None, read_project, project_dir)
            fix_p = ("ERREUR DE SYNTAXE (py_compile):\n" + "\n".join(syntax_errs) +
                     f"\n\nCODE ACTUEL:\n{format_context(cur)}\n\n"
                     f"Corrige la syntaxe. Format strict:\n###FILE: nom.ext\n<code>\n###ENDFILE")
            fix_resp = await stream_agent(websocket, agent_name, fix_p, coder_fix_system_dyn, cancel_event=cancel_event)
            fixed = extract_files(fix_resp)
            if fixed:
                await asyncio.get_event_loop().run_in_executor(None, write_files, project_dir, fixed)

        # ── Auto-correction STATIQUE (AST, zero appel LLM) avant d'executer ──
        # Detecte la classe de bug la plus couteuse constatee au banc de tests (collision
        # de nom local/import appele comme fonction -> RecursionError/TypeError garanti a
        # l'execution) SANS jamais lancer le code : economise un cycle complet
        # install+execute+diagnostic pour un probleme deja certain avant meme de tourner.
        static_probs = await asyncio.get_event_loop().run_in_executor(None, static_self_check, project_dir)
        if static_probs and not cancel_event.is_set():
            await websocket.send_json({"type":"file_created","name":"⚠ Collision de nom detectee — correction rapide","size":""})
            cur = await asyncio.get_event_loop().run_in_executor(None, read_project, project_dir)
            fix_p = ("PROBLEME POTENTIEL DETECTE PAR ANALYSE STATIQUE (avant meme execution) :\n" +
                     "\n".join(static_probs) +
                     f"\n\nCODE ACTUEL:\n{format_context(cur)}\n\n"
                     f"Si c'est reellement un bug (le nom local masque un import DISTINCT qui devrait "
                     f"etre appele a la place), corrige en renommant le nom local en collision. "
                     f"Si au contraire c'est une recursion INTENTIONNELLE sans rapport avec l'import "
                     f"(le nom ne fait que coincider), NE CHANGE RIEN a ce fichier et renvoie-le identique. "
                     f"Format strict:\n###FILE: nom.ext\n<code>\n###ENDFILE")
            fix_resp = await stream_agent(websocket, agent_name, fix_p, coder_fix_system_dyn, cancel_event=cancel_event)
            fixed = extract_files(fix_resp)
            if fixed:
                await asyncio.get_event_loop().run_in_executor(None, write_files, project_dir, fixed)

        # ── Lint FATAL (ruff famille F, sinon pyflakes) — zero appel LLM, sub-seconde ──
        # Attrape les hallucinations que py_compile et les checks AST maison ratent : nom
        # non defini (F821), imports casses, format strings invalides. Le smoke-test de 15s
        # n'atteint souvent jamais la ligne fautive -> le lint la voit sans executer.
        lint_fatal = await asyncio.get_event_loop().run_in_executor(None, lint_check, project_dir)
        if lint_fatal and not cancel_event.is_set():
            await websocket.send_json({"type":"file_created","name":f"⚠ Lint: {len(lint_fatal)} erreur(s) fatale(s) — correction rapide","size":""})
            cur = await asyncio.get_event_loop().run_in_executor(None, read_project, project_dir)
            fix_p = ("ERREURS DETECTEES PAR LE LINTER (ruff/pyflakes, avant execution) :\n" +
                     "\n".join(lint_fatal) +
                     f"\n\nCODE ACTUEL:\n{format_context(cur)}\n\n"
                     f"Corrige ces erreurs (souvent : nom/variable non defini, import manquant ou "
                     f"mal nomme). Format strict:\n###FILE: nom.ext\n<code>\n###ENDFILE")
            fix_resp = await stream_agent(websocket, agent_name, fix_p, coder_fix_system_dyn, cancel_event=cancel_event)
            fixed = extract_files(fix_resp)
            if fixed:
                await asyncio.get_event_loop().run_in_executor(None, write_files, project_dir, fixed)

        # ── Application Android (APK) : detecte via mots-cles dans le prompt ──
        # Contourne TOUT le flux Python (deps/execution/auto-correction) : le contenu
        # genere est du HTML/CSS/JS (meme convention que "site web statique" dans
        # CODER_SYSTEM), injecte dans le squelette Android WebView puis compile via
        # Gradle (cf. build_android_apk). Voir memoire "devllma-android-apk".
        if wants_android_apk(prompt):
            apk_ok, apk_msg, apk_path = await asyncio.get_event_loop().run_in_executor(
                None, build_android_apk, project_dir, files)
            run_ok, run_out, entry = apk_ok, apk_msg, (os.path.basename(apk_path) if apk_path else None)
            await websocket.send_json({"type":"run_result","ok":run_ok,"output":run_out,"entry":entry})
            if apk_ok:
                await websocket.send_json({"type":"file_created","name":os.path.basename(apk_path),"size":""})
            # Le commit git automatique (si run_ok) est gere plus bas, commun aux 2 branches.
        else:
            # Deduire requirements.txt des imports (le modele l'oublie souvent) AVANT l'install
            added_deps = await asyncio.get_event_loop().run_in_executor(None, derive_requirements, project_dir)
            if added_deps:
                await websocket.send_json({"type":"file_created",
                    "name":f"requirements.txt (+{len(added_deps)} dépendance(s) déduite(s): {', '.join(added_deps[:6])})","size":""})
            # Equivalent Node.js : deduire package.json des require()/import (meme raison : le
            # modele utilise souvent axios/node-fetch/etc. sans jamais creer package.json).
            added_node_deps = await asyncio.get_event_loop().run_in_executor(None, derive_node_requirements, project_dir)
            if added_node_deps:
                await websocket.send_json({"type":"file_created",
                    "name":f"package.json (+{len(added_node_deps)} dépendance(s) déduite(s): {', '.join(added_node_deps[:6])})","size":""})
            # Installer les dépendances
            ok_dep, _ = await asyncio.get_event_loop().run_in_executor(None, install_deps, project_dir)

            if cancel_event.is_set():
                await websocket.send_json({"type":"done"}); return

            # ── Garde-fou : bloquer le code destructeur AVANT exécution ──
            all_code = "\n".join(c for _, c in files)
            safe, danger_reasons = safety_check(all_code)
            if not safe:
                await websocket.send_json({
                    "type":"blocked",
                    "reasons":danger_reasons
                })
                run_ok, run_out, entry = None, "Exécution bloquée par sécurité", None
            else:
                # ── Phase 5 : Exécuter ────────────────────────────────────
                run_ok, run_out, entry = await asyncio.get_event_loop().run_in_executor(
                    None, execute_project, project_dir
                )

            if run_ok is not None:
                await websocket.send_json({"type":"run_result","ok":run_ok,"output":run_out,"entry":entry})

                # ── Phase 6 : Boucle de correction auto ───────────────────
                if not run_ok:
                    prev_err = None
                    stuck = 0
                    initial_error = run_out
                    last_fix_summary = ""
                    for iteration in range(1, 4):
                        if cancel_event.is_set():
                            break
                        _telemetry["iterations"] = iteration
                        await websocket.send_json({"type":"iter_start","n":iteration})

                        # Detection de blocage : si la MEME erreur persiste malgre la correction
                        # precedente, le modele tourne en rond -> on escalade (temperature plus
                        # haute + consigne explicite de changer d'approche) au lieu de reessayer
                        # a l'identique indefiniment (cf. logique deja presente dans dev_agent.py).
                        cur_files = await asyncio.get_event_loop().run_in_executor(None, read_project, project_dir)
                        clean_err = extract_error_line(run_out)
                        stuck = stuck + 1 if clean_err == prev_err else 0
                        prev_err = clean_err
                        escalate = stuck >= 1
                        fix_temp = 0.2 if not escalate else min(0.7, 0.3 + 0.15 * stuck)
                        escalate_note = (
                            "\nATTENTION: cette erreur PERSISTE malgre ta correction precedente. "
                            "Change d'APPROCHE sur la cause de l'erreur (corrige ou remplace la ligne "
                            "fautive par une autre methode) — NE RECOPIE PAS LE MEME CODE. "
                            "Conserve toute la logique/fonctionnalites qui marchent deja."
                        ) if escalate else ""

                        # Brain analyse UNIQUEMENT quand on est bloque (meme erreur qui persiste).
                        # Tant que l'erreur CHANGE (le modele progresse), on saute cette passe :
                        # brain et coder sont le MEME modele 30b -> l'analyse re-evalue tout le
                        # contexte (trace + code) que le coder re-evalue juste apres -> prompt-eval
                        # double pour rien sur ce CPU. On ne paie l'analyse que si on tourne en rond.
                        analysis = ""
                        if escalate:
                            err_ctx = (f"ERREUR PRECISE: {clean_err}\n\nTRACE COMPLETE:\n{run_out}\n\n"
                                       f"CODE:\n{format_context(cur_files)}\n\n"
                                       f"Identifie la cause racine EXACTE et dis exactement quoi corriger.{escalate_note}")
                            analysis = await asyncio.get_event_loop().run_in_executor(
                                None, call_brain, err_ctx, BRAIN_FIX_SYSTEM, 400
                            )
                            if cancel_event.is_set():
                                break
                            await websocket.send_json({"type":"agent_start","agent":"brain"})
                            await websocket.send_json({"type":"brain_think","text":analysis})

                        # ── Auto-debug web (rail Capacites) : quand on est BLOQUE sur la meme
                        # erreur et que la capacite est active, on recherche l'erreur sur le web
                        # (DuckDuckGo, gratuit) et on injecte des pistes dans le prompt de
                        # correction. Gate sur escalate uniquement -> aucun cout tant que le
                        # modele progresse tout seul.
                        web_note = ""
                        if escalate and cfg_on("autodebug"):
                            try:
                                q = ((clean_err or "")[:120] + " python").strip()
                                hits = await asyncio.get_event_loop().run_in_executor(
                                    None, lambda: _web_search(q, 3))
                                good = [h for h in (hits or []) if h.get("url") and h.get("snippet")]
                                if good:
                                    web_note = ("PISTES WEB (recherche sur l'erreur — inspire-toi de la "
                                                "cause, ne copie pas aveuglement) :\n"
                                                + "\n".join(f"- {(h.get('title') or '')[:80]} : "
                                                            f"{(h.get('snippet') or '')[:160]}" for h in good[:3])
                                                + "\n\n")
                                    await websocket.send_json({"type":"agent_start","agent":"researcher"})
                                    await websocket.send_json({"type":"brain_think",
                                        "text":"\U0001F50E Recherche web sur l'erreur : "+q+"\n"+web_note})
                            except Exception:
                                pass

                        # Agent corrige (n'injecte la section ANALYSE que si le brain a tourne)
                        analyse_note = f"ANALYSE:\n{analysis}\n\n" if analysis else ""
                        # Verrou anti-derive de langage — constate (Go) : sur une correction, le
                        # modele a parfois REECRIT le projet dans un AUTRE langage (Python) au lieu
                        # de corriger le code existant, meme avec le vrai code affiche dans CODE
                        # ACTUEL (meme famille de bug que la derive Kivy/Buildozer sur les demandes
                        # Android). Rappel explicite + nom de fichier attendu = meme remede efficace.
                        _lang_lock = ""
                        if entry:
                            _eext = os.path.splitext(entry)[1].lower()
                            _lang_names = {".go": "Go", ".js": "JavaScript/Node.js", ".mjs": "JavaScript/Node.js",
                                           ".cjs": "JavaScript/Node.js", ".ps1": "PowerShell", ".sh": "Bash"}
                            if _eext in _lang_names:
                                _ln = _lang_names[_eext]
                                _lang_lock = (
                                    f"RAPPEL CRITIQUE : ce projet est en {_ln} (point d'entree '{entry}'). "
                                    f"CORRIGE LE CODE {_ln.upper()} EXISTANT ci-dessous, ne bascule JAMAIS "
                                    f"vers un autre langage (ex: Python) meme si ca semble plus simple — "
                                    f"changer de langage n'est PAS une correction. Le point d'entree DOIT "
                                    f"rester exactement '{entry}'.\n\n"
                                )
                        fix_p = (f"{_lang_lock}ERREUR PRECISE A CORRIGER: {clean_err}{escalate_note}\n\n"
                                 f"{web_note}{analyse_note}TRACE COMPLETE:\n{run_out}\n\n"
                                 f"CODE ACTUEL:\n{format_context(cur_files)}\n\n"
                                 f"Corrige. Format strict:\n###FILE: nom.ext\n<code>\n###ENDFILE")
                        # Bloqué (escalate) OU deja en mode Colab -> on delegue la correction au GPU.
                        fix_resp = await _gen_code(websocket, agent_name, fix_p, coder_fix_system_dyn,
                                                   cancel_event, temperature=fix_temp,
                                                   via_colab=(escalate or big_project))
                        if cancel_event.is_set():
                            break

                        fixed = extract_files(fix_resp)
                        # Garde-fou anti-regression : rejeter une "correction" qui vide un fichier
                        # qui avait deja du contenu substantiel (sinon une correction ratee peut
                        # ecraser un fichier fonctionnel par un fichier quasi-vide).
                        # Deuxieme garde-fou (contenu, pas seulement longueur) : constate sur le
                        # MEME bug de stub que plus haut (Hello World/placeholder) — le premier
                        # essai avait REELLEMENT implemente la demande (Luhn, imports strconv/
                        # strings) mais avec un bug de compilation mineur (imports inutilises) ;
                        # la boucle de correction NORMALE a "corrige" cette erreur en supprimant
                        # toute la logique reelle, regressant vers un simple stub "Hello, World!"
                        # -> le garde-fou de LONGUEUR seul ne suffit pas a detecter cette regression
                        # de CONTENU (le nouveau fichier peut rester au-dessus du seuil de taille
                        # tout en devenant un stub). On rejette aussi une correction qui introduit
                        # un motif de stub la ou l'ancien contenu n'en avait pas.
                        applied = []
                        for fname, code in fixed:
                            old = cur_files.get(fname, "")
                            if old and len(old) > 200 and len(code) < 0.4 * len(old):
                                continue
                            # Pas de seuil "len(old) > 200" ici (contrairement au garde-fou de
                            # longueur ci-dessus) : un essai COURT mais REEL (ex: une fonction Luhn
                            # compacte de 100 caracteres) merite la meme protection qu'un long — la
                            # regression stub-vs-reel importe independamment de la taille absolue.
                            old_is_stub = bool(_PLACEHOLDER_STUB_RE.search(old) or _HELLO_STUB_RE.search(old))
                            new_is_stub = bool(_PLACEHOLDER_STUB_RE.search(code) or _HELLO_STUB_RE.search(code))
                            if old and new_is_stub and not old_is_stub:
                                continue
                            applied.append((fname, code))
                        if applied:
                            await asyncio.get_event_loop().run_in_executor(None, write_files, project_dir, applied)
                            last_fix_summary = "\n".join(f"### {fname}\n{code[:600]}" for fname, code in applied)

                        # Re-validation STATIQUE avant de relancer (bien moins cher qu'un cycle
                        # install+execute) : une "correction" peut reintroduire une collision de
                        # nom, casser un import, ou laisser un nom non defini. Si un defaut statique
                        # subsiste, on saute l'execution et on renvoie ces messages a l'iteration
                        # suivante pour reparer directement — sans gaspiller un run complet.
                        stat2 = await asyncio.get_event_loop().run_in_executor(None, syntax_check, project_dir)
                        if not stat2:
                            stat2 = await asyncio.get_event_loop().run_in_executor(None, static_self_check, project_dir)
                        if not stat2:
                            stat2 = await asyncio.get_event_loop().run_in_executor(None, lint_check, project_dir)
                        if stat2:
                            run_ok, run_out = False, ("Defaut statique persistant apres correction :\n" + "\n".join(stat2[:8]))
                            entry = None
                            await websocket.send_json({"type":"run_result","ok":run_ok,"output":run_out,"entry":entry})
                            continue  # compte comme une iteration : la boucle ne peut pas tourner a l'infini

                        run_ok, run_out, entry = await asyncio.get_event_loop().run_in_executor(
                            None, execute_project, project_dir
                        )
                        await websocket.send_json({"type":"run_result","ok":run_ok,"output":run_out,"entry":entry})
                        if run_ok: break

                    # Auto-apprentissage (fire-and-forget, jamais awaite : ne doit pas retarder
                    # la reponse). Distille ce cycle echec+correction en une regle generale et
                    # l'indexe pour les PROCHAINS projets (cf. record_lesson plus haut).
                    asyncio.get_event_loop().run_in_executor(
                        None, _record_lesson_tracked, initial_error, last_fix_summary, run_ok, run_out)

        # Commit git automatique d'une version qui marche (point de restauration)
        if run_ok:
            await asyncio.get_event_loop().run_in_executor(
                None, Skills.git_commit, project_dir, f"DevLLMA: {project_name} OK"
            )

        # ── Passe de verification + AUTO-CORRECTION ciblee (rail Capacites) ──
        # L'execution prouve que le code TOURNE, pas qu'il REPOND a la demande. Un relecteur
        # compare code vs demande ; s'il trouve des ecarts CONCRETS et que le code tourne, on
        # lance UNE passe corrective ciblee, PROTEGEE PAR SNAPSHOT : si la correction regresse
        # (ne tourne plus), on restaure la version qui marchait -> jamais de regression.
        if cfg_on("verify", True):
            try:
                _verify_sys = ("Tu es un relecteur de code exigeant. On te donne une demande et le "
                               "code produit. Dis si le code REPOND vraiment a la demande. Reponds "
                               "exactement 'CONFORME' si tout y est ; sinon liste au maximum 3 ecarts "
                               "CONCRETS (fonctionnalite manquante ou bug logique), une ligne chacun. "
                               "Sois bref et factuel, pas de bla-bla.")
                cur_files = await asyncio.get_event_loop().run_in_executor(None, read_project, project_dir)
                verify_ctx = (f"DEMANDE:\n{prompt[:800]}\n\nCODE PRODUIT:\n{format_context(cur_files)}")
                verdict = await asyncio.get_event_loop().run_in_executor(
                    None, call_brain, verify_ctx, _verify_sys, 300)
                verdict = (verdict or "").strip()
                if verdict and not verdict.startswith("("):  # "(...indisponible...)" = panne Ollama
                    conforme = verdict.upper().strip(" .\"'") == "CONFORME"
                    await websocket.send_json({"type":"agent_start","agent":"reviewer"})
                    if conforme:
                        await websocket.send_json({"type":"brain_think",
                            "text":"✅ Vérification : le code répond à la demande."})
                    else:
                        await websocket.send_json({"type":"brain_think",
                            "text":"\U0001F50D Vérification — écarts détectés :\n" + verdict})
                        if run_ok and not cancel_event.is_set():
                            snap = await asyncio.get_event_loop().run_in_executor(
                                None, lambda: SnapshotManager.snapshot(project_dir, label="avant-verif-fix"))
                            vfix_p = (f"La DEMANDE etait :\n{prompt[:800]}\n\nEcarts releves par le relecteur "
                                      f"a corriger :\n{verdict}\n\nCODE ACTUEL :\n{format_context(cur_files)}\n\n"
                                      f"Corrige UNIQUEMENT ces ecarts sans casser ce qui marche deja. "
                                      f"Format strict :\n###FILE: nom.ext\n<code>\n###ENDFILE")
                            vfix_resp = await _gen_code(websocket, agent_name, vfix_p, coder_fix_system_dyn,
                                                        cancel_event, temperature=0.2, via_colab=big_project)
                            vfixed = extract_files(vfix_resp)
                            vapplied = []
                            for fn, code in vfixed:
                                old = cur_files.get(fn, "")
                                if old and len(old) > 200 and len(code) < 0.4 * len(old):
                                    continue
                                vapplied.append((fn, code))
                            if vapplied:
                                await asyncio.get_event_loop().run_in_executor(None, write_files, project_dir, vapplied)
                                vstat = await asyncio.get_event_loop().run_in_executor(None, syntax_check, project_dir)
                                r2ok = False
                                if not vstat:
                                    r2ok, r2out, r2entry = await asyncio.get_event_loop().run_in_executor(
                                        None, execute_project, project_dir)
                                if r2ok:
                                    run_ok, run_out = True, r2out
                                    await websocket.send_json({"type":"run_result","ok":True,"output":r2out,"entry":r2entry})
                                    await websocket.send_json({"type":"brain_think",
                                        "text":"✅ Écarts corrigés — le projet répond maintenant à la demande."})
                                    await asyncio.get_event_loop().run_in_executor(
                                        None, Skills.git_commit, project_dir, f"DevLLMA: {project_name} verif OK")
                                else:
                                    sid = snap[0] if snap else None
                                    if sid:
                                        await asyncio.get_event_loop().run_in_executor(
                                            None, lambda: SnapshotManager.restore(sid, project_dir))
                                    await websocket.send_json({"type":"brain_think",
                                        "text":"⚠️ Correction des écarts non concluante — version fonctionnelle précédente restaurée."})
            except Exception:
                pass

        # ── Tests auto-generes (rail Capacites) — garde-fou supplementaire a l'execution.
        # execute_project prouve seulement que le point d'entree ne plante pas AU LANCEMENT ;
        # des fonctions annexes jamais appelees par ce chemin peuvent rester cassees sans que
        # rien ne le detecte. Scope Python uniquement (pytest) : generaliser a JS/PowerShell/Bash
        # demanderait un framework de test par langage, hors scope pour l'instant. Informatif —
        # un echec de test n'annule jamais le run_ok deja obtenu (pas de garde-fou bloquant ici).
        if cfg_on("tests") and run_ok and entry and entry.endswith(".py") and not cancel_event.is_set():
            try:
                cur_files = await asyncio.get_event_loop().run_in_executor(None, read_project, project_dir)
                _test_sys = ("Tu es un ingenieur QA Python. On te donne un projet Python fonctionnel. "
                             "Ecris UN SEUL fichier de tests pytest qui teste les fonctions/logiques "
                             "CLES du projet (pas de test bidon, pas de test qui depend d'un reseau "
                             "ou d'un serveur externe). Format strict :\n"
                             "###FILE: tests/test_main.py\n<code>\n###ENDFILE")
                test_ctx = f"DEMANDE INITIALE: {prompt[:500]}\n\nCODE DU PROJET:\n{format_context(cur_files)}"
                test_resp = await _gen_code(websocket, agent_name, test_ctx, _test_sys,
                                            cancel_event, temperature=0.2)
                test_files = [(fn, code) for fn, code in extract_files(test_resp)
                              if fn.endswith(".py") and "test" in fn.lower()]
                if test_files and not cancel_event.is_set():
                    await asyncio.get_event_loop().run_in_executor(None, write_files, project_dir, test_files)
                    for fn, _ in test_files:
                        await websocket.send_json({"type":"file_created","name":fn,"size":""})
                    r = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: subprocess.run(
                            [PYTHON, "-m", "pytest", "-q", "--tb=line"], cwd=project_dir,
                            capture_output=True, text=True, timeout=60,
                            encoding="utf-8", errors="replace"))
                    tests_out = (r.stdout + r.stderr).strip()[:600]
                    tests_passed = r.returncode == 0
                    await websocket.send_json({"type":"agent_start","agent":"tester"})
                    await websocket.send_json({"type":"brain_think",
                        "text": ("🧪 Tests générés et exécutés — "
                                 + ("tous passent ✅" if tests_passed else "certains échouent ⚠️")
                                 + f"\n{tests_out}")})
            except Exception:
                pass

    # Marquer dernier todo done
    for i,t in enumerate(todos):
        todos[i]["done"]=True; todos[i]["active"]=False
    if todos: await websocket.send_json({"type":"todos","items":todos})

    # Telemetrie du run (append JSONL, best-effort en executor : ne doit jamais casser la reponse)
    _telemetry["run_ok"] = bool(run_ok)
    await asyncio.get_event_loop().run_in_executor(None, lambda: _log_pipeline_run(_telemetry))

    # Envoyer liste projets mise à jour (force=True : fichiers peut-etre ecrits juste avant)
    await asyncio.get_event_loop().run_in_executor(None, lambda: load_workspace_memory(force=True))
    await websocket.send_json({"type":"projects","items":[p["name"] for p in BRAIN_MEMORY["projects"]]})
    await websocket.send_json({"type":"done"})

    # Le pipeline (appels brain+coder avec d'autres prompts) vient d'evincer le cache KV
    # du prefixe de l'agent generaliste : on le reprechauffe en arriere-plan pour que la
    # prochaine demande a l'agent reparte a chaud (~4s) et non a froid (~min sur ce CPU).
    from agent_core import warm_agent_cache
    threading.Thread(target=warm_agent_cache, daemon=True).start()

@app.websocket("/ws")
async def ws_handler(websocket: WebSocket):
    await websocket.accept()
    global _MAIN_LOOP
    if _MAIN_LOOP is None:                 # capture la boucle pour les notifs venues d'un thread
        _MAIN_LOOP = asyncio.get_running_loop()
    _WS_CLIENTS.add(websocket)
    # Pousse l'etat Colab connu tout de suite -> l'UI affiche le bon indicateur des
    # l'ouverture, sans attendre la prochaine transition detectee par _colab_keepalive.
    try:
        await websocket.send_json({"type": "colab_status",
                                   "configured": _COLAB_STATE["configured"], "up": _COLAB_STATE["up"]})
    except Exception:
        pass
    sid_box = {"sid": new_session("Web Session")}   # une session par connexion
    queue = asyncio.Queue()
    cancel_event = asyncio.Event()
    busy = {"v": False}
    last_prompt = {"v": None}

    async def worker():
        while True:
            prompt = await queue.get()
            busy["v"] = True
            _ACTIVE_GEN["n"] += 1
            cancel_event.clear()
            try:
                await handle_prompt(websocket, sid_box, prompt, cancel_event)
            except Exception as e:
                try:
                    await websocket.send_json({"type":"token","text":f"\nErreur: {e}"})
                    await websocket.send_json({"type":"done"})
                except Exception:
                    pass
            finally:
                _ACTIVE_GEN["n"] = max(0, _ACTIVE_GEN["n"] - 1)
            busy["v"] = False
            queue.task_done()

    worker_task = asyncio.create_task(worker())
    try:
        while True:
            data = await websocket.receive_json()

            # Annulation de la génération en cours (bouton stop)
            if data["type"] == "stop":
                if busy["v"]:
                    cancel_event.set()
                else:
                    await websocket.send_json({"type":"stopped"})
                continue

            # Init : reprendre une session existante (rejeu historique) ou garder la nouvelle
            if data["type"] == "init":
                want = data.get("session")
                if want:
                    try:
                        sid_box["sid"] = int(want)
                        rows = history(sid_box["sid"], 60)  # (agent, role, content) du plus ancien au plus récent
                        msgs = [{"agent": a, "role": r, "content": c} for a, r, c in rows]
                        await websocket.send_json({"type":"session_history","sid":sid_box["sid"],"messages":msgs})
                    except Exception:
                        await websocket.send_json({"type":"session_set","sid":sid_box["sid"]})
                else:
                    await websocket.send_json({"type":"session_set","sid":sid_box["sid"]})
                continue

            # Envoyer liste projets
            if data["type"] == "get_projects":
                # executor : sur cache TTL froid le scan disque gelerait la boucle
                await asyncio.get_event_loop().run_in_executor(None, load_workspace_memory)
                names = [p["name"] for p in BRAIN_MEMORY["projects"]]
                await websocket.send_json({"type":"projects","items":names})
                continue

            # Changer de modèle (nom complet, ex: deepseek-coder-v2:16b)
            if data["type"] == "model":
                model = data.get("model") or ("qwen2.5-coder:" + data.get("size","7b"))
                # Le brain/researcher garde un modele dedie a la conversation (moins rigide) —
                # seuls les agents codeurs suivent le selecteur.
                for a in AGENTS:
                    if a not in ("brain", "researcher"):
                        AGENTS[a]["model"] = model
                # Précharger le nouveau modèle en arrière-plan (run_in_executor : un
                # requests.post direct ici gelait toutes les WebSocket actives pendant l'appel).
                def _preload_one(m):
                    try:
                        # options=_opts() OBLIGATOIRE : sans ca Ollama charge le modele a son
                        # defaut (num_ctx=4096) et keep_alive=-1 le FIGE la -> ca jetait le cache
                        # KV prechauffe a 32k et forçait un rechargement a froid (~min) au prochain
                        # appel reel (defaut identifie en revue de code). Memes options que partout.
                        _http.post(OLLAMA+"/api/generate",
                                      json={"model":m,"prompt":"","keep_alive":KEEP_ALIVE,
                                            "options":_opts()}, timeout=5)
                    except Exception: pass
                asyncio.get_event_loop().run_in_executor(None, _preload_one, model)
                await websocket.send_json({"type":"agent_start","agent":"brain"})
                await websocket.send_json({"type":"token","text":"Modèle actif : " + model})
                await websocket.send_json({"type":"done"})
                continue

            # Nouvelle session : repart à zéro (nouvelle entrée en base + chat vidé)
            if data["type"] == "new_session":
                sid_box["sid"] = new_session("Web Session")
                await websocket.send_json({"type":"session_new","sid":sid_box["sid"]})
                continue

            # Message normal -> mis en file, traité par le worker (permet d'envoyer "stop" pendant le traitement)
            if data["type"] == "message":
                last_prompt["v"] = data["text"]
                await queue.put(data["text"])
                continue

            # Regenerer la derniere reponse (renvoie le dernier prompt utilisateur)
            if data["type"] == "regenerate":
                if last_prompt["v"]:
                    await queue.put(last_prompt["v"])
                continue

            # Telecharger un nouveau modele Ollama (panneau Modeles) — independant du flux de chat
            if data["type"] == "pull_model":
                model_name = (data.get("model") or "").strip()
                if model_name:
                    asyncio.create_task(handle_pull_model(websocket, model_name))
                continue

    except WebSocketDisconnect:
        # Le cancel() asyncio n'atteint pas le thread executor de run_agent_sync :
        # sans set(), sa boucle ReAct (appels Ollama de plusieurs minutes + outils
        # a effets de bord) continue en zombie apres fermeture de l'onglet et
        # bloque l'unique slot de generation CPU pour la session suivante.
        cancel_event.set()
        worker_task.cancel()
    finally:
        _WS_CLIENTS.discard(websocket)  # ne plus lui diffuser de notifications

def _startup_warmup():
    """Precharge le modele PUIS le cache KV du prefixe de l'agent (systeme+outils).
    Sans ce prechauffage, la premiere demande apres un redemarrage paie ~6 min
    d'evaluation de prompt a froid sur ce CPU (mesure) et semble en panne."""
    check_ollama_ready()
    preload_models()
    from agent_core import warm_agent_cache
    warm_agent_cache()

def _self_watchdog():
    """Auto-surveillance INTERNE : constate (05/07/2026) qu'un process peut rester
    vivant des heures sans jamais repondre (bloque avant l'ouverture du port),
    sans que Task Scheduler ne le detecte (il n'a pas "plante"). Une tache Windows
    EXTERNE separee chargee de detecter/relancer ca a ete supprimee par l'antivirus
    quelques secondes apres sa creation (signature de persistance : SYSTEM,
    recurrente, pouvoir de tuer des process). Solution : le process se surveille
    LUI-MEME et se termine (os._exit) s'il ne repond plus a une requete HTTP locale
    pendant plusieurs minutes — le redemarrage automatique deja configure sur la
    tache planifiee (RestartCount=3, RestartInterval=1 min) reprend alors la main.
    Ne tue jamais rien d'autre que soi-meme : ne devrait pas etre vu comme un outil
    de persistance/attaque.

    CORRECTIF (14/07/2026, banc de 60 tests Colab) : ce watchdog confondait "CPU
    sature par une generation locale longue" (event loop asyncio qui met plusieurs
    secondes a repondre a une requete HTTP concurrente, sur ce poste 100% CPU, sans
    GPU) et "serveur mort". Un test qui a bascule en fallback local pendant ~3 min a
    coincide exactement avec un redemarrage force du process en plein milieu de sa
    generation. On consulte maintenant _ACTIVE_GEN : si une generation est en cours,
    un echec HTTP n'est PAS compte comme un vrai echec (juste "lent, pas mort") et on
    utilise un timeout de requete plus genereux."""
    time.sleep(60)  # laisse le temps au demarrage normal (bind du port quasi instantane,
                     # mais on evite tout faux positif pendant les tout premiers instants)
    fails = 0
    while True:
        gen_busy = _ACTIVE_GEN["n"] > 0
        try:
            r = requests.get("http://127.0.0.1:8080/", timeout=(30 if gen_busy else 10))
            fails = 0 if r.status_code == 200 else fails + 1
        except Exception:
            if gen_busy:
                print("[AUTO-SURVEILLANCE] serveur lent a repondre pendant une generation "
                      "active (CPU sature) -> pas compte comme un echec", flush=True)
            else:
                fails += 1
        if fails:
            print(f"[AUTO-SURVEILLANCE] serveur local injoignable ({fails}/3)", flush=True)
        if fails >= 3:
            print("[AUTO-SURVEILLANCE] injoignable depuis ~4-5 min -> arret force "
                  "pour declencher le redemarrage automatique de la tache planifiee", flush=True)
            os._exit(1)
        time.sleep(90)

def _colab_keepalive():
    """Garde le worker Colab chaud et SURVEILLE son etat, tant qu'une URL est
    configuree (mem_get('colab_url')).

    Ce que ca fait / ne fait PAS :
      - Ping /toutes les ~3 min -> garde le tunnel ngrok et le serveur uvicorn
        reactifs (pas de reveil a froid sur la 1re vraie generation), et donne a
        DevLLMA une connaissance LIVE de l'etat du worker (up/down).
      - Ca ne remet PAS a zero le minuteur d'inactivite de Colab (celui-ci est cote
        FRONTEND) : le vrai anti-deconnexion ~90 min est la cellule heartbeat au
        premier plan + l'auto-clic navigateur (cf. Colab-Worker.md). Ici on se
        contente de tracer les transitions pour que l'utilisateur sache quand
        re-executer le notebook -- le repli local automatique (_gen_code) prend le
        relais entre-temps.

    Purement sortant vers une URL que l'utilisateur a lui-meme configuree ; ne tue
    et ne redemarre rien."""
    headers = {"ngrok-skip-browser-warning": "1"}
    last = None  # dernier etat diffuse : None (jamais) / (configured, up)
    time.sleep(45)
    while True:
        base = mem_get("colab_url")
        if not base:
            state = (False, None)          # worker non configure : rien a surveiller
        else:
            try:
                r = _http.get(base + "/", headers=headers, timeout=(5, 10))
                state = (True, r.status_code == 200)
            except Exception:
                state = (True, False)
        if state != last:
            configured, up = state
            if configured and up:
                print("[COLAB] worker JOIGNABLE (tunnel chaud, GPU dispo)", flush=True)
            elif configured and last is not None:
                print("[COLAB] worker INJOIGNABLE -> repli local actif. "
                      "Re-execute le notebook Colab (Executer tout) pour le relancer.", flush=True)
            _COLAB_STATE["configured"], _COLAB_STATE["up"] = configured, up
            _broadcast_threadsafe({"type": "colab_status", "configured": configured, "up": up})
            last = state
        # 60s (au lieu de 180) : garde le tunnel ngrok / uvicorn plus chauds et detecte une
        # coupure plus vite (bandeau UI + repli local a jour). Le vrai anti-idle reste la
        # cellule keep-alive auto-reparante cote notebook (Colab-Cellule-Heartbeat.py).
        time.sleep(60)

if __name__ == "__main__":
    import threading
    threading.Thread(target=_startup_warmup, daemon=True).start()
    threading.Thread(target=_self_watchdog, daemon=True).start()
    threading.Thread(target=_colab_keepalive, daemon=True).start()
    # host="0.0.0.0" est INTENTIONNEL, pas un oubli : c'est ce qui permet l'acces
    # LAN (192.168.1.30) et Tailscale (100.112.22.79, PWA mobile) mis en place
    # deliberement. Le repasser en 127.0.0.1 casserait l'acces telephone/tablette.
    # La protection contre les requetes cross-origin malveillantes passe par le
    # CORS restreint (_PRIVATE_ORIGIN_RE ci-dessus) et le pare-feu Windows, pas
    # par le binding — ne pas "corriger" ceci sans re-craquer l'acces distant.
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")
