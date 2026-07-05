import sqlite3, os, json
import requests
from datetime import datetime

DB = r"C:\Devllma\database\devllma.db"
OLLAMA = "http://localhost:11434"
# snowflake-arctic-embed2 : embedding MULTILINGUE (1024 dims) nettement meilleur que
# nomic-embed-text (768 dims, anglophone) sur les souvenirs en francais. Changer ce modele
# rend illisibles les vecteurs de l'ancien (dimensions differentes) -> mem_search les ignore
# proprement, et il faut RE-INDEXER l'existant (reindex_embeddings ci-dessous, lance une fois).
EMBED_MODEL = "snowflake-arctic-embed2"
# Session partagee (connexion TCP + keep-alive reutilises) : embed() est appele a
# CHAQUE mem_search/mem_index, une nouvelle connexion a chaque fois est un cout
# evitable meme en local.
_http = requests.Session()

def cx():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB)
    # WAL : les lecteurs (mem_search, history...) ne bloquent plus sur un ecrivain
    # (msg, mem_index...) — sans ca, plusieurs WebSocket actives en meme temps
    # peuvent produire "database is locked" (mode rollback-journal par defaut,
    # une seule connexion a la fois quelle que soit lecture/ecriture). WAL est un
    # reglage persistant du fichier .db (le PRAGMA suivant est quasi gratuit une
    # fois deja active) ; busy_timeout fait patienter au lieu d'echouer immediatement
    # sur la rare collision d'ecriture concurrente.
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    return c

def init():
    with cx() as c:
        c.executescript("""
CREATE TABLE IF NOT EXISTS agents_cfg(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE, model TEXT, description TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS sessions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER, agent TEXT, role TEXT,
    content TEXT, ts TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS tasks(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER, agent TEXT,
    description TEXT, result TEXT,
    status TEXT DEFAULT 'done', ts TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS memory(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE, value TEXT, ts TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS artifacts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT, path TEXT, lang TEXT,
    content TEXT, agent TEXT, ts TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS embeddings(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT, ref_id INTEGER, ref_name TEXT,
    chunk TEXT, vector TEXT, ts TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS idx_embeddings_kind ON embeddings(kind);
INSERT OR IGNORE INTO agents_cfg(name,model,description) VALUES
    ('brain','brain-llma','Orchestrateur central - analyse et distribue les taches'),
    ('architect','architect-llma','Architecture et conception systeme'),
    ('coder','coder-llma','Ecriture de code propre et efficace'),
    ('debugger','debugger-llma','Detection et correction de bugs'),
    ('reviewer','reviewer-llma','Revue de code et qualite'),
    ('tester','tester-llma','Tests unitaires et integration'),
    ('devops','devops-llma','CI/CD Docker et deploiement'),
    ('database','database-llma','Base de donnees et SQL'),
    ('frontend','frontend-llma','Interface utilisateur et UX'),
    ('backend','backend-llma','API REST et logique serveur'),
    ('security','security-llma','Securite et vulnerabilites');
INSERT OR IGNORE INTO memory(key,value) VALUES
    ('system','DevLLMA v1.0'),
    ('workspace','C:\\Devllma\\workspace'),
    ('agents_count','11');
""")
    print(f"[DB] Base initialisee: {DB}")
    return DB

def new_session(title="Session"):
    with cx() as c:
        return c.execute("INSERT INTO sessions(title) VALUES(?)", (title,)).lastrowid

def delete_session(sid):
    with cx() as c:
        c.execute("DELETE FROM messages WHERE session_id=?", (sid,))
        c.execute("DELETE FROM tasks WHERE session_id=?", (sid,))
        c.execute("DELETE FROM sessions WHERE id=?", (sid,))

def msg(sid, agent, role, content):
    with cx() as c:
        c.execute("INSERT INTO messages(session_id,agent,role,content) VALUES(?,?,?,?)",
                  (sid, agent, role, content[:4000]))

def save_task(sid, agent, desc, result):
    with cx() as c:
        c.execute("INSERT INTO tasks(session_id,agent,description,result) VALUES(?,?,?,?)",
                  (sid, agent, desc[:500], result[:4000]))

def mem_set(key, val):
    with cx() as c:
        c.execute("INSERT OR REPLACE INTO memory(key,value,ts) VALUES(?,?,?)",
                  (key, val, datetime.now().isoformat()))

