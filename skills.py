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
import os, re, shutil, subprocess, sqlite3, json, ast
from datetime import datetime

DEVLLMA   = r"C:\Devllma"
WORKSPACE = os.path.join(DEVLLMA, "workspace")
BACKUPS   = os.path.join(DEVLLMA, "backups")
DB        = os.path.join(DEVLLMA, "database", "devllma.db")
PYTHON    = r"C:\Users\Admin\AppData\Local\Programs\Python\Python311\python.exe"
GIT       = "git"

os.makedirs(BACKUPS, exist_ok=True)

IGNORE_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "venv", ".backups",
               "build", "dist", "env", ".env", ".tox", "site-packages", ".mypy_cache",
               ".pytest_cache", ".ruff_cache"}


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
    # PowerShell absent -> tombait sur "output.powershell" (mauvaise extension, jamais
    # reconnue par find_entry_point/_interpreter_cmd qui n'attendent que ".ps1") : constate
    # sur un script genere en bloc ```powershell au lieu du format ###FILE, projet jamais
    # execute (project=None). "posh"/"pwsh" ajoutes par prudence (tags markdown alternatifs).
    "powershell":"main.ps1","ps1":"main.ps1","posh":"main.ps1","pwsh":"main.ps1",
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

    # ---- Fallback #FILE sans ###ENDFILE (marqueur de fin oublie/tronque) --
    # Constate (projet "api_04", recettes de cuisine) : le modele emet ###FILE:
    # pour chaque fichier mais oublie le tout DERNIER ###ENDFILE (reponse tronquee
    # ou generation interrompue) -> le regex strict ci-dessus ne matche RIEN et
    # l'INTEGRALITE du code genere est perdue silencieusement, sans aucune erreur
    # visible. On decoupe alors sur les seuls marqueurs ###FILE:, chaque fichier
    # allant jusqu'au prochain marqueur (ou la fin du texte).
    loose = list(re.finditer(r'#{2,3}\s*FILE:\s*([^\n]+?)\s*\n', text, re.IGNORECASE))
    if loose:
        out, seen = [], set()
        for i, m in enumerate(loose):
            name = m.group(1).strip().strip("`*").replace("\\", "/").lstrip("/")
            start = m.end()
            end = loose[i + 1].start() if i + 1 < len(loose) else len(text)
            body = text[start:end]
            body = re.sub(r'#{2,3}\s*ENDFILE\s*$', '', body, flags=re.IGNORECASE).strip()
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
#  static_self_check — analyse AST rapide, SANS appel LLM (auto-correction)
# ════════════════════════════════════════════════════════════════════════════
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
                ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)

def _iter_py_files(project_dir):
    """Itere (relpath, fullpath) sur CHAQUE .py du projet, SOUS-DOSSIERS INCLUS, en
    ignorant les dossiers techniques (IGNORE_DIRS). Les checks statiques doivent voir
    les projets multi-dossiers (app/main.py, app/database.py, que CODER_SYSTEM demande
    justement) — sinon tous les garde-fous AST etaient silencieusement contournes pour
    exactement les projets les plus susceptibles de tomber sur ces bugs (os.listdir ne
    voyait que la racine)."""
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
        for f in sorted(files):
            if f.endswith(".py"):
                full = os.path.join(root, f)
                rel = os.path.relpath(full, project_dir).replace("\\", "/")
                yield rel, full

def _assigned_names(target):
    """Noms lies par une cible d'affectation/boucle (for x in .., x,y = .., with .. as x)."""
    return {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}

