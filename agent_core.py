"""
Agent generaliste "type Claude Code" pour DevLLMA.

Boucle agentique a outils (ReAct) : le modele recoit la demande de l'utilisateur
(dev ou non-dev, peu importe) et decide LUI-MEME, etape par etape, quels outils
utiliser (lire/ecrire un fichier, executer une commande, chercher sur le web,
consulter/ecrire sa memoire long terme) jusqu'a resoudre la tache ou repondre
directement. Remplace les anciens chemins figes handle_chat/handle_research/
action-systeme de webui.py par un chemin unique, plus flexible.

Le pipeline de generation de projet multi-fichiers (plan -> code -> ecriture ->
scan securite -> execution -> auto-correction -> commit) N'EST PAS remplace :
il reste le chemin dedie pour les grosses taches de dev (deja durci et teste),
declenche par le meme routage qu'avant (has_dev_keywords / is_edit / projet
existant). Cet agent gere tout le reste : questions, recherche, actions systeme,
lecture/edition ponctuelle de fichiers, taches "de la vie de tous les jours".
"""
import os, re, sys, ast, json, time, threading, difflib, fnmatch, subprocess, requests
from datetime import datetime

from agents import OLLAMA
from skills import safety_check
from tools import web_search as _web_search
from db import mem_search, mem_index
from documents import is_office_doc, read_document, write_document

_http = requests.Session()  # connexion reutilisee (keep-alive) pour les appels Ollama

AGENT_MODEL = "qwen3-coder:30b"
# -1 = modele garde en RAM en permanence : l'evaluation du prompt (19 outils +
# consignes ~4700 tokens) prend ~6 min A FROID sur ce CPU contre ~4s avec le
# cache KV chaud — un dechargement apres 30 min d'inactivite rendait la
# premiere reponse suivante inutilisable.
KEEP_ALIVE = -1
MAX_STEPS = 8

# Chemins REELS resolus dynamiquement (meme process/compte que le service DevLLMA) —
# sans ca, le modele hallucine des chemins generiques ("C:\Users\Utilisateur\...")
# qui n'existent pas sur ce poste : write_file y ecrit quand meme (creation des
# dossiers manquants) et le modele rapporte un succes alors que le fichier a
# atterri au mauvais endroit (faux succes, cf. HANDOFF.md).
HOME_DIR = os.path.expanduser("~")
DESKTOP_DIR = os.path.join(HOME_DIR, "Desktop")
WORKSPACE_DIR = r"C:\Devllma\workspace"

# Dossiers systeme sensibles proteges meme si l'utilisateur a donne carte blanche
# sur le reste du disque (evite qu'une hallucination du modele casse l'OS lui-meme).
_DENY_PREFIXES = [
    r"c:\windows", r"c:\program files", r"c:\program files (x86)",
    r"c:\programdata", r"c:\$recycle.bin", r"c:\system volume information",
]

def _guard_path(path):
    ap = os.path.abspath(os.path.expandvars(path))
    low = ap.lower()
    for d in _DENY_PREFIXES:
        if low.startswith(d):
            raise PermissionError(f"accès refusé — dossier système protégé : {ap}")
    return ap

