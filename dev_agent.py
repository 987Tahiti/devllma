# -*- coding: utf-8 -*-
"""
DevLLMA — Agent de développement autonome (mini Claude Code).
Pipeline: plan -> code -> ecrire -> install -> TEST REEL -> (raisonnement +
recherche web + correction en boucle) -> COMPILATION exe -> test exe.

Usage: python dev_agent.py <spec.json>
spec.json = {"name","brief","entry","test_args":[...],"build_exe":true,"model":"..."}
"""
import sys, os, json, re, time, subprocess, requests
sys.path.insert(0, r"C:\Devllma")
os.chdir(r"C:\Devllma")

from agents import AGENTS, OLLAMA
from skills import extract_files, safety_check, security_scan, Skills, BrainMemory
from tools import functional_test, syntax_check, search_solution, read_image_text, detect_runtime_error, extract_error_line

PYTHON    = r"C:\Users\Admin\AppData\Local\Programs\Python\Python311\python.exe"
WORKSPACE = r"C:\Devllma\workspace"
MAX_FIX   = 7   # tentatives de correction guidées par test réel (avec escalade si bloqué)

# ── Prompts ──────────────────────────────────────────────────────────────────
BRAIN_PLAN = """Tu es le BRAIN de DevLLMA. Réfléchis avant d'agir.
Plan structuré: 1.PROJET 2.STACK 3.FICHIERS(liste) 4.ARCHITECTURE 5.POINTS CLÉS 6.TODOS. Concis."""

BRAIN_REASON = """Tu es le BRAIN. Du code a échoué à l'EXÉCUTION (pas juste compilation).
Analyse la cause RACINE de l'erreur runtime en 2-3 phrases. Si des infos web sont fournies, utilise-les.
Dis EXACTEMENT quel fichier et quelle ligne corriger, et comment."""

CODER = """Tu es CODER, expert Python. Tu CODES directement.
FORMAT: chaque fichier commence par ###FILE: nom.ext et finit par ###ENDFILE.
EXEMPLE:
###FILE: main.py
import outil
###ENDFILE
RÈGLES: main.py en premier; tout module importé DOIT avoir son fichier;
requirements.txt = packages pip externes uniquement (jamais os/sys/time/threading/argparse/json/re/queue);
si "en parallèle" demandé -> threading.Thread (start/join); code complet, aucun ``` ni texte hors blocs."""

CODER_FIX = """Tu es CODER. Tu corriges une erreur d'EXÉCUTION (runtime).
On te donne l'analyse du brain, l'erreur réelle, et parfois des solutions web.
Réécris les fichiers à corriger en ENTIER. Format: ###FILE: nom.ext ... ###ENDFILE. Aucun texte hors blocs."""

