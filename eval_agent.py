"""
Banc de tests DevLLMA — verifie le comportement REEL du systeme de bout en bout.

Envoie des taches au serveur via WebSocket (comme un utilisateur), puis verifie
le RESULTAT CONCRET (fichier cree au bon endroit avec le bon contenu, reponse
factuelle correcte, requete SQL executee...) — pas seulement "le modele a repondu".

Usage :
    python eval_agent.py            # lance toute la batterie
    python eval_agent.py time doc   # lance seulement les tests nommes

Chaque echec est une lecon : le rapport JSON (eval_results.json) sert a
enrichir la memoire semantique et ajuster les prompts (boucle d'apprentissage).
"""
import asyncio, json, os, re, sys, time
import websockets

URI = "ws://127.0.0.1:8080/ws"
DESKTOP = os.path.join(os.path.expanduser("~"), "Desktop")
EVAL_DIR = r"C:\Devllma\workspace\_eval"
RESULTS_FILE = r"C:\Devllma\eval_results.json"


async def ask(prompt, wait=240):
    """Envoie un prompt et collecte la reponse complete + evenements.
    En cas de depassement de delai, envoie STOP et attend l'arret effectif :
    sinon la generation abandonnee continue d'occuper le CPU et fait deborder
    tous les tests suivants en cascade (constate au premier run : 0/8)."""
    async with websockets.connect(URI, ping_timeout=None, max_size=None) as ws:
        await ws.send(json.dumps({"type": "init", "session": ""}))
        await asyncio.wait_for(ws.recv(), timeout=10)
        await ws.send(json.dumps({"type": "message", "text": prompt}))
        t0 = time.time()
        text, events = "", []
        timed_out = False
        while time.time() - t0 < wait:
            try:
                m = await asyncio.wait_for(ws.recv(), timeout=max(1, wait - (time.time() - t0)))
            except asyncio.TimeoutError:
                timed_out = True
                break
            d = json.loads(m)
            t = d.get("type")
            if t == "token":
                text += d.get("text", "")
            elif t == "done":
                break
            elif t != "speed":
                events.append(d)
        else:
            timed_out = True
        if timed_out:
            try:
                await ws.send(json.dumps({"type": "stop"}))
                grace = time.time() + 30
                while time.time() < grace:
                    m = await asyncio.wait_for(ws.recv(), timeout=max(1, grace - time.time()))
                    if json.loads(m).get("type") in ("done", "stopped"):
                        break
            except Exception:
                pass
        return text, events, round(time.time() - t0, 1)


# ─── Definition des tests ────────────────────────────────────────────────────
# Chaque test = (prompt, verify(text, events) -> (ok, detail))

def _v_time(text, events):
    # L'agent doit etre passe par l'outil get_datetime (via l'evenement tool_step),
    # pas inventer une heure — chercher le mot "horloge" dans le TEXTE FINAL etait
    # errone : le modele ne nomme pas l'outil qu'il a utilise dans sa reponse.
    used_tool = any(e.get("type") == "tool_step" and e.get("name") == "get_datetime" for e in events)
    m = re.search(r'(\d{1,2})[hH:](\d{2})', text)
    if not m:
        return False, "aucune heure dans la reponse"
    h = int(m.group(1))
    real_h = time.localtime().tm_hour
    ok = used_tool and abs(h - real_h) <= 1
    return ok, f"heure annoncee={h}h, heure reelle={real_h}h, outil get_datetime utilise={used_tool}"

def _v_doc_word(text, events):
    p = os.path.join(EVAL_DIR, "rapport_eval.docx")
    if not os.path.isfile(p):
        return False, f"fichier absent : {p}"
    try:
        from documents import read_document
        content = read_document(p)
    except Exception as e:
        return False, f"docx illisible : {e}"
    ok = "DevLLMA" in content
    return ok, f"contenu lu ({len(content)} car.) : {content[:120]!r}"

def _v_sqlite(text, events):
    # La reponse doit contenir le resultat correct du COUNT (3 lignes inserees).
    return ("3" in text), f"reponse : {text[-300:]!r}"

def _v_search(text, events):
    low = text.lower()
    ok = ("paris" in low)
    return ok, f"reponse : {text[:200]!r}"