# NB : descriptions volontairement compactes — chaque token du prefixe systeme+outils
# coute cher en evaluation de prompt a froid sur ce CPU (~14 tok/s, mesure).
TOOLS = [
    {"type":"function","function":{
        "name":"read_file",
        "description":"Lit un fichier (texte/code, et docx/xlsx/pdf dont le texte est extrait).",
        "parameters":{"type":"object","properties":{
            "path":{"type":"string","description":"chemin complet"}
        },"required":["path"]}
    }},
    {"type":"function","function":{
        "name":"write_file",
        "description":"Cree ou remplace un fichier entier (texte/code, ou docx/xlsx/pdf generes depuis le texte ; xlsx: lignes '### Feuille: Nom' puis cellules separees par tabulation). Pour MODIFIER un fichier existant, utiliser edit_file.",
        "parameters":{"type":"object","properties":{
            "path":{"type":"string"},
            "content":{"type":"string"}
        },"required":["path","content"]}
    }},
    {"type":"function","function":{
        "name":"list_dir",
        "description":"Liste le contenu d'UN dossier.",
        "parameters":{"type":"object","properties":{
            "path":{"type":"string"}
        },"required":["path"]}
    }},
    {"type":"function","function":{
        "name":"run_powershell",
        "description":"Execute une commande PowerShell.",
        "parameters":{"type":"object","properties":{
            "command":{"type":"string"},
            "timeout":{"type":"integer","description":"s, defaut 25"}
        },"required":["command"]}
    }},
    {"type":"function","function":{
        "name":"web_search",
        "description":"Recherche web (extraits courts).",
        "parameters":{"type":"object","properties":{
            "query":{"type":"string"}
        },"required":["query"]}
    }},
    {"type":"function","function":{
        "name":"memory_search",
        "description":"Cherche dans la memoire long terme (projets/notes passes).",
        "parameters":{"type":"object","properties":{
            "query":{"type":"string"}
        },"required":["query"]}
    }},
    {"type":"function","function":{
        "name":"get_datetime",
        "description":"Date et heure REELLES du poste. Obligatoire pour toute reponse liee a l'heure/date du jour.",
        "parameters":{"type":"object","properties":{}}
    }},
    {"type":"function","function":{
        "name":"memory_save",
        "description":"Memorise une information pour les prochaines conversations.",
        "parameters":{"type":"object","properties":{
            "title":{"type":"string"},
            "content":{"type":"string"}
        },"required":["title","content"]}
    }},
    {"type":"function","function":{
        "name":"fetch_url",
        "description":"Telecharge UNE page web (URL connue) et en extrait le texte complet.",
        "parameters":{"type":"object","properties":{
            "url":{"type":"string"}
        },"required":["url"]}
    }},
    {"type":"function","function":{
        "name":"execute_python",
        "description":"Execute un court script Python et renvoie ses print(). Pour tout calcul precis.",
        "parameters":{"type":"object","properties":{
            "code":{"type":"string"}
        },"required":["code"]}
    }},
    {"type":"function","function":{
        "name":"run_sql",
        "description":"Requete SQL sur SQLite (database=chemin .db) ou SQL Server via ODBC (database=nom, server, user/password ou auth Windows si omis).",
        "parameters":{"type":"object","properties":{
            "engine":{"type":"string","enum":["sqlite","mssql"]},
            "query":{"type":"string"},
            "database":{"type":"string"},
            "server":{"type":"string"},
            "user":{"type":"string"},
            "password":{"type":"string"}
        },"required":["engine","query","database"]}
    }},
    {"type":"function","function":{
        "name":"edit_file",
        "description":"Modifie un fichier existant : remplace old_string (texte exact, assez long pour etre unique) par new_string. TOUJOURS preferer a write_file pour une modification. Erreur explicite si 0 ou plusieurs occurrences.",
        "parameters":{"type":"object","properties":{
            "path":{"type":"string"},
            "old_string":{"type":"string","description":"texte exact actuel"},
            "new_string":{"type":"string"},
            "replace_all":{"type":"boolean","description":"defaut false"}
        },"required":["path","old_string","new_string"]}
    }},
    {"type":"function","function":{
        "name":"grep_search",
        "description":"Cherche un texte/regex DANS les fichiers d'un dossier (recursif). Renvoie fichier+ligne.",
        "parameters":{"type":"object","properties":{
            "pattern":{"type":"string"},
            "root":{"type":"string","description":"defaut workspace"},
            "glob":{"type":"string","description":"ex *.py"},
            "ignore_case":{"type":"boolean","description":"defaut true"}
        },"required":["pattern"]}
    }},
    {"type":"function","function":{
        "name":"find_files",
        "description":"Retrouve des fichiers par NOM (recursif, du plus recent au plus ancien) quand le chemin est inconnu.",
        "parameters":{"type":"object","properties":{
            "pattern":{"type":"string","description":"ex *facture*.pdf"},
            "root":{"type":"string","description":"defaut dossier utilisateur"}
        },"required":["pattern"]}
    }},
    {"type":"function","function":{
        "name":"read_lines",
        "description":"Lit une plage de lignes numerotees d'un fichier texte (gros fichiers, preparation d'edit_file). outline=true sur un .py : plan classes/fonctions.",
        "parameters":{"type":"object","properties":{
            "path":{"type":"string"},
            "start_line":{"type":"integer"},
            "count":{"type":"integer","description":"defaut 120, max 250"},
            "outline":{"type":"boolean"}
        },"required":["path"]}
    }},
    {"type":"function","function":{
        "name":"read_image",
        "description":"Lit le texte d'une image (.png/.jpg, capture d'ecran...) par OCR local.",
        "parameters":{"type":"object","properties":{
            "path":{"type":"string"}
        },"required":["path"]}
    }},
    {"type":"function","function":{
        "name":"http_request",
        "description":"Appel d'API REST/JSON (GET/POST/PUT/PATCH/DELETE, headers, corps JSON), y compris http://localhost.",
        "parameters":{"type":"object","properties":{
            "url":{"type":"string"},
            "method":{"type":"string","enum":["GET","POST","PUT","PATCH","DELETE"]},
            "query_params":{"type":"object"},
            "json_body":{"type":"string"},
            "headers":{"type":"object"},
            "timeout":{"type":"integer","description":"defaut 15, max 30"}
        },"required":["url"]}
    }},
    {"type":"function","function":{
        "name":"csv_analyze",
        "description":"Analyse un CSV/TSV : colonnes, types, stats, valeurs frequentes. A utiliser en PREMIER sur un fichier de donnees.",
        "parameters":{"type":"object","properties":{
            "path":{"type":"string"},
            "delimiter":{"type":"string"},
            "max_rows":{"type":"integer","description":"defaut 5000"},
            "encoding":{"type":"string"}
        },"required":["path"]}
    }},
    {"type":"function","function":{
        "name":"open_path",
        "description":"Ouvre un fichier/dossier (application Windows par defaut) ou une URL http(s). Refuse les executables. Pour montrer le resultat a l'utilisateur.",
        "parameters":{"type":"object","properties":{
            "target":{"type":"string"}
        },"required":["target"]}
    }},
]

def _tool_read_file(args):
    try:
        path = _guard_path(args["path"])
    except PermissionError as e:
        return {"error": str(e)}
    if not os.path.isfile(path):
        return {"error": f"fichier introuvable : {path}"}
    try:
        if is_office_doc(path):
            content = read_document(path)
        else:
            content = open(path, encoding="utf-8", errors="replace").read()
    except Exception as e:
        return {"error": f"lecture impossible : {e}"}
    truncated = len(content) > 8000
    return {"content": content[:8000], "truncated": truncated, "path": path}

