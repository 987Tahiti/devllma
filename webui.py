import sys, os, json, requests, re, subprocess, asyncio, time, threading, queue as _queue
sys.path.insert(0, r"C:\Devllma")
os.chdir(r"C:\Devllma")

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, Response, PlainTextResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from db import init, new_session, msg, history, list_sessions, mem_search, mem_index, mem_get, mem_set, stats as db_stats, delete_session, mem_list, mem_delete, mem_purge
from agents import AGENTS, route, OLLAMA, has_dev_keywords, is_research_question, GREETINGS, _kw_match, strip_accents
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
import uvicorn

init()

# Garde le modèle chargé en RAM en PERMANENCE (-1) : le recharger + réévaluer les
# prompts à froid coûte plusieurs minutes sur ce CPU (cf. agent_core.KEEP_ALIVE).
KEEP_ALIVE = -1
# Modèle du Brain : qwen3-coder:30b (MoE, ~3.3B actifs/token -> aussi rapide qu'un 7b dense
# mais bien plus capable ; mesuré ~2x plus rapide que qwen2.5-coder:7b sur ce CPU, cf bench_models.py)
BRAIN_MODEL = "qwen3-coder:30b"
app = FastAPI(title="DevLLMA")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

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

def load_workspace_memory():
    """Scanne le workspace et charge la mémoire des projets existants"""
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

Pour chaque demande produis un plan structuré:
1. PROJET: nom exact du projet
2. STACK: technologies choisies
3. FICHIERS: liste exacte à créer (avec chemins relatifs)
4. ARCHITECTURE: structure en 3-5 lignes
5. POINTS CLÉS: ce qu'il ne faut pas oublier
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
    r = subprocess.run([PYTHON,"-m","pip","install","-r",req,"-q","--no-warn-script-location"],
                       capture_output=True, text=True, timeout=120,
                       encoding="utf-8", errors="replace")
    return r.returncode==0, (r.stdout+r.stderr).strip()[:200]

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
    """Devine le port qu'un projet serveur va essayer d'ouvrir en scannant son code
    (sinon on ne peut pas verifier reellement qu'il a demarre)."""
    port = None
    is_server = False
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
            if any(mk in low for mk in SERVER_MARKERS):
                is_server = True
            m = re.search(r'port\s*=\s*(\d{2,5})', content)
            if m and port is None:
                port = int(m.group(1))
    return is_server, (port or 8000)

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
    is_server, guessed_port = _detect_server_port(project_dir)
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
                port_open = _wait_port_open(guessed_port, max_wait=min(8, timeout))
                if port_open:
                    ok_http, detail = _http_probe(guessed_port)
                    _kill_process_tree(proc.pid)
                    try: proc.communicate(timeout=3)
                    except Exception: pass
                    if ok_http:
                        return True, f"(serveur actif — répond en HTTP {detail} sur le port {guessed_port})", ep
                    return False, f"(port {guessed_port} ouvert mais aucune réponse HTTP valide: {detail})", ep
                _kill_process_tree(proc.pid)
                try: proc.communicate(timeout=3)
                except Exception: pass
                return False, f"(le serveur n'a jamais répondu sur le port {guessed_port} après {timeout}s — démarrage probablement en échec silencieux)", ep
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

def slug(text):
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

# ─── Appels IA ────────────────────────────────────────────────────────────────
def call_brain(prompt, system=None, max_tokens=450):
    s = system if system else make_brain_system()
    last_err = None
    for attempt in range(3):
        try:
            r = requests.post(OLLAMA+"/api/generate", json={
                "model":BRAIN_MODEL, "system":s,
                "prompt":prompt, "stream":False, "keep_alive":KEEP_ALIVE,
                "options":{"temperature":0.3,"num_predict":max_tokens}
            }, timeout=300)
            return r.json().get("response","")
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_err = e
            time.sleep(2 * (attempt + 1))  # Ollama redemarre parfois brievement -> on relaisse le temps
        except Exception as e:
            return f"(brain indisponible: {e})"
    return f"(brain indisponible apres 3 tentatives: {last_err})"

def preload_models():
    """Précharge UNIQUEMENT le modèle actif (évite de saturer la RAM avec 3 modèles)."""
    active = AGENTS["coder"]["model"]
    for m in {active, BRAIN_MODEL}:
        try:
            requests.post(OLLAMA+"/api/generate",
                          json={"model":m,"prompt":"","keep_alive":KEEP_ALIVE},
                          timeout=120)
        except Exception:
            pass

def _ollama_stream_worker(cfg, sys_p, prompt, out_q, stop_flag, temperature=0.2):
    """Tourne dans un thread separe: l'appel HTTP bloquant NE DOIT PAS geler la boucle asyncio
    (sinon le bouton stop et la reception websocket restent bloques jusqu'a la fin de la generation)."""
    r = None
    try:
        r = None
        last_err = None
        for attempt in range(3):
            try:
                r = requests.post(OLLAMA+"/api/generate", json={
                    "model":cfg["model"], "system":sys_p,
                    "prompt":prompt, "stream":True, "keep_alive":KEEP_ALIVE,
                    "options":{"temperature":temperature,"num_predict":4000}
                }, stream=True, timeout=(10, 600))
                break
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                last_err = e
                r = None
                time.sleep(2 * (attempt + 1))  # Ollama redemarre parfois brievement -> on relaisse le temps
        if r is None:
            raise last_err or ConnectionError("Ollama injoignable")
        for line in r.iter_lines():
            if stop_flag.is_set():
                out_q.put(("stopped", None))
                return
            if line:
                out_q.put(("line", line))
                try:
                    if json.loads(line).get("done"):
                        break
                except Exception:
                    pass
        out_q.put(("end", None))
    except Exception as e:
        out_q.put(("error", str(e)))
    finally:
        if r is not None:
            try: r.close()
            except Exception: pass

async def stream_agent(ws, agent_name, prompt, system=None, cancel_event=None, temperature=0.2):
    cfg = AGENTS.get(agent_name, AGENTS["coder"])
    sys_p = system or cfg["system"]
    full = ""
    t0 = time.time()
    ntok = 0
    stopped = False
    await ws.send_json({"type":"agent_start","agent":agent_name})

    loop = asyncio.get_event_loop()
    out_q = _queue.Queue()
    stop_flag = threading.Event()
    th = threading.Thread(target=_ollama_stream_worker, args=(cfg, sys_p, prompt, out_q, stop_flag, temperature), daemon=True)
    th.start()

    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                stop_flag.set()
                stopped = True
                break
            try:
                kind, payload = await loop.run_in_executor(None, out_q.get, True, 0.3)
            except _queue.Empty:
                continue
            if kind == "error":
                await ws.send_json({"type":"token","text":f"\nErreur: {payload}"})
                break
            if kind in ("end", "stopped"):
                if kind == "stopped":
                    stopped = True
                break
            d = json.loads(payload)
            token = d.get("response","")
            if token:
                full += token
                ntok += 1
                await ws.send_json({"type":"token","text":token})
                if ntok % 6 == 0:
                    elapsed = time.time() - t0
                    if elapsed > 0.5:
                        await ws.send_json({"type":"speed","tps":round(ntok/elapsed,1)})
            if d.get("done"):
                break
    finally:
        stop_flag.set()
        th.join(timeout=2)
    if stopped:
        await ws.send_json({"type":"stopped"})
    return full

