# -*- coding: utf-8 -*-
"""
DevLLMA — Banc d'essai comparatif entre modeles Ollama candidats.
Pour chaque modele: genere une solution a 2 taches de code fixes, mesure la vitesse
reelle (tok/s cote decode, fournie par Ollama) et verifie fonctionnellement le resultat.
Resultats ecrits dans bench_results.json (au fur et a mesure, resumable).
"""
import json, os, re, subprocess, time, requests, sys

OLLAMA = "http://localhost:11434"
WORKDIR = r"C:\Devllma\workspace\_bench_models"
RESULTS = r"C:\Devllma\bench_results.json"
PYTHON = r"C:\Users\Admin\AppData\Local\Programs\Python\Python311\python.exe"
os.makedirs(WORKDIR, exist_ok=True)

CODER_SYSTEM = """Tu es CODER, expert Python. Tu CODES directement sans questions ni explication.
FORMAT DE SORTIE OBLIGATOIRE. Le fichier commence par ###FILE: nom.py et finit par ###ENDFILE.
Code complet et fonctionnel, aucun texte hors du bloc ###FILE...###ENDFILE."""

TASKS = [
    {
        "id": "prime",
        "prompt": ("Ecris un script Python autonome (un seul fichier main.py) avec une fonction "
                   "is_prime(n) qui teste si n est premier. Ajoute a la fin des assert pour verifier: "
                   "is_prime(2)==True, is_prime(4)==False, is_prime(17)==True, is_prime(18)==False, "
                   "is_prime(97)==True. Si tous les assert passent, affiche exactement 'ALL TESTS PASSED'."),
        "check": "run",
        "expect_stdout": "ALL TESTS PASSED",
    },
    {
        "id": "todo_api",
        "prompt": ("Cree une API REST complete avec FastAPI et SQLite pour gerer une liste de taches (Todo) "
                   "dans un seul fichier main.py: GET /todos (liste), POST /todos (creer, champs title/done), "
                   "PUT /todos/{id} (modifier), DELETE /todos/{id} (supprimer). Utilise sqlite3 standard."),
        "check": "static",
        "expect_patterns": [r"@app\.get\(.\/todos", r"@app\.post\(.\/todos", r"@app\.put\(.\/todos",
                            r"@app\.delete\(.\/todos", r"import\s+sqlite3", r"FastAPI\("],
    },
]

def extract_file(text):
    m = re.search(r'#{2,3}\s*FILE:\s*([^\n]+?)\s*\n([\s\S]*?)#{2,3}\s*ENDFILE', text, re.IGNORECASE)
    if m:
        body = re.sub(r'^```[\w+-]*\r?\n', '', m.group(2).strip())
        body = re.sub(r'\n```\s*$', '', body).strip()
        return body
    # fallback: bloc markdown
    m2 = re.search(r'```(?:python)?\n([\s\S]*?)```', text)
    return m2.group(1).strip() if m2 else text.strip()

def generate(model, prompt, max_tokens=3000, timeout=900):
    t0 = time.time()
    r = requests.post(f"{OLLAMA}/api/generate", json={
        "model": model, "system": CODER_SYSTEM, "prompt": prompt,
        "stream": False, "keep_alive": "10m",
        "options": {"temperature": 0.2, "num_predict": max_tokens}
    }, timeout=timeout)
    wall = time.time() - t0
    d = r.json()
    eval_count = d.get("eval_count", 0)
    eval_dur_s = d.get("eval_duration", 0) / 1e9
    prompt_eval_dur_s = d.get("prompt_eval_duration", 0) / 1e9
    load_dur_s = d.get("load_duration", 0) / 1e9
    tok_s = (eval_count / eval_dur_s) if eval_dur_s > 0 else 0
    return {
        "response": d.get("response", ""),
        "wall_s": round(wall, 1),
        "load_s": round(load_dur_s, 1),
        "prompt_eval_s": round(prompt_eval_dur_s, 1),
        "eval_tokens": eval_count,
        "decode_tok_s": round(tok_s, 2),
    }

def run_check(task, code):
    if task["check"] == "run":
        pdir = os.path.join(WORKDIR, task["id"])
        os.makedirs(pdir, exist_ok=True)
        fpath = os.path.join(pdir, "main.py")
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(code)
        try:
            r = subprocess.run([PYTHON, fpath], capture_output=True, text=True, timeout=20,
                               encoding="utf-8", errors="replace")
            out = (r.stdout or "") + (r.stderr or "")
            ok = task["expect_stdout"] in out and r.returncode == 0
            return ok, out[-300:]
        except Exception as e:
            return False, str(e)
    else:  # static
        missing = [p for p in task["expect_patterns"] if not re.search(p, code)]
        return (len(missing) == 0), ("manque: " + ", ".join(missing) if missing else "tous les patterns presents")

def load_results():
    if os.path.exists(RESULTS):
        return json.load(open(RESULTS, encoding="utf-8"))
    return {}

def save_results(r):
    with open(RESULTS, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=2)

def bench_model(model, results):
    results.setdefault(model, {})
    for task in TASKS:
        key = task["id"]
        if key in results[model]:
            print(f"[SKIP] {model} / {key} deja fait")
            continue
        print(f"[RUN] {model} / {key} ...")
        try:
            gen = generate(model, task["prompt"])
        except Exception as e:
            results[model][key] = {"error": str(e)}
            save_results(results)
            continue
        code = extract_file(gen["response"])
        ok, detail = run_check(task, code)
        results[model][key] = {
            "ok": ok, "detail": detail[:300],
            "wall_s": gen["wall_s"], "load_s": gen["load_s"],
            "prompt_eval_s": gen["prompt_eval_s"],
            "eval_tokens": gen["eval_tokens"], "decode_tok_s": gen["decode_tok_s"],
            "chars": len(code),
        }
        save_results(results)
        print(f"  -> ok={ok} wall={gen['wall_s']}s decode={gen['decode_tok_s']}tok/s tokens={gen['eval_tokens']}")

if __name__ == "__main__":
    models = sys.argv[1:] or ["qwen2.5-coder:7b"]
    results = load_results()
    for m in models:
        bench_model(m, results)
    print("\n=== RESUME ===")
    for m, tasks in results.items():
        line = f"{m}: "
        for k, v in tasks.items():
            if "error" in v:
                line += f"[{k}: ERREUR] "
            else:
                line += f"[{k}: {'OK' if v['ok'] else 'ECHEC'} {v['decode_tok_s']}tok/s {v['wall_s']}s] "
        print(line)