def _tool_write_file(args):
    try:
        path = _guard_path(args["path"])
    except PermissionError as e:
        return {"error": str(e)}
    content = args.get("content", "")
    safe, reasons = safety_check(content)
    if not safe:
        return {"error": f"écriture bloquée par sécurité : {reasons}"}
    parent = os.path.dirname(path) or "."
    # Ne JAMAIS creer silencieusement un dossier parent manquant en dehors du
    # workspace de projet : c'est exactement ce qui transforme un chemin
    # hallucine par le modele (mauvais nom de compte, faute de frappe...) en un
    # "succes" au mauvais endroit au lieu d'une erreur visible.
    if not os.path.isdir(parent) and not parent.lower().startswith(WORKSPACE_DIR.lower()):
        return {"error": f"le dossier parent n'existe pas : {parent} — vérifie le chemin exact "
                          f"(list_dir peut aider) ou crée-le explicitement via run_powershell si voulu"}
    try:
        if is_office_doc(path):
            write_document(path, content)
        else:
            os.makedirs(parent, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
    except Exception as e:
        return {"error": f"écriture impossible : {e}"}
    _audit_write_if_outside_workspace("write_file", path)
    return {"ok": True, "path": path, "bytes": len(content)}

def _tool_list_dir(args):
    try:
        path = _guard_path(args.get("path") or r"C:\Devllma\workspace")
    except PermissionError as e:
        return {"error": str(e)}
    if not os.path.isdir(path):
        return {"error": f"dossier introuvable : {path}"}
    ignore = {"__pycache__", ".git", "node_modules", ".venv"}
    entries = []
    try:
        for name in sorted(os.listdir(path)):
            if name in ignore:
                continue
            full = os.path.join(path, name)
            entries.append({"name": name, "type": "dir" if os.path.isdir(full) else "file"})
            if len(entries) >= 300:
                break
    except Exception as e:
        return {"error": str(e)}
    return {"path": path, "entries": entries}

AUDIT_LOG_PATH = r"C:\Devllma\logs\agent_audit.log"

def _audit_log(tool, detail, outcome):
    """Journal d'audit des actions a fort impact (commandes systeme, code execute) —
    le safety_check regex reste contournable (alias PowerShell, base64, code genere
    par le modele) ; a defaut d'un vrai bac a sable, avoir une trace de CE QUI a
    tourne reellement permet au moins de detecter/investiguer apres coup."""
    try:
        os.makedirs(os.path.dirname(AUDIT_LOG_PATH), exist_ok=True)
        with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"[{ts}] {tool} | {detail[:300].replace(chr(10),' ')} | {outcome}\n")
    except Exception:
        pass

# Journal JSONL complementaire du .log texte ci-dessus : le JSONL couvre 100% des
# outils a impact AU POINT DE DISPATCH (une ligne machine-exploitable par appel :
# ts, outil, args resumes, ok/erreur, duree) ; le .log texte garde le DETAIL interne
# des outils sensibles (raisons de blocage safety_check, returncode, timeout) que
# le dispatch ne voit pas. Les DEUX sont conserves.
AUDIT_JSONL_PATH = r"C:\Devllma\logs\agent_audit.jsonl"
_audit_lock = threading.Lock()  # run_agent_sync tourne dans des threads executor
_AUDITED_TOOLS = {"run_powershell", "execute_python", "write_file", "edit_file", "run_sql", "http_request", "open_path"}
# Cles REELLES porteuses de contenu volumineux, verifiees dans les _tool_* :
# write_file->content, execute_python->code, edit_file->old_string/new_string,
# run_powershell->command, run_sql->query (PAS 'sql'), http_request->json_body (PAS 'body')
_CONTENT_KEYS = {"content", "code", "old_string", "new_string", "command", "query", "json_body"}
# run_sql mssql recoit password/user en clair ; http_request accepte un dict headers
# pouvant porter Authorization/API keys -> jamais de valeur en clair dans le journal
_SECRET_KEYS = {"password", "pwd", "token", "api_key", "authorization"}

def _summarize_args(args):
    out = {}
    for k, v in args.items():
        kl = k.lower()
        if kl in _SECRET_KEYS:
            out[k] = "<redige>"
        elif kl == "headers" and isinstance(v, dict):
            out[k] = "<cles: " + ", ".join(sorted(str(x) for x in v)) + ">"  # noms seulement, jamais les valeurs
        else:
            s = str(v)  # json_body peut etre un dict apres json.loads — toujours str-ifier avant de trancher
            if kl in _CONTENT_KEYS:
                out[k] = f"<{len(s)} cars> " + s[:120].replace("\n", " ").replace("\r", " ")
            else:
                out[k] = s[:300]
    return out

