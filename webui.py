import sys, os, json, requests, re, subprocess, asyncio, time, threading, queue as _queue
sys.path.insert(0, r"C:\Devllma")
os.chdir(r"C:\Devllma")

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, Response, PlainTextResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from db import init, new_session, msg, history, list_sessions, mem_search, mem_index, mem_get, mem_set, stats as db_stats, delete_session, mem_list, mem_delete, mem_purge
from agents import AGENTS, route, OLLAMA, has_dev_keywords, is_research_question, GREETINGS, _kw_match, strip_accents, is_trivial_snippet
from skills import SnapshotManager, Skills, safety_check, security_scan, BrainMemory, extract_files as extract_files_robust
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
from agent_core import run_agent_sync
from ollama_client import (
    KEEP_ALIVE, BRAIN_MODEL, call_brain, preload_models, stream_agent,
    _fetch_ollama_tags, handle_pull_model, check_ollama_ready, _http,
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

CODER_SYSTEM = """Tu es CODER, expert Python/JS/web. Tu CODES directement sans questions.

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
- Site web statique (HTML/CSS/JS, pas de backend demandé) -> produis DIRECTEMENT index.html, style.css, main.js etc.
  EN CLAIR (pas de balises ``` a l'interieur du bloc ###FILE). N'ECRIS JAMAIS de script Python qui genere/ecrit
  ces fichiers a la place — les fichiers eux-memes sont le livrable.
- Projet Python -> produis TOUS les fichiers, main.py (point d'entrée) en PREMIER
- RÈGLE CRITIQUE: tout module Python importé (ex: import downloader) DOIT avoir son fichier créé (downloader.py)
- requirements.txt = UNIQUEMENT les packages pip externes (ex: requests). JAMAIS la stdlib (os, sys, time, threading, argparse, json, re, queue...)
- Respecte les contraintes: si "en parallèle" est demandé, utilise threading.Thread (start puis join), pas une boucle séquentielle
- Code complet, jamais tronqué. Aucun texte ni ``` hors des blocs ###FILE
- Ne JAMAIS dire "je vais faire..." — produis les blocs directement
- Reste concis dans le HTML/CSS (pas de commentaires superflus, pas de contenu redondant) pour ne pas depasser la limite de sortie
- Serveur web (FastAPI/Flask/etc.) -> le fichier principal DOIT se terminer par un bloc
  if __name__ == "__main__": qui lance reellement le serveur (ex: uvicorn.run(app, host="0.0.0.0", port=8000)).
  SANS ce bloc, "python main.py" ne demarre rien et se termine immediatement sans erreur.
- SQLite utilise avec FastAPI -> sqlite3.connect(..., check_same_thread=False), sinon erreur
  "SQLite objects created in a thread can only be used in that same thread" des la premiere requete.
- Toute donnee saisie par l'utilisateur affichee dans du HTML genere en f-string DOIT etre
  echappee avec html.escape() (ou utiliser Jinja2 dont l'auto-echappement est natif) pour eviter le XSS.
- Projet multi-fichiers reparti dans un sous-dossier (ex: app/main.py, app/database.py) -> les
  imports entre ces fichiers DOIVENT etre en imports simples SANS prefixe de package et SANS point
  (ex: "from database import get_db", PAS "from app.database import get_db" ni "from .database import get_db").
  Le point d'entree est execute directement (python app/main.py), donc "app" n'est PAS un package
  importable — seul l'import simple par nom de fichier fonctionne."""

CODER_FIX_SYSTEM = """Tu es CODER. Tu corriges du code en erreur.
Réécris UNIQUEMENT les fichiers à corriger, format strict:
###FILE: nom.ext
<code corrigé complet>
###ENDFILE
Pas de texte hors des blocs."""

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

def write_files(project_dir, files):
    os.makedirs(project_dir, exist_ok=True)
    created = []
    for fname, code in files:
        fpath = os.path.join(project_dir, fname.replace("/", os.sep))
        os.makedirs(os.path.dirname(fpath), exist_ok=True)
        with open(fpath, "w", encoding="utf-8") as f: f.write(code)
        created.append(fname)
    return created

def install_deps(project_dir):
    req = os.path.join(project_dir, "requirements.txt")
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
    sep = "" if (not existing_text or existing_text.endswith("\n")) else "\n"
    try:
        with open(req_path, "a", encoding="utf-8") as f:
            f.write(sep + "\n".join(missing) + "\n")
    except Exception:
        return []
    return missing

bootstrap_memory()

# ─── Exécution de code ────────────────────────────────────────────────────────
ENTRY_POINTS = ["main.py","app.py","run.py","server.py","start.py","index.py"]

def find_entry_point(project_dir):
    """Cherche un point d'entree a la racine, PUIS dans un sous-dossier direct
    (layout package Python du type monpaquet/main.py). Sans ce fallback, un projet
    structuré en package n'est jamais exécuté ni testé (faux "succès" silencieux)."""
    for ep in ENTRY_POINTS:
        if os.path.exists(os.path.join(project_dir, ep)):
            return ep
    ignore = {"__pycache__", ".git", "node_modules", ".venv", "build", "dist"}
    for entry in sorted(os.listdir(project_dir)):
        sub = os.path.join(project_dir, entry)
        if os.path.isdir(sub) and entry not in ignore:
            for ep in ENTRY_POINTS:
                if os.path.exists(os.path.join(sub, ep)):
                    return os.path.join(entry, ep)
    # Dernier recours : un script unique nomme autrement (ex: calculatrice.py seul
    # a la racine). Sans ce fallback il n'est jamais execute ni teste — le pipeline
    # declarait "termine" sans run_result (constate au banc de tests).
    root_py = [f for f in sorted(os.listdir(project_dir))
               if f.endswith(".py") and os.path.isfile(os.path.join(project_dir, f))]
    if len(root_py) == 1:
        return root_py[0]
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
                   "runserver", "waitress", "gunicorn", "websockets.serve")

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
            if not n.endswith(".py"):
                continue
            try:
                content = open(os.path.join(root, n), encoding="utf-8", errors="replace").read()
            except Exception:
                continue
            low = content.lower()
            for mk in SERVER_MARKERS:
                if mk in low:
                    is_server = True; frameworks.add(mk)
            for m in re.findall(r'port\s*=\s*(\d{2,5})', content):
                p = int(m)
                if p != 8080 and p not in ports:  # 8080 = PROD, jamais un projet genere
                    ports.append(p)
    # Defauts par framework (le port explicite prime, mais sert de repli s'il manque)
    if frameworks & {"flask", "app.run("} and 5000 not in ports:
        ports.append(5000)
    if frameworks & {"uvicorn", "http.server", "runserver", "waitress", "gunicorn"} and 8000 not in ports:
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
    qu'une vraie appli repond, pas juste qu'un port TCP est ouvert par autre chose."""
    try:
        r = requests.get(f"http://127.0.0.1:{port}/", timeout=3)
        return True, r.status_code
    except Exception as e:
        return False, str(e)

def execute_project(project_dir, timeout=15):
    ep = find_entry_point(project_dir)
    if not ep:
        return None, "Aucun fichier principal trouvé (main.py, app.py…)", None
    fpath = os.path.join(project_dir, ep)
    is_server, server_ports = _detect_server_port(project_dir)
    proc = None
    try:
        proc = subprocess.Popen([PYTHON, fpath], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, cwd=project_dir, encoding="utf-8", errors="replace")
        try:
            out, _ = proc.communicate(timeout=timeout)
            combined = (out or "").strip()[:800]
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
    return "_".join(text.split()[:4]) or "projet"

def is_edit(prompt):
    low = strip_accents(prompt.lower())
    return any(_kw_match(k, low) for k in ["modifie","modifier","corrige","corriger","ajoute","ajouter",
                                             "change","ameliore","refactore","fixe","fix",
                                             "mets a jour","met a jour","reprend","reprends","continue",
                                             "continuer","complete","termine","terminer"])

_FILE_OR_DOC_RE = re.compile(
    r'\b(fichier|dossier|document|note|classeur|feuille excel|tableau excel|fichier word|fichier pdf|word|excel|pdf)\b')
_STRONG_DEV_RE = re.compile(
    r'\b(api|application|app|site|projet|programme|logiciel|jeu|bot|fonction|classe|serveur|'
    r'backend|frontend|base de donnees|script python|interface|dashboard|tableau de bord)\b')

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
def is_heavy_media_request(prompt):
    return bool(_HEAVY_MEDIA_RE.search(strip_accents(prompt.lower())))

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

def _colab_llm(system, prompt, timeout=600):
    """Generation de code par le LLM GPU du worker Colab (/llm). Texte, ou None si indispo."""
    base = mem_get("colab_url")
    if not base:
        return None
    headers = {"Content-Type": "application/json"}
    tok = mem_get("colab_token")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    try:
        r = _http.post(base + "/llm", headers=headers,
                       json={"system": system or "", "prompt": prompt}, timeout=timeout)
        if r.status_code != 200:
            return None
        return (r.json().get("response") or "").strip() or None
    except Exception:
        return None

async def _gen_code(websocket, agent_name, prompt, system, cancel_event, temperature=0.2, via_colab=False):
    """Genere du code via le GPU Colab si via_colab ET Colab dispo, sinon via le modele local
    (stream). Renvoie le texte complet. Repli local automatique si Colab ne repond pas."""
    if via_colab and mem_get("colab_url"):
        await websocket.send_json({"type":"agent_start","agent":"colab-gpu"})
        await websocket.send_json({"type":"token","text":"⚡ délégué au GPU Colab...\n"})
        txt = await asyncio.get_event_loop().run_in_executor(None, _colab_llm, system, prompt)
        if txt is not None:
            await websocket.send_json({"type":"token","text":txt})
            return txt
        await websocket.send_json({"type":"token","text":"(GPU Colab indisponible — je continue en local)\n"})
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

    # ── Contexte conversationnel : le pipeline projet suit maintenant le fil du chat
    # (avant, seul l'agent generaliste l'avait) -> "ajoute X au projet precedent",
    # "reprends l'idee d'avant" fonctionnent sans re-decrire tout le contexte.
    conv = _history_text(sid, prompt)
    conv_note = f"\n\nCONVERSATION RECENTE (contexte ; la demande a traiter est ci-dessus):\n{conv}" if conv else ""

    # ── Phase 1 : Brain pense et planifie ────────────────────────────
    await websocket.send_json({"type":"thinking"})
    plan = await asyncio.get_event_loop().run_in_executor(None, call_brain, prompt + conv_note + mem_note)
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
            "name":"⚡ gros projet — génération déléguée au GPU Colab","size":""})

    # ── Phase 2 : Lire le code existant si c'est une modif/reprise ───
    existing = {}
    if editing and os.path.isdir(project_dir):
        # executor : lecture disque de tout le projet, ne pas geler la boucle asyncio
        existing = await asyncio.get_event_loop().run_in_executor(None, read_project, project_dir)
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
    enriched = (
        f"PLAN DU BRAIN:\n{plan}\n\n"
        f"DEMANDE: {prompt}{ctx_note}{conv_note}{mem_note}\n\n"
        f"Projet: C:\\Devllma\\workspace\\{project_name}\\\n"
        f"Produis chaque fichier au format strict:\n###FILE: nom.ext\n<code>\n###ENDFILE\n"
        f"{consigne}"
    )
    code_resp = await _gen_code(websocket, agent_name, enriched, CODER_SYSTEM, cancel_event,
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
    run_ok = None
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
            fix_resp = await stream_agent(websocket, agent_name, fix_p, CODER_FIX_SYSTEM, cancel_event=cancel_event)
            fixed = extract_files(fix_resp)
            if fixed:
                await asyncio.get_event_loop().run_in_executor(None, write_files, project_dir, fixed)

        # Deduire requirements.txt des imports (le modele l'oublie souvent) AVANT l'install
        added_deps = await asyncio.get_event_loop().run_in_executor(None, derive_requirements, project_dir)
        if added_deps:
            await websocket.send_json({"type":"file_created",
                "name":f"requirements.txt (+{len(added_deps)} dépendance(s) déduite(s): {', '.join(added_deps[:6])})","size":""})
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
                for iteration in range(1, 4):
                    if cancel_event.is_set():
                        break
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

                    # Agent corrige (n'injecte la section ANALYSE que si le brain a tourne)
                    analyse_note = f"ANALYSE:\n{analysis}\n\n" if analysis else ""
                    fix_p = (f"ERREUR PRECISE A CORRIGER: {clean_err}{escalate_note}\n\n"
                             f"{analyse_note}TRACE COMPLETE:\n{run_out}\n\n"
                             f"CODE ACTUEL:\n{format_context(cur_files)}\n\n"
                             f"Corrige. Format strict:\n###FILE: nom.ext\n<code>\n###ENDFILE")
                    # Bloqué (escalate) OU deja en mode Colab -> on delegue la correction au GPU.
                    fix_resp = await _gen_code(websocket, agent_name, fix_p, CODER_FIX_SYSTEM,
                                               cancel_event, temperature=fix_temp,
                                               via_colab=(escalate or big_project))
                    if cancel_event.is_set():
                        break

                    fixed = extract_files(fix_resp)
                    # Garde-fou anti-regression : rejeter une "correction" qui vide un fichier
                    # qui avait deja du contenu substantiel (sinon une correction ratee peut
                    # ecraser un fichier fonctionnel par un fichier quasi-vide).
                    applied = []
                    for fname, code in fixed:
                        old = cur_files.get(fname, "")
                        if old and len(old) > 200 and len(code) < 0.4 * len(old):
                            continue
                        applied.append((fname, code))
                    if applied:
                        await asyncio.get_event_loop().run_in_executor(None, write_files, project_dir, applied)

                    run_ok, run_out, entry = await asyncio.get_event_loop().run_in_executor(
                        None, execute_project, project_dir
                    )
                    await websocket.send_json({"type":"run_result","ok":run_ok,"output":run_out,"entry":entry})
                    if run_ok: break

        # Commit git automatique d'une version qui marche (point de restauration)
        if run_ok:
            await asyncio.get_event_loop().run_in_executor(
                None, Skills.git_commit, project_dir, f"DevLLMA: {project_name} OK"
            )

    # Marquer dernier todo done
    for i,t in enumerate(todos):
        todos[i]["done"]=True; todos[i]["active"]=False
    if todos: await websocket.send_json({"type":"todos","items":todos})

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
    sid_box = {"sid": new_session("Web Session")}   # une session par connexion
    queue = asyncio.Queue()
    cancel_event = asyncio.Event()
    busy = {"v": False}
    last_prompt = {"v": None}

    async def worker():
        while True:
            prompt = await queue.get()
            busy["v"] = True
            cancel_event.clear()
            try:
                await handle_prompt(websocket, sid_box, prompt, cancel_event)
            except Exception as e:
                try:
                    await websocket.send_json({"type":"token","text":f"\nErreur: {e}"})
                    await websocket.send_json({"type":"done"})
                except Exception:
                    pass
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
                        _http.post(OLLAMA+"/api/generate",
                                      json={"model":m,"prompt":"","keep_alive":KEEP_ALIVE}, timeout=5)
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

def _startup_warmup():
    """Precharge le modele PUIS le cache KV du prefixe de l'agent (systeme+outils).
    Sans ce prechauffage, la premiere demande apres un redemarrage paie ~6 min
    d'evaluation de prompt a froid sur ce CPU (mesure) et semble en panne."""
    check_ollama_ready()
    preload_models()
    from agent_core import warm_agent_cache
    warm_agent_cache()

if __name__ == "__main__":
    import threading
    threading.Thread(target=_startup_warmup, daemon=True).start()
    # host="0.0.0.0" est INTENTIONNEL, pas un oubli : c'est ce qui permet l'acces
    # LAN (192.168.1.30) et Tailscale (100.112.22.79, PWA mobile) mis en place
    # deliberement. Le repasser en 127.0.0.1 casserait l'acces telephone/tablette.
    # La protection contre les requetes cross-origin malveillantes passe par le
    # CORS restreint (_PRIVATE_ORIGIN_RE ci-dessus) et le pare-feu Windows, pas
    # par le binding — ne pas "corriger" ceci sans re-craquer l'acces distant.
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")