# ─── HTML ─────────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DevLLMA</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0d1117;--sf:#161b22;--sf2:#1c2128;--bd:#30363d;
  --bl:#58a6ff;--gn:#3fb950;--pu:#bc8cff;--or:#f97316;--rd:#ef4444;
  --tx:#e6edf3;--mu:#8b949e
}
body{background:var(--bg);color:var(--tx);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;height:100vh;display:flex;flex-direction:column;overflow:hidden}
/* Header */
header{padding:8px 16px;border-bottom:1px solid var(--bd);display:flex;align-items:center;gap:8px;flex-shrink:0;flex-wrap:wrap}
.logo{font-family:monospace;font-size:.95rem;font-weight:700;color:var(--bl);letter-spacing:-.02em}
.chip{font-size:.6rem;font-family:monospace;padding:2px 7px;border-radius:3px;font-weight:700;letter-spacing:.03em}
.c-g{background:#22c55e18;color:#22c55e;border:1px solid #22c55e40}
.c-b{background:#58a6ff18;color:#58a6ff;border:1px solid #58a6ff40}
.c-p{background:#bc8cff18;color:#bc8cff;border:1px solid #bc8cff40}
.c-o{background:#f9731618;color:#fb923c;border:1px solid #f9731640}
.dot{width:7px;height:7px;border-radius:50%;background:var(--gn);animation:pu 2s infinite;flex-shrink:0}
@keyframes pu{0%,100%{box-shadow:0 0 3px var(--gn)}50%{box-shadow:0 0 8px var(--gn)}}
select{margin-left:auto;background:var(--sf);border:1px solid var(--bd);color:var(--tx);padding:3px 8px;border-radius:5px;font-size:.75rem;cursor:pointer}
#newSessionBtn{margin-left:6px;background:var(--bl);color:#fff;border:none;border-radius:5px;padding:4px 10px;font-size:.72rem;font-weight:600;cursor:pointer;white-space:nowrap}
#newSessionBtn:hover{filter:brightness(1.1)}
/* Barre ressources */
#statusbar{flex-shrink:0;display:flex;align-items:center;gap:18px;padding:4px 16px;border-top:1px solid var(--bd);background:var(--sf);font-family:monospace;font-size:.66rem;color:var(--mu)}
#statusbar .stat{display:flex;align-items:center;gap:6px}
#statusbar .stat-l{color:var(--mu);white-space:nowrap}
#statusbar .stat b{color:var(--tx);min-width:42px;text-align:right;font-weight:700}
#statusbar .stat-bar{width:72px;height:7px;background:var(--bg);border:1px solid var(--bd);border-radius:4px;overflow:hidden}
#statusbar .stat-bar i{display:block;height:100%;width:0%;background:var(--gn);transition:width .5s ease,background .5s}
#stat-host{margin-left:auto;color:var(--mu)}
/* Layout */
.layout{display:flex;flex:1;overflow:hidden}
/* Sidebar projets */
#sidebar{width:190px;flex-shrink:0;border-right:1px solid var(--bd);display:flex;flex-direction:column;overflow:hidden;background:var(--sf)}
.sb-head{padding:8px 10px;font-size:.62rem;font-family:monospace;color:var(--mu);text-transform:uppercase;letter-spacing:.07em;border-bottom:1px solid var(--bd);flex-shrink:0}
#proj-list{flex:1;overflow-y:auto;padding:4px}
#session-list{max-height:180px;overflow-y:auto;padding:4px;flex-shrink:0;border-bottom:1px solid var(--bd)}
.sess-item{padding:4px 6px;border-radius:5px;font-size:.71rem;font-family:monospace;display:flex;align-items:center;gap:4px;transition:background .15s}
.sess-item:hover{background:var(--sf2)}
.sess-item.active{background:#58a6ff18;color:var(--bl)}
.sess-label{flex:1;cursor:pointer;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sess-del{background:none;border:none;color:var(--mu);cursor:pointer;font-size:.85rem;line-height:1;padding:0 3px;flex-shrink:0;opacity:.55}
.sess-del:hover{opacity:1;color:var(--rd)}
.proj-item{padding:5px 8px;border-radius:5px;cursor:pointer;font-size:.73rem;font-family:monospace;display:flex;align-items:center;gap:5px;transition:background .15s}
.proj-item:hover{background:var(--sf2)}
.proj-item.active{background:#58a6ff18;color:var(--bl)}
.proj-dot{width:5px;height:5px;border-radius:50%;background:var(--gn);flex-shrink:0}
/* Todos sidebar */
#todos{border-top:1px solid var(--bd);padding:6px 10px;flex-shrink:0}
.todo-head{font-size:.6rem;font-family:monospace;color:var(--mu);text-transform:uppercase;letter-spacing:.07em;margin-bottom:4px}
.todo-item{display:flex;align-items:flex-start;gap:5px;font-size:.7rem;color:var(--mu);line-height:1.4;padding:1px 0}
.todo-item.done{color:var(--gn);text-decoration:line-through}
.todo-item.active{color:var(--tx)}
.todo-cb{flex-shrink:0;width:11px;height:11px;border:1px solid var(--bd);border-radius:2px;margin-top:1px;display:flex;align-items:center;justify-content:center;font-size:.6rem}
.todo-cb.done{background:var(--gn);border-color:var(--gn);color:#000}
/* Chat */
#chat-area{flex:1;display:flex;flex-direction:column;overflow:hidden;position:relative}
#chat{flex:1;overflow-y:auto;padding:14px 18px;display:flex;flex-direction:column;gap:10px}
.msg{display:flex;flex-direction:column;gap:4px;max-width:92%}
.msg.user{align-self:flex-end;align-items:flex-end}
.msg.agent{align-self:flex-start;align-items:flex-start;width:100%;max-width:100%}
.atag{font-size:.58rem;font-family:monospace;padding:2px 6px;border-radius:3px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;display:inline-block;margin-bottom:2px}
.bbl{padding:9px 13px;border-radius:10px;font-size:.83rem;line-height:1.7;white-space:pre-wrap;word-break:break-word}
.msg.user .bbl{background:#1f6feb;color:#fff;border-bottom-right-radius:3px}
.msg.agent .bbl{background:var(--sf);border:1px solid var(--bd);border-bottom-left-radius:3px;width:100%}
.bbl code{font-family:monospace;background:#0d1117;padding:1px 4px;border-radius:3px;font-size:.77rem;color:#79c0ff}
.bbl pre{background:#0d1117;border:1px solid var(--bd);border-radius:6px;padding:9px 11px;overflow-x:auto;margin:6px 0;font-size:.73rem;font-family:monospace;line-height:1.5}
/* Think block */
.think{border-left:3px solid var(--pu);border-radius:0 6px 6px 0;background:#bc8cff08;margin:3px 0}
.think-h{display:flex;align-items:center;gap:6px;padding:6px 10px;cursor:pointer;font-size:.68rem;font-family:monospace;color:var(--pu);font-weight:700;user-select:none}
.think-h:hover{background:#ffffff05}
.arr{transition:transform .2s;font-size:.6rem}
.think.open .arr{transform:rotate(90deg)}
.think-b{padding:8px 11px 10px;font-size:.73rem;color:var(--mu);white-space:pre-wrap;font-family:monospace;line-height:1.55;border-top:1px solid var(--bd);display:none}
.think.open .think-b{display:block}
/* File card */
.fcard{display:flex;align-items:center;gap:7px;background:#0d1117;border-left:2px solid var(--gn);border-radius:0 5px 5px 0;padding:5px 10px;margin:2px 0;font-size:.72rem;font-family:monospace}
.fcard.err{border-color:var(--rd)}
.fcard .fn{color:var(--gn);font-weight:700}.fcard.err .fn{color:var(--rd)}
.fcard .fi{color:var(--mu);font-size:.64rem}
.fcard .fbtn{margin-left:auto;background:none;border:1px solid var(--bd);color:var(--mu);border-radius:4px;font-size:.6rem;padding:2px 6px;cursor:pointer;font-family:inherit}
.fcard .fbtn:hover{color:var(--tx);border-color:var(--bl)}
/* Arborescence workspace */
.tree-toggle{background:none;border:none;color:var(--mu);cursor:pointer;padding:0 2px;font-size:.65rem}
.tree-node{padding-left:16px}
.tree-file{display:flex;align-items:center;gap:5px;padding:2px 6px;font-size:.7rem;color:var(--tx);cursor:pointer;border-radius:4px}
.tree-file:hover{background:var(--sf2)}
.tree-dir{font-size:.7rem;color:var(--mu);padding:2px 6px;display:flex;align-items:center;gap:4px;cursor:pointer}
.tree-dir:hover{color:var(--tx)}
/* Modale de previsualisation de fichier */
#fileModal .modal-box{width:760px}
#fileModal pre{max-height:60vh;overflow:auto}
/* Run result */
.run-ok,.run-err{border-radius:6px;padding:8px 12px;margin:4px 0;font-size:.72rem;font-family:monospace}
.run-ok{background:#0f1f10;border:1px solid #22c55e33}
.run-err{background:#1f0e0e;border:1px solid #ef444433}
.run-label{font-size:.6rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;margin-bottom:4px}
.run-ok .run-label{color:var(--gn)}.run-err .run-label{color:var(--rd)}
.run-out{color:var(--tx);white-space:pre-wrap}
/* Project done box */
.pbox{background:var(--sf);border:1px solid #58a6ff25;border-radius:7px;padding:9px 13px;margin:4px 0}
.ptitle{font-size:.67rem;font-family:monospace;color:var(--bl);font-weight:700;margin-bottom:4px;text-transform:uppercase;letter-spacing:.05em}
.ppath{font-family:monospace;font-size:.67rem;color:var(--mu);margin-bottom:6px}
.chips{display:flex;flex-wrap:wrap;gap:3px}
.fchip{background:#58a6ff12;border:1px solid #58a6ff28;color:#79c0ff;font-family:monospace;font-size:.65rem;padding:2px 5px;border-radius:3px}
/* Iteration */
.iter{font-size:.63rem;font-family:monospace;color:#f59e0b;padding:3px 8px;background:#f59e0b15;border:1px solid #f59e0b30;border-radius:4px;margin:3px 0;display:inline-block}
/* Snapshot badge */
.snap{font-size:.62rem;font-family:monospace;color:var(--mu);padding:3px 8px;background:var(--sf2);border:1px solid var(--bd);border-radius:4px;margin:3px 0;display:inline-flex;align-items:center;gap:8px}
.snap b{color:var(--bl)}
.snap button{background:none;border:1px solid var(--bd);color:var(--bl);border-radius:3px;font-size:.6rem;font-family:monospace;padding:1px 6px;cursor:pointer}
.snap button:hover{border-color:var(--bl)}
/* Memoire semantique (RAG) */
.mem{font-size:.62rem;font-family:monospace;color:var(--pu);padding:3px 8px;background:#bc8cff0f;border:1px solid #bc8cff30;border-radius:4px;margin:3px 0;display:inline-flex;flex-wrap:wrap;align-items:center;gap:6px}
.mem b{color:var(--pu)}
.mem .mitem{color:var(--mu);background:#00000030;border-radius:3px;padding:1px 5px}
/* Bouton copier code */
.pre-wrap{position:relative}
.copy-btn{position:absolute;top:5px;right:5px;background:var(--sf2);border:1px solid var(--bd);color:var(--mu);border-radius:4px;font-size:.62rem;font-family:monospace;padding:2px 7px;cursor:pointer;opacity:.75}
.copy-btn:hover{opacity:1;color:var(--tx);border-color:var(--bl)}
/* Coloration syntaxique legere */
.tok-kw{color:#ff7b72}.tok-str{color:#a5d6ff}.tok-com{color:#8b949e;font-style:italic}.tok-num{color:#79c0ff}.tok-fn{color:#d2a8ff}
/* Vitesse generation */
#speed-chip{font-size:.6rem;font-family:monospace;color:var(--gn);padding:2px 7px;border-radius:3px;background:#22c55e12;border:1px solid #22c55e30;display:none}
/* Stop / regenerer */
#stopBtn{background:var(--rd);color:#fff;border:none;border-radius:8px;padding:0 13px;font-size:.86rem;cursor:pointer;font-weight:600;height:40px;white-space:nowrap;display:none}
#stopBtn:hover{filter:brightness(1.1)}
.regen-btn{background:none;border:1px solid var(--bd);color:var(--mu);border-radius:4px;font-size:.62rem;font-family:monospace;padding:2px 7px;cursor:pointer;margin-top:3px;align-self:flex-start}
.regen-btn:hover{color:var(--tx);border-color:var(--bl)}
/* Theme toggle + export */
#themeBtn,#exportBtn,#regenBtn,#modelsBtn{background:var(--sf);border:1px solid var(--bd);color:var(--tx);border-radius:5px;padding:3px 8px;font-size:.72rem;cursor:pointer}
#themeBtn:hover,#exportBtn:hover,#regenBtn:hover,#modelsBtn:hover{border-color:var(--bl)}
/* Modale gestion des modeles */
.modal-overlay{position:fixed;inset:0;background:#000a;display:flex;align-items:center;justify-content:center;z-index:200}
.modal-box{background:var(--sf);border:1px solid var(--bd);border-radius:10px;width:620px;max-width:92vw;max-height:85vh;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 12px 40px #000a}
.modal-head{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;border-bottom:1px solid var(--bd);font-family:monospace;font-size:.85rem;font-weight:700}
.modal-body{padding:6px 14px 14px;overflow-y:auto;flex:1}
.modal-section-title{font-size:.65rem;font-family:monospace;color:var(--mu);text-transform:uppercase;letter-spacing:.06em;margin:14px 0 6px;border-bottom:1px solid var(--bd);padding-bottom:4px}
.model-row{display:flex;align-items:center;gap:8px;padding:4px 2px;font-size:.75rem;font-family:monospace;border-bottom:1px solid var(--bd)}
.model-row .mn{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.model-row .mn.mstar{color:var(--gn)}
.model-row .ms{color:var(--mu);font-size:.68rem;flex-shrink:0}
#pullRow{display:flex;gap:6px}
#pullInput{flex:1;background:var(--bg);border:1px solid var(--bd);border-radius:6px;padding:6px 9px;color:var(--tx);font-family:monospace;font-size:.8rem}
#pullBtn{background:var(--bl);color:#fff;border:none;border-radius:6px;padding:0 14px;font-size:.8rem;cursor:pointer}
#pullBtn:disabled{opacity:.5;cursor:not-allowed}
#pull-log{font-family:monospace;font-size:.66rem;color:var(--mu);white-space:pre-wrap;max-height:120px;overflow-y:auto;background:var(--bg);border:1px solid var(--bd);border-radius:6px;padding:6px;margin-top:6px;display:none}
.bench-row{display:flex;gap:8px;padding:3px 2px;font-size:.7rem;font-family:monospace;border-bottom:1px solid var(--bd)}
#memTestRow{display:flex;gap:6px}
#memTestRow input{flex:1;background:var(--bg);border:1px solid var(--bd);border-radius:6px;padding:6px 9px;color:var(--tx);font-family:monospace;font-size:.8rem}
#memTestRow button{background:var(--bl);color:#fff;border:none;border-radius:6px;padding:0 14px;font-size:.8rem;cursor:pointer}
.mem-row{display:flex;align-items:flex-start;gap:8px;padding:6px 2px;font-size:.7rem;border-bottom:1px solid var(--bd)}
.mem-row .mk{font-family:monospace;font-size:.6rem;padding:1px 6px;border-radius:3px;flex-shrink:0;text-transform:uppercase}
.mem-row .mc{flex:1;color:var(--tx);word-break:break-word}
.mem-row .mref{color:var(--mu);font-size:.62rem;display:block;margin-bottom:2px}
.mem-row .mdel{background:none;border:none;color:var(--mu);cursor:pointer;flex-shrink:0}
.mem-row .mdel:hover{color:var(--rd)}
.mem-score{color:var(--gn);font-family:monospace;font-size:.65rem;margin-left:6px}
.bench-row .bn{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* Scroll to bottom */
#scrollBtn{position:absolute;right:16px;bottom:66px;background:var(--bl);color:#fff;border:none;border-radius:50%;width:32px;height:32px;font-size:.9rem;cursor:pointer;display:none;box-shadow:0 2px 8px #0006;z-index:10}
/* Horodatage message */
.msg-time{font-size:.58rem;color:var(--mu);font-family:monospace;margin-top:1px}
/* Theme clair */
body.light{--bg:#f6f8fa;--sf:#ffffff;--sf2:#eef1f4;--bd:#d0d7de;--tx:#1f2328;--mu:#57606a}
body.light .bbl code{background:#eef1f4;color:#0550ae}
body.light .bbl pre{background:#f6f8fa}
/* Sécurité */
.secbox{background:var(--sf);border:1px solid #f9731633;border-radius:7px;padding:9px 13px;margin:4px 0}
.sec-title{font-size:.67rem;font-family:monospace;color:#fb923c;font-weight:700;margin-bottom:6px;text-transform:uppercase;letter-spacing:.05em}
.sec-item{display:flex;align-items:flex-start;gap:7px;font-size:.71rem;font-family:monospace;padding:3px 0;border-top:1px solid var(--bd)}
.sec-sev{flex-shrink:0;font-size:.58rem;font-weight:700;padding:1px 5px;border-radius:3px}
.sev-HAUTE{background:#ef444422;color:#f87171}
.sev-MOYENNE{background:#f59e0b22;color:#fbbf24}
.sev-BASSE{background:#58a6ff22;color:#79c0ff}
.sec-msg{color:var(--tx)}.sec-loc{color:var(--mu);font-size:.64rem}
/* Bloqué */
.blocked{background:#1f0d0d;border:1px solid var(--rd);border-radius:7px;padding:9px 13px;margin:4px 0;font-size:.74rem}
.blocked-t{color:var(--rd);font-weight:700;font-family:monospace;font-size:.67rem;text-transform:uppercase;margin-bottom:5px}
.blocked-r{color:var(--tx);font-family:monospace;font-size:.71rem;padding:1px 0}
/* Typing */
#typing{align-self:flex-start}
.dots{display:flex;gap:4px;padding:10px 13px;background:var(--sf);border:1px solid var(--bd);border-radius:10px;border-bottom-left-radius:3px}
.dots span{width:5px;height:5px;border-radius:50%;background:var(--mu);animation:bl 1.2s infinite}
.dots span:nth-child(2){animation-delay:.2s}.dots span:nth-child(3){animation-delay:.4s}
@keyframes bl{0%,80%,100%{opacity:.2}40%{opacity:1}}
/* Footer */
footer{padding:9px 16px;border-top:1px solid var(--bd);display:flex;gap:7px;flex-shrink:0}
#inp{flex:1;background:var(--sf);border:1px solid var(--bd);border-radius:8px;padding:8px 12px;color:var(--tx);font-size:.86rem;resize:none;height:40px;max-height:140px;overflow-y:auto;outline:none;font-family:inherit;transition:border-color .2s}
#inp:focus{border-color:var(--bl)}
#btn{background:var(--bl);color:#fff;border:none;border-radius:8px;padding:0 15px;font-size:.86rem;cursor:pointer;font-weight:600;height:40px;white-space:nowrap}
#btn:disabled{opacity:.4;cursor:not-allowed}
#clipBtn{background:var(--sf);color:var(--tx);border:1px solid var(--bd);border-radius:8px;width:40px;height:40px;font-size:1.05rem;cursor:pointer;flex-shrink:0}
#clipBtn:hover{border-color:var(--bl)}
body.dragging{outline:3px dashed var(--bl);outline-offset:-6px}
.s-btn{background:none;border:none;color:var(--mu);cursor:pointer;font-size:.8rem;padding:0 3px;line-height:1}
.s-btn:hover{color:var(--tx)}
/* Timeline des etapes d'outils de l'agent */
.tstep{align-self:flex-start;background:var(--sf);border:1px solid var(--bd);border-left:3px solid var(--bl);border-radius:7px;padding:6px 11px;margin:2px 0;font-size:.74rem;max-width:80%}
.tstep .tst-head{display:flex;align-items:center;gap:7px;font-family:monospace}
.tstep .tst-label{color:var(--tx);font-weight:600}
.tstep .tst-arg{color:var(--mu);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:340px}
.tstep .tst-status{margin-left:auto;font-weight:700}
.tstep.ok{border-left-color:var(--gn)}
.tstep.err{border-left-color:var(--rd)}
.tstep .tst-ms{color:var(--mu);font-size:.64rem}
.tstep details{margin-top:4px}
.tstep details summary{cursor:pointer;color:var(--mu);font-size:.64rem}
.tstep pre{white-space:pre-wrap;word-break:break-all;font-size:.66rem;color:var(--mu);margin-top:3px;max-height:180px;overflow-y:auto}
.tspin{width:10px;height:10px;border:2px solid var(--bd);border-top-color:var(--bl);border-radius:50%;animation:tsp .8s linear infinite;flex-shrink:0}
@keyframes tsp{to{transform:rotate(360deg)}}
/* Tableau resultats SQL */
.sqlwrap{align-self:flex-start;max-width:88%;overflow-x:auto;background:var(--sf);border:1px solid var(--bd);border-radius:7px;margin:3px 0}
.sqlwrap table{border-collapse:collapse;font-size:.72rem;font-family:monospace;min-width:200px}
.sqlwrap th{background:var(--sf2);color:var(--bl);padding:5px 10px;text-align:left;cursor:pointer;user-select:none;white-space:nowrap;border-bottom:1px solid var(--bd)}
.sqlwrap th:hover{color:var(--tx)}
.sqlwrap td{padding:4px 10px;color:var(--tx);border-bottom:1px solid var(--bd)}
.sqlwrap tr:last-child td{border-bottom:none}
.sql-meta{display:flex;align-items:center;gap:8px;padding:4px 10px;font-size:.64rem;color:var(--mu);border-bottom:1px solid var(--bd)}
.sql-copy{background:none;border:1px solid var(--bd);color:var(--mu);border-radius:5px;font-size:.62rem;padding:2px 8px;cursor:pointer;margin-left:auto}
.sql-copy:hover{color:var(--tx);border-color:var(--bl)}
/* Chip piece jointe document */
#attach-bar{display:none;padding:4px 16px 0;flex-shrink:0}
.attach-chip{display:inline-flex;align-items:center;gap:7px;background:var(--sf);border:1px solid var(--bl);border-radius:15px;padding:4px 12px;font-size:.72rem;color:var(--tx)}
.attach-chip .ac-x{cursor:pointer;color:var(--mu);font-weight:700}
.attach-chip .ac-x:hover{color:var(--rd)}
/* Recherche sessions */
#searchBox{width:calc(100% - 16px);margin:4px 8px;background:var(--sf);border:1px solid var(--bd);border-radius:6px;padding:5px 9px;color:var(--tx);font-size:.72rem;outline:none}
#searchBox:focus{border-color:var(--bl)}
.snip mark{background:#f59e0b44;color:var(--tx);border-radius:2px;padding:0 1px}
.snip{font-size:.62rem;color:var(--mu);margin-top:2px;line-height:1.35}
/* Menu slash (bibliotheque de prompts) */
#slashMenu{display:none;position:absolute;bottom:62px;left:16px;right:16px;max-width:560px;background:var(--sf);border:1px solid var(--bd);border-radius:9px;box-shadow:0 8px 24px #0008;z-index:60;max-height:260px;overflow-y:auto}
.slash-item{padding:8px 13px;font-size:.76rem;cursor:pointer;border-bottom:1px solid var(--bd)}
.slash-item:last-child{border-bottom:none}
.slash-item.selected,.slash-item:hover{background:var(--sf2)}
.slash-item b{color:var(--bl);font-family:monospace;font-size:.7rem}
.slash-item span{color:var(--mu);display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#saveTplBtn{background:var(--sf);color:var(--mu);border:1px solid var(--bd);border-radius:8px;width:40px;height:40px;font-size:.95rem;cursor:pointer;flex-shrink:0}
#saveTplBtn:hover{border-color:var(--bl);color:var(--tx)}
</style>
</head>
<body>
<header>
  <div class="dot"></div>
  <span class="logo">&gt;_ DevLLMA</span>
  <span class="chip c-p">&#129504; Brain actif</span>
  <span class="chip c-g">&#9889; Exécution</span>
  <span class="chip c-b">&#128196; Lecture/Écriture</span>
  <span class="chip c-o">&#128260; Auto-correction</span>
  <select id="modelSel" onchange="chgM(this.value)" title="Choisir le modèle IA">
    <option>chargement…</option>
  </select>
  <button id="newSessionBtn" onclick="newSession()" title="Démarrer une nouvelle session">&#10133; Nouvelle session</button>
  <button id="exportBtn" onclick="exportSession()" title="Exporter la session en markdown">&#11015; Export</button>
  <button id="regenBtn" onclick="regenLast()" title="Regenerer la derniere reponse">&#8635; Regenerer</button>
  <button id="modelsBtn" onclick="openModelsModal()" title="Gerer les modeles Ollama">&#128230; Modeles</button>
  <button id="themeBtn" onclick="toggleTheme()" title="Theme clair/sombre">&#127768;</button>
</header>
<div class="layout">
  <div id="sidebar">
    <div class="sb-head">&#128172; Sessions</div>
    <input id="searchBox" placeholder="&#128269; Rechercher dans les sessions..." autocomplete="off">
    <div id="session-list"><div style="padding:8px;font-size:.7rem;color:var(--mu)">Chargement...</div></div>
    <div class="sb-head">&#128193; Workspace</div>
    <div id="proj-list"><div style="padding:8px;font-size:.7rem;color:var(--mu)">Chargement...</div></div>
    <div id="todos">
      <div class="todo-head">&#9745; Tâches en cours</div>
      <div id="todo-list"><div style="font-size:.7rem;color:var(--mu)">En attente...</div></div>
    </div>
  </div>
  <div id="chat-area">
    <div id="chat">
      <div class="msg agent">
        <div class="atag" style="background:#f59e0b22;color:#f59e0b">&#9679; brain</div>
        <div class="bbl">Prêt. Mon cerveau est chargé avec la mémoire du workspace.<br><br>
&#8226; <code>Crée une API FastAPI avec SQLite et authentification</code><br>
&#8226; <code>Fais un tableau de bord HTML avec graphiques</code><br>
&#8226; <code>Développe un scraper Python avec export CSV</code><br>
&#8226; <code>Cree un dossier test sur le bureau</code><br>
&#8226; <code>Quelle est la derniere version de Python ?</code> (recherche web)</div>
      </div>
    </div>
    <div id="attach-bar"></div>
    <footer style="position:relative">
      <div id="slashMenu"></div>
      <input type="file" id="imgInput" accept=".png,.jpg,.jpeg,.webp,.bmp,.docx,.xlsx,.pdf,image/*" style="display:none" onchange="handleFile(this.files[0]);this.value=''">
      <button id="clipBtn" onclick="document.getElementById('imgInput').click()" title="Joindre une image (OCR) ou un document Word/Excel/PDF — ou colle/dépose le fichier">&#128206;</button>
      <textarea id="inp" placeholder="Décris ta tâche… (/ pour les modèles de prompts, colle/dépose une image ou un document)" onkeydown="onK(event)" oninput="onInp()"></textarea>
      <button id="saveTplBtn" onclick="saveTemplate()" title="Sauver le texte comme modèle de prompt">&#128190;</button>
      <button id="btn" onclick="send()">Envoyer &#9654;</button>
      <button id="stopBtn" onclick="stopGen()">&#9209; Stop</button>
    </footer>
    <button id="scrollBtn" onclick="chat.scrollTop=chat.scrollHeight" title="Aller en bas">&#8595;</button>
  </div>
</div>
<!-- Panneau de gestion des modeles Ollama -->
<div id="modelsModal" class="modal-overlay" style="display:none" onclick="if(event.target===this)closeModelsModal()">
  <div class="modal-box">
    <div class="modal-head"><span>&#128230; Gestion des modeles</span>
      <button class="s-btn" onclick="closeModelsModal()" title="Fermer">&#10005;</button>
    </div>
    <div class="modal-body">
      <div class="modal-section-title">Modeles installes</div>
      <div id="models-table"><div style="color:var(--mu);font-size:.72rem">Chargement...</div></div>
      <div class="modal-section-title">Telecharger un nouveau modele</div>
      <div id="pullRow">
        <input id="pullInput" placeholder="ex: qwen3-coder:30b (voir ollama.com/library)" onkeydown="if(event.key==='Enter')pullModel()">
        <button id="pullBtn" onclick="pullModel()">Telecharger</button>
      </div>
      <div id="pull-log"></div>
      <div class="modal-section-title">Derniers resultats de benchmark</div>
      <div id="bench-table"><div style="color:var(--mu);font-size:.72rem">Chargement...</div></div>
    </div>
  </div>
</div>
<!-- Previsualisation d'un fichier du workspace -->
<div id="fileModal" class="modal-overlay" style="display:none" onclick="if(event.target===this)closeFileModal()">
  <div class="modal-box">
    <div class="modal-head"><span id="fileModalTitle">&#128196; Fichier</span>
      <span style="margin-left:auto"></span>
      <button class="s-btn" onclick="downloadCurrentFile()" title="Telecharger">&#11015;</button>
      <button class="s-btn" onclick="closeFileModal()" title="Fermer">&#10005;</button>
    </div>
    <div class="modal-body"><div id="fileModalBody" style="color:var(--mu);font-size:.72rem">Chargement...</div></div>
  </div>
</div>
<!-- Memoire semantique -->
<div id="memModal" class="modal-overlay" style="display:none" onclick="if(event.target===this)closeMemModal()">
  <div class="modal-box">
    <div class="modal-head"><span>&#129504; Memoire semantique</span>
      <button class="s-btn" onclick="closeMemModal()" title="Fermer">&#10005;</button>
    </div>
    <div class="modal-body">
      <div class="modal-section-title">Tester ce dont l'agent se souviendrait</div>
      <div id="memTestRow">
        <input id="memTestInput" placeholder="Tape une demande pour voir les souvenirs associes..." onkeydown="if(event.key==='Enter')testMemory()">
        <button onclick="testMemory()">Tester</button>
      </div>
      <div id="memTestResult"></div>
      <div class="modal-section-title">Souvenirs enregistres <button class="s-btn" style="margin-left:8px;border:1px solid var(--rd);color:var(--rd);border-radius:4px;padding:1px 6px" onclick="purgeMemories()">Tout purger</button></div>
      <div id="mem-table"><div style="color:var(--mu);font-size:.72rem">Chargement...</div></div>
    </div>
  </div>
</div>
<!-- Barre d'usage ressources temps réel -->
<div id="statusbar">
  <span class="stat"><span class="stat-l">CPU</span><span class="stat-bar"><i id="cpu-fill"></i></span><b id="cpu-v">--</b></span>
  <span class="stat"><span class="stat-l">RAM</span><span class="stat-bar"><i id="ram-fill"></i></span><b id="ram-v">--</b></span>
  <span class="stat"><span class="stat-l">&#127777; Temp</span><b id="temp-v">--</b></span>
  <span id="speed-chip">-- tok/s</span>
  <span id="mem-count" style="font-size:.62rem;color:var(--mu);cursor:pointer" title="Cliquer pour gerer la memoire" onclick="openMemModal()">&#129504; -- souvenirs</span>
  <span id="stat-host">192.168.1.30</span>
</div>
<script>
const chat=document.getElementById("chat"),inp=document.getElementById("inp"),btn=document.getElementById("btn");
let ws,curB=null,curR="",curW=null,todos=[],projList=[];
const AC={brain:"#f59e0b",coder:"#3b82f6",architect:"#8b5cf6",debugger:"#ef4444",reviewer:"#10b981",
          tester:"#06b6d4",devops:"#f97316",database:"#6366f1",frontend:"#ec4899",
          backend:"#14b8a6",security:"#dc2626",systeme:"#22c55e",researcher:"#0ea5e9",agent:"#a3e635"};

// ── Sessions ──
const INITIAL_SESSION = new URLSearchParams(location.search).get("session") || "";
let currentSid = null;
const WELCOME = '<div class="msg agent"><div class="atag" style="background:#f59e0b22;color:#f59e0b">&#9679; brain</div>'
  + '<div class="bbl">Pr&#234;t. Mon cerveau est charg&#233; avec la m&#233;moire du workspace.<br><br>'
  + '&#8226; <code>Cr&#233;e une API FastAPI avec SQLite</code><br>'
  + '&#8226; <code>Fais un tableau de bord HTML avec graphiques</code><br>'
  + '&#8226; <code>D&#233;veloppe un scraper Python avec export CSV</code><br>'
  + '&#8226; <code>Quelle est la derni&#232;re version de Python ?</code> (recherche web)</div></div>';
function clearChat(){chat.innerHTML=WELCOME;curB=null;curR="";curW=null;todos=[];renderTodos();}
function newSession(){
  if(ws&&ws.readyState===1)ws.send(JSON.stringify({type:"new_session"}));
}
function openSession(id){
  if(!id)return;
  window.open(location.pathname+"?session="+encodeURIComponent(id),"_blank");
}
function deleteSession(id,evt){
  if(evt)evt.stopPropagation();
  if(!confirm("Supprimer la session #"+id+" ? Cette action est irreversible."))return;
  fetch("/sessions/"+id,{method:"DELETE"}).then(r=>r.json()).then(()=>{loadSessions();}).catch(()=>{});
}
function loadSessions(){
  fetch("/sessions").then(r=>r.json()).then(d=>{
    const el=document.getElementById("session-list");
    const list=d.sessions||[];
    if(!list.length){el.innerHTML='<div style="padding:8px;font-size:.7rem;color:var(--mu)">Aucune session</div>';return;}
    el.innerHTML=list.map(s=>{
      const dt=(s.created_at||"").substring(5,16);
      const active=(String(s.id)===String(currentSid))?" active":"";
      return '<div class="sess-item'+active+'">'
        +'<span class="sess-label" onclick="openSession('+s.id+')" title="Ouvrir dans un nouvel onglet">#'+s.id+' '+esc(dt)+'</span>'
        +'<button class="sess-del" onclick="deleteSession('+s.id+',event)" title="Supprimer">&times;</button></div>';
    }).join("");
  }).catch(()=>{});
}
// ── Recherche dans l'historique des sessions ──
let searchTimer=null;
document.getElementById("searchBox").addEventListener("input",function(){
  clearTimeout(searchTimer);
  const q=this.value.trim();
  if(!q){loadSessions();return;}
  searchTimer=setTimeout(()=>{
    fetch("/search?q="+encodeURIComponent(q)).then(r=>r.json()).then(d=>{
      const el=document.getElementById("session-list");
      const res=d.results||[];
      if(!res.length){el.innerHTML='<div style="padding:8px;font-size:.7rem;color:var(--mu)">Aucun r&#233;sultat</div>';return;}
      const rxSafe=q.replace(/[.*+?^${}()|[\]\\]/g,"\\$&");
      el.innerHTML=res.map(s=>{
        // esc() D'ABORD, puis surlignage du terme echappe (jamais l'inverse -> injection)
        let snip=esc(s.snippet);
        snip=snip.replace(new RegExp("("+esc(rxSafe)+")","gi"),"<mark>$1</mark>");
        return '<div class="sess-item">'
          +'<span class="sess-label" onclick="openSession('+s.session_id+')">#'+s.session_id
          +' '+esc((s.ts||"").substring(5,16))+'<div class="snip">'+snip+'</div></span></div>';
      }).join("");
    }).catch(()=>{});
  },300);
});

function connect(){
  ws=new WebSocket("ws://"+location.host+"/ws");
  ws.onopen=()=>{
    ws.send(JSON.stringify({type:"init",session:INITIAL_SESSION}));
    ws.send(JSON.stringify({type:"get_projects"}));
    loadSessions();
  };
  ws.onmessage=e=>{
    const d=JSON.parse(e.data);
    if(d.type==="token"){curR+=d.text;if(curB)curB.innerHTML=fmt(curR);sc();}
    else if(d.type==="agent_start"){
      rmT();const c=AC[d.agent]||"#8b949e";setGenerating(true);
      curW=mk("div","msg agent");
      curW.innerHTML='<div class="atag" style="background:'+c+'22;color:'+c+'">&#9679; '+d.agent+'</div><div class="bbl"></div><div class="msg-time">'+nowStr()+'</div>';
      chat.appendChild(curW);curB=curW.querySelector(".bbl");curR="";sc();
    }
    else if(d.type==="speed"){
      const sp=document.getElementById("speed-chip");sp.style.display="inline-block";sp.textContent=d.tps+" tok/s";
    }
    else if(d.type==="memory"){
      const el=mk("div","mem");
      el.innerHTML='&#129504; <b>Memoire</b>'+d.items.map(m=>'<span class="mitem">'+esc(m.ref)+' ('+m.score+')</span>').join("");
      (curW||chat).appendChild(el);sc();
    }
    else if(d.type==="tool_step"){
      if(d.phase==="start"){
        rmT();
        const el=mk("div","tstep");el.dataset.stepId=d.id;
        el.innerHTML='<div class="tst-head"><div class="tspin"></div><span class="tst-label">'+esc(d.label)+'</span>'
          +'<span class="tst-arg">'+esc(d.args_preview||"")+'</span><span class="tst-status"></span></div>'
          +'<details><summary>d&#233;tails</summary><pre>'+esc(d.args_full||"")+'</pre></details>';
        chat.appendChild(el);sc();
      }else{
        const el=chat.querySelector('.tstep[data-step-id="'+d.id+'"]');
        if(el){
          el.classList.add(d.ok?"ok":"err");
          const spin=el.querySelector(".tspin");if(spin)spin.remove();
          const st=el.querySelector(".tst-status");
          if(st)st.innerHTML=(d.ok?'<span style="color:var(--gn)">&#10003;</span>':'<span style="color:var(--rd)">&#10007;</span>')
            +(d.ms!=null?' <span class="tst-ms">'+(d.ms>=1000?(d.ms/1000).toFixed(1)+"s":d.ms+"ms")+'</span>':'');
          const pre=el.querySelector("pre");
          if(pre&&d.result_preview)pre.textContent+="\n→ "+d.result_preview;
        }
        sc();
      }
    }
    else if(d.type==="sql_result"){
      const wrap=mk("div","sqlwrap");
      wrap._rows=d.rows;wrap._cols=d.columns;wrap._sortCol=-1;wrap._sortAsc=true;
      let html='<div class="sql-meta">&#128202; '+d.rows.length+' ligne(s)'+(d.truncated?' (tronqu&#233;)':'')
        +'<button class="sql-copy">Copier CSV</button></div><table><thead><tr>';
      html+=d.columns.map((c,i)=>'<th data-col="'+i+'">'+esc(c)+'</th>').join("")+'</tr></thead><tbody>';
      html+=d.rows.map(r=>'<tr>'+r.map(c=>'<td>'+esc(c==null?"":c)+'</td>').join("")+'</tr>').join("");
      html+='</tbody></table>';
      wrap.innerHTML=html;
      wrap.addEventListener("click",ev=>{
        const th=ev.target.closest("th");
        if(th){
          const col=parseInt(th.dataset.col,10);
          wrap._sortAsc=(wrap._sortCol===col)?!wrap._sortAsc:true;wrap._sortCol=col;
          const sorted=[...wrap._rows].sort((a,b)=>{
            const x=a[col],y=b[col];
            const nx=parseFloat(x),ny=parseFloat(y);
            const cmp=(!isNaN(nx)&&!isNaN(ny))?nx-ny:String(x??"").localeCompare(String(y??""));
            return wrap._sortAsc?cmp:-cmp;
          });
          wrap.querySelector("tbody").innerHTML=sorted.map(r=>'<tr>'+r.map(c=>'<td>'+esc(c==null?"":c)+'</td>').join("")+'</tr>').join("");
        }
        const cp=ev.target.closest(".sql-copy");
        if(cp){
          const q=v=>'"'+String(v==null?"":v).replace(/"/g,'""')+'"';
          const csv=[wrap._cols.map(q).join(";")].concat(wrap._rows.map(r=>r.map(q).join(";"))).join("\n");
          navigator.clipboard.writeText(csv).then(()=>{const o=cp.textContent;cp.textContent="✓ Copié";setTimeout(()=>cp.textContent=o,1200);}).catch(()=>{});
        }
      });
      chat.appendChild(wrap);sc();
    }
    else if(d.type==="stopped"){
      setGenerating(false);
      const el=mk("div","mem");el.style.color="var(--or)";el.style.background="#f973160f";el.style.borderColor="#f9731630";
      el.innerHTML="&#9209; Generation arretee par l'utilisateur";
      (curW||chat).appendChild(el);sc();
    }
    else if(d.type==="pull_progress"){
      const log=document.getElementById("pull-log");
      if(log){log.style.display="block";log.textContent+=d.line+"\n";log.scrollTop=log.scrollHeight;}
    }
    else if(d.type==="pull_done"){
      const log=document.getElementById("pull-log");
      if(log)log.textContent+=(d.ok?"\n✔ Téléchargement terminé.\n":"\n✘ Échec : "+(d.error||"")+"\n");
      const btn2=document.getElementById("pullBtn"),inp2=document.getElementById("pullInput");
      if(btn2){btn2.disabled=false;inp2.disabled=false;}
      loadModelsDetail();loadModels();
    }
    else if(d.type==="brain_think"){
      const tb=mk("div","think");
      tb.innerHTML='<div class="think-h" onclick="this.parentElement.classList.toggle(\'open\')"><span class="arr">&#9658;</span><span>&#129504; Réflexion Brain</span><span style="margin-left:auto;color:var(--mu);font-weight:400;font-size:.6rem">clic</span></div><div class="think-b">'+esc(d.text)+'</div>';
      (curW||chat).appendChild(tb);sc();
    }
    else if(d.type==="todos"){
      todos=d.items;renderTodos();
    }
    else if(d.type==="todo_done"){
      todos=todos.map((t,i)=>i===d.index?{...t,done:true}:t);renderTodos();
    }
    else if(d.type==="file_created"){
      const e2=mk("div","fcard");
      e2.innerHTML='<span>&#128196;</span><span class="fn">'+esc(d.name)+'</span><span class="fi">'+esc(d.size)+'</span>'
        +(d.path?'<button class="fbtn" onclick="openFilePreview(\''+esc(d.path).replace(/'/g,"\\'")+'\')">Aper&#231;u</button>'
                +'<a class="fbtn" style="text-decoration:none" href="/dl?p='+encodeURIComponent(d.path)+'">&#11015;</a>':'');
      (curW||chat).appendChild(e2);sc();
    }
    else if(d.type==="project_done"){
      const b=mk("div","pbox");
      b.innerHTML='<div class="ptitle">&#9989; Projet créé — '+d.count+' fichier(s)</div><div class="ppath">&#128193; '+esc(d.path)+'</div><div class="chips">'+d.files.map(f=>'<span class="fchip">'+esc(f)+'</span>').join("")+'</div>';
      (curW||chat).appendChild(b);
      addProj(d.project_name,d.path);sc();
    }
    else if(d.type==="run_result"){
      const ok=d.ok,el=mk("div",ok?"run-ok":"run-err");
      el.innerHTML='<div class="run-label">'+(ok?"&#9654; Exécuté — OK":"&#9888; Erreur d\'exécution")+(d.entry?" ("+esc(d.entry)+")":"")+'</div><div class="run-out">'+esc(d.output||"(aucune sortie)")+'</div>';
      (curW||chat).appendChild(el);sc();
    }
    else if(d.type==="iter_start"){
      const it=mk("div","iter");
      it.textContent="&#128260; Auto-correction — tentative "+d.n+"/3";
      (curW||chat).appendChild(it);sc();
    }
    else if(d.type==="exec_result"){
      const ok=d.status==="ok",el=mk("div","fcard"+(ok?"":" err"));
      el.innerHTML='<span>'+(ok?"&#10003;":"&#10007;")+'</span><span class="fn">'+esc(d.output||"OK")+'</span>';
      (curW||chat).appendChild(el);sc();
    }
    else if(d.type==="snapshot"){
      const el=mk("div","snap");
      el.innerHTML='&#128190; Sauvegarde créée &#8212; <b>#'+d.id+'</b> ('+d.files+' fichiers)'
        +'<button onclick="restoreSnap('+d.id+')">&#9100; Restaurer</button>';
      (curW||chat).appendChild(el);sc();
    }
    else if(d.type==="security"){
      const b=mk("div","secbox");
      let h='<div class="sec-title">&#128737; Sécurité &#8212; '+d.findings.length+' point(s) détecté(s)</div>';
      h+=d.findings.map(f=>'<div class="sec-item"><span class="sec-sev sev-'+f.severity+'">'+f.severity+'</span><div><div class="sec-msg">'+esc(f.message)+'</div><div class="sec-loc">'+esc(f.file)+':'+f.line+'</div></div></div>').join("");
      b.innerHTML=h;(curW||chat).appendChild(b);sc();
    }
    else if(d.type==="blocked"){
      const b=mk("div","blocked");
      b.innerHTML='<div class="blocked-t">&#9940; Exécution bloquée &#8212; code potentiellement destructeur</div>'+d.reasons.map(r=>'<div class="blocked-r">&#8226; '+esc(r)+'</div>').join("");
      (curW||chat).appendChild(b);sc();
    }
    else if(d.type==="projects"){
      projList=d.items;renderProjs();
    }
    else if(d.type==="done"){curB=null;curR="";curW=null;setGenerating(false);inp.focus();}
    else if(d.type==="session_set"){currentSid=d.sid;}
    else if(d.type==="session_new"){
      currentSid=d.sid;clearChat();loadSessions();
      const n=mk("div","msg agent");
      n.innerHTML='<div class="atag" style="background:#22c55e22;color:#22c55e">&#9679; systeme</div><div class="bbl">Nouvelle session #'+d.sid+' d&#233;marr&#233;e.</div>';
      chat.appendChild(n);sc();
    }
    else if(d.type==="session_history"){
      currentSid=d.sid;chat.innerHTML="";
      const banner=mk("div","msg agent");
      banner.innerHTML='<div class="atag" style="background:#58a6ff22;color:#58a6ff">&#9679; session</div><div class="bbl">Reprise de la session #'+d.sid+' ('+(d.messages?d.messages.length:0)+' messages).</div>';
      chat.appendChild(banner);
      (d.messages||[]).forEach(m=>{
        if(m.role==="user"){
          const w=mk("div","msg user");w.innerHTML='<div class="bbl">'+esc(m.content)+'</div>';chat.appendChild(w);
        }else{
          const c=AC[m.agent]||"#8b949e";const w=mk("div","msg agent");
          w.innerHTML='<div class="atag" style="background:'+c+'22;color:'+c+'">&#9679; '+esc(m.agent)+'</div><div class="bbl">'+fmt(m.content)+'</div>';
          chat.appendChild(w);
        }
      });
      loadSessions();sc();
    }
    else if(d.type==="thinking"){addT();}
  };
  ws.onclose=()=>setTimeout(connect,1500);
}

function renderTodos(){
  const el=document.getElementById("todo-list");
  if(!todos.length){el.innerHTML='<div style="font-size:.7rem;color:var(--mu)">En attente...</div>';return;}
  el.innerHTML=todos.map((t,i)=>'<div class="todo-item'+(t.done?" done":t.active?" active":"")+'"><div class="todo-cb'+(t.done?" done":"")+'">'+( t.done?"&#10003;":"")+'</div><span>'+esc(t.text)+'</span></div>').join("");
}
function renderProjs(){
  const el=document.getElementById("proj-list");
  if(!projList.length){el.innerHTML='<div style="padding:8px;font-size:.7rem;color:var(--mu)">Aucun projet</div>';return;}
  el.innerHTML=projList.map(p=>'<div class="proj-item" title="Clic: reprendre ce projet" data-p="'+esc(p)+'">'
    +'<span class="tree-toggle" onclick="event.stopPropagation();toggleTree(this,'+"'"+esc(p).replace(/'/g,"\\'")+"'"+')">&#9656;</span>'
    +'<span onclick="resumeProject(this.parentElement.dataset.p)" style="flex:1;cursor:pointer"><div class="proj-dot" style="display:inline-block"></div>'+esc(p)+'</span>'
    +'</div><div class="tree-holder" data-holder="'+esc(p)+'"></div>').join("");
}
function resumeProject(name){
  // Pre-remplit l'entree pour reprendre/continuer le projet (DevLLMA relit les fichiers existants)
  inp.value="Reprends le projet « "+name+" » : lis les fichiers existants dans son dossier et continue-le. ";
  inp.style.height="40px";inp.style.height=Math.min(inp.scrollHeight,140)+"px";
  inp.focus();
}
function addProj(name,path){
  if(!projList.includes(name))projList=[...projList,name];
  renderProjs();
}
// ── Arborescence workspace + visionneuse de fichiers ──
function renderTreeNodes(nodes,projName){
  return nodes.map(n=>{
    const p=projName+"/"+n.name;
    if(n.type==="dir"){
      return '<div class="tree-dir" onclick="this.nextElementSibling.style.display=this.nextElementSibling.style.display===\'none\'?\'block\':\'none\'">&#128193; '+esc(n.name)+'</div>'
        +'<div class="tree-node" style="display:none">'+renderTreeNodes(n.children||[],p)+'</div>';
    }
    return '<div class="tree-file" onclick="openFilePreview(\''+p.replace(/'/g,"\\'")+'\')">&#128196; '+esc(n.name)
      +'<span style="margin-left:auto;color:var(--mu);font-size:.6rem">'+(n.size?Math.round(n.size/1024)+" Ko":"")+'</span></div>';
  }).join("");
}
function toggleTree(btn,projName){
  const holder=document.querySelector('.tree-holder[data-holder="'+CSS.escape(projName)+'"]');
  if(!holder)return;
  if(holder.dataset.loaded==="1"){
    const open=holder.style.display!=="none";
    holder.style.display=open?"none":"block";
    btn.innerHTML=open?"&#9656;":"&#9662;";
    return;
  }
  fetch("/tree/"+encodeURIComponent(projName)).then(r=>r.json()).then(d=>{
    holder.innerHTML='<div class="tree-node">'+(renderTreeNodes(d.children||[],projName)||'<div style="color:var(--mu);font-size:.65rem;padding:2px 6px">(vide)</div>')+'</div>';
    holder.dataset.loaded="1";holder.style.display="block";
    btn.innerHTML="&#9662;";
  }).catch(()=>{holder.innerHTML='<div style="color:var(--rd);font-size:.65rem;padding:2px 6px">Erreur de chargement</div>';});
}
let currentPreviewPath=null;
function openFilePreview(path){
  currentPreviewPath=path;
  document.getElementById("fileModal").style.display="flex";
  document.getElementById("fileModalTitle").textContent="📄 "+path.split("/").pop();
  const body=document.getElementById("fileModalBody");
  body.innerHTML="Chargement...";
  fetch("/file?p="+encodeURIComponent(path)).then(r=>r.json()).then(d=>{
    if(d.kind==="document"){body.innerHTML='<pre style="white-space:pre-wrap">'+esc(d.content)+'</pre>';}
    else{body.innerHTML='<pre>'+highlight(d.content)+'</pre>';}
  }).catch(()=>{body.innerHTML='<span style="color:var(--rd)">Erreur de chargement</span>';});
}
function closeFileModal(){document.getElementById("fileModal").style.display="none";currentPreviewPath=null;}
function downloadCurrentFile(){if(currentPreviewPath)window.open("/dl?p="+encodeURIComponent(currentPreviewPath),"_blank");}

const KW=/\b(def|class|import|from|as|return|if|elif|else|for|while|try|except|finally|with|pass|break|continue|lambda|yield|async|await|True|False|None|self|function|const|let|var|new|export|default|extends|public|private|static|void|null|undefined|this|of|in|switch|case)\b/g;
const TOKEN_RE=/(#.*$|\/\/.*$)|("(?:[^"\\n]|\.)*"|'(?:[^'\\n]|\.)*')|\b(\d+\.?\d*)\b|\b(def|class|import|from|as|return|if|elif|else|for|while|try|except|finally|with|pass|break|continue|lambda|yield|async|await|True|False|None|self|function|const|let|var|new|export|default|extends|public|private|static|void|null|undefined|this|of|in|switch|case)\b/gm;
function highlight(raw){
  const escaped=esc(raw);
  return escaped.replace(TOKEN_RE,function(match,com,str,num,kw){
    if(com!==undefined)return '<span class="tok-com">'+com+'</span>';
    if(str!==undefined)return '<span class="tok-str">'+str+'</span>';
    if(num!==undefined)return '<span class="tok-num">'+num+'</span>';
    if(kw!==undefined)return '<span class="tok-kw">'+kw+'</span>';
    return match;
  });
}
let preCounter=0;
function fmt(t){
  return t.replace(/```[\w]*\n?([\s\S]*?)```/g,(_,c)=>{
        const id="pre"+(preCounter++);
        return '<div class="pre-wrap"><button class="copy-btn" data-target="'+id+'">&#128203; Copier</button><pre id="'+id+'">'+highlight(c.trim())+'</pre></div>';
      })
      .replace(/`([^`\n]+)`/g,(_,c)=>"<code>"+esc(c)+"</code>")
      .replace(/\*\*(.+?)\*\*/g,"<strong>$1</strong>")
      .replace(/^#{1,3}\s(.+)$/gm,"<strong>$1</strong>")
      .replace(/\n/g,"<br>");
}
function esc(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
function nowStr(){const d=new Date();return String(d.getHours()).padStart(2,"0")+":"+String(d.getMinutes()).padStart(2,"0");}
chat.addEventListener("click",e=>{
  const b=e.target.closest(".copy-btn");
  if(!b)return;
  const pre=document.getElementById(b.dataset.target);
  if(!pre)return;
  navigator.clipboard.writeText(pre.innerText).then(()=>{
    const old=b.innerHTML;b.innerHTML="&#10003; Copie";setTimeout(()=>b.innerHTML=old,1200);
  }).catch(()=>{});
});
chat.addEventListener("scroll",()=>{
  const nearBottom=chat.scrollHeight-chat.scrollTop-chat.clientHeight<60;
  document.getElementById("scrollBtn").style.display=nearBottom?"none":"block";
});
function mk(t,c){const e=document.createElement(t);if(c)e.className=c;return e;}
function addT(){rmT();const e=mk("div");e.id="typing";e.innerHTML='<div class="dots"><span></span><span></span><span></span></div>';chat.appendChild(e);sc();}
function rmT(){const t=document.getElementById("typing");if(t)t.remove();}
function sc(){chat.scrollTop=chat.scrollHeight;}
function send(){
  const t=inp.value.trim();if(!t||!ws||ws.readyState!==1)return;
  if(pendingDoc&&pendingDoc.loading){alert("Le document est encore en cours de lecture, patiente une seconde.");return;}
  let full=t;
  let shown=esc(t);
  if(pendingDoc){
    full="Contenu du document « "+pendingDoc.name+" » :\n"+pendingDoc.text+"\n\n"+t;
    shown='<span class="attach-chip" style="margin-bottom:4px">&#128206; '+esc(pendingDoc.name)+'</span><br>'+esc(t);
    pendingDoc=null;renderAttach();
  }
  const w=mk("div","msg user");w.innerHTML='<div class="bbl">'+shown+'</div><div class="msg-time">'+nowStr()+'</div>';chat.appendChild(w);sc();
  ws.send(JSON.stringify({type:"message",text:full}));
  inp.value="";inp.style.height="40px";
  // L'input reste ACTIF: on peut continuer a ecrire/envoyer pendant qu'une tache tourne (messages mis en file)
  todos=[];renderTodos();inp.focus();
}
function chgM(m){if(ws&&ws.readyState===1)ws.send(JSON.stringify({type:"model",model:m}));}
let generating=false;
function setGenerating(v){
  generating=v;
  document.getElementById("stopBtn").style.display=v?"inline-block":"none";
  if(!v)document.getElementById("speed-chip").style.display="none";
}
function stopGen(){if(ws&&ws.readyState===1)ws.send(JSON.stringify({type:"stop"}));}
function regenLast(){if(ws&&ws.readyState===1)ws.send(JSON.stringify({type:"regenerate"}));}
function exportSession(){if(currentSid)window.open("/export/"+currentSid,"_blank");else alert("Aucune session active");}
function toggleTheme(){
  document.body.classList.toggle("light");
  localStorage.setItem("devllma_theme",document.body.classList.contains("light")?"light":"dark");
}
function restoreSnap(id){
  if(!confirm("Restaurer ce snapshot ? Les fichiers actuels du projet seront ecrases."))return;
  fetch("/restore/"+id,{method:"POST"}).then(r=>r.json()).then(d=>{alert(d.msg||(d.ok?"Restaure":"Echec"));})
    .catch(()=>alert("Erreur reseau"));
}
if(localStorage.getItem("devllma_theme")==="light")document.body.classList.add("light");
function loadModels(){
  fetch("/models").then(r=>r.json()).then(d=>{
    const sel=document.getElementById("modelSel");
    if(!d.models||!d.models.length){sel.innerHTML='<option>aucun modèle</option>';return;}
    sel.innerHTML=d.models.map(m=>'<option value="'+m.name+'"'+(m.active?' selected':'')+'>'+esc(m.label)+'</option>').join("");
  }).catch(()=>{});
}
loadModels();
// ── Panneau de gestion des modeles ──
function openModelsModal(){
  document.getElementById("modelsModal").style.display="flex";
  loadModelsDetail();loadBenchResults();
}
function closeModelsModal(){document.getElementById("modelsModal").style.display="none";}
function loadModelsDetail(){
  fetch("/models_detail").then(r=>r.json()).then(d=>{
    const el=document.getElementById("models-table");
    const list=d.models||[];
    el.innerHTML=list.length?list.map(m=>
      '<div class="model-row"><span class="mn'+(m.active?" mstar":"")+'">'+(m.active?"&#9733; ":"")+esc(m.label)+'</span><span class="ms">'+m.size_gb+' Go</span></div>'
    ).join(""):'<div style="color:var(--mu);font-size:.72rem">Aucun modele installe</div>';
  }).catch(()=>{});
}
function loadBenchResults(){
  fetch("/bench_results").then(r=>r.json()).then(d=>{
    const el=document.getElementById("bench-table");
    const rows=[];
    const results=d.results||{};
    for(const model in results){
      for(const task in results[model]){
        const res=results[model][task];
        if(res.error){rows.push('<div class="bench-row"><span class="bn">'+esc(model)+' / '+esc(task)+'</span><span>erreur</span></div>');continue;}
        rows.push('<div class="bench-row"><span class="bn">'+esc(model)+' / '+esc(task)+'</span><span>'+(res.ok?"&#9989;":"&#10060;")+' '+res.decode_tok_s+' tok/s</span></div>');
      }
    }
    el.innerHTML=rows.length?rows.join(""):'<div style="color:var(--mu);font-size:.72rem">Aucun benchmark enregistre</div>';
  }).catch(()=>{});
}
// ── Panneau memoire semantique ──
const MEM_COLORS={qa:"#0ea5e9",project:"#3fb950",lesson:"#ef4444",system:"#8b5cf6",knowledge:"#f59e0b",note:"#8b949e"};
function openMemModal(){
  document.getElementById("memModal").style.display="flex";
  document.getElementById("memTestInput").value="";
  document.getElementById("memTestResult").innerHTML="";
  loadMemTable();
}
function closeMemModal(){document.getElementById("memModal").style.display="none";}
function renderMemRow(m,searchMode){
  const c=MEM_COLORS[m.kind]||"#8b949e";
  return '<div class="mem-row"><span class="mk" style="background:'+c+'22;color:'+c+'">'+esc(m.kind)+'</span>'
    +'<div class="mc"><span class="mref">'+esc(m.ref_name||m.ref||"")+(searchMode?'<span class="mem-score">score '+m.score+'</span>':'')+'</span>'+esc(m.chunk)+'</div>'
    +(searchMode?'':'<button class="mdel" title="Supprimer" onclick="deleteMemory('+m.id+')">&times;</button>')+'</div>';
}
function loadMemTable(){
  const el=document.getElementById("mem-table");
  fetch("/memories").then(r=>r.json()).then(d=>{
    const items=d.items||[];
    el.innerHTML=items.length?items.map(m=>renderMemRow(m,false)).join("")
      :'<div style="color:var(--mu);font-size:.72rem">Aucun souvenir enregistre</div>';
  }).catch(()=>{el.innerHTML='<div style="color:var(--rd);font-size:.72rem">Erreur de chargement</div>';});
}
function testMemory(){
  const q=document.getElementById("memTestInput").value.trim();
  const el=document.getElementById("memTestResult");
  if(!q){el.innerHTML="";return;}
  el.innerHTML='<div style="color:var(--mu);font-size:.72rem">Recherche...</div>';
  fetch("/memories?q="+encodeURIComponent(q)).then(r=>r.json()).then(d=>{
    const items=d.items||[];
    el.innerHTML=items.length?items.map(m=>renderMemRow(m,true)).join("")
      :'<div style="color:var(--mu);font-size:.72rem">Aucun souvenir associe (l\'agent repondrait sans rappel memoire)</div>';
  }).catch(()=>{el.innerHTML='<div style="color:var(--rd);font-size:.72rem">Erreur reseau</div>';});
}
function deleteMemory(id){
  fetch("/memories/"+id,{method:"DELETE"}).then(()=>loadMemTable()).catch(()=>{});
}
function purgeMemories(){
  if(!confirm("Supprimer TOUS les souvenirs enregistres ? Cette action est irreversible."))return;
  fetch("/memories",{method:"DELETE"}).then(()=>loadMemTable()).catch(()=>{});
}
function pullModel(){
  const inp2=document.getElementById("pullInput"),btn2=document.getElementById("pullBtn");
  const name=inp2.value.trim();
  if(!name||!ws||ws.readyState!==1)return;
  const log=document.getElementById("pull-log");
  log.style.display="block";log.textContent="Démarrage du téléchargement de "+name+"...\n";
  btn2.disabled=true;inp2.disabled=true;
  ws.send(JSON.stringify({type:"pull_model",model:name}));
}
// ── Bibliotheque de prompts (menu slash) ──
const DEFAULT_TEMPLATES=[
  {name:"resume-doc",text:"Résume ce document en 10 points clés : "},
  {name:"explique-erreur",text:"Explique cette erreur et propose la correction : "},
  {name:"cree-projet",text:"Crée une application complète qui "},
  {name:"verifie-excel",text:"Analyse ce tableau Excel et signale les anomalies : "},
  {name:"traduis",text:"Traduis ce texte en anglais professionnel : "},
  {name:"recherche",text:"Fais une recherche web et donne-moi une synthèse sourcée sur : "}
];
function getTemplates(){
  try{const t=JSON.parse(localStorage.getItem("devllma_templates"));if(Array.isArray(t)&&t.length)return t;}catch(_){}
  return DEFAULT_TEMPLATES.slice();
}
let slashSel=0;
function renderSlash(filter){
  const menu=document.getElementById("slashMenu");
  const items=getTemplates().filter(t=>!filter||t.name.toLowerCase().includes(filter)||t.text.toLowerCase().includes(filter));
  if(!items.length){menu.style.display="none";return;}
  slashSel=Math.min(slashSel,items.length-1);
  menu.innerHTML=items.map((t,i)=>'<div class="slash-item'+(i===slashSel?" selected":"")+'" data-i="'+i+'"><b>/'+esc(t.name)+'</b><span>'+esc(t.text)+'</span></div>').join("");
  menu.style.display="block";
  menu._items=items;
  menu.querySelectorAll(".slash-item").forEach(el=>{
    el.onclick=()=>applyTemplate(items[parseInt(el.dataset.i,10)]);
  });
}
function applyTemplate(t){
  if(!t)return;
  inp.value=t.text;
  document.getElementById("slashMenu").style.display="none";
  inp.focus();inp.setSelectionRange(inp.value.length,inp.value.length);
  inp.style.height="40px";inp.style.height=Math.min(inp.scrollHeight,140)+"px";
}
function saveTemplate(){
  const t=inp.value.trim();
  if(!t){alert("Écris d'abord le texte du modèle dans la zone de saisie.");return;}
  const name=window.prompt("Nom du modèle (sans espace) :");
  if(!name)return;
  const list=getTemplates();
  list.push({name:name.replace(/\s+/g,"-").toLowerCase(),text:t});
  localStorage.setItem("devllma_templates",JSON.stringify(list));
  alert("Modèle « /"+name+" » enregistré. Tape / pour le retrouver.");
}
function onInp(){
  const v=inp.value;
  if(v.startsWith("/")&&!v.includes("\n")){slashSel=0;renderSlash(v.slice(1).toLowerCase());}
  else document.getElementById("slashMenu").style.display="none";
}
function onK(e){
  const menu=document.getElementById("slashMenu");
  if(menu.style.display==="block"&&menu._items){
    if(e.key==="ArrowDown"){e.preventDefault();slashSel=Math.min(slashSel+1,menu._items.length-1);renderSlash(inp.value.slice(1).toLowerCase());return;}
    if(e.key==="ArrowUp"){e.preventDefault();slashSel=Math.max(slashSel-1,0);renderSlash(inp.value.slice(1).toLowerCase());return;}
    if(e.key==="Enter"){e.preventDefault();applyTemplate(menu._items[slashSel]);return;}
    if(e.key==="Escape"){menu.style.display="none";return;}
  }
  if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();send();}
  setTimeout(()=>{inp.style.height="40px";inp.style.height=Math.min(inp.scrollHeight,140)+"px";},0);
}
// ── Usage ressources temps réel ──
function colorFor(v){return v<60?"var(--gn)":(v<85?"#f59e0b":"var(--rd)");}
function pollStats(){
  fetch("/stats",{cache:"no-store"}).then(r=>r.json()).then(d=>{
    if(d.cpu!=null){const f=document.getElementById("cpu-fill");f.style.width=d.cpu+"%";f.style.background=colorFor(d.cpu);document.getElementById("cpu-v").textContent=d.cpu+"%";}
    if(d.ram_pct!=null){const f=document.getElementById("ram-fill");f.style.width=d.ram_pct+"%";f.style.background=colorFor(d.ram_pct);document.getElementById("ram-v").textContent=d.ram_pct+"% ("+d.ram_used+"/"+d.ram_total+"G)";}
    const tv=document.getElementById("temp-v");
    if(d.temp!=null){tv.textContent=d.temp+"°C";tv.style.color=d.temp<60?"var(--gn)":(d.temp<80?"#f59e0b":"var(--rd)");}
    else{tv.textContent="N/A";}
    if(d.memories!=null){document.getElementById("mem-count").innerHTML="&#129504; "+d.memories+" souvenirs";}
  }).catch(()=>{});
}
setInterval(pollStats,2000);pollStats();
// ── Pieces jointes : images (OCR) et documents Word/Excel/PDF ──
let pendingDoc=null; // {name, text}
function renderAttach(){
  const bar=document.getElementById("attach-bar");
  if(!pendingDoc){bar.style.display="none";bar.innerHTML="";return;}
  bar.style.display="block";
  bar.innerHTML='<span class="attach-chip">&#128206; '+esc(pendingDoc.name)
    +' <span style="color:var(--mu)">('+Math.round(pendingDoc.text.length/1000)+' k car.'
    +(pendingDoc.truncated?", tronqu&#233;":"")+')</span>'
    +' <span class="ac-x" onclick="pendingDoc=null;renderAttach()" title="Retirer">&times;</span></span>';
}
function ocrFile(f){
  if(!f)return;
  const fd=new FormData();fd.append("file",f);
  const prev=inp.value;inp.value="(lecture de l'image en cours…)";
  fetch("/ocr",{method:"POST",body:fd}).then(r=>r.json()).then(d=>{
    inp.value="Analyse ce retour / cette erreur (texte lu dans l'image) :\n"+(d.text||"(rien détecté)")+"\n\n";
    inp.style.height="40px";inp.style.height=Math.min(inp.scrollHeight,140)+"px";inp.focus();
  }).catch(()=>{inp.value=prev;});
}
function docFile(f){
  const fd=new FormData();fd.append("file",f);
  pendingDoc={name:f.name,text:"",truncated:false,loading:true};
  const bar=document.getElementById("attach-bar");
  bar.style.display="block";
  bar.innerHTML='<span class="attach-chip">&#8987; lecture de '+esc(f.name)+'…</span>';
  fetch("/upload_doc",{method:"POST",body:fd}).then(r=>r.json()).then(d=>{
    if(d.error){pendingDoc=null;renderAttach();alert("Document illisible : "+d.error);return;}
    pendingDoc={name:d.name,text:d.text,truncated:d.truncated};
    renderAttach();inp.focus();
  }).catch(()=>{pendingDoc=null;renderAttach();alert("Erreur réseau pendant la lecture du document");});
}
function handleFile(f){
  if(!f)return;
  const n=(f.name||"").toLowerCase();
  if(n.endsWith(".docx")||n.endsWith(".xlsx")||n.endsWith(".pdf")){docFile(f);}
  else if(f.type&&f.type.indexOf("image")===0){ocrFile(f);}
  else alert("Format non pris en charge : joins une image ou un document .docx/.xlsx/.pdf");
}
document.addEventListener("paste",e=>{
  const items=e.clipboardData&&e.clipboardData.items;if(!items)return;
  for(const it of items){if(it.type&&it.type.indexOf("image")===0){ocrFile(it.getAsFile());e.preventDefault();return;}}
});
document.addEventListener("dragover",e=>{e.preventDefault();document.body.classList.add("dragging");});
document.addEventListener("dragleave",e=>{document.body.classList.remove("dragging");});
document.addEventListener("drop",e=>{
  document.body.classList.remove("dragging");
  const f=e.dataTransfer&&e.dataTransfer.files&&e.dataTransfer.files[0];
  if(f){handleFile(f);e.preventDefault();}
});
connect();
</script>
</body>
</html>"""


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
    try:
        r = requests.get(OLLAMA+"/api/tags", timeout=8)
        names = [m["name"] for m in r.json().get("models", [])]
    except Exception:
        names = []
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
    try:
        r = requests.get(OLLAMA+"/api/tags", timeout=8)
        models_raw = r.json().get("models", [])
    except Exception:
        models_raw = []
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

@app.get("/bench_results")
async def bench_results():
    """Derniers resultats du banc d'essai comparatif (bench_models.py), si disponibles."""
    if os.path.exists(BENCH_RESULTS_PATH):
        try:
            return {"results": json.load(open(BENCH_RESULTS_PATH, encoding="utf-8"))}
        except Exception:
            pass
    return {"results": {}}

def _strip_ansi(s):
    return re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]', '', s)

def _pull_worker(model_name, out_q):
    try:
        proc = subprocess.Popen(
            ["ollama", "pull", model_name],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1
        )
        for line in proc.stdout:
            clean = _strip_ansi(line).strip()
            if clean:
                out_q.put(("line", clean))
        proc.wait()
        out_q.put(("end", proc.returncode))
    except Exception as e:
        out_q.put(("error", str(e)))

async def handle_pull_model(websocket, model_name):
    """Telecharge un modele Ollama en arriere-plan, en relayant la progression au navigateur."""
    await websocket.send_json({"type": "pull_progress", "line": f"Démarrage : ollama pull {model_name}"})
    loop = asyncio.get_event_loop()
    out_q = _queue.Queue()
    th = threading.Thread(target=_pull_worker, args=(model_name, out_q), daemon=True)
    th.start()
    last_sent = 0.0
    try:
        while True:
            try:
                kind, payload = await loop.run_in_executor(None, out_q.get, True, 0.3)
            except _queue.Empty:
                continue
            if kind == "line":
                now = time.time()
                if now - last_sent > 0.5:
                    await websocket.send_json({"type": "pull_progress", "line": payload})
                    last_sent = now
            elif kind == "end":
                await websocket.send_json({"type": "pull_done", "model": model_name, "ok": payload == 0})
                break
            elif kind == "error":
                await websocket.send_json({"type": "pull_done", "model": model_name, "ok": False, "error": payload})
                break
    finally:
        th.join(timeout=1)

try:
    import psutil
    psutil.cpu_percent(interval=None)  # amorce la mesure
except Exception:
    psutil = None

_temp_cache = {"v": None, "t": 0.0, "history": [], "unreliable": False, "last_check": 0.0}
def get_temp():
    """Température (°C) via WMI MSAcpi_ThermalZoneTemperature, mise en cache 5s.
    Sur beaucoup de cartes meres desktop, cette zone ACPI n'est pas reellement
    cablee et renvoie une valeur figee (constate sur ce poste: 27.9°C en boucle,
    identique a 0% comme a 4% de charge CPU). Plutot que d'afficher un chiffre
    trompeur, on detecte le gel (6 lectures fraiches consecutives ~30s identiques)
    et on bascule sur N/A — avec une nouvelle tentative toutes les 5 min au cas
    ou le capteur redevienne fonctionnel (redemarrage, pilote different...)."""
    now = time.time()
    if _temp_cache["v"] is not None and now - _temp_cache["t"] < 5:
        return _temp_cache["v"]
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
async def stats_endpoint():
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
async def get_snapshots(project: str):
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
async def get_tree(project: str):
    """Arborescence complete d'un projet du workspace, pour la visionneuse sidebar."""
    full = _safe_workspace_path(project)
    if not full or not os.path.isdir(full):
        return Response(status_code=404)
    return {"name": project, "children": _build_tree(full)}

@app.get("/file")
async def get_file(p: str = ""):
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
async def get_memories(kind: str = "", q: str = "", offset: int = 0):
    """Liste des souvenirs, ou test de recherche semantique si q est fourni
    (score abaisse a 0.3 pour le mode 'test' — but different de la vraie injection)."""
    if q.strip():
        hits = mem_search(q, 20, kind=kind or None, min_score=0.3)
        return {"mode": "search", "items": hits}
    data = mem_list(kind=kind or None, limit=50, offset=offset)
    return {"mode": "list", **data}

@app.delete("/memories/{mem_id}")
async def del_memory(mem_id: int):
    mem_delete(mem_id)
    return {"ok": True}

@app.delete("/memories")
async def purge_memories(kind: str = ""):
    mem_purge(kind or None)
    return {"ok": True}

@app.post("/restore/{snapshot_id}")
async def restore_snapshot(snapshot_id: int):
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
async def export_session(sid: int):
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
        txt = read_image_text(p)
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
        txt = read_document(p)
        truncated = len(txt) > 15000
        if truncated:
            txt = txt[:15000] + "\n[... document tronqué ...]"
        return {"name": file.filename, "text": txt, "truncated": truncated}
    except Exception as e:
        return {"error": f"lecture du document impossible : {e}"}

@app.get("/search")
async def search_sessions(q: str = ""):
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
async def sessions():
    """Liste les sessions récentes (pour reprendre une session)."""
    try:
        rows = list_sessions()  # (id, title, created_at)
        return {"sessions": [{"id": r[0], "title": r[1], "created_at": r[2]} for r in rows]}
    except Exception:
        return {"sessions": []}

@app.delete("/sessions/{sid}")
async def remove_session(sid: int):
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

async def handle_agent(websocket, sid_box, prompt, cancel_event):
    """Agent generaliste a outils (type Claude Code) : gere tout ce qui n'est pas une grosse
    tache de generation de projet (questions, recherche, actions systeme, fichiers ponctuels,
    memoire) en decidant lui-meme, etape par etape, quel outil utiliser. Remplace les anciens
    chemins figes handle_chat/handle_research/action-systeme."""
    sid = sid_box["sid"]
    hist = history(sid, 6)
    history_text = "\n".join(f"[{h[0].upper()}]: {h[2][:150]}" for h in hist) if hist else ""

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

async def handle_prompt(websocket, sid_box, prompt, cancel_event):
    sid = sid_box["sid"]
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
    dev_signal = (has_dev_keywords(prompt) or match_existing_project(prompt) or is_edit(prompt)) \
                 and not is_file_or_doc_action(prompt) and not is_doc_message and not is_question
    if not dev_signal:
        await handle_agent(websocket, sid_box, prompt, cancel_event)
        return

    # ── Charger mémoire brain ─────────────────────────────────────────
    load_workspace_memory()
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

    # ── Phase 0 : Mémoire sémantique — souvenirs pertinents (RAG) ────────
    memories = await asyncio.get_event_loop().run_in_executor(None, lambda: mem_search(prompt, 3))
    mem_note = ""
    if memories:
        mem_note = "\n\nSOUVENIRS PERTINENTS (projets/taches passees proches de cette demande):\n" + "\n".join(
            f"- [{m['kind']}] {m['ref_name']}: {m['chunk'][:200]}" for m in memories)
        await websocket.send_json({"type":"memory","items":[{"ref":m["ref_name"],"score":m["score"]} for m in memories]})

    # ── Phase 1 : Brain pense et planifie ────────────────────────────
    await websocket.send_json({"type":"thinking"})
    plan = await asyncio.get_event_loop().run_in_executor(None, call_brain, prompt + mem_note)
    if cancel_event.is_set():
        await websocket.send_json({"type":"stopped"}); await websocket.send_json({"type":"done"}); return
    await websocket.send_json({"type":"agent_start","agent":"brain"})
    await websocket.send_json({"type":"brain_think","text":plan})

    # Extraire et envoyer les todos
    todos = parse_todos(plan)
    if todos:
        todos[0]["active"] = True
        await websocket.send_json({"type":"todos","items":todos})

    # ── Phase 2 : Lire le code existant si c'est une modif/reprise ───
    existing = {}
    if editing and os.path.isdir(project_dir):
        existing = read_project(project_dir)
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
        f"DEMANDE: {prompt}{ctx_note}{mem_note}\n\n"
        f"Projet: C:\\Devllma\\workspace\\{project_name}\\\n"
        f"Produis chaque fichier au format strict:\n###FILE: nom.ext\n<code>\n###ENDFILE\n"
        f"{consigne}"
    )
    code_resp = await stream_agent(websocket, agent_name, enriched, cancel_event=cancel_event)
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
            snap_id, snap_n = SnapshotManager.snapshot(project_dir, label="avant-modif")
            if snap_id:
                await websocket.send_json({"type":"snapshot","id":snap_id,"files":snap_n})

        created = write_files(project_dir, files)
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
        load_workspace_memory()

        # ── Pre-verification syntaxe (rapide) avant d'installer/executer ────
        # Evite de gaspiller un cycle complet d'install+execution sur du code qui ne compile meme pas.
        syntax_errs = await asyncio.get_event_loop().run_in_executor(None, syntax_check, project_dir)
        if syntax_errs and not cancel_event.is_set():
            await websocket.send_json({"type":"file_created","name":"⚠ Erreur de syntaxe — correction rapide","size":""})
            fix_p = ("ERREUR DE SYNTAXE (py_compile):\n" + "\n".join(syntax_errs) +
                     f"\n\nCODE ACTUEL:\n{format_context(read_project(project_dir))}\n\n"
                     f"Corrige la syntaxe. Format strict:\n###FILE: nom.ext\n<code>\n###ENDFILE")
            fix_resp = await stream_agent(websocket, agent_name, fix_p, CODER_FIX_SYSTEM, cancel_event=cancel_event)
            fixed = extract_files(fix_resp)
            if fixed:
                write_files(project_dir, fixed)

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
                    cur_files = read_project(project_dir)
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

                    # Brain analyse
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

                    # Agent corrige
                    fix_p = (f"ERREUR PRECISE A CORRIGER: {clean_err}{escalate_note}\n\n"
                             f"ANALYSE:\n{analysis}\n\nTRACE COMPLETE:\n{run_out}\n\n"
                             f"CODE ACTUEL:\n{format_context(cur_files)}\n\n"
                             f"Corrige. Format strict:\n###FILE: nom.ext\n<code>\n###ENDFILE")
                    fix_resp = await stream_agent(websocket, agent_name, fix_p, CODER_FIX_SYSTEM,
                                                  cancel_event=cancel_event, temperature=fix_temp)
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
                    if applied: write_files(project_dir, applied)

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

    # Envoyer liste projets mise à jour
    load_workspace_memory()
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
                load_workspace_memory()
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
                # Précharger le nouveau modèle en arrière-plan
                try:
                    requests.post(OLLAMA+"/api/generate",
                                  json={"model":model,"prompt":"","keep_alive":KEEP_ALIVE}, timeout=5)
                except Exception: pass
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
        worker_task.cancel()

def _startup_warmup():
    """Precharge le modele PUIS le cache KV du prefixe de l'agent (systeme+outils).
    Sans ce prechauffage, la premiere demande apres un redemarrage paie ~6 min
    d'evaluation de prompt a froid sur ce CPU (mesure) et semble en panne."""
    preload_models()
    from agent_core import warm_agent_cache
    warm_agent_cache()

if __name__ == "__main__":
    import threading
    threading.Thread(target=_startup_warmup, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")