def _audit_jsonl(tool, args, status, duration_s):
    """L'audit ne doit JAMAIS faire echouer l'agent : tout est avale."""
    try:
        line = json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), "tool": tool,
                            "args": _summarize_args(args), "status": status,
                            "duree_s": duration_s}, ensure_ascii=False)
        with _audit_lock:
            os.makedirs(os.path.dirname(AUDIT_JSONL_PATH), exist_ok=True)
            with open(AUDIT_JSONL_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass

def _audit_write_if_outside_workspace(tool, path):
    """write_file/edit_file ne passent PAS par safety_check pour le CHEMIN (seulement
    pour le contenu) : une ecriture hors du dossier de projets est le cas le plus a
    risque (fichiers systeme, config d'autres logiciels...) et merite une trace,
    meme si elle n'est pas bloquee (l'utilisateur peut legitimement vouloir editer
    un fichier ailleurs sur le poste — cf. l'agent generaliste concu pour ca)."""
    if not os.path.realpath(path).lower().startswith(os.path.realpath(WORKSPACE_DIR).lower()):
        _audit_log(tool, path, "ECRITURE HORS WORKSPACE")

def _tool_run_powershell(args):
    cmd = args.get("command", "")
    safe, reasons = safety_check(cmd)
    if not safe:
        _audit_log("run_powershell", cmd, f"BLOQUE: {reasons}")
        return {"error": f"commande bloquée par sécurité : {reasons}"}
    timeout = min(int(args.get("timeout") or 25), 60)
    try:
        r = subprocess.run(["powershell", "-NonInteractive", "-Command", cmd],
                            capture_output=True, text=True, timeout=timeout,
                            encoding="utf-8", errors="replace")
        _audit_log("run_powershell", cmd, f"returncode={r.returncode}")
        return {"returncode": r.returncode,
                "stdout": (r.stdout or "").strip()[:3000],
                "stderr": (r.stderr or "").strip()[:1000]}
    except subprocess.TimeoutExpired:
        _audit_log("run_powershell", cmd, "TIMEOUT")
        return {"error": f"la commande a dépassé le timeout de {timeout}s"}
    except Exception as e:
        _audit_log("run_powershell", cmd, f"EXCEPTION: {e}")
        return {"error": str(e)}

def _tool_web_search(args):
    try:
        hits = _web_search(args.get("query", ""), 5)
    except Exception as e:
        return {"error": str(e)}
    return {"results": hits}

def _tool_memory_search(args):
    try:
        hits = mem_search(args.get("query", ""), 5)
    except Exception as e:
        return {"error": str(e)}
    return {"results": [{"kind": h["kind"], "ref": h["ref_name"], "chunk": h["chunk"][:300],
                          "score": h["score"]} for h in hits]}

def _tool_get_datetime(args):
    now = datetime.now()
    return {"iso": now.isoformat(), "readable": now.strftime("%A %d %B %Y, %Hh%M")}

def _tool_memory_save(args):
    try:
        mem_index("note", (args.get("title") or "note")[:80], args.get("content", ""))
    except Exception as e:
        return {"error": str(e)}
    return {"ok": True}

def _tool_fetch_url(args):
    url = args.get("url", "")
    if not re.match(r'^https?://', url):
        return {"error": "URL invalide (doit commencer par http:// ou https://)"}
    try:
        from bs4 import BeautifulSoup
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0 DevLLMA-agent"})
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()
        text = re.sub(r'\n{3,}', '\n\n', soup.get_text("\n").strip())
        return {"status": r.status_code, "content": text[:6000], "truncated": len(text) > 6000}
    except Exception as e:
        return {"error": str(e)}

# Motifs SQL destructeurs a l'echelle de l'instance/serveur (pas les operations
# normales type DELETE/DROP TABLE qui font partie du travail legitime demande).
_SQL_DANGER = [
    r'\bdrop\s+database\b', r'\bxp_cmdshell\b', r'\bshutdown\b',
    r'\balter\s+login\b.*\bsa\b', r'\bdrop\s+login\b',
]

def _tool_run_sql(args):
    query = args.get("query", "")
    low = query.lower()
    for pat in _SQL_DANGER:
        if re.search(pat, low):
            return {"error": f"requête bloquée par sécurité (opération niveau serveur) : {pat}"}
    engine = args.get("engine", "sqlite")
    try:
        if engine == "sqlite":
            import sqlite3
            path = _guard_path(args["database"])
            conn = sqlite3.connect(path)
        elif engine == "mssql":
            import pyodbc
            server = args.get("server") or "localhost"
            database = args.get("database", "")
            user, pwd = args.get("user"), args.get("password")
            if user:
                conn_str = f"DRIVER={{SQL Server}};SERVER={server};DATABASE={database};UID={user};PWD={pwd}"
            else:
                conn_str = f"DRIVER={{SQL Server}};SERVER={server};DATABASE={database};Trusted_Connection=yes"
            conn = pyodbc.connect(conn_str, timeout=10)
        else:
            return {"error": f"moteur inconnu : {engine}"}
        cur = conn.cursor()
        cur.execute(query)
        if cur.description:
            cols = [c[0] for c in cur.description]
            rows = cur.fetchmany(200)
            result = {"columns": cols, "rows": [list(r) for r in rows], "truncated": len(rows) == 200}
        else:
            conn.commit()
            result = {"rowcount": cur.rowcount}
        conn.close()
        return result
    except Exception as e:
        return {"error": str(e)}

def _tool_execute_python(args):
    code = args.get("code", "")
    safe, reasons = safety_check(code)
    if not safe:
        _audit_log("execute_python", code, f"BLOQUE: {reasons}")
        return {"error": f"code bloqué par sécurité : {reasons}"}
    import uuid
    script_path = os.path.join(os.environ.get("TEMP", "."), f"_devllma_agent_exec_{uuid.uuid4().hex}.py")
    try:
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(code)
        r = subprocess.run([sys.executable, script_path], capture_output=True, text=True,
                            timeout=20, encoding="utf-8", errors="replace")
        _audit_log("execute_python", code, f"returncode={r.returncode}")
        return {"stdout": (r.stdout or "").strip()[:3000],
                "stderr": (r.stderr or "").strip()[:1000],
                "returncode": r.returncode}
    except subprocess.TimeoutExpired:
        _audit_log("execute_python", code, "TIMEOUT")
        return {"error": "le script a dépassé le timeout de 20s"}
    except Exception as e:
        _audit_log("execute_python", code, f"EXCEPTION: {e}")
        return {"error": str(e)}
    finally:
        try: os.remove(script_path)
        except Exception: pass

def _tool_edit_file(args):
    try:
        path = _guard_path(args.get("path", ""))
    except PermissionError as e:
        return {"error": str(e)}
    if not os.path.isfile(path):
        return {"error": f"fichier introuvable : {path}"}
    if is_office_doc(path):
        return {"error": "edition ciblee impossible sur un document Office/PDF — utilise write_file pour le regenerer"}
    old = args.get("old_string", "")
    new = args.get("new_string", "")
    if not old:
        return {"error": "old_string manquant — fournis le texte exact a remplacer"}
    if old == new:
        return {"error": "old_string et new_string identiques — rien a modifier"}
    safe, reasons = safety_check(new)
    if not safe:
        return {"error": f"modification bloquee par securite : {reasons}"}
    try:
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
            src = f.read()
    except Exception as e:
        return {"error": f"lecture impossible : {e}"}
    # normalisation CRLF : le modele produit presque toujours du \n pur
    crlf = "\r\n" in src
    work = src.replace("\r\n", "\n") if crlf else src
    old_n = old.replace("\r\n", "\n")
    new_n = new.replace("\r\n", "\n")
    n = work.count(old_n)
    if n == 0:
        first = (old_n.splitlines() or [old_n])[0][:80]
        close = difflib.get_close_matches(first, work.splitlines(), n=1, cutoff=0.5)
        hint = f" — ligne la plus proche : {close[0][:120]!r}" if close else ""
        return {"error": f"texte introuvable dans {os.path.basename(path)} — relis la zone avec read_lines et copie le texte A L'IDENTIQUE (espaces, accents){hint}"}
    replace_all = bool(args.get("replace_all", False))
    if n > 1 and not replace_all:
        return {"error": f"{n} occurrences de old_string — ajoute des lignes de contexte autour pour le rendre unique, ou passe replace_all=true"}
    out = work.replace(old_n, new_n, -1 if replace_all else 1)
    if crlf:
        out = out.replace("\n", "\r\n")
    tmp = path + ".devllma_tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(out)
        os.replace(tmp, path)
    except Exception as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return {"error": f"ecriture impossible : {e}"}
    _audit_write_if_outside_workspace("edit_file", path)
    line = work[:work.find(old_n)].count("\n") + 1
    return {"ok": True, "path": path, "remplacements": n if replace_all else 1, "premiere_ligne_modifiee": line}

_SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "backups", "$recycle.bin"}