def _v_list_dir(text, events):
    # workspace contient ces dossiers connus ; l'agent doit les rapporter reellement.
    ok = "application_chat_temps_reel" in text or "mini_application_tableau_kanban" in text
    return ok, f"reponse : {text[:250]!r}"

def _v_calc(text, events):
    # 1234 * 5678 = 7 006 652 — un modele 30B se trompe souvent de tete ;
    # il doit passer par execute_python.
    ok = "7006652" in text.replace(" ", "").replace(" ", "").replace(",", "")
    return ok, f"reponse : {text[-200:]!r}"

def _v_no_dev_pipeline(text, events):
    # Une question conversationnelle ne doit PAS declencher le pipeline projet
    # (pas d'evenement project_done/file_created).
    kinds = {e.get("type") for e in events}
    ok = "project_done" not in kinds and len(text.strip()) > 10
    return ok, f"evenements : {sorted(kinds)}"

def _v_dev_pipeline(text, events):
    # Une vraie demande de dev DOIT passer par le pipeline projet et reussir.
    kinds = {e.get("type") for e in events}
    if "project_done" not in kinds:
        return False, f"pipeline projet non declenche, evenements : {sorted(kinds)}"
    run_results = [e for e in events if e.get("type") == "run_result"]
    ok = any(e.get("ok") for e in run_results)
    return ok, f"run_results : {[(e.get('ok'), str(e.get('output'))[:80]) for e in run_results]}"

TESTS = {
    "time":  ("Quelle heure est-il en ce moment ?", _v_time, 240),
    "doc":   (f"Cree un fichier Word {EVAL_DIR}\\rapport_eval.docx contenant le titre 'Rapport DevLLMA' et une phrase de test.", _v_doc_word, 360),
    "sql":   (f"Avec l'outil SQL sur la base sqlite {EVAL_DIR}\\eval.db : cree une table personnes(nom TEXT) si elle n'existe pas, vide-la, insere trois lignes: Alice, Bob, Carol, puis dis-moi combien de lignes contient la table.", _v_sqlite, 480),
    "search": ("Quelle est la capitale de la France ? Reponds en une phrase.", _v_search, 240),
    "lsdir": (r"Liste le contenu du dossier C:\Devllma\workspace et cite les noms exacts.", _v_list_dir, 300),
    "calc":  ("Combien font exactement 1234 multiplie par 5678 ? Calcule precisement.", _v_calc, 300),
    "chat":  ("Explique-moi en deux phrases la difference entre une liste et un tuple en Python.", _v_no_dev_pipeline, 240),
    "dev":   ("Cree un script python calculatrice_eval qui affiche le resultat de 6*7 puis se termine.", _v_dev_pipeline, 900),
}


async def main(selected=None):
    os.makedirs(EVAL_DIR, exist_ok=True)
    names = selected or list(TESTS)
    results = {}
    for name in names:
        if name not in TESTS:
            print(f"?? test inconnu : {name}")
            continue
        prompt, verify, wait = TESTS[name]
        print(f"\n=== [{name}] {prompt[:90]}...")
        try:
            text, events, dur = await ask(prompt, wait)
            ok, detail = verify(text, events)
        except Exception as e:
            ok, detail, dur = False, f"exception banc de test : {e}", 0
        status = "OK  " if ok else "FAIL"
        print(f"  -> {status} ({dur}s) {detail[:250]}")
        results[name] = {"ok": ok, "detail": detail, "seconds": dur,
                         "prompt": prompt, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
        await asyncio.sleep(5)  # laisse le CPU retomber entre deux tests
    n_ok = sum(1 for r in results.values() if r["ok"])
    print(f"\n=== BILAN : {n_ok}/{len(results)} tests reussis ===")
    # Historique cumulatif : chaque run est ajoute (mesure de progression).
    history = []
    if os.path.exists(RESULTS_FILE):
        try:
            history = json.load(open(RESULTS_FILE, encoding="utf-8"))
        except Exception:
            history = []
    history.append({"run_ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "score": f"{n_ok}/{len(results)}", "results": results})
    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=1)
    print(f"Rapport ajoute a {RESULTS_FILE}")
    return results


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or None))
