"""
DevLLMA — Système de Skills + Sauvegardes + Sécurité
Module autonome importé par webui.py

Fournit :
- SnapshotManager : sauvegarde réversible avant chaque modif (rollback possible)
- Skills : capacités de dev réutilisables (git, venv, tests, format, lint)
- safety_check : garde-fou qui bloque le code destructeur AVANT exécution
- security_scan : détecte les failles dans le code généré (injections, secrets, eval…)
- BrainMemory : mémoire persistante du cerveau (SQLite), chargée à chaque dev
"""
import os, re, shutil, subprocess, sqlite3, json
from datetime import datetime

DEVLLMA   = r"C:\Devllma"
WORKSPACE = os.path.join(DEVLLMA, "workspace")
BACKUPS   = os.path.join(DEVLLMA, "backups")
DB        = os.path.join(DEVLLMA, "database", "devllma.db")
PYTHON    = r"C:\Users\Admin\AppData\Local\Programs\Python\Python311\python.exe"
GIT       = "git"

os.makedirs(BACKUPS, exist_ok=True)

IGNORE_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "venv", ".backups"}


# ════════════════════════════════════════════════════════════════════════════
#  DB — tables additionnelles (backups + skills_log)
# ════════════════════════════════════════════════════════════════════════════
def _cx():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    return sqlite3.connect(DB)

def init_skills_db():
    with _cx() as c:
        c.executescript("""
CREATE TABLE IF NOT EXISTS backups(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT, label TEXT, path TEXT,
    files INTEGER, ts TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS skills_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT, skill TEXT, ok INTEGER,
    output TEXT, ts TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS brain_state(
    key TEXT PRIMARY KEY, value TEXT,
    ts TEXT DEFAULT CURRENT_TIMESTAMP);
""")


# ════════════════════════════════════════════════════════════════════════════
#  extract_files — extracteur ROBUSTE universel (tous formats de LLM)
# ════════════════════════════════════════════════════════════════════════════
_FNAME = r'[\w./\\-]+\.\w{1,5}'
_LANG_DEFAULT = {
    "python":"main.py","py":"main.py","javascript":"index.js","js":"index.js",
    "typescript":"index.ts","ts":"index.ts","html":"index.html","css":"style.css",
    "sql":"schema.sql","bash":"run.sh","sh":"run.sh","json":"config.json",
    "yaml":"config.yml","yml":"config.yml","txt":"requirements.txt","text":"requirements.txt",
    "dockerfile":"Dockerfile","md":"README.md","markdown":"README.md",
}