def _tool_grep_search(args):
    try:
        root = _guard_path(args.get("root") or WORKSPACE_DIR)
    except PermissionError as e:
        return {"error": str(e)}
    if not os.path.isdir(root):
        return {"error": f"dossier introuvable : {root}"}
    pattern = args.get("pattern", "")
    if not pattern:
        return {"error": "pattern manquant (texte ou regex a chercher)"}
    try:
        rx = re.compile(pattern, re.IGNORECASE if args.get("ignore_case", True) else 0)
    except re.error as e:
        return {"error": f"regex invalide : {e} — echappe les caracteres speciaux ou simplifie le motif"}
    glob = (args.get("glob") or "*").lower()
    hits, scanned, stopped = [], 0, False
    deadline = time.monotonic() + 10  # budget temps dur (pas de signal sous Windows en thread)
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d.lower() not in _SKIP_DIRS]
        for name in fn:
            if not fnmatch.fnmatch(name.lower(), glob):
                continue
            fp = os.path.join(dp, name)
            try:
                if os.path.getsize(fp) > 1_000_000:
                    continue
                with open(fp, "rb") as fb:
                    if b"\x00" in fb.read(1024):
                        continue  # binaire
                scanned += 1
                per_file = 0
                with open(fp, encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        if rx.search(line):
                            hits.append({"fichier": os.path.relpath(fp, root), "ligne": i, "texte": line.strip()[:160]})
                            per_file += 1
                            if per_file >= 5:
                                break
            except OSError:
                continue
            if len(hits) >= 30 or scanned >= 2000 or time.monotonic() > deadline:
                stopped = True
                break
        if stopped:
            break
    return {"racine": root, "resultats": hits, "total": len(hits), "tronque": stopped}

def _tool_find_files(args):
    pattern = (args.get("pattern") or "").strip()
    if not pattern:
        return {"error": "pattern manquant, ex: *facture*.pdf"}
    if "*" not in pattern and "?" not in pattern:
        pattern = f"*{pattern}*"  # tolere un nom sans joker
    try:
        root = _guard_path(args.get("root") or HOME_DIR)
    except PermissionError as e:
        return {"error": str(e)}
    if not os.path.isdir(root):
        return {"error": f"dossier introuvable : {root}"}
    skip = {"appdata", "node_modules", ".git", "__pycache__", ".venv", "$recycle.bin", "windows"}
    hits, stopped = [], False
    deadline = time.monotonic() + 10
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d.lower() not in skip]
        for name in fn:
            if fnmatch.fnmatch(name.lower(), pattern.lower()):
                fp = os.path.join(dp, name)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                hits.append((st.st_mtime, fp, st.st_size))
        if time.monotonic() > deadline or len(hits) >= 300:
            stopped = True
            break
    hits.sort(reverse=True)  # plus recent d'abord
    top = [{"chemin": fp, "ko": round(sz / 1024, 1),
            "modifie": datetime.fromtimestamp(mt).strftime("%d/%m/%Y %H:%M")}
           for mt, fp, sz in hits[:30]]
    if not top:
        return {"racine": root, "fichiers": [], "info": "aucun fichier trouve — essaie un motif plus large ou une autre racine (ex: C:\\Devllma\\workspace)"}
    return {"racine": root, "fichiers": top, "total_trouves": len(hits), "tronque": stopped or len(hits) > 30}

