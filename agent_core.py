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
from db import mem_search, mem_index, mem_get, mem_set
from documents import is_office_doc, read_document, write_document

_http = requests.Session()  # connexion reutilisee (keep-alive) pour les appels Ollama

AGENT_MODEL = "qwen3-coder:30b"
# -1 = modele garde en RAM en permanence : l'evaluation du prompt (19 outils +
# consignes ~4700 tokens) prend ~6 min A FROID sur ce CPU contre ~4s avec le
# cache KV chaud — un dechargement apres 30 min d'inactivite rendait la
# premiere reponse suivante inutilisable.
KEEP_ALIVE = -1
# Meme fenetre de contexte que ollama_client.NUM_CTX : le modele est partage (brain/coder/
# agent) et keep_alive=-1 le fige au num_ctx du premier chargeur. Si le warmup de l'agent
# charge le modele en premier sans num_ctx, il retombe au defaut 4096 (constate apres reboot).
NUM_CTX = 32768
# 14 (au lieu de 8) : les flux reels enchainent lire x3 -> editer x2 -> lancer -> verifier,
# ce qui saturait la limite de 8 et forcait un resume premature en plein milieu d'une tache.
# Le resume-a-la-limite borne toujours le pire cas.
MAX_STEPS = 14
# Modele VISION (multimodal) : lit ET comprend une image (captures, schemas, photos),
# la ou l'OCR ne rend que le texte brut. Opt-in via le parametre `question` de read_image
# pour ne pas ralentir le cas courant (lecture de texte = OCR, instantane). keep_alive=0 :
# le modele vision (~6 Go) se decharge apres usage pour ne PAS evincer le coder 30b
# garde chaud en permanence (RAM limitee sur ce poste).
VISION_MODEL = "qwen2.5vl:7b"
VISION_KEEP_ALIVE = 0