def _scan_function_body(func_node, shadow_name):
    """Cherche un Call(shadow_name) dans le corps DIRECT de func_node, sans jamais
    descendre dans un sous-scope (fonction/lambda/comprehension imbriquee) qui a sa
    PROPRE resolution de noms — une closure ou une comprehension peuvent re-lier ou
    capturer ce nom differemment, et le determiner avec certitude demanderait une
    vraie analyse de scope. Par prudence (un faux positif ici POURRAIT casser du code
    correct via la correction automatique), on ignore ces sous-arbres plutot que de
    risquer une fausse alerte (constate en revue : closures, comprehensions et boucles
    for qui re-lient le nom localement produisaient des faux positifs a 100%).
    Retourne None si le nom est RE-LIE ailleurs dans le corps direct (for/assign/with) —
    dans ce cas la resolution reelle est trop ambigue, on prefere ne rien signaler."""
    rebound = False
    call_hit = None

    def visit(node):
        nonlocal rebound, call_hit
        if isinstance(node, _SCOPE_NODES):
            return  # nouveau scope : ne pas descendre, resolution de nom potentiellement differente
        if isinstance(node, (ast.For, ast.AsyncFor)) and shadow_name in _assigned_names(node.target):
            rebound = True
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if shadow_name in _assigned_names(t):
                    rebound = True
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.target is not None:
            if shadow_name in _assigned_names(node.target):
                rebound = True
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            if shadow_name in _assigned_names(node.optional_vars):
                rebound = True
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == shadow_name and call_hit is None):
            call_hit = node
        for child in ast.iter_child_nodes(node):
            visit(child)

    for child in ast.iter_child_nodes(func_node):
        visit(child)
    return None if rebound else call_hit

def static_self_check(project_dir):
    """Detecte par analyse AST (pas d'inference, quasi instantane) la classe de bug
    la plus couteuse constatee au banc de tests (14/07/2026) : un nom LOCAL (fonction
    de route, parametre) qui porte EXACTEMENT le meme nom qu'une fonction importee et
    qui est ensuite APPELE comme une fonction dans le meme scope -> le nom local ecrase
    l'import, l'appel invoque le mauvais objet (RecursionError si c'est la fonction qui
    s'appelle elle-meme, TypeError 'X object is not callable' si c'est un parametre).
    Rencontre sous 2 formes independantes (route FastAPI qui rappelle le CRUD du meme
    nom ; flag CLI booleen qui porte le nom de la fonction qu'il declenche) -> pattern
    generalisable, detectable sans jamais executer le code.
    Scope-aware (cf. _scan_function_body) : ignore closures/comprehensions/boucles qui
    re-lient le nom localement, pour eviter les faux positifs sur du code idiomatique
    correct (constate en revue de code independante avant mise en prod).
    Retourne une liste de descriptions (vide si rien trouve). Best-effort : toute
    erreur de parsing (syntaxe deja invalide, gere ailleurs par syntax_check) est
    simplement ignoree ici."""
    problems = []
    for fname, fpath in _iter_py_files(project_dir):
        try:
            src = open(fpath, encoding="utf-8").read()
            tree = ast.parse(src, filename=fname)
        except Exception:
            continue

        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    imported_names.add(alias.asname or alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported_names.add(alias.asname or alias.name.split(".")[0])
        if not imported_names:
            continue

        # FunctionDef ET AsyncFunctionDef : les routes FastAPI (idiome dominant) sont
        # presque toujours "async def" — un check limite a FunctionDef les ratait toutes
        # (constate en revue de code : "async def create_user(user): return create_user(user)"
        # passait inapercu, alors que l'equivalent synchrone etait detecte).
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            local_names = ({node.name} | {a.arg for a in node.args.args}
                           | {a.arg for a in node.args.kwonlyargs})
            shadowed = local_names & imported_names
            if not shadowed:
                continue
            for shadow_name in shadowed:
                hit = _scan_function_body(node, shadow_name)
                if hit is not None:
                    role = "le nom de la fonction" if shadow_name == node.name else "un parametre"
                    problems.append(
                        f"{fname}, fonction '{node.name}' (ligne {node.lineno}) : '{shadow_name}' est "
                        f"{role} ET porte EXACTEMENT le meme nom qu'un import de ce fichier — l'appel "
                        f"'{shadow_name}(...)' ligne {hit.lineno} invoque tres probablement {shadow_name} "
                        f"local (parametre ou la fonction elle-meme), PAS l'import prevu. Renomme le "
                        f"parametre/la fonction locale (ex: suffixe _flag/_route) ou importe le module "
                        f"entier et appelle-le via prefixe (module.{shadow_name}(...))."
                    )
    problems.extend(_check_cross_file_imports(project_dir))
    problems.extend(_check_sqlalchemy_engine_args(project_dir))
    return problems


def _check_sqlalchemy_engine_args(project_dir):
    """Detecte 'create_engine(..., check_same_thread=...)' -> TypeError au demarrage
    ('Invalid argument check_same_thread sent to create_engine'). check_same_thread est
    un argument du DBAPI sqlite3, PAS de create_engine : il doit passer par connect_args.
    Constate 3 fois independamment au banc de tests (api_03, social_01, social_07) et la
    boucle d'auto-correction n'a JAMAIS reussi a le corriger seule -> un check statique
    explicite avec la correction exacte est bien plus fiable ici. Detection AST sure :
    un appel a une fonction nommee create_engine avec un mot-cle check_same_thread."""
    problems = []
    for fname, fpath in _iter_py_files(project_dir):
        try:
            tree = ast.parse(open(fpath, encoding="utf-8").read())
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else None)
            if name != "create_engine":
                continue
            if any(kw.arg == "check_same_thread" for kw in node.keywords):
                problems.append(
                    f"{fname} (ligne {node.lineno}) : create_engine(..., check_same_thread=...) provoque "
                    f"un TypeError au demarrage. 'check_same_thread' est un argument du DBAPI sqlite3, PAS "
                    f"de create_engine. Corrige en le passant via connect_args : "
                    f"create_engine(URL, connect_args={{'check_same_thread': False}}). Retire-le des "
                    f"arguments directs de create_engine."
                )
    return problems


