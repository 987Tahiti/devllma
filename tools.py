# -*- coding: utf-8 -*-
"""
DevLLMA — Outils avancés (style Claude Code) :
- read_image_text : OCR, lit le texte/erreur d'une image (capture d'écran, photo)
- web_search      : recherche Internet pour trouver une solution à une erreur
- functional_test : EXÉCUTE réellement le code et capture les erreurs runtime
- detect_runtime_error / summarize_error : analyse des sorties d'exécution
"""
import os, re, subprocess, json, urllib.parse, html as _html

PYTHON = r"C:\Users\Admin\AppData\Local\Programs\Python\Python311\python.exe"

# ── OCR : lire le texte d'une image ──────────────────────────────────────────
_ocr_engine = None
def read_image_text(path):
    """Lit le texte présent dans une image (OCR). Retourne le texte ou un message."""
    global _ocr_engine
    if not os.path.exists(path):
        return f"(image introuvable: {path})"
    try:
        if _ocr_engine is None:
            from rapidocr_onnxruntime import RapidOCR
            _ocr_engine = RapidOCR()
        result, _ = _ocr_engine(path)
        if not result:
            return "(aucun texte détecté)"
        # result = [[box, text, score], ...]
        lines = [item[1] for item in result]
        return "\n".join(lines)
    except ImportError:
        return "(OCR indisponible: rapidocr_onnxruntime pas encore installé)"
    except Exception as e:
        return f"(erreur OCR: {e})"

# ── Recherche Internet ───────────────────────────────────────────────────────
def web_search(query, n=4):
    """Recherche DuckDuckGo (sans clé API). Retourne [{title, snippet, url}]."""
    import requests
    try:
        r = requests.post("https://html.duckduckgo.com/html/",
                          data={"q": query},
                          headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                          timeout=15)
        htmltext = r.text
    except Exception as e:
        return [{"title": "erreur réseau", "snippet": str(e), "url": ""}]
    results = []
    # Blocs de résultats
    for m in re.finditer(r'result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', htmltext, re.S):
        url, title = m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip()
        results.append({"url": _html.unescape(url), "title": _html.unescape(title), "snippet": ""})
        if len(results) >= n:
            break
    # Snippets
    snippets = re.findall(r'result__snippet"[^>]*>(.*?)</a>', htmltext, re.S)
    for i, sn in enumerate(snippets[:len(results)]):
        results[i]["snippet"] = _html.unescape(re.sub(r"<[^>]+>", "", sn)).strip()[:300]
    if not results:
        return [{"title": "aucun résultat", "snippet": "", "url": ""}]
    return results

def extract_error_line(error_text):
    """Extrait la VRAIE ligne d'exception (type+message), pas le header 'Traceback'."""
    lines = [l.strip() for l in error_text.splitlines() if l.strip()]
    # Chercher de la fin vers le début la derniere ligne du type 'XxxError: message'
    for line in reversed(lines):
        if re.match(r'^[\w.]+(Error|Exception|Warning)\b', line) or re.search(r'\b\w*(Error|Exception):', line):
            return line[:200]
    # Sinon, derniere ligne non vide qui n'est pas un chemin de fichier
    for line in reversed(lines):
        if not line.startswith(("File ", "Traceback")):
            return line[:200]
    return error_text[:150]

def fetch_answer(url, max_chars=1500):
    """Récupère une page (StackOverflow…) et extrait le texte + code de la 1re réponse."""
    import requests
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        page = r.text
    except Exception:
        return ""
    # StackOverflow: réponse dans <div class="answercell"> ... <div class="s-prose">
    m = re.search(r'class="[^"]*s-prose[^"]*"[^>]*>(.*?)</div>\s*</div>', page, re.S)
    block = m.group(1) if m else page
    # Extraire les blocs de code <code>...</code>
    codes = re.findall(r'<code>(.*?)</code>', block, re.S)
    text = re.sub(r"<[^>]+>", " ", block)
    text = _html.unescape(re.sub(r"\s+", " ", text)).strip()
    out = text[:max_chars]
    if codes:
        snippet = _html.unescape("\n".join(re.sub(r"<[^>]+>", "", c) for c in codes[:3]))
        out += "\n\nCODE PROPOSE:\n" + snippet[:800]
    return out

def search_solution(error_text, context="", deep=True):
    """Construit une requête à partir de la VRAIE erreur et renvoie un résumé des solutions web.
    deep=True: récupère aussi le contenu concret (code) de la meilleure page."""
    key = extract_error_line(error_text)
    # Nettoyer les chemins/valeurs spécifiques pour une requête générique
    q = re.sub(r"[\'\"][^\'\"]*[\'\"]", "", key)         # retirer les littéraux
    q = re.sub(r"[A-Za-z]:\\[^\s]+", "", q)               # retirer les chemins Windows
    query = (q.strip() or key) + " python"
    hits = web_search(query, n=4)
    summary = f"Recherche web pour: {query}\n"
    for h in hits:
        summary += f"- {h['title']}: {h['snippet']}\n"
    # Approfondir: récupérer le code concret de la meilleure réponse
    if deep:
        for h in hits:
            u = h.get("url", "")
            if "duckduckgo" in u or not u.startswith("http"):
                continue
            detail = fetch_answer(u)
            if detail and len(detail) > 80:
                summary += f"\nDETAIL ({h['title']}):\n{detail}\n"
                break
    return summary, hits

# ── Test fonctionnel : EXÉCUTER réellement le code ───────────────────────────
ERROR_MARKERS = ("Traceback (most recent call last)", "Error:", "Exception",
                 "SyntaxError", "ImportError", "ModuleNotFoundError",
                 "NameError", "TypeError", "ValueError", "AttributeError")

def detect_runtime_error(output, returncode):
    """Détecte une erreur dans une sortie d'exécution."""
    if output and any(m in output for m in ERROR_MARKERS):
        return True
    if returncode not in (0, None):
        return True
    return False

def functional_test(project_dir, entry, args=None, timeout=30):
    """EXÉCUTE le programme avec des arguments réels et capture la sortie.
    Retourne (ok, output, returncode). ok=False si erreur runtime détectée."""
    args = args or []
    fpath = os.path.join(project_dir, entry)
    if not os.path.exists(fpath):
        return False, f"point d'entrée introuvable: {entry}", 1
    try:
        r = subprocess.run([PYTHON, fpath] + list(args),
                           capture_output=True, text=True, timeout=timeout,
                           cwd=project_dir, encoding="utf-8", errors="replace")
        output = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
        ok = not detect_runtime_error(output, r.returncode)
        # IMPORTANT: garder la QUEUE (l'exception réelle est à la fin du traceback)
        return ok, output[-2000:], r.returncode
    except subprocess.TimeoutExpired:
        # Pour un serveur/boucle infinie, un timeout sans crash = OK
        return True, f"(toujours en cours après {timeout}s — pas de crash)", None
    except Exception as e:
        return False, f"erreur de lancement: {e}", 1

def syntax_check(project_dir):
    """Vérifie que les .py compilent. Retourne liste d'erreurs."""
    errors = []
    for f in os.listdir(project_dir):
        if f.endswith(".py"):
            r = subprocess.run([PYTHON, "-m", "py_compile", os.path.join(project_dir, f)],
                               capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                errors.append(f"{f}: {(r.stderr or '').strip()[:300]}")
    return errors