def _tool_read_lines(args):
    try:
        path = _guard_path(args.get("path", ""))
    except PermissionError as e:
        return {"error": str(e)}
    if not os.path.isfile(path):
        return {"error": f"fichier introuvable : {path}"}
    if is_office_doc(path):
        return {"error": "document Office/PDF — utilise read_file"}
    try:
        src = open(path, encoding="utf-8", errors="replace").read()
    except Exception as e:
        return {"error": f"lecture impossible : {e}"}
    lines = src.splitlines()
    if args.get("outline") and path.lower().endswith(".py"):
        try:
            tree = ast.parse(src)
        except SyntaxError as e:
            return {"error": f"fichier .py invalide (ligne {e.lineno}) : {e.msg}"}
        items = sorted(
            (n.lineno, "class" if isinstance(n, ast.ClassDef) else "def", n.name)
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
        return {"plan": [f"L{l} {k} {name}" for l, k, name in items][:80],
                "total_lignes": len(lines), "path": path}
    try:
        start = max(1, int(args.get("start_line") or 1))
        count = min(max(1, int(args.get("count") or 120)), 250)
    except (TypeError, ValueError):
        start, count = 1, 120
    chunk = lines[start - 1:start - 1 + count]
    if not chunk:
        return {"error": f"start_line={start} au-dela de la fin du fichier ({len(lines)} lignes)"}
    contenu = "\n".join(f"{i}: {t[:200]}" for i, t in enumerate(chunk, start))[:3500]
    end = min(start + count - 1, len(lines))
    return {"contenu": contenu, "de": start, "a": end,
            "total_lignes": len(lines), "suite": end < len(lines)}

def _tool_read_image(args):
    try:
        path = _guard_path(args.get("path", ""))
    except PermissionError as e:
        return {"error": str(e)}
    if not os.path.isfile(path):
        return {"error": f"image introuvable : {path} — utilise find_files pour la localiser"}
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff", ".gif"):
        return {"error": "ce n'est pas une image — utilise read_file pour les documents texte/Office/PDF"}
    from tools import read_image_text  # lazy : le moteur OCR ne se charge qu'au 1er usage
    try:
        text = read_image_text(path) or ""
    except Exception as e:
        return {"error": f"OCR impossible : {e}"}
    if not text.strip():
        return {"text": "", "note": "aucun texte detecte dans l'image", "path": path}
    return {"text": text[:3500], "truncated": len(text) > 3500, "path": path}

def _tool_http_request(args):
    url = (args.get("url") or "").strip()
    if not re.match(r"^https?://", url, re.I):
        return {"error": "URL invalide — http:// ou https:// requis"}
    method = (args.get("method") or "GET").upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        return {"error": f"methode non supportee : {method}"}
    body = args.get("json_body")
    if isinstance(body, str) and body.strip():
        try:
            body = json.loads(body)
        except ValueError:
            return {"error": "json_body n'est pas du JSON valide — corrige la syntaxe"}
    elif not body:
        body = None
    if body is not None and len(json.dumps(body)) > 100_000:
        return {"error": "json_body trop volumineux (>100 Ko)"}
    headers = {"User-Agent": "Mozilla/5.0 DevLLMA-agent"}
    if isinstance(args.get("headers"), dict):
        headers.update({str(k): str(v) for k, v in args["headers"].items()})
    try:
        timeout = min(int(args.get("timeout") or 15), 30)
    except (TypeError, ValueError):
        timeout = 15
    qp = args.get("query_params")
    try:
        r = requests.request(method, url, headers=headers,
                             params=qp if isinstance(qp, dict) and qp else None,
                             json=body, timeout=timeout)
    except requests.RequestException as e:
        return {"error": f"requete echouee : {e.__class__.__name__} — {str(e)[:150]}"}
    try:
        body_txt = json.dumps(r.json(), ensure_ascii=False)
    except ValueError:
        body_txt = r.text
    return {"status": r.status_code,
            "content_type": r.headers.get("content-type", "")[:60],
            "body": body_txt[:3400], "truncated": len(body_txt) > 3400}

def _tool_csv_analyze(args):
    import csv, statistics
    from collections import Counter
    try:
        path = _guard_path(args.get("path", ""))
    except PermissionError as e:
        return {"error": str(e)}
    if not os.path.isfile(path):
        return {"error": f"fichier introuvable : {path}"}
    if not path.lower().endswith((".csv", ".tsv", ".txt")):
        return {"error": "pas un fichier CSV/TSV — pour Excel utilise read_file, pour une base .db utilise run_sql"}
    try:
        max_rows = min(int(args.get("max_rows") or 5000), 50000)
    except (TypeError, ValueError):
        max_rows = 5000
    delim = args.get("delimiter") or None
    try:
        with open(path, encoding=args.get("encoding") or "utf-8-sig", errors="replace", newline="") as f:
            head = f.read(4096)
            f.seek(0)
            if not delim:
                try:
                    delim = csv.Sniffer().sniff(head, delimiters=",;\t|").delimiter
                except csv.Error:
                    delim = ";" if head.count(";") >= head.count(",") else ","
            reader = csv.reader(f, delimiter=delim)
            header = next(reader, None)
            if not header:
                return {"error": "fichier vide ou illisible comme CSV"}
            cols = [(h.strip() or f"col_{i}")[:40] for i, h in enumerate(header)]
            cols_tronquees = len(cols) > 30
            cols = cols[:30]
            data = [[] for _ in cols]
            sample, n, more = [], 0, False
            for row in reader:  # streaming ligne a ligne, jamais f.read() complet
                if n >= max_rows:
                    more = True
                    break
                if n < 3:
                    sample.append([c[:40] for c in row[:len(cols)]])
                for i in range(len(cols)):
                    data[i].append((row[i] if i < len(row) else "").strip())
                n += 1
    except Exception as e:
        return {"error": f"lecture CSV impossible : {e}"}
    def col_stats(vals):
        nonempty = [v for v in vals if v != ""]
        vides = len(vals) - len(nonempty)
        if not nonempty:
            return {"type": "vide", "vides": vides}
        try:
            nums = [float(v.replace(",", ".").replace(" ", "")) for v in nonempty]
            entier = all(x == int(x) for x in nums[:200])
            return {"type": "entier" if entier else "decimal", "vides": vides,
                    "min": min(nums), "max": max(nums),
                    "moyenne": round(statistics.fmean(nums), 3),
                    "mediane": round(statistics.median(nums), 3)}
        except ValueError:
            return {"type": "texte", "vides": vides, "distinct": len(set(nonempty)),
                    "top": [[v[:40], c] for v, c in Counter(nonempty).most_common(5)]}
    out = {"path": path, "delimiteur": delim, "lignes_analysees": n,
           "plus_de_lignes": more, "colonnes_tronquees": cols_tronquees,
           "colonnes": [{"nom": c, **col_stats(data[i])} for i, c in enumerate(cols)]}
    if len(json.dumps(out, ensure_ascii=False)) < 3300:
        out["echantillon"] = sample
    return out

_BLOCKED_EXEC = {".exe", ".bat", ".cmd", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf",
                 ".msi", ".msp", ".scr", ".lnk", ".hta", ".jar", ".com", ".pif", ".reg"}

def _tool_open_path(args):
    target = (args.get("target") or "").strip().strip('"')
    if not target:
        return {"error": "cible manquante (chemin de fichier/dossier ou URL http)"}
    if re.match(r"^https?://", target, re.I):
        import webbrowser
        webbrowser.open(target)
        return {"ouvert": target, "type": "url"}
    # refuse tout autre schema (file://, ms-settings:, mailto:...) mais accepte les chemins C:\...
    if re.match(r"^[a-z][a-z0-9+.-]+:", target, re.I) and not re.match(r"^[a-zA-Z]:[\\/]", target):
        return {"error": "seuls les liens http(s) et les chemins locaux sont autorises"}
    try:
        path = _guard_path(target)
    except PermissionError as e:
        return {"error": str(e)}
    if not os.path.exists(path):
        return {"error": f"introuvable : {path} — verifie le chemin avec find_files ou list_dir"}
    if os.path.splitext(path)[1].lower() in _BLOCKED_EXEC:
        return {"error": "ouverture de programmes executables refusee par securite — demande a l'utilisateur de le lancer lui-meme"}
    try:
        os.startfile(path)  # non bloquant, application par defaut de Windows
    except OSError as e:
        return {"error": f"ouverture impossible : {e}"}
    return {"ouvert": path, "type": "dossier" if os.path.isdir(path) else "fichier"}

TOOL_IMPL = {
    "read_file": _tool_read_file,
    "write_file": _tool_write_file,
    "list_dir": _tool_list_dir,
    "run_powershell": _tool_run_powershell,
    "web_search": _tool_web_search,
    "memory_search": _tool_memory_search,
    "memory_save": _tool_memory_save,
    "get_datetime": _tool_get_datetime,
    "fetch_url": _tool_fetch_url,
    "run_sql": _tool_run_sql,
    "execute_python": _tool_execute_python,
    "edit_file": _tool_edit_file,
    "grep_search": _tool_grep_search,
    "find_files": _tool_find_files,
    "read_lines": _tool_read_lines,
    "read_image": _tool_read_image,
    "http_request": _tool_http_request,
    "csv_analyze": _tool_csv_analyze,
    "open_path": _tool_open_path,
}

AGENT_SYSTEM = f"""Tu es l'agent generaliste de DevLLMA, assistant IA local autonome (dev + bureautique + recherche), en francais.

CHEMINS REELS DE CE POSTE (ne jamais en inventer d'autres) :
- Dossier personnel : {HOME_DIR}
- Bureau : {DESKTOP_DIR}
- Workspace projets : {WORKSPACE_DIR}
"le bureau"/"mes documents" sans chemin complet = ces chemins EXACTS. Jamais de nom de compte
generique (Utilisateur, User). En cas de doute, verifie avec list_dir AVANT d'ecrire.

Regles :
- Question simple dont tu es sur -> reponds direct, sans outil. Action reelle ou info incertaine
  -> utilise l'outil adapte, ne devine JAMAIS un contenu de fichier ou un resultat.
- Heure/date du jour -> get_datetime, TOUJOURS. Actualite ou fait recent -> web_search.
  N'invente jamais une heure, une date ou un evenement recent.
- Modification de fichier -> edit_file (old_string copie A L'IDENTIQUE : indentation, accents).
  S'il repond "introuvable"/"plusieurs occurrences" -> read_lines sur la zone puis reessaie.
- Fichier long -> read_lines (outline=true pour le plan d'un .py). Chercher DANS les fichiers ->
  grep_search. Retrouver un fichier par nom -> find_files.
- Calcul numerique non trivial -> execute_python avec print(), jamais de tete.
- Donnees CSV -> csv_analyze d'abord. Image -> read_image.
- APIs sans cle pour http_request : api.open-meteo.com/v1/forecast?latitude=..&longitude=..&current_weather=true ;
  api.frankfurter.app/latest?from=EUR&to=USD ; fr.wikipedia.org/api/rest_v1/page/summary/<Titre>.
- Resultat produit (fichier/dossier) -> termine par open_path pour l'ouvrir a l'utilisateur.
- Verifie le resultat d'un outil avant d'annoncer un succes. Ne repete jamais un appel identique.
- Jamais de donnees sensibles (mots de passe, cles) dans memory_save.
- Reponds concis, en francais.
"""


def warm_agent_cache():
    """Prechauffe le cache KV d'Ollama avec le prefixe systeme+outils de l'agent.
    A appeler au demarrage du service : sans ca, la PREMIERE demande apres un
    redemarrage paie ~6 min d'evaluation de prompt a froid (mesure sur ce CPU),
    ce qui depasse tous les timeouts et ressemble a une panne."""
    try:
        _http.post(OLLAMA + "/api/chat", json={
            "model": AGENT_MODEL,
            "messages": [{"role": "system", "content": AGENT_SYSTEM},
                          {"role": "user", "content": "ping"}],
            "tools": TOOLS, "stream": False, "keep_alive": KEEP_ALIVE,
            "options": {"num_predict": 1},
        }, timeout=600)
        return True
    except Exception:
        return False


def _chat_call(messages, tools=None, temperature=0.3):
    payload = {
        "model": AGENT_MODEL,
        "messages": messages,
        "stream": False,
        "keep_alive": KEEP_ALIVE,
        "options": {"temperature": temperature},
    }
    if tools:
        payload["tools"] = tools
    last_err = None
    for attempt in range(3):
        try:
            r = _http.post(OLLAMA + "/api/chat", json=payload, timeout=300)
            body = r.json()
            # Ollama repond 200/404 avec {"error": "..."} (ex: modele non pulle) plutot
            # qu'une exception HTTP — sans ce controle, .get("message",{}) retombe
            # silencieusement sur un contenu vide et l'agent semble ne rien repondre.
            if "error" in body:
                return {"role": "assistant", "content":
                        f"(modele '{AGENT_MODEL}' indisponible sur Ollama : {body['error']} — "
                        f"verifie qu'il est bien telecharge dans le panneau Modeles)"}
            return body.get("message", {"role": "assistant", "content": ""})
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_err = e
            time.sleep(2 * (attempt + 1))
        except Exception as e:
            return {"role": "assistant", "content": f"(agent indisponible : {e})"}
    return {"role": "assistant", "content": f"(agent indisponible après 3 tentatives : {last_err})"}


def run_agent_sync(prompt, history_text="", notify=None, should_stop=None):
    """Boucle ReAct synchrone (a lancer dans un thread/executor — appels HTTP bloquants).
    `notify(kind, payload)` est appelee a chaque etape pour remonter la progression
    (kind: 'tool_call' | 'tool_result' | 'final'). `should_stop()` est consultee entre
    chaque etape : bouton Stop -> la boucle s'arrete au prochain point de controle au
    lieu de continuer a occuper le CPU en arriere-plan. Retourne le texte final."""
    def _notify(kind, payload):
        if notify:
            try: notify(kind, payload)
            except Exception: pass

    def _stopped():
        try:
            return bool(should_stop and should_stop())
        except Exception:
            return False

    user_content = prompt if not history_text else f"CONTEXTE RECENT DE LA CONVERSATION:\n{history_text}\n\nDEMANDE ACTUELLE: {prompt}"

    # Souvenirs pertinents injectes d'office (lecons de bugs passes, faits sur ce
    # poste, bonnes pratiques) : le modele ne pense pas toujours a appeler
    # memory_search de lui-meme, or ces rappels evitent des erreurs deja commises.
    # Les anciennes questions/reponses ('qa') sont EXCLUES : une vieille reponse
    # contenant une heure/date perimee pousse le modele a la recopier au lieu
    # d'appeler l'outil (constate au banc de tests : il a repondu l'heure d'hier).
    try:
        memories = [m for m in mem_search(prompt, 6, min_score=0.62)
                    if m["kind"] != "qa"][:3]
    except Exception:
        memories = []
    if memories:
        mem_note = "\n\nRAPPELS DE TA MEMOIRE (lecons/faits verifies sur CE poste, a respecter) :\n" + \
                   "\n".join(f"- [{m['kind']}] {m['chunk'][:400]}" for m in memories)
        user_content += mem_note

    messages = [
        {"role": "system", "content": AGENT_SYSTEM},
        {"role": "user", "content": user_content},
    ]
    seen_calls = set()

    for step in range(MAX_STEPS):
        if _stopped():
            _notify("final", "(arrêté par l'utilisateur)")
            return "(arrêté par l'utilisateur)"
        message = _chat_call(messages, tools=TOOLS)
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            final_text = message.get("content", "").strip() or "(pas de réponse)"
            _notify("final", final_text)
            return final_text

        messages.append(message)
        for call in tool_calls:
            fn = call.get("function", {})
            name = fn.get("name")
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try: args = json.loads(args)
                except Exception: args = {}
            sig = (name, json.dumps(args, sort_keys=True))
            impl = TOOL_IMPL.get(name)
            if impl is None:
                result = {"error": f"outil inconnu: {name}"}
            elif sig in seen_calls:
                result = {"error": "appel identique déjà tenté — change d'approche, ne répète pas cet appel"}
            else:
                seen_calls.add(sig)
                _notify("tool_call", {"name": name, "args": args})
                t0 = time.perf_counter()
                try:
                    result = impl(args)
                except Exception:
                    # un impl qui leve (rare, la plupart retournent {'error':...})
                    # doit quand meme laisser une trace avant de remonter
                    if name in _AUDITED_TOOLS:
                        _audit_jsonl(name, args, "exception", round(time.perf_counter() - t0, 3))
                    raise
                if name in _AUDITED_TOOLS:
                    # les blocages safety_check remontent deja en {'error': ...} -> 'erreur'
                    status = "erreur" if isinstance(result, dict) and "error" in result else "ok"
                    _audit_jsonl(name, args, status, round(time.perf_counter() - t0, 3))
                _notify("tool_result", {"name": name, "result": result})
            messages.append({
                "role": "tool",
                "content": json.dumps(result, ensure_ascii=False)[:4000],
            })

    if _stopped():
        _notify("final", "(arrêté par l'utilisateur)")
        return "(arrêté par l'utilisateur)"
    # Budget d'etapes epuise -> forcer une synthese finale sans outils plutot que
    # de laisser la conversation sans reponse.
    messages.append({"role": "user", "content":
        "Tu as atteint la limite d'étapes. Résume ce que tu as accompli/découvert et réponds "
        "du mieux possible avec les informations déjà obtenues, sans appeler d'autre outil."})
    message = _chat_call(messages, tools=None)
    final_text = message.get("content", "").strip() or "(limite d'étapes atteinte sans réponse claire)"
    _notify("final", final_text)
    return final_text