def extract_files(text):
    """Extrait [(nom, code)] depuis une réponse LLM.
    PRIORITÉ 1: format strict ###FILE: nom ... ###ENDFILE (non ambigu).
    FALLBACK: parsing markdown (**fichier**, ### N. `fichier`, langage...)."""
    # ---- Format strict ###FILE: --------------------------------------------
    strict = re.findall(r'#{2,3}\s*FILE:\s*([^\n]+?)\s*\n([\s\S]*?)#{2,3}\s*ENDFILE',
                        text, re.IGNORECASE)
    if strict:
        out, seen = [], set()
        for name, body in strict:
            name = name.strip().strip("`*").replace("\\", "/").lstrip("/")
            # retirer un éventuel fence markdown ou balise <code>/<pre> a l'interieur
            # (le modele emet parfois <code>...</code> au lieu de ``` — sans ce nettoyage,
            # la balise se retrouve ecrite en dur en tete de fichier -> SyntaxError immediat)
            body = body.strip()
            body = re.sub(r'^```[\w+-]*\r?\n', '', body)
            body = re.sub(r'\n```\s*$', '', body)
            body = re.sub(r'^<(?:code|pre)>\s*\r?\n?', '', body, flags=re.IGNORECASE)
            body = re.sub(r'\r?\n?</(?:code|pre)>\s*$', '', body, flags=re.IGNORECASE)
            body = body.strip()
            if name and len(body) > 0 and name not in seen:
                out.append((name, body)); seen.add(name)
        if out:
            return out

    # ---- Fallback markdown -------------------------------------------------
    files, seen = [], set()
    # Repère tous les blocs de code avec leur position
    for m in re.finditer(r'```([\w+-]*)\r?\n([\s\S]*?)```', text):
        lang = (m.group(1) or "").lower().strip()
        code = m.group(2).strip()
        if len(code) < 5:
            continue
        fname = None

        # 1) Chercher un nom de fichier dans les 3 lignes précédant le bloc
        before = text[:m.start()].rstrip()
        prev_lines = before.split("\n")[-3:]
        for line in reversed(prev_lines):
            # **fichier.ext**  |  ### 2. `fichier.ext`  |  `fichier.ext`  |  Fichier: x.ext
            fm = re.search(r'[`*]*\s*(?:\d+[\.\)]\s*)?[`*]*\s*(' + _FNAME + r')\s*[`*:]*\s*$', line.strip())
            if fm and "." in fm.group(1):
                cand = fm.group(1).strip(" `*:")
                # éviter de capturer une phrase entière
                if len(cand) <= 60 and "/" not in cand[:1]:
                    fname = cand
                    break

        # 2) Sinon, 1re ligne de commentaire dans le code (# fichier.ext)
        if not fname:
            cm = re.match(r'(?:#|//)\s*(' + _FNAME + r')\s*$', code.split("\n")[0].strip())
            if cm:
                fname = cm.group(1).strip()

        # 3) Sinon, déduire du langage
        if not fname:
            fname = _LANG_DEFAULT.get(lang, ("output." + lang) if lang else "output.txt")

        # Normaliser / dédupliquer
        fname = fname.replace("\\", "/").lstrip("/")
        if fname not in seen:
            files.append((fname, code)); seen.add(fname)
        else:
            # même nom => fusionner (ex: 2 blocs pour le même fichier)
            for i,(n,c) in enumerate(files):
                if n == fname:
                    files[i] = (n, c + "\n\n" + code); break
    return files


