"""
Couche d'appel a Ollama pour DevLLMA — extrait de webui.py pour readabilite
(cf. HANDOFF/revue de code : webui.py etait devenu un monolithe).

Regroupe tout ce qui parle HTTP a Ollama : generation (bloquante et streamee),
telechargement de modele, verification de disponibilite au demarrage. Ne contient
AUCUNE route FastAPI ni etat de session — uniquement des fonctions pures/async
parametrees, appelees par webui.py.

`make_brain_system` (qui depend de BRAIN_MEMORY, un etat vivant de webui.py) est
importee paresseusement a l'interieur de call_brain() pour eviter un import
circulaire — au moment de l'appel, webui.py est deja completement charge.
"""
import re, json, time, asyncio, threading, subprocess, queue as _queue
import requests

from agents import AGENTS, OLLAMA
from db import EMBED_MODEL

# Garde le modèle chargé en RAM en PERMANENCE (-1) : le recharger + réévaluer les
# prompts à froid coûte plusieurs minutes sur ce CPU (mesure, cf agent_core.KEEP_ALIVE).
KEEP_ALIVE = -1
# Fenetre de contexte forcee. Avec keep_alive=-1 le modele reste charge avec le num_ctx
# du PREMIER appel qui le charge ; si personne ne le fixe, Ollama retombe sur son defaut
# (4096 constate apres un reboot) -> generation multi-fichiers bridee et lente. On impose
# 32768 partout (warmup + generation) pour un contexte stable, valeur qui tournait deja
# sur ce poste avant reboot (RAM suffisante).
NUM_CTX = 32768
# Modèle du Brain : qwen3-coder:30b (MoE, ~3.3B actifs/token -> aussi rapide qu'un 7b dense
# mais bien plus capable ; mesuré ~2x plus rapide que qwen2.5-coder:7b sur ce CPU, cf bench_models.py)
BRAIN_MODEL = "qwen3-coder:30b"

_http = requests.Session()  # connexion reutilisee (keep-alive) pour les appels Ollama


def call_brain(prompt, system=None, max_tokens=450):
    if system:
        s = system
    else:
        from webui import make_brain_system  # import paresseux : evite le cycle webui<->ollama_client
        s = make_brain_system()
    last_err = None
    for attempt in range(3):
        try:
            r = _http.post(OLLAMA+"/api/generate", json={
                "model":BRAIN_MODEL, "system":s,
                "prompt":prompt, "stream":False, "keep_alive":KEEP_ALIVE,
                "options":{"temperature":0.3,"num_predict":max_tokens,"num_ctx":NUM_CTX}
            }, timeout=300)
            body = r.json()
            if "error" in body:
                return (f"(modele '{BRAIN_MODEL}' indisponible sur Ollama : {body['error']} — "
                        f"verifie qu'il est bien telecharge dans le panneau Modeles)")
            return body.get("response","")
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
            _http.post(OLLAMA+"/api/generate",
                          json={"model":m,"prompt":"","keep_alive":KEEP_ALIVE,
                                "options":{"num_ctx":NUM_CTX}},  # charge le modele au bon contexte des le warmup
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
                r = _http.post(OLLAMA+"/api/generate", json={
                    "model":cfg["model"], "system":sys_p,
                    "prompt":prompt, "stream":True, "keep_alive":KEEP_ALIVE,
                    "options":{"temperature":temperature,"num_predict":4000,"num_ctx":NUM_CTX}
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
                try:
                    parsed = json.loads(line)
                except Exception:
                    parsed = {}
                # Ollama repond 200 avec {"error": "..."} (modele non pulle, requete
                # invalide...) au lieu d'une erreur HTTP : sans ce controle, la ligne
                # est traitee comme un token vide et la generation semble juste vide.
                if "error" in parsed:
                    out_q.put(("error",
                               f"modele '{cfg['model']}' indisponible sur Ollama : "
                               f"{parsed['error']} — verifie qu'il est bien telecharge "
                               f"dans le panneau Modeles"))
                    return
                out_q.put(("line", line))
                if parsed.get("done"):
                    break
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
    err = None
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
                # pas d'envoi de token ici : le worker de webui.py affichera le
                # message UNE seule fois en attrapant l'exception levee plus bas
                err = payload
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
    # une panne Ollama mid-stream doit remonter en exception, sinon le pipeline
    # ecrit du code tronque puis boucle en auto-correction contre un serveur mort.
    # Le cas 'stopped' (annulation volontaire) reste inchange : jamais de raise.
    if err:
        raise RuntimeError(err)
    if stopped:
        await ws.send_json({"type":"stopped"})
    return full

def _fetch_ollama_tags():
    try:
        r = _http.get(OLLAMA + "/api/tags", timeout=8)
        return r.json().get("models", [])
    except Exception:
        return []

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

def check_ollama_ready():
    """Verifie au demarrage qu'Ollama repond ET que les modeles requis sont bien
    pulles — sans ca, le premier vrai probleme n'est decouvert qu'au 1er message
    d'un utilisateur (erreur opaque en plein milieu d'une reponse). Le resultat est
    juste journalise (print, capture par la tache planifiee) : on ne bloque jamais
    le demarrage du serveur pour ca, Ollama peut redemarrer plus tard tout seul."""
    required = {AGENTS["coder"]["model"], BRAIN_MODEL, EMBED_MODEL}
    try:
        installed = {m["name"] for m in _fetch_ollama_tags()}
    except Exception as e:
        print(f"[DEMARRAGE] ATTENTION : Ollama ({OLLAMA}) injoignable au demarrage : {e}")
        return
    if not installed:
        print(f"[DEMARRAGE] ATTENTION : Ollama ({OLLAMA}) ne repond pas ou n'a aucun modele installe.")
        return
    # Ollama nomme les modeles avec un tag (":latest" par defaut) ; on compare en ignorant
    # le tag pour ne pas signaler a tort "snowflake-arctic-embed2" comme manquant alors que
    # "snowflake-arctic-embed2:latest" est bien installe (faux positif au demarrage).
    installed_base = {n.split(":")[0] for n in installed}
    missing = [m for m in required if m not in installed and m.split(":")[0] not in installed_base]
    if missing:
        print(f"[DEMARRAGE] ATTENTION : modele(s) manquant(s) dans Ollama : {', '.join(missing)} "
              f"— a telecharger via 'ollama pull <modele>' ou le panneau Modeles de l'interface.")
    else:
        print(f"[DEMARRAGE] OK : Ollama joignable, {len(required)} modeles requis presents.")