def _module_exported_names(tree):
    """Noms disponibles au niveau module (importables depuis l'exterieur) : fonctions,
    classes, variables de niveau module, et ce que le module lui-meme importe (re-export)."""
    names = set()
    for node in tree.body:  # niveau module UNIQUEMENT (pas ast.walk : evite les noms internes)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                names |= {n.id for n in ast.walk(t) if isinstance(n, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names

def _check_cross_file_imports(project_dir):
    """Detecte AVANT execution un 'from <module_local> import <nom>' ou <nom> N'EST PAS
    defini dans ce module local -> ImportError garanti au demarrage (constate a 2 reprises
    independantes au banc de tests : main.py importait 'scan_network'/'generate_report'
    alors que le module fournissait des fonctions aux noms DIFFERENTS). Purement statique,
    ne verifie QUE les modules LOCAUX du projet (un .py present dans project_dir) — jamais
    la stdlib ni les paquets pip (impossible a resoudre de facon fiable sans les importer)."""
    problems = []
    # 1) Cartographie des noms exportes par chaque module local du projet (sous-dossiers
    #    inclus). Clef = nom de module (basename sans .py) car la convention DevLLMA impose
    #    des imports PLATS ("from database import get_db"). Si deux dossiers ont un module
    #    de meme nom, on UNIONNE leurs exports : accepter un nom defini dans l'un OU l'autre
    #    evite un faux positif (qui declencherait a tort une correction).
    local_exports = {}
    for fname, fpath in _iter_py_files(project_dir):
        try:
            tree = ast.parse(open(fpath, encoding="utf-8").read())
        except Exception:
            return problems  # un fichier ne parse pas -> analyse peu fiable, on s'abstient
        mod_base = os.path.basename(fname)[:-3]
        local_exports.setdefault(mod_base, set()).update(_module_exported_names(tree))
    # 2) Verifie chaque import depuis un module local
    for fname, fpath in _iter_py_files(project_dir):
        try:
            tree = ast.parse(open(fpath, encoding="utf-8").read())
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level:  # ignore imports relatifs (.mod)
                continue
            parts = (node.module or "").split(".")
            # On ne verifie QUE les imports PLATS a un seul composant ("from database import X"),
            # qui sont la convention DevLLMA (CODER_SYSTEM impose des imports plats sans prefixe de
            # package) ET la forme des 2 vrais bugs constates (scanner, report_generator). Pour un
            # import POINTE ("from app.core import settings"), le dernier composant peut etre un
            # SOUS-MODULE (app/core/settings.py) et non un nom exporte par un fichier plat homonyme
            # -> resoudre "app.core" vers un "core.py" plat sans rapport produisait un FAUX POSITIF
            # (identifie en revue de code). On s'abstient donc sur les imports pointes.
            if len(parts) != 1:
                continue
            mod = parts[0] if parts[0] in local_exports else None
            if mod is None:
                continue
            available = local_exports[mod]
            for alias in node.names:
                if alias.name == "*":
                    continue
                if alias.name not in available:
                    problems.append(
                        f"{fname} (ligne {node.lineno}) : 'from {node.module} import {alias.name}' "
                        f"mais le module local '{mod}.py' ne definit PAS '{alias.name}' "
                        f"(il expose : {', '.join(sorted(available)) or 'rien'}). ImportError garanti au "
                        f"demarrage. Corrige le nom importe pour qu'il corresponde a une fonction/classe "
                        f"REELLEMENT definie dans {mod}.py (ou ajoute cette definition dans {mod}.py)."
                    )
    return problems


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
    # (?<![\'"\w.]) : ignore 'eval(' a l'interieur d'une chaine (ex: liste anti-XSS
    # ['eval(', ...]) ou d'un attribut (obj.eval(), re.eval...) -> evite le faux positif
    # constate en usage reel sur du code prudent qui LISTE eval comme motif a bloquer.
    (r'(?<![\'"\w.])eval\s*\(',
        "HAUTE", "Usage de eval() — exécution de code arbitraire"),
    (r'(?<![\'"\w.])exec\s*\(',
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


# Codes Ruff FATAUX (famille F = pyflakes) : erreurs REELLES qui plantent a l'execution.
# On ne garde QUE ceux-la pour ne jamais faire tourner la boucle de correction sur de la
# cosmetique. Couvre les hallucinations que py_compile + les checks AST maison ratent :
# nom non defini (F821), __all__ invalide (F822), variable locale avant affectation (F823),
# format strings casses (F50x), comparaisons/assertions fautives (F63x), 'break' hors boucle...
# F811 (redefinition) est VOLONTAIREMENT EXCLU : c'est du Python VALIDE a l'execution (la
# derniere definition gagne) et il FAUSSE-POSITIVE sur des handlers de routes FastAPI de meme
# nom (@app.get("/") def index / @app.get("/about") def index) — les deux routes s'enregistrent
# via le decorateur ; le signaler declencherait une "correction" qui supprimerait une vraie route.
# F401 (import inutilise) est exclu aussi : cosmetique, et un import peut servir a un EFFET DE BORD.
_RUFF_FATAL = {"F821", "F822", "F823", "F831", "F632", "F501", "F502", "F503",
               "F504", "F506", "F507", "F508", "F509", "F521", "F522", "F524", "F525",
               "F701", "F702", "F704", "F706", "F707", "F601", "F602"}

def lint_check(project_dir):
    """Filet anti-hallucination AVANT execution (sub-seconde, sans LLM). Renvoie UNIQUEMENT les
    erreurs FATALES (nom non defini, format string invalide...) a re-injecter dans la boucle de
    correction. Ruff si dispo, sinon repli pyflakes.
    NE MODIFIE JAMAIS les fichiers : le '--fix' initial (F401) supprimait en silence des imports
    a EFFET DE BORD pourtant necessaires ('import models' pour que Base.metadata.create_all voie
    les tables SQLAlchemy ; 'import readline'...) -> corruption d'un code deja correct (defaut
    identifie en revue de code independante). On se contente donc de RAPPORTER.
    Retourne une liste de lignes de diagnostic (vide = rien de fatal)."""
    # Rapport concis des seules erreurs de la famille F (AUCUNE modification de fichier)
    try:
        ok, out = _run([PYTHON, "-m", "ruff", "check", "--select", "F",
                        "--output-format", "concise", "."], project_dir, timeout=45)
    except Exception:
        out = "No module"
        ok = False
    if "No module" in (out or "") or "No such" in (out or ""):
        # Repli pyflakes : on filtre pour matcher le set fatal de ruff — on EXCLUT le cosmetique
        # (import inutilise F401, redefinition F811) pour ne pas declencher de fausse correction.
        ok2, out2 = Skills.lint(project_dir)
        if ok2 is None:  # pyflakes absent lui aussi
            return []
        lines = [l for l in (out2 or "").splitlines()
                 if l.strip() and ":" in l
                 and "imported but unused" not in l
                 and "redefinition" not in l.lower()
                 and "unable to detect undefined names" not in l]
        return lines[:12]
    # Ruff format concis : "chemin:ligne:col: CODE message" -> ne garde que les codes fatals
    fatal = []
    for line in (out or "").splitlines():
        m = re.search(r'\b(F\d{3})\b', line)
        if m and m.group(1) in _RUFF_FATAL:
            fatal.append(line.strip())
    return fatal[:12]


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