def log(logf, msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass  # détaché sans console: stdout peut être invalide
    try:
        with open(logf, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def ollama(model, system, prompt, max_tokens=4000, temp=0.2):
    r = requests.post(OLLAMA+"/api/generate", json={
        "model": model, "system": system, "prompt": prompt,
        "stream": False, "keep_alive": "30m",
        "options": {"temperature": temp, "num_predict": max_tokens}
    }, timeout=600)
    return r.json().get("response", "")

def write_files(pdir, files):
    os.makedirs(pdir, exist_ok=True)
    created = []
    for fn, code in files:
        fp = os.path.join(pdir, fn.replace("/", os.sep))
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            f.write(code)
        created.append(fn)
    return created

def read_all(pdir, limit=3500):
    out = {}
    for f in os.listdir(pdir):
        if f.endswith((".py", ".txt", ".md")) and not f.startswith("_"):
            try:
                out[f] = open(os.path.join(pdir, f), encoding="utf-8", errors="replace").read()[:limit]
            except Exception: pass
    return out

def ctx(files):
    return "\n\n".join(f"###FILE: {n}\n{c}\n###ENDFILE" for n, c in files.items())

def install_deps(pdir):
    req = os.path.join(pdir, "requirements.txt")
    if os.path.exists(req):
        subprocess.run([PYTHON, "-m", "pip", "install", "-r", req, "-q", "--no-warn-script-location"],
                       capture_output=True, text=True, timeout=240)

def build_exe(pdir, entry, name, logf):
    """Compile en .exe avec PyInstaller. Retourne (ok, chemin_ou_erreur)."""
    log(logf, f"COMPILATION: PyInstaller --onefile {entry} ...")
    r = subprocess.run(
        [PYTHON, "-m", "PyInstaller", "--onefile", "--name", name,
         "--distpath", os.path.join(pdir, "dist"),
         "--workpath", os.path.join(pdir, "build"),
         "--specpath", pdir, entry],
        capture_output=True, text=True, timeout=600, cwd=pdir, encoding="utf-8", errors="replace")
    exe = os.path.join(pdir, "dist", name + ".exe")
    if os.path.exists(exe):
        return True, exe
    return False, (r.stderr or r.stdout or "echec")[-500:]

def main():
    spec = json.load(open(sys.argv[1], encoding="utf-8"))
    name      = spec["name"]
    brief     = spec["brief"]
    entry     = spec.get("entry", "main.py")
    test_args = spec.get("test_args", ["--help"])
    do_exe    = spec.get("build_exe", True)
    model     = spec.get("model", AGENTS["coder"]["model"])

    pdir = os.path.join(WORKSPACE, name)
    os.makedirs(pdir, exist_ok=True)
    for f in os.listdir(pdir):
        if not f.startswith("_") and f not in (".git",):
            p = os.path.join(pdir, f)
            try:
                if os.path.isfile(p): os.remove(p)
            except Exception: pass
    logf = os.path.join(pdir, "_supervision_log.txt")
    open(logf, "w", encoding="utf-8").close()
    t0 = time.time()
    log(logf, f"=== DEV AUTONOME: {name} | modele {model} ===")

    # 1. PLAN
    log(logf, "PHASE 1: Brain planifie...")
    t = time.time()
    plan = ollama(model, BRAIN_PLAN, brief, max_tokens=500, temp=0.3)
    log(logf, f"Plan en {time.time()-t:.0f}s")

    # 2. CODE
    log(logf, "PHASE 2: Coder genere...")
    t = time.time()
    resp = ollama(model, CODER, f"PLAN:\n{plan}\n\nDEMANDE:\n{brief}\n\nProduis tous les fichiers.", max_tokens=5000)
    log(logf, f"Code en {time.time()-t:.0f}s")
    with open(os.path.join(pdir, "_raw.txt"), "w", encoding="utf-8") as f: f.write(resp)
    files = extract_files(resp)
    if not files:
        log(logf, "ECHEC: aucun fichier extrait"); return
    created = write_files(pdir, files)
    log(logf, f"Fichiers: {created}")
    safe, reasons = safety_check("\n".join(c for _, c in files))
    log(logf, f"Garde-fou: {'SUR' if safe else 'BLOQUE '+str(reasons)}")
    if not safe: return
    install_deps(pdir)

    # Validation de sortie: le programme doit PRODUIRE le bon résultat (pas juste ne pas crasher)
    expect_file    = spec.get("expect_file")            # fichier de sortie attendu
    expect_min     = spec.get("expect_min_size", 1)     # taille minimale du fichier
    expect_out_min = spec.get("expect_output_min", 0)   # nb min de caractères non-espace en sortie
    expect_out_re  = spec.get("expect_output_regex")    # regex que la sortie doit contenir

    # Test multi-étapes (round-trip): si 'test_steps' fourni, on enchaine les commandes,
    # la sortie validee est celle de la DERNIERE etape (ex: add puis get => verifier le secret)
    test_steps = spec.get("test_steps")
    steps = test_steps if test_steps else [test_args]

    def run_and_validate():
        # reset pour un test propre (bases + fichiers de sortie)
        for junk in os.listdir(pdir):
            if junk.endswith(".db") or (expect_file and (junk == expect_file or junk.startswith(expect_file + ".part"))):
                try: os.remove(os.path.join(pdir, junk))
                except Exception: pass
        ok2, out2, rc2 = True, "", 0
        for st in steps:
            ok2, o, rc2 = functional_test(pdir, entry, st, timeout=40)
            out2 = o
            if not ok2:
                return False, f"Echec a l'etape '{' '.join(map(str,st))}':\n{o}", rc2
        # Validation fichier de sortie
        if ok2 and expect_file:
            fp = os.path.join(pdir, expect_file)
            if not os.path.exists(fp):
                parts = [f for f in os.listdir(pdir) if f.startswith(expect_file + ".part")]
                hint = f" (fichiers .part trouves: {parts} -> les segments ne sont PAS assembles dans {expect_file})" if parts else ""
                return False, f"Le programme se termine sans erreur MAIS le fichier de sortie '{expect_file}' n'a pas ete cree.{hint}\nSortie:\n{out2}", rc2
            sz = os.path.getsize(fp)
            if sz < expect_min:
                return False, f"Fichier '{expect_file}' cree mais trop petit ({sz} octets, attendu >= {expect_min}). Resultat incomplet.\nSortie:\n{out2}", rc2
        # Validation de la SORTIE (anti faux-positif: un programme vide ne doit pas passer)
        if ok2 and (expect_out_min or expect_out_re):
            stripped = re.sub(r"\s", "", out2 or "")
            if expect_out_min and len(stripped) < expect_out_min:
                return False, (f"Le programme se termine sans erreur MAIS n'affiche quasiment rien "
                               f"({len(stripped)} caracteres non-espace, attendu >= {expect_out_min}). "
                               f"Il DOIT afficher un resultat reel pour la commande testee.\nSortie:\n{out2}"), rc2
            if expect_out_re and not re.search(expect_out_re, out2 or ""):
                return False, (f"La sortie ne contient pas le resultat attendu (motif: {expect_out_re}). "
                               f"Le programme doit produire ce resultat.\nSortie:\n{out2}"), rc2
        return ok2, out2, rc2

    # 3. BOUCLE TEST REEL + VALIDATION + RAISONNEMENT + RECHERCHE WEB + CORRECTION
    log(logf, f"PHASE 3: Test fonctionnel reel + validation sortie (args={test_args}, attendu={expect_file})")
    ok, output, rc = run_and_validate()
    log(logf, f"Test initial: {'OK' if ok else 'ECHEC'} | {output[:200]}")
    iteration = 0
    prev_err = None
    stuck = 0
    while not ok and iteration < MAX_FIX:
        iteration += 1
        log(logf, f"--- Correction {iteration}/{MAX_FIX} ---")
        cur = read_all(pdir)
        # Si c'est un echec de VALIDATION (pas un traceback), transmettre le message tel quel
        first_line = next((l.strip() for l in output.splitlines() if l.strip()), "")
        if first_line.startswith(("Le programme", "Echec a l'etape", "Fichier", "La sortie")):
            clean_err = first_line
        else:
            clean_err = extract_error_line(output)   # exception réelle (pas le bruit thread)
        log(logf, f"  Erreur precise: {clean_err}")
        # Detection de boucle: meme erreur => escalade (changer d'approche, temp plus haute)
        if clean_err == prev_err:
            stuck += 1
        else:
            stuck = 0
        prev_err = clean_err
        escalate = stuck >= 1
        fix_temp = 0.2 if not escalate else min(0.7, 0.3 + 0.15 * stuck)
        escalate_note = ("\nATTENTION: cette erreur PERSISTE malgre ta correction precedente. "
                         "Change d'APPROCHE sur la cause de l'erreur (corrige ou remplace la ligne fautive par une autre methode). "
                         "MAIS conserve TOUTE la logique metier et TOUTES les fonctionnalites demandees. "
                         "NE SUPPRIME JAMAIS le corps des fonctions ni le code qui marche, ne rends pas le fichier vide.") if escalate else ""
        if escalate:
            log(logf, f"  [escalade] meme erreur x{stuck}, temp={fix_temp}")
        # Recherche web de la solution (des la 1ere tentative)
        log(logf, "  Recherche Internet d'une solution...")
        web, _ = search_solution(output, brief)
        log(logf, f"  Web: {web[:250]}")
        analysis = ollama(model, BRAIN_REASON,
                          f"ERREUR PRECISE: {clean_err}\n\nTRACE:\n{output}\n\nCODE:\n{ctx(cur)}\n\nSOLUTIONS WEB:\n{web}",
                          max_tokens=400)
        log(logf, f"  Analyse brain: {analysis[:200]}")
        # Correction du coder — on insiste sur l'erreur precise et la solution web
        fix = ollama(model, CODER_FIX,
                     f"ERREUR PRECISE A CORRIGER: {clean_err}{escalate_note}\n\n"
                     f"SOLUTION WEB A APPLIQUER:\n{web}\n\n"
                     f"ANALYSE BRAIN:\n{analysis}\n\nTRACE COMPLETE:\n{output}\n\n"
                     f"CODE ACTUEL:\n{ctx(cur)}\n\n"
                     f"Applique la solution. Reecris les fichiers concernes en entier.\n"
                     f"IMPORTANT — NE REGRESSE PAS: garde TOUS les correctifs deja en place. "
                     f"Toutes les variables doivent rester initialisees (ex: 'speed' avant usage), "
                     f"ET l'assemblage des segments .part dans le fichier final -o doit etre conserve. "
                     f"Corrige UNIQUEMENT l'erreur courante sans casser ce qui marche deja.",
                     max_tokens=5000, temp=fix_temp)
        fixed = extract_files(fix)
        # Garde-fou anti-vidage: rejeter une "correction" qui supprime le code
        applied = []
        for fn, code in fixed:
            old = cur.get(fn, "")
            noncomment = "\n".join(l for l in code.splitlines()
                                   if l.strip() and not l.strip().startswith(("#", "//")))
            if old and len(old) > 200 and len(code) < 0.4 * len(old):
                log(logf, f"  [REJET] correction de {fn} trop courte ({len(code)} vs {len(old)} car.) -> code precedent conserve")
                continue
            if old and len(noncomment.strip()) < 12:
                log(logf, f"  [REJET] {fn} quasi vide apres correction -> code precedent conserve")
                continue
            applied.append((fn, code))
        if applied:
            write_files(pdir, applied)
            install_deps(pdir)
            log(logf, f"  Corrige: {[f for f,_ in applied]}")
        else:
            log(logf, "  (aucune correction valide: toutes rejetees ou vides)")
        ok, output, rc = run_and_validate()
        log(logf, f"  Re-test: {'OK' if ok else 'ECHEC'} | {output[:200]}")

    log(logf, f"TEST FONCTIONNEL: {'REUSSI' if ok else 'ECHEC apres '+str(iteration)+' tentatives'}")

    # 4. COMPILATION EXE
    exe_ok = None
    if do_exe and ok:
        exe_ok, info = build_exe(pdir, entry, name.replace(" ", "_"), logf)
        if exe_ok:
            log(logf, f"EXE OK: {info}")
            # tester l'exe
            try:
                r = subprocess.run([info, "--help"], capture_output=True, text=True, timeout=30,
                                   encoding="utf-8", errors="replace")
                exe_runs = not detect_runtime_error((r.stdout or "")+(r.stderr or ""), r.returncode)
                log(logf, f"Test exe --help: {'OK' if exe_runs else 'probleme'}")
            except Exception as e:
                log(logf, f"Test exe: {e}")
        else:
            log(logf, f"EXE ECHEC: {info}")

    # 5. BILAN
    total = time.time() - t0
    final = [f for f in os.listdir(pdir) if not f.startswith("_") and f not in ("build",)]
    log(logf, "=== BILAN ===")
    log(logf, f"Temps total: {total:.0f}s ({total/60:.1f} min)")
    log(logf, f"Fichiers: {final}")
    log(logf, f"Test fonctionnel reel: {'REUSSI' if ok else 'ECHEC'}")
    log(logf, f"Compilation exe: {('REUSSIE' if exe_ok else 'ECHEC') if do_exe else 'non demandee'}")
    log(logf, f"Corrections auto: {iteration}")
    if ok:
        Skills.git_init(pdir)
        BrainMemory.append_event(f"{name}: test reel {'OK' if ok else 'KO'}, exe {'OK' if exe_ok else 'NA'}, {iteration} corrections")
    log(logf, "=== FIN ===")

if __name__ == "__main__":
    main()