def mem_get(key):
    with cx() as c:
        r = c.execute("SELECT value FROM memory WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

def save_artifact(name, content, lang="", path="", agent="coder"):
    with cx() as c:
        c.execute("INSERT INTO artifacts(name,path,lang,content,agent) VALUES(?,?,?,?,?)",
                  (name, path, lang, content, agent))

def history(sid, n=6):
    with cx() as c:
        rows = c.execute(
            "SELECT agent,role,content FROM messages WHERE session_id=? ORDER BY ts DESC LIMIT ?",
            (sid, n)).fetchall()
        return list(reversed(rows))

def list_sessions():
    with cx() as c:
        return c.execute("SELECT id,title,created_at FROM sessions ORDER BY created_at DESC LIMIT 20").fetchall()

def search_messages(q, limit=30):
    """Recherche plein texte dans les messages ; renvoie le meilleur extrait par session.
    -> [{session_id, title, snippet, ts}] du plus recent au plus ancien."""
    like = f"%{q}%"
    with cx() as c:
        rows = c.execute(
            "SELECT m.session_id, s.title, m.content, m.ts FROM messages m "
            "JOIN sessions s ON s.id = m.session_id "
            "WHERE m.content LIKE ? ORDER BY m.ts DESC LIMIT 200",
            (like,)).fetchall()
    seen, results = set(), []
    for sid, title, content, ts in rows:
        if sid in seen:
            continue
        seen.add(sid)
        pos = content.lower().find(q.lower())
        start = max(0, pos - 80)
        snippet = content[start:pos + len(q) + 80].replace("\n", " ").strip()
        results.append({"session_id": sid, "title": title, "snippet": snippet, "ts": ts})
        if len(results) >= limit:
            break
    return results

def stats():
    with cx() as c:
        return {
            'sessions': c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
            'messages': c.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            'tasks':    c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            'artifacts':c.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0],
            'memories': c.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0],
        }

# ── Cache RAM des embeddings ─────────────────────────────────────────────────
# json.loads() de N vecteurs de 768 floats a CHAQUE mem_search coute lineairement
# avec la taille de la memoire. On garde donc en RAM une matrice numpy
# PRE-NORMALISEE (lignes de norme 1 : la similarite cosinus devient un simple
# produit matrice-vecteur) + les metadonnees alignees. Invalidation par compteur
# de generation : chaque ecriture (mem_index reussie, mem_delete, mem_purge)
# incremente _mem_gen ; mem_search reconstruit si son cache est plus ancien.
# NOTE inter-process : le compteur est local au process. Si un AUTRE process
# ouvre la meme base (instance prod + instance de test, ou seed_knowledge.py
# lance pendant qu'un serveur tourne), ses ecritures ne seront vues ici qu'a la
# prochaine ecriture locale (en pratique au premier QA indexe). Acceptable :
# chaque process relit ce qu'il ecrit lui-meme, et la memoire semantique tolere
# cette staleness. Le chemin dedup de mem_index n'invalide pas (aucune ecriture).
import threading
_mem_gen = 0
_mem_cache = {"gen": -1, "mat": None, "meta": []}  # meta: [(kind, ref_name, chunk)]
_mem_lock = threading.Lock()  # mem_search tourne dans des threads (run_in_executor cote webui)

def _mem_invalidate():
    global _mem_gen
    with _mem_lock:
        _mem_gen += 1

def _mem_matrix():
    """(matrice normalisee, meta) depuis le cache ; reconstruit apres une ecriture."""
    import numpy as np
    with _mem_lock:
        if _mem_cache["gen"] == _mem_gen and _mem_cache["mat"] is not None:
            return _mem_cache["mat"], _mem_cache["meta"]
        gen = _mem_gen  # capture AVANT le SELECT : une ecriture concurrente re-invalidera
        with cx() as c:
            rows = c.execute("SELECT kind,ref_name,chunk,vector FROM embeddings").fetchall()
        meta, vecs, dim = [], [], None
        for knd, ref_name, chunk, vecjson in rows:
            try:
                v = json.loads(vecjson)
            except Exception:
                continue
            if dim is None:
                dim = len(v)
            if len(v) != dim:  # vecteur d'un autre modele d'embedding : ignore plutot que crash
                continue
            vecs.append(v)
            meta.append((knd, ref_name, chunk))
        if not vecs:
            _mem_cache.update(gen=gen, mat=np.zeros((0, 1), dtype=np.float32), meta=[])
        else:
            mat = np.asarray(vecs, dtype=np.float32)
            norms = np.linalg.norm(mat, axis=1, keepdims=True)
            norms[norms == 0] = 1e-9
            _mem_cache.update(gen=gen, mat=mat / norms, meta=meta)
        return _mem_cache["mat"], _mem_cache["meta"]

# ════════════════════════════════════════════════════════════════════════════
#  Memoire semantique (RAG) — via nomic-embed-text, recherche par similarite
# ════════════════════════════════════════════════════════════════════════════
def embed(text):
    """Calcule le vecteur d'un texte via Ollama (EMBED_MODEL). None si indisponible."""
    try:
        r = _http.post(f"{OLLAMA}/api/embeddings",
                          json={"model": EMBED_MODEL, "prompt": text[:6000]},
                          timeout=60)
        vec = r.json().get("embedding")
        return vec if vec else None
    except Exception:
        return None