# Chemins REELS de l'utilisateur INTERACTIF (Admin), en DUR — sans ca, le modele
# hallucine des chemins generiques ("C:\Users\Utilisateur\...") qui n'existent pas
# sur ce poste : write_file y ecrit quand meme (creation des dossiers manquants) et
# le modele rapporte un succes alors que le fichier a atterri au mauvais endroit
# (faux succes, cf. HANDOFF.md).
# NE PAS deriver dynamiquement via os.path.expanduser("~")/Path.home() : ca resolvait
# correctement tant que le service DevLLMA tournait sous la session interactive Admin,
# mais depuis que la tache planifiee DevLLMAWeb tourne en compte SYSTEM (evite de
# stocker le mot de passe Windows), ces appels retournent le profil FANTOME de SYSTEM
# (C:\Windows\system32\config\systemprofile) au lieu du bureau reel de l'utilisateur —
# constate en prod : "genere une image et copie-la sur le bureau" atterrissait dans
# system32 (acces refuse, dossier systeme protege). Le compte interactif de ce poste
# est Admin ; son bureau ne bouge pas, un chemin en dur est donc fiable ici.
HOME_DIR = r"C:\Users\Admin"
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
    # Neutralise les contournements Windows : prefixe de chemin etendu (\\?\) et namespace
    # device (\\.\) preservent le chemin sans "c:\" (donc echappaient au startswith) ; les
    # chemins UNC (\\serveur\C$, \\127.0.0.1\C$) pointent vers le MEME disque reel. On retire
    # \\?\ / \\.\, puis tout UNC restant est refuse d'office (hors perimetre de l'assistant).
    if low.startswith("\\\\?\\") or low.startswith("\\\\.\\"):
        ap = ap[4:]; low = low[4:]
    if low.startswith("\\\\"):
        raise PermissionError(f"accès refusé — chemin réseau/UNC non autorisé : {ap}")
    for d in _DENY_PREFIXES:
        # `== d or startswith(d + "\\")` (au lieu du startswith nu) evite de bloquer a tort
        # un "C:\Windows_autre" tout en couvrant "C:\Windows" et tout ce qu'il contient.
        if low == d or low.startswith(d + "\\"):
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
        "description":"Lit une image (.png/.jpg, capture d'ecran...). Sans question : OCR local (texte brut, rapide). Avec 'question' : un modele vision DECRIT et interprete reellement l'image (contenu, schema, UI, graphe) et repond a la question.",
        "parameters":{"type":"object","properties":{
            "path":{"type":"string"},
            "question":{"type":"string","description":"optionnel : ce qu'on veut savoir sur l'image (declenche l'analyse visuelle au lieu du simple OCR)"}
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
    {"type":"function","function":{
        "name":"generate_media",
        "description":"Genere une IMAGE ou une VIDEO (ce poste CPU ne peut pas le faire en local). A utiliser DES qu'on demande de creer/generer une image ou une video. Utilise en priorite une API hebergee (Hugging Face, gratuit, toujours dispo) puis un GPU Colab en repli. Renvoie le chemin du fichier local. Les cles se donnent UNE fois (memorisees).",
        "parameters":{"type":"object","properties":{
            "task":{"type":"string","enum":["image","video"],"description":"image ou video"},
            "prompt":{"type":"string","description":"description de ce qu'il faut generer"},
            "hf_token":{"type":"string","description":"optionnel : cle Hugging Face (hf_...), a donner une fois"},
            "colab_url":{"type":"string","description":"optionnel : URL d'un worker GPU Colab (repli, surtout video)"},
            "colab_token":{"type":"string","description":"optionnel : jeton du worker Colab"},
            "params":{"type":"object","description":"optionnel : reglages (steps, seconds, ...)"}
        },"required":["task","prompt"]}
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
_AUDITED_TOOLS = {"run_powershell", "execute_python", "write_file", "edit_file", "run_sql", "http_request", "open_path", "generate_media", "colab_run"}
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
    # Plafond de taille AVANT tout chargement : la vision encode l'image en base64 (+33 %) dans
    # un corps JSON, et l'OCR recharge le bitmap entier -> une image enorme saturerait la RAM
    # (poste limite). Couvre les deux chemins.
    try:
        taille = os.path.getsize(path)
    except OSError as e:
        return {"error": f"image illisible : {e}"}
    if taille > 20_000_000:
        return {"error": f"image trop volumineuse ({round(taille/1_048_576,1)} Mo, max 20 Mo) — réduis/convertis-la avant analyse"}
    # Avec une question -> analyse VISUELLE (comprend le contenu), sinon OCR (texte brut, rapide).
    question = (args.get("question") or "").strip()
    if question:
        res = _vision_read(path, question)
        if res is not None:              # None = vision indisponible -> repli OCR ci-dessous
            return res
    from tools import read_image_text  # lazy : le moteur OCR ne se charge qu'au 1er usage
    try:
        text = read_image_text(path) or ""
    except Exception as e:
        return {"error": f"OCR impossible : {e}"}
    if not text.strip():
        return {"text": "", "note": "aucun texte detecte dans l'image (essaie avec une 'question' pour l'analyse visuelle)", "path": path}
    return {"text": text[:3500], "truncated": len(text) > 3500, "path": path}

def _vision_read(path, question):
    """Analyse une image avec le modele vision (qwen2.5vl). Retourne {description,...} ou
    None si le modele est indisponible (l'appelant retombe alors sur l'OCR). Le modele se
    decharge apres usage (keep_alive=0) pour ne pas evincer le coder garde en RAM."""
    import base64
    try:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
    except Exception as e:
        return {"error": f"lecture image impossible : {e}"}
    try:
        r = _http.post(OLLAMA + "/api/generate", json={
            "model": VISION_MODEL,
            "prompt": question + "\n\nDecris precisement ce que montre l'image et retranscris tout texte visible. Reponds en francais.",
            "images": [b64], "stream": False, "keep_alive": VISION_KEEP_ALIVE,
            "options": {"temperature": 0.2, "num_predict": 700},
        }, timeout=300)
        body = r.json()
        if "error" in body:   # modele non installe / autre -> repli OCR
            return None
        desc = (body.get("response") or "").strip()
        return {"description": desc[:3500], "path": path, "mode": "vision"} if desc else None
    except Exception:
        return None  # vision KO -> repli OCR

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

# Extensions "actives" (executees par le shell Windows via os.startfile) refusees par open_path.
# Ajout des manquantes reperees en revue : .py/.pyw/.pyc (Python associe = execution), .scf/.url
# (raccourcis a effet de bord), .cpl/.msc/.gadget/.application/.msh* (panneaux/scripts actifs).
_BLOCKED_EXEC = {".exe", ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf",
                 ".wsh", ".ws", ".wsc", ".msi", ".msp", ".scr", ".lnk", ".hta", ".jar", ".com",
                 ".pif", ".reg", ".py", ".pyw", ".pyc", ".pyz", ".pyzw", ".scf", ".url", ".cpl",
                 ".msc", ".msh", ".msh1", ".msh2", ".gadget", ".application"}

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

# ── Generation de MEDIA lourd (image/video) : ce poste CPU ne peut pas le faire ──
# Deux backends, essayes dans l'ordre : (1) API HEBERGEE (Hugging Face, gratuit, TOUJOURS
# dispo, rien a demarrer), (2) repli GPU Colab (si un notebook worker tourne). Les cles/URL
# se configurent UNE fois (memorisees en base) puis l'agent appelle l'outil tout seul.
_MEDIA_EXT = {"image": "png", "video": "mp4"}

# Enrichissement QUALITE du prompt image : ajoute des descripteurs de rendu et surtout
# d'anatomie (mains a 5 doigts...) — le defaut FLUX.1-schnell (~4 etapes, optimise vitesse)
# batclait les details, notamment les mains (constate sur un rendu utilisateur). Ces termes
# poussent le modele vers plus de soin. On n'empile PAS si le prompt est deja enrichi.
_IMG_QUALITY_SUFFIX = ("highly detailed, intricate details, sharp focus, masterpiece, best quality, "
                       "professional artwork, anatomically correct hands, five fingers per hand, "
                       "detailed symmetrical face, clean linework, coherent composition")
# Prompt NEGATIF : liste des artefacts a bannir. N'est REELLEMENT exploite que par les
# modeles a CFG classique (SDXL...) ; FLUX (schnell/dev) est distille sans guidage negatif,
# donc on ne le lui envoie pas (il l'ignorerait ou renverrait une erreur).
_IMG_NEGATIVE = ("bad hands, extra fingers, missing fingers, fused fingers, deformed hands, "
                 "mutated hands, extra limbs, malformed limbs, deformed face, disfigured, "
                 "blurry, lowres, low quality, jpeg artifacts, watermark, text, signature, "
                 "cropped, out of frame, bad anatomy, bad proportions, ugly")

def _enhance_image_prompt(prompt):
    low = prompt.lower()
    if any(t in low for t in ("highly detailed", "masterpiece", "best quality")):
        return prompt  # deja enrichi -> ne pas empiler les memes termes
    return f"{prompt}, {_IMG_QUALITY_SUFFIX}"

def _hf_params_for(model, params):
    """Parametres de generation adaptes au MODELE (les reglages qualite different selon
    la famille). Surchargeables via params (steps/guidance/width/height/negative_prompt)."""
    m = (model or "").lower()
    p = {"width": int(params.get("width", 1024)), "height": int(params.get("height", 1024))}
    if "schnell" in m:
        # schnell : distille pour 1-4 etapes, guidage distille -> ni guidance ni negatif utiles.
        p["num_inference_steps"] = int(params.get("steps", 6))
    elif "flux" in m:
        # FLUX.1-dev : bien plus de detail que schnell, guidance distille (~3.5).
        p["num_inference_steps"] = int(params.get("steps", 30))
        p["guidance_scale"] = float(params.get("guidance", 3.5))
    else:
        # SDXL et modeles CFG classiques : le prompt NEGATIF corrige efficacement les mains.
        p["num_inference_steps"] = int(params.get("steps", 35))
        p["guidance_scale"] = float(params.get("guidance", 7.0))
        p["negative_prompt"] = params.get("negative_prompt") or _IMG_NEGATIVE
    return p

# Providers Hugging Face "Inference Providers" a essayer, dans l'ordre, pour un modele donne.
# L'ancien provider serverless "hf-inference" a ete DEPRECIE pour les modeles image (renvoie
# desormais HTTP 410 "model deprecated" — constate 16/07/2026) ; on passe par la route
# OpenAI-compatible /{provider}/v1/images/generations. "together" sert FLUX.1-schnell/dev
# (verifie fonctionnel avec la cle de ce poste). On tente plusieurs providers/modeles : le
# 1er qui renvoie une image gagne.
_HF_IMAGE_ROUTES = [
    ("together", "black-forest-labs/FLUX.1-schnell"),
    ("together", "black-forest-labs/FLUX.1-dev"),
    ("fal-ai",   "black-forest-labs/FLUX.1-schnell"),
    ("nebius",   "black-forest-labs/FLUX.1-dev"),
]

def _hf_image_backend(prompt, params):
    """Image via Hugging Face Inference Providers (route OpenAI-compatible actuelle).
    -> (bytes, ext) ou None. Essaie plusieurs providers/modeles ; 1er succes gagne.
    Un couple force via mem 'hf_image_provider' + 'hf_image_model' court-circuite la liste."""
    import base64 as _b64
    key = mem_get("hf_token")
    if not key:
        return None
    prov = mem_get("hf_image_provider"); mdl = mem_get("hf_image_model")
    routes = [(prov, mdl)] if (prov and mdl) else _HF_IMAGE_ROUTES
    size = f"{int(params.get('width', 1024))}x{int(params.get('height', 1024))}"
    for provider, model in routes:
        body = {"model": model, "prompt": prompt, "response_format": "b64_json", "size": size}
        # FLUX est distille sans guidage negatif -> on n'envoie negative_prompt qu'aux modeles
        # non-FLUX (SDXL...) qui savent l'exploiter.
        if params.get("negative_prompt") and "flux" not in model.lower():
            body["negative_prompt"] = params["negative_prompt"]
        try:
            r = _http.post(f"https://router.huggingface.co/{provider}/v1/images/generations",
                           headers={"Authorization": f"Bearer {key}"}, json=body, timeout=180)
        except requests.RequestException:
            continue  # provider suivant
        if r.status_code != 200:
            continue  # 400 (modele non servi par ce provider), 402 (credits), 410... -> suivant
        try:
            data = r.json().get("data") or []
            b64 = data[0].get("b64_json") if data else None
            if b64:
                return _b64.b64decode(b64), "png"
        except Exception:
            continue
    return None

# Providers/modeles HF Inference Providers pour la VIDEO (text-to-video), dans l'ordre.
# fal-ai / Wan2.2 fonctionne avec la cle de ce poste (verifie 18/07/2026, video mp4 produite)
# -> plus besoin du GPU Colab pour la video. together/novita en repli.
_HF_VIDEO_ROUTES = [
    ("fal-ai",   "Wan-AI/Wan2.2-TI2V-5B"),
    ("together", "Wan-AI/Wan2.2-T2V-A14B"),
    ("novita",   "Wan-AI/Wan2.2-T2V-A14B"),
]

def _hf_video_backend(prompt, params):
    """Video via Hugging Face Inference Providers (text-to-video, client huggingface_hub qui
    gere le routage/polling par provider). -> (bytes 'mp4') ou None.
    NB : ces modeles produisent une video SILENCIEUSE (pas de bande-son) et COURTE (~5 s) ;
    la 'musique' demandee ne peut pas etre ajoutee par le modele."""
    key = mem_get("hf_token")
    if not key:
        return None
    try:
        from huggingface_hub import InferenceClient
    except Exception:
        return None
    fp = mem_get("hf_video_provider"); fm = mem_get("hf_video_model")
    routes = [(fp, fm)] if (fp and fm) else _HF_VIDEO_ROUTES
    for provider, model in routes:
        try:
            cli = InferenceClient(provider=provider, api_key=key, timeout=600)
            vid = cli.text_to_video(prompt, model=model)
            if vid:
                return bytes(vid), "mp4"
        except Exception:
            continue  # provider suivant (credits epuises, modele indispo, etc.)
    return None

def _pollinations_image_backend(prompt, params):
    """Image via Pollinations.ai — GRATUIT, SANS CLE, illimite (aucun credit requis).
    Backend d'image le plus robuste : marche meme si HF est a court de credits. -> (bytes,'jpg')."""
    import urllib.parse
    try:
        w = int(params.get("width", 1024)); h = int(params.get("height", 1024))
        seed = int(params.get("seed") or (abs(hash(prompt)) % 100000))
        url = (f"https://image.pollinations.ai/prompt/{urllib.parse.quote(prompt)}"
               f"?width={w}&height={h}&nologo=true&model=flux&seed={seed}")
        r = _http.get(url, timeout=120)
        if r.status_code == 200 and "image" in r.headers.get("content-type", ""):
            return r.content, "jpg"
    except Exception:
        return None
    return None

def _ltx_space_video_backend(prompt, params):
    """VRAIE video ANIMEE via le Space Hugging Face LTX-Video (GPU du Space, ZeroGPU) ->
    GRATUIT, sans cle ni credit, sans le GPU de l'utilisateur. Clip ~5 s, SILENCIEUX.
    METHODE : image-to-video. On genere D'ABORD une image FIDELE du sujet (Pollinations),
    puis on l'ANIME. C'est bien meilleur que le text-to-video pur sur deux plans, constate :
      - FIDELITE : le personnage ressemble a ce qui est demande (le t2v pur l'inventait).
      - MOUVEMENT : l'animation d'une image de reference bouge vraiment (mesure ~20-44 de
        difference inter-frames, contre ~1.5 en t2v pur = quasi statique).
    -> (bytes,'mp4') ou None (echec -> backends suivants)."""
    try:
        from gradio_client import Client, handle_file
    except Exception:
        return None
    import urllib.parse, tempfile
    W, H = 768, 512
    neg = (params.get("negative_prompt")
           or "static, still image, no motion, frozen, worst quality, blurry, distorted")
    # 1) Image de reference fidele (gratuite, Pollinations)
    ref = None
    try:
        ip = f"{prompt}, highly detailed, cinematic, dramatic dynamic pose, 4k"
        url = (f"https://image.pollinations.ai/prompt/{urllib.parse.quote(ip)}"
               f"?width={W}&height={H}&nologo=true&model=flux&seed=77")
        r = _http.get(url, timeout=120)
        if r.status_code == 200 and "image" in r.headers.get("content-type", ""):
            ref = os.path.join(tempfile.gettempdir(), f"devllma_ltxref_{os.getpid()}.png")
            open(ref, "wb").write(r.content)
    except Exception:
        ref = None
    # 2) Animation LTX : image-to-video si on a une reference, sinon text-to-video
    motion = (f"{prompt}, dynamic dramatic cinematic motion, glowing energy and particles moving, "
              f"cape and cloth flowing, camera slowly pushing in")
    try:
        c = Client("Lightricks/ltx-video-distilled")
        common = dict(negative_prompt=neg, height_ui=H, width_ui=W,
                      duration_ui=float(params.get("seconds", 5)), ui_frames_to_use=9,
                      seed_ui=42, randomize_seed=True,
                      ui_guidance_scale=float(params.get("guidance", 1.5)), improve_texture_flag=True)
        if ref:
            res = c.predict(prompt=motion, input_image_filepath=handle_file(ref),
                            input_video_filepath=None, mode="image-to-video",
                            api_name="/image_to_video", **common)
        else:
            res = c.predict(prompt=motion, input_image_filepath=None,
                            input_video_filepath=None, mode="text-to-video",
                            api_name="/text_to_video", **common)
        vid = res[0] if isinstance(res, (list, tuple)) else res
        path = vid.get("video") if isinstance(vid, dict) else vid
        if path and os.path.exists(path):
            return open(path, "rb").read(), "mp4"
    except Exception:
        return None
    finally:
        if ref:
            try: os.remove(ref)
            except Exception: pass
    return None

def _slideshow_video_backend(prompt, params):
    """Video GRATUITE, sans GPU ni credit : genere 4 images (Pollinations) declinant le prompt,
    puis les assemble en mp4 avec effet zoom/pan cinematographique (Ken Burns) via ffmpeg
    embarque (imageio-ffmpeg). Video SILENCIEUSE (~11 s). C'est le filet de securite video quand
    fal-ai (credits) et le worker Colab (GPU) sont indisponibles. -> (bytes,'mp4') ou None."""
    import io, os, tempfile, urllib.parse
    try:
        import numpy as np, imageio
        from PIL import Image, ImageOps
    except Exception:
        return None  # dependances media absentes -> pas de repli slideshow
    OUT_W, OUT_H, FPS = 1280, 720, 24
    NFR = int(2.6 * FPS)
    variants = ["cinematic wide establishing shot", "dramatic dynamic action moment",
                "epic intense close-up", "atmospheric majestic finale"]
    pans = [(0.05, -0.03), (-0.04, 0.03), (0.06, 0.0), (-0.05, -0.02)]
    imgs = []
    for i, v in enumerate(variants):
        p = f"{prompt}, {v}, highly detailed, cinematic, epic, 4k"
        url = (f"https://image.pollinations.ai/prompt/{urllib.parse.quote(p)}"
               f"?width=1280&height=720&nologo=true&model=flux&seed={1000 + i * 7}")
        try:
            r = _http.get(url, timeout=120)
            if r.status_code == 200 and "image" in r.headers.get("content-type", ""):
                imgs.append(Image.open(io.BytesIO(r.content)).convert("RGB"))
        except Exception:
            pass
    if len(imgs) < 2:
        return None
    def ken_burns(img, n, px, py, zoom=0.16):
        big = ImageOps.fit(img, (OUT_W * 2, OUT_H * 2), Image.LANCZOS); BW, BH = big.size; out = []
        for i in range(n):
            t = i / max(1, n - 1); z = 1.0 + zoom * t; cw, ch = BW / z, BH / z
            cx = BW / 2 + px * BW * (t - 0.5); cy = BH / 2 + py * BH * (t - 0.5)
            l = max(0, min(BW - cw, cx - cw / 2)); u = max(0, min(BH - ch, cy - ch / 2))
            out.append(np.asarray(big.crop((int(l), int(u), int(l + cw), int(u + ch))).resize((OUT_W, OUT_H), Image.LANCZOS)))
        return out
    scenes = [ken_burns(im, NFR, *pans[i % len(pans)]) for i, im in enumerate(imgs)]
    tmp = os.path.join(tempfile.gettempdir(), f"devllma_slideshow_{os.getpid()}.mp4")
    try:
        w = imageio.get_writer(tmp, fps=FPS, codec="libx264", quality=7,
                               macro_block_size=8, ffmpeg_params=["-pix_fmt", "yuv420p"])
        for si, frames in enumerate(scenes):
            for f in frames:
                w.append_data(f)
            if si < len(scenes) - 1:  # fondu enchaine 10 frames
                a = frames[-1].astype(np.float32); b = scenes[si + 1][0].astype(np.float32)
                for k in range(10):
                    al = (k + 1) / 11; w.append_data((a * (1 - al) + b * al).astype(np.uint8))
        w.close()
        data = open(tmp, "rb").read()
        return data, "mp4"
    except Exception:
        return None
    finally:
        try: os.remove(tmp)
        except Exception: pass

def _colab_backend(task, prompt, params):
    """Image/video via le worker GPU Colab (si configure). -> (bytes, ext) ou None."""
    import base64
    base = mem_get("colab_url")
    if not base:
        return None
    # ngrok-skip-browser-warning : ngrok gratuit renvoie sinon une page d'avertissement HTML
    # au lieu du JSON du worker -> parsing casse. Cet en-tete la contourne.
    headers = {"Content-Type": "application/json", "ngrok-skip-browser-warning": "1"}
    tok = mem_get("colab_token")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    try:
        r = _http.post(base + "/run", headers=headers,
                       json={"task": task, "prompt": prompt, "params": params or {}}, timeout=600)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        body = r.json()
    except Exception:
        return None
    b64 = body.get("file_base64")
    if not b64:
        return None
    ext = (body.get("ext") or _MEDIA_EXT.get(task, "bin")).lstrip(".")
    try:
        return base64.b64decode(b64), ext
    except Exception:
        return None

def _tool_generate_media(args):
    task = (args.get("task") or "image").strip().lower()
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return {"error": "prompt vide"}
    # Config persistante (cles/url donnees une fois, memorisees ensuite)
    for k in ("hf_token", "hf_image_model", "colab_url", "colab_token"):
        v = (args.get(k) or "").strip()
        if v:
            mem_set(k, v.rstrip("/") if k == "colab_url" else v)
    params = dict(args.get("params") or {})
    # Enrichissement qualite du prompt image (mains/details) — desactivable via params.enhance=False.
    # Fait UNE fois ici pour que les DEUX backends (HF + Colab) en beneficient identiquement.
    if task == "image" and params.get("enhance") is not False:
        prompt = _enhance_image_prompt(prompt)
    # Prompt negatif par defaut pour le backend Colab (diffusers/SDXL l'exploite) si non fourni.
    if task == "image" and "negative_prompt" not in params:
        params["negative_prompt"] = _IMG_NEGATIVE
    data = ext = backend = None
    def _use(res, name):
        # n'assigne que si rien n'a encore abouti ET que ce backend a produit un resultat
        nonlocal data, ext, backend
        if data is None and res:
            data, ext, backend = res[0], res[1], name
    # Ordre ADAPTE A LA TACHE, du plus GRATUIT/FIABLE au plus contraint :
    #  - IMAGE : Pollinations D'ABORD (gratuit, sans cle, illimite -> marche TOUJOURS),
    #    puis Hugging Face (si credits), puis GPU Colab (si worker allume).
    #  - VIDEO : Hugging Face fal-ai (vraie video IA, consomme des credits) -> GPU Colab
    #    (si worker) -> DIAPORAMA local gratuit (images Pollinations + montage zoom/pan
    #    ffmpeg) qui reussit TOUJOURS sans GPU ni credit (video silencieuse ~11 s).
    if task == "image":
        _use(_pollinations_image_backend(prompt, params), "Pollinations (gratuit)")
        if data is None: _use(_hf_image_backend(prompt, params), "Hugging Face")
        if data is None: _use(_colab_backend("image", prompt, params), "GPU Colab")
    elif task == "video":
        # 1) LTX-Video Space : VRAIE video animee, GRATUITE (GPU du Space), sans credit.
        _use(_ltx_space_video_backend(prompt, params), "LTX-Video (Space HF, gratuit)")
        # 2) fal-ai : vraie video IA de meilleure qualite mais consomme des credits HF.
        if data is None: _use(_hf_video_backend(prompt, params), "Hugging Face (fal-ai)")
        # 3) worker GPU Colab si allume.
        if data is None: _use(_colab_backend("video", prompt, params), "GPU Colab")
        # 4) filet ultime : diaporama anime local (marche toujours, mais images animees).
        if data is None: _use(_slideshow_video_backend(prompt, params), "Diaporama local (gratuit)")
    else:
        _use(_colab_backend(task, prompt, params), "GPU Colab")
    if data is None:
        if not mem_get("hf_token") and not mem_get("colab_url"):
            return {"error": "aucun backend media configure : demande a l'utilisateur une cle Hugging Face "
                             "(hf_token='hf_...') pour la voie hebergee gratuite, ou l'URL d'un worker Colab."}
        if task == "video":
            return {"error": "generation video echouee (providers Hugging Face video indisponibles ou "
                             "credits Inference epuises, et worker GPU Colab injoignable). Reessaie dans "
                             "quelques minutes ; si ca persiste, verifie tes credits Inference Providers HF."}
        return {"error": "generation image impossible (cle HF invalide/quota, modele en chargement, "
                         "et worker Colab injoignable). Reessaie dans 1 min ou verifie la cle Hugging Face."}
    out_dir = os.path.join(WORKSPACE_DIR, "media_out")
    os.makedirs(out_dir, exist_ok=True)
    stem = re.sub(r'[^\w]+', '_', prompt.lower())[:40].strip('_') or task
    path = os.path.join(out_dir, f"{stem}.{ext}")
    i = 2
    while os.path.exists(path):
        path = os.path.join(out_dir, f"{stem}_{i}.{ext}"); i += 1
    try:
        with open(path, "wb") as f:
            f.write(data)
    except Exception as e:
        return {"error": f"ecriture du fichier impossible : {e}"}
    return {"path": path, "task": task, "backend": backend, "note": f"{task} genere via {backend}"}

def _tool_colab_run(args):  # compat : ancien nom -> logique unifiee
    return _tool_generate_media(args)

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
    "generate_media": _tool_generate_media,
    "colab_run": _tool_colab_run,  # compat (ancien nom)
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
- Donnees CSV -> csv_analyze d'abord. LIRE une image -> read_image.
- GENERER une image ou une video (ce poste CPU ne peut pas) -> generate_media, AUTOMATIQUEMENT
  et sans demander confirmation (backend heberge Hugging Face en priorite, GPU Colab en repli).
  Si l'outil dit "aucun backend configure", explique a l'utilisateur qu'il faut une cle Hugging
  Face (gratuite) ou un worker Colab, puis reessaie. Ne tente JAMAIS de generer en local.
- APIs sans cle pour http_request : api.open-meteo.com/v1/forecast?latitude=..&longitude=..&current_weather=true ;
  api.frankfurter.app/latest?from=EUR&to=USD ; fr.wikipedia.org/api/rest_v1/page/summary/<Titre>.
- Resultat produit (fichier/dossier) -> termine par open_path pour l'ouvrir a l'utilisateur.
  N'utilise JAMAIS run_powershell pour lancer une application graphique (notepad, mspaint,
  explorer...) a la place ou en plus de open_path : ce service tourne SANS session Bureau
  interactive, une appli graphique lancee ainsi ne peut jamais etre fermee -> le process reste
  bloque jusqu'au timeout (60s) puis survit orpheline indefiniment (constate : notepad.exe reste
  actif en tache de fond apres un appel run_powershell "notepad fichier.md"). open_path (via
  os.startfile) est deja non-bloquant et gere ce cas correctement -> un seul appel suffit.
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
        from ollama_client import _opts
        _http.post(OLLAMA + "/api/chat", json={
            "model": AGENT_MODEL,
            "messages": [{"role": "system", "content": AGENT_SYSTEM},
                          {"role": "user", "content": "ping"}],
            "tools": TOOLS, "stream": False, "keep_alive": KEEP_ALIVE,
            # MEMES options que _chat_call (num_ctx/num_thread/num_batch/sampling) : le warmup
            # doit charger le modele avec exactement les reglages du runtime, sinon inutile.
            "options": _opts(num_predict=1),
        }, timeout=600)
        return True
    except Exception:
        return False


def _chat_call(messages, tools=None, temperature=0.3):
    # Memes reglages CPU + echantillonnage que le pipeline de dev (num_thread/num_batch/
    # sampling), via le helper partage d'ollama_client : coherence obligatoire car keep_alive=-1
    # fige les parametres du premier appel qui charge le modele.
    from ollama_client import _opts
    payload = {
        "model": AGENT_MODEL,
        "messages": messages,
        "stream": False,
        "keep_alive": KEEP_ALIVE,
        "options": _opts(temperature=temperature),
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


# qwen3-coder EMET PARFOIS ses appels d'outils en TEXTE au lieu du format tool_calls natif
# d'Ollama, p.ex. :
#     <function=generate_media>
#     <parameter=task>video</parameter>
#     <parameter=prompt>...</parameter>
#     </function>
# Ollama ne parse PAS ce format -> message.tool_calls est vide, l'agent croit avoir fini et
# l'outil n'est JAMAIS execute (l'agent "parle" de l'action sans la faire = "il ne bouge pas",
# constate en prod sur une demande video). On reconnait ce format et on reconstruit des
# tool_calls exploitables.
_TEXT_FUNC_RE = re.compile(r'<function\s*=\s*([A-Za-z0-9_]+)\s*>(.*?)(?:</function>|\Z)', re.DOTALL)
_TEXT_PARAM_RE = re.compile(r'<parameter\s*=\s*([A-Za-z0-9_]+)\s*>(.*?)</parameter>', re.DOTALL)

def _parse_text_tool_calls(content):
    calls = []
    for fm in _TEXT_FUNC_RE.finditer(content or ""):
        name = fm.group(1)
        args = {k: v.strip() for k, v in _TEXT_PARAM_RE.findall(fm.group(2))}
        calls.append({"function": {"name": name, "arguments": args}})
    return calls


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
            # Repli : le modele a peut-etre emis l'appel d'outil en TEXTE (<function=...>)
            # au lieu du format natif -> on le parse et on l'execute quand meme.
            parsed = _parse_text_tool_calls(message.get("content", ""))
            if parsed:
                tool_calls = parsed
            else:
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
            # Les outils LECTURE SEULE sont exemptes du blocage anti-repetition : re-lire
            # un fichier APRES l'avoir modifie (verification post-edition) est un appel
            # "identique" mais LEGITIME et souhaitable — le bloquer empechait justement
            # l'etape verifier-apres-changement qu'on veut encourager. Seuls les outils a
            # effet de bord (ecriture/exec/etc.) restent proteges contre les boucles.
            _READONLY_TOOLS = {"read_file", "read_lines", "list_dir", "grep_search",
                               "find_files", "read_image", "get_datetime"}
            if impl is None:
                result = {"error": f"outil inconnu: {name}"}
            elif sig in seen_calls and name not in _READONLY_TOOLS:
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
