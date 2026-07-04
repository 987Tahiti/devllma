import sqlite3, os, json
from datetime import datetime

DB = r"C:\Devllma\database\devllma.db"
OLLAMA = "http://localhost:11434"
EMBED_MODEL = "nomic-embed-text"

def cx():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    return sqlite3.connect(DB)

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

# ════════════════════════════════════════════════════════════════════════════
#  Memoire semantique (RAG) — via nomic-embed-text, recherche par similarite
# ════════════════════════════════════════════════════════════════════════════
def embed(text):
    """Calcule le vecteur d'un texte via Ollama (nomic-embed-text). None si indisponible."""
    import requests
    try:
        r = requests.post(f"{OLLAMA}/api/embeddings",
                          json={"model": EMBED_MODEL, "prompt": text[:6000]},
                          timeout=30)
        vec = r.json().get("embedding")
        return vec if vec else None
    except Exception:
        return None

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
        return c.execute(
            "INSERT INTO embeddings(kind,ref_id,ref_name,chunk,vector) VALUES(?,?,?,?,?)",
            (kind, ref_id, ref_name, text[:2000], json.dumps(vec))).lastrowid

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

def mem_purge(kind=None):
    with cx() as c:
        if kind:
            c.execute("DELETE FROM embeddings WHERE kind=?", (kind,))
        else:
            c.execute("DELETE FROM embeddings")

def mem_search(query, k=5, kind=None, min_score=0.55):
    """Recherche semantique: renvoie les k souvenirs les plus proches de query.
    -> [{kind, ref_name, chunk, score}], tries par pertinence decroissante.
    Similarite cosinus vectorisee (numpy) plutot qu'une boucle Python."""
    import numpy as np
    qv = embed(query)
    if not qv:
        return []
    with cx() as c:
        if kind:
            rows = c.execute("SELECT kind,ref_name,chunk,vector FROM embeddings WHERE kind=?", (kind,)).fetchall()
        else:
            rows = c.execute("SELECT kind,ref_name,chunk,vector FROM embeddings").fetchall()
    if not rows:
        return []
    meta, vecs = [], []
    for knd, ref_name, chunk, vecjson in rows:
        try:
            vecs.append(json.loads(vecjson))
            meta.append((knd, ref_name, chunk))
        except Exception:
            continue
    if not vecs:
        return []
    try:
        q = np.asarray(qv, dtype=np.float32)
        mat = np.asarray(vecs, dtype=np.float32)
        qn = np.linalg.norm(q)
        norms = np.linalg.norm(mat, axis=1)
        norms[norms == 0] = 1e-9
        sims = (mat @ q) / (norms * (qn if qn else 1e-9))
    except ValueError:
        return []  # dimensions incoherentes (embeddings d'un autre modele) -> pas de resultat plutot qu'un crash
    scored = []
    for i, score in enumerate(sims.tolist()):
        knd, ref_name, chunk = meta[i]
        if score >= min_score:
            scored.append({"kind": knd, "ref_name": ref_name, "chunk": chunk, "score": round(score, 3)})
    scored.sort(key=lambda x: -x["score"])
    return scored[:k]