# ════════════════════════════════════════════════════════════════════════════
#  SnapshotManager — sauvegardes réversibles
# ════════════════════════════════════════════════════════════════════════════
class SnapshotManager:
    """Sauvegarde un projet avant modification. Permet le rollback."""

    @staticmethod
    def _project_backup_dir(project):
        d = os.path.join(BACKUPS, project)
        os.makedirs(d, exist_ok=True)
        return d

    @staticmethod
    def snapshot(project_dir, label="auto", keep=10):
        """Copie l'état actuel du projet dans backups/<projet>/<timestamp>/.
        Retourne (snapshot_id, nb_fichiers) ou (None, 0) si le projet n'existe pas."""
        if not os.path.isdir(project_dir):
            return None, 0
        project = os.path.basename(project_dir.rstrip("\\/"))
        stamp   = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest    = os.path.join(SnapshotManager._project_backup_dir(project), stamp)

        n = 0
        for root, dirs, files in os.walk(project_dir):
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
            for f in files:
                src = os.path.join(root, f)
                rel = os.path.relpath(src, project_dir)
                dst = os.path.join(dest, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                try:
                    shutil.copy2(src, dst); n += 1
                except Exception:
                    pass

        if n == 0:
            if os.path.isdir(dest):
                shutil.rmtree(dest, ignore_errors=True)
            return None, 0

        sid = None
        with _cx() as c:
            sid = c.execute(
                "INSERT INTO backups(project,label,path,files) VALUES(?,?,?,?)",
                (project, label, dest, n)).lastrowid

        SnapshotManager._prune(project, keep)
        return sid, n

    @staticmethod
    def _prune(project, keep):
        """Garde uniquement les `keep` derniers snapshots."""
        with _cx() as c:
            rows = c.execute(
                "SELECT id,path FROM backups WHERE project=? ORDER BY id DESC",
                (project,)).fetchall()
        for bid, path in rows[keep:]:
            shutil.rmtree(path, ignore_errors=True)
            with _cx() as c:
                c.execute("DELETE FROM backups WHERE id=?", (bid,))

    @staticmethod
    def list_snapshots(project):
        with _cx() as c:
            return c.execute(
                "SELECT id,label,files,ts FROM backups WHERE project=? ORDER BY id DESC",
                (project,)).fetchall()

    @staticmethod
    def restore(snapshot_id, project_dir):
        """Restaure un snapshot vers le projet (écrase l'état courant)."""
        with _cx() as c:
            row = c.execute("SELECT path FROM backups WHERE id=?", (snapshot_id,)).fetchone()
        if not row or not os.path.isdir(row[0]):
            return False, "Snapshot introuvable"
        src = row[0]
        # Vider le projet (sauf dossiers ignorés) puis recopier
        for root, dirs, files in os.walk(project_dir):
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
            for f in files:
                try: os.remove(os.path.join(root, f))
                except Exception: pass
        n = 0
        for root, dirs, files in os.walk(src):
            for f in files:
                s = os.path.join(root, f)
                rel = os.path.relpath(s, src)
                d = os.path.join(project_dir, rel)
                os.makedirs(os.path.dirname(d), exist_ok=True)
                shutil.copy2(s, d); n += 1
        return True, f"{n} fichiers restaurés"


# ════════════════════════════════════════════════════════════════════════════
#  safety_check — garde-fou : bloque le code DESTRUCTEUR avant exécution
# ════════════════════════════════════════════════════════════════════════════
# Motifs qui ne doivent JAMAIS être exécutés sur le PC (protection système).
# Garde-fou best-effort : des regex ne peuvent PAS être exhaustives face à
# l'obfuscation (concaténation de chaînes, variables, iex, base64 imbriqué...).
# Le vrai contrôle est ailleurs : _DENY_PREFIXES/_guard_path (agent_core.py) pour
# les chemins, les journaux d'audit _audit_log/_audit_jsonl (agent_core.py) pour
# la trace de ce qui a réellement tourné. Ne pas empiler des dizaines de motifs
# fragiles ici : seuls les motifs sans usage légitime plausible et à très faible
# risque de faux positif.
DANGER_PATTERNS = [
    (r'shutil\.rmtree\s*\(\s*[\'"]?[A-Za-z]:[\\/]?[\'"]?\s*\)', "rmtree sur une racine de disque"),
    (r'rmdir\s+/s\s+/q\s+[A-Za-z]:', "rmdir récursif sur un disque"),
    (r'Remove-Item.*-Recurse.*[A-Za-z]:\\(?:Windows|Users|Program)', "Remove-Item sur dossier système"),
    (r'format\s+[A-Za-z]:', "formatage de disque"),
    (r'os\.system\s*\(\s*[\'"](?:rm\s+-rf|del\s+/|format)', "commande système destructrice"),
    (r'subprocess.*[\'"](?:rm\s+-rf\s+/|format\s+c:)', "subprocess destructeur"),
    (r'reg\s+delete\s+HK', "suppression de clé registre"),
    (r'(?:diskpart|cipher\s+/w|sdelete)', "outil d'effacement disque"),
    (r'while\s+True\s*:(?:[^\n]*\n)?\s*(?:os\.fork|subprocess|Thread)', "fork-bomb potentielle"),
    (r':\(\)\s*\{\s*:\|:&\s*\}\s*;', "fork-bomb bash"),
    (r'-enc(?:odedcommand)?\s+[A-Za-z0-9+/=]{16,}', "commande PowerShell encodée base64 (contournement d'inspection)"),  # >=16 cars base64 : '-enc utf8'/'-Encoding utf8' passent ; FP residuel accepte hors PowerShell
    (r'Set-MpPreference\s[^\n]*-Disable', "désactivation de Windows Defender"),
    (r'vssadmin\s+delete\s+shadows', "suppression des clichés instantanés (schéma ransomware)"),
    (r'bcdedit\s+/(?:set|delete|deletevalue)', "modification de la configuration de démarrage Windows"),
    (r'\b(?:ri|rm|rd|del|erase)\s[^\n;|&]*-Recurse[^\n;|&]*[A-Za-z]:\\(?:Windows|Users|Program)', "suppression récursive sur dossier système via alias PowerShell"),  # [^;|&] : ne pas enjamber un separateur d'instructions
]

def safety_check(code):
    """Retourne (safe, [raisons]). safe=False => NE PAS exécuter."""
    reasons = []
    for pat, desc in DANGER_PATTERNS:
        if re.search(pat, code, re.IGNORECASE):
            reasons.append(desc)
    return (len(reasons) == 0), reasons


# ════════════════════════════════════════════════════════════════════════════
#  security_scan — détecte les FAILLES dans le code généré
# ════════════════════════════════════════════════════════════════════════════
SECURITY_RULES = [
    # (regex, sévérité, message)
    (r'(?i)(?:password|passwd|secret|api_?key|token)\s*=\s*[\'"][^\'"\n]{4,}[\'"]',
        "HAUTE", "Secret/mot de passe en dur dans le code"),
    (r'(?i)execute\s*\(\s*[f]?[\'"].*?(?:%s|\{|\+)',
        "HAUTE", "Injection SQL possible (requête construite par concaténation)"),
    (r'\beval\s*\(',
        "HAUTE", "Usage de eval() — exécution de code arbitraire"),
    (r'\bexec\s*\(',
        "HAUTE", "Usage de exec() — exécution de code arbitraire"),
    (r'subprocess\.\w+\([^)]*shell\s*=\s*True',
        "MOYENNE", "subprocess avec shell=True — injection de commande"),
    (r'(?i)pickle\.loads?\s*\(',
        "MOYENNE", "Désérialisation pickle — RCE si donnée non fiable"),
    (r'(?i)yaml\.load\s*\((?![^)]*Loader)',
        "MOYENNE", "yaml.load sans SafeLoader"),
    (r'(?i)debug\s*=\s*True',
        "BASSE", "Mode debug activé (à désactiver en production)"),
    (r'(?i)allow_origins\s*=\s*\[\s*[\'"]\*[\'"]',
        "BASSE", "CORS ouvert à tous (*)"),
    (r'(?i)verify\s*=\s*False',
        "MOYENNE", "Vérification SSL désactivée"),
    (r'(?i)md5\s*\(',
        "BASSE", "MD5 — algorithme de hash obsolète"),
    (r'\.\./\.\./',
        "MOYENNE", "Path traversal possible (../..)"),
]

def security_scan(files):
    """files = {nom: contenu}. Retourne liste de findings triés par sévérité."""
    findings = []
    order = {"HAUTE": 0, "MOYENNE": 1, "BASSE": 2}
    for fname, content in files.items():
        for pat, sev, msg in SECURITY_RULES:
            for m in re.finditer(pat, content):
                line = content[:m.start()].count("\n") + 1
                findings.append({
                    "file": fname, "line": line,
                    "severity": sev, "message": msg,
                    "snippet": m.group(0)[:80]
                })
    findings.sort(key=lambda f: order.get(f["severity"], 3))
    return findings


# ════════════════════════════════════════════════════════════════════════════
#  Skills — capacités de dev réutilisables
# ════════════════════════════════════════════════════════════════════════════
def _run(cmd, cwd, timeout=120):
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        return r.returncode == 0, (r.stdout + r.stderr).strip()[:500]
    except FileNotFoundError:
        return False, f"Introuvable: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return False, f"Timeout ({timeout}s)"
    except Exception as e:
        return False, str(e)

def _log_skill(project, skill, ok, output):
    with _cx() as c:
        c.execute("INSERT INTO skills_log(project,skill,ok,output) VALUES(?,?,?,?)",
                  (project, skill, 1 if ok else 0, (output or "")[:500]))

class Skills:
    """Capacités appelables sur un projet."""

    @staticmethod
    def git_init(project_dir):
        proj = os.path.basename(project_dir.rstrip("\\/"))
        if os.path.isdir(os.path.join(project_dir, ".git")):
            return True, "Dépôt git déjà initialisé"
        ok, out = _run([GIT, "init"], project_dir)
        if ok:
            _run([GIT, "add", "."], project_dir)
            _run([GIT, "-c", "user.email=dev@llma.local",
                  "-c", "user.name=DevLLMA", "commit", "-m", "Initial commit"], project_dir)
            out = "Dépôt git initialisé + premier commit"
        _log_skill(proj, "git_init", ok, out)
        return ok, out

    @staticmethod
    def git_commit(project_dir, message="Modif DevLLMA"):
        proj = os.path.basename(project_dir.rstrip("\\/"))
        if not os.path.isdir(os.path.join(project_dir, ".git")):
            Skills.git_init(project_dir)
        _run([GIT, "add", "."], project_dir)
        ok, out = _run([GIT, "-c", "user.email=dev@llma.local",
                        "-c", "user.name=DevLLMA", "commit", "-m", message], project_dir)
        _log_skill(proj, "git_commit", ok, out)
        return ok, out

    @staticmethod
    def run_tests(project_dir):
        proj = os.path.basename(project_dir.rstrip("\\/"))
        has_tests = any(
            f.startswith("test_") or f.endswith("_test.py") or f == "tests"
            for f in os.listdir(project_dir)
        ) if os.path.isdir(project_dir) else False
        if not has_tests:
            return None, "Aucun test trouvé"
        ok, out = _run([PYTHON, "-m", "pytest", "-q", "--no-header"], project_dir, timeout=90)
        _log_skill(proj, "run_tests", ok, out)
        return ok, out

    @staticmethod
    def format_code(project_dir):
        proj = os.path.basename(project_dir.rstrip("\\/"))
        ok, out = _run([PYTHON, "-m", "black", "-q", "."], project_dir, timeout=60)
        if not ok and "No module" in out:
            return None, "black non installé"
        _log_skill(proj, "format_code", ok, out)
        return ok, out or "Code formaté"

    @staticmethod
    def lint(project_dir):
        proj = os.path.basename(project_dir.rstrip("\\/"))
        ok, out = _run([PYTHON, "-m", "pyflakes", "."], project_dir, timeout=60)
        if not ok and "No module" in out:
            return None, "pyflakes non installé"
        _log_skill(proj, "lint", ok, out or "Aucun problème détecté")
        return ok, out or "Aucun problème détecté"


# ════════════════════════════════════════════════════════════════════════════
#  BrainMemory — mémoire persistante du cerveau (survit aux redémarrages)
# ════════════════════════════════════════════════════════════════════════════
class BrainMemory:
    @staticmethod
    def save(key, value):
        with _cx() as c:
            c.execute("INSERT OR REPLACE INTO brain_state(key,value,ts) VALUES(?,?,?)",
                      (key, json.dumps(value, ensure_ascii=False), datetime.now().isoformat()))

    @staticmethod
    def load(key, default=None):
        with _cx() as c:
            r = c.execute("SELECT value FROM brain_state WHERE key=?", (key,)).fetchone()
        if r:
            try: return json.loads(r[0])
            except Exception: return default
        return default

    @staticmethod
    def append_event(event):
        """Ajoute un événement à l'historique persistant du brain."""
        hist = BrainMemory.load("history", [])
        hist.append({"event": event, "ts": datetime.now().isoformat()})
        BrainMemory.save("history", hist[-50:])  # garde les 50 derniers

    @staticmethod
    def recent_events(n=8):
        return BrainMemory.load("history", [])[-n:]


# Initialiser à l'import
init_skills_db()