def reindex_embeddings():
    """Recalcule TOUS les vecteurs avec le modele EMBED_MODEL courant (a lancer une fois
    apres un changement de modele : les anciens vecteurs, d'une autre dimension, sont sinon
    ignores par mem_search). Idempotent : re-lancer ne fait que recalculer. Le texte source
    (colonne chunk) est deja stocke -> pas besoin des documents d'origine.
    Retourne (ok, total, echecs)."""
    with cx() as c:
        rows = c.execute("SELECT id, chunk FROM embeddings").fetchall()
    ok = fail = 0
    for eid, chunk in rows:
        vec = embed(chunk)
        if not vec:
            fail += 1
            continue
        with cx() as c:
            c.execute("UPDATE embeddings SET vector=? WHERE id=?", (json.dumps(vec), eid))
        ok += 1
    return ok, len(rows), fail

def mem_index(kind, ref_name, text, ref_id=None):
    """Indexe un texte (tache, artifact, fichier projet...) pour recherche semantique future."""
    text = (text or "").strip()
    if len(text) < 20:
        return None
    with cx() as c:
        dup = c.execute(
            "SELECT id FROM embeddings WHERE kind=? AND ref_name=? AND chunk=?",
            (kind, ref_name, text[:2000])).fetchone()
        if dup:
            return dup[0]
    vec = embed(text)
    if not vec:
        return None
    with cx() as c:
        rid = c.execute(
            "INSERT INTO embeddings(kind,ref_id,ref_name,chunk,vector) VALUES(?,?,?,?,?)",
            (kind, ref_id, ref_name, text[:2000], json.dumps(vec))).lastrowid
    # HORS du with : l'invalidation doit suivre le COMMIT (sortie du with), sinon un
    # mem_search concurrent pourrait reconstruire le cache sur les donnees d'avant
    # tout en enregistrant la generation d'apres -> cache perime en permanence.
    _mem_invalidate()
    return rid

def mem_list(kind=None, limit=100, offset=0):
    """Liste paginee des souvenirs, du plus recent au plus ancien (panneau memoire UI)."""
    with cx() as c:
        if kind:
            rows = c.execute(
                "SELECT id,kind,ref_name,substr(chunk,1,220),ts FROM embeddings "
                "WHERE kind=? ORDER BY id DESC LIMIT ? OFFSET ?", (kind, limit, offset)).fetchall()
            total = c.execute("SELECT COUNT(*) FROM embeddings WHERE kind=?", (kind,)).fetchone()[0]
        else:
            rows = c.execute(
                "SELECT id,kind,ref_name,substr(chunk,1,220),ts FROM embeddings "
                "ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
            total = c.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    return {"total": total,
            "items": [{"id": r[0], "kind": r[1], "ref_name": r[2], "chunk": r[3], "ts": r[4]} for r in rows]}

def mem_delete(mem_id):
    with cx() as c:
        c.execute("DELETE FROM embeddings WHERE id=?", (mem_id,))
    _mem_invalidate()  # apres le commit (sortie du with), cf. mem_index

def mem_purge(kind=None):
    with cx() as c:
        if kind:
            c.execute("DELETE FROM embeddings WHERE kind=?", (kind,))
        else:
            c.execute("DELETE FROM embeddings")
    _mem_invalidate()  # apres le commit (sortie du with), cf. mem_index

# Seuil de similarite par defaut. Calibre pour snowflake-arctic-embed2 : ses scores cosinus
# sont sur une echelle plus BASSE que nomic-embed-text (un match pertinent sort ~0.45-0.50,
# pas ~0.55-0.70) -> un seuil a 0.55 rejetait TOUT (mesure). 0.40 capte les vrais matchs
# sans laisser passer trop de bruit.
def mem_search(query, k=5, kind=None, min_score=0.40):
    """Recherche semantique: renvoie les k souvenirs les plus proches de query.
    -> [{kind, ref_name, chunk, score}], tries par pertinence decroissante.
    Matrice servie par le cache RAM (_mem_matrix) : plus de rechargement/parse
    JSON de toute la table a chaque appel. Le filtre kind se fait en Python
    apres scoring pour qu'une seule matrice cache serve tous les appels."""
    import numpy as np
    qv = embed(query)
    if not qv:
        return []
    mat, meta = _mem_matrix()
    if mat.shape[0] == 0:
        return []
    q = np.asarray(qv, dtype=np.float32)
    if q.shape[0] != mat.shape[1]:
        return []  # dimension incoherente (modele d'embedding change) -> vide plutot qu'un crash
    qn = np.linalg.norm(q)
    sims = mat @ (q / (qn if qn else 1e-9))
    scored = []
    for i, score in enumerate(sims.tolist()):
        knd, ref_name, chunk = meta[i]
        if score >= min_score and (kind is None or knd == kind):
            scored.append({"kind": knd, "ref_name": ref_name, "chunk": chunk, "score": round(score, 3)})
    scored.sort(key=lambda x: -x["score"])
    return scored[:k]
