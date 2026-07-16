# DevLLMA — Worker GPU sur Google Colab (URL FIXE via ngrok)

Ce worker fait tourner les **tâches lourdes** (image, vidéo) sur le **GPU gratuit de Colab**
et les expose à DevLLMA via une **URL ngrok FIXE**. Avantage : l'URL ne change jamais →
DevLLMA est configuré une seule fois, et ensuite tu n'as **plus qu'à démarrer le notebook**.

---

## Étape 0 — Créer un compte ngrok (gratuit, une seule fois dans ta vie)

1. Va sur https://dashboard.ngrok.com/signup → crée un compte (gratuit).
2. **Authtoken** : https://dashboard.ngrok.com/get-started/your-authtoken → copie-le (secret).
3. **Domaine statique gratuit** : https://dashboard.ngrok.com/domains → « Create Domain » →
   tu obtiens un domaine du type `xxxx-yyyy.ngrok-free.app` (gratuit, 1 par compte, fixe à vie).

Garde ces 2 valeurs : ton **authtoken** et ton **domaine**.

## Étape 1 — Dis-moi ton domaine
Donne-moi **le domaine** (`xxxx.ngrok-free.app`) et **un mot de passe/token de ton choix**
(ex: `mon-secret-123`). Je pré-configure DevLLMA avec, une fois pour toutes.
⚠️ L'**authtoken** ngrok, lui, ne va QUE dans le notebook (cellule 3) — ne me le donne pas,
ne le mets pas sur GitHub.

---

## Le notebook (3 cellules à coller dans Colab, GPU T4)

> Exécution → Modifier le type d'exécution → **T4 GPU** → Enregistrer.

### Cellule 1 — Dépendances
```python
!pip -q install diffusers transformers accelerate safetensors fastapi uvicorn nest-asyncio pyngrok >/dev/null 2>&1
print("Dépendances installées.")
```

### Cellule 2 — Charger les modèles GPU
```python
import torch
from diffusers import StableDiffusionPipeline, DiffusionPipeline

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
assert DEVICE == "cuda", "Active le GPU T4 : Exécution > Modifier le type d'exécution."

img_pipe = StableDiffusionPipeline.from_pretrained(
    "runwayml/stable-diffusion-v1-5", torch_dtype=torch.float16, safety_checker=None
).to(DEVICE)

vid_pipe = None
def get_vid_pipe():
    global vid_pipe
    if vid_pipe is None:
        vid_pipe = DiffusionPipeline.from_pretrained(
            "damo-vilab/text-to-video-ms-1.7b", torch_dtype=torch.float16, variant="fp16"
        ).to(DEVICE)
    return vid_pipe
print("Modèle image prêt (vidéo chargée à la demande).")
```

### Cellule 3 — Serveur + tunnel ngrok À URL FIXE
> ⚠️ Remplace les 3 valeurs `<...>` par les tiennes.
```python
NGROK_AUTHTOKEN = "<TON_AUTHTOKEN_NGROK>"      # secret, reste ici
NGROK_DOMAIN    = "<xxxx.ngrok-free.app>"       # ton domaine fixe
TOKEN           = "<mon-secret-123>"            # le meme que tu donnes a DevLLMA

import base64, io, threading, time, nest_asyncio, uvicorn, torch
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from diffusers.utils import export_to_video
from pyngrok import ngrok, conf

app = FastAPI()
class Job(BaseModel):
    task: str = "image"; prompt: str = ""; params: dict = {}

@app.get("/")
def health(): return {"ok": True, "gpu": torch.cuda.get_device_name(0)}

@app.post("/run")
def run(job: Job, authorization: str = Header(default="")):
    if authorization != f"Bearer {TOKEN}": raise HTTPException(401, "token invalide")
    if not job.prompt.strip(): raise HTTPException(400, "prompt vide")
    if job.task == "image":
        steps = int(job.params.get("steps", 25))
        image = img_pipe(job.prompt, num_inference_steps=steps).images[0]
        buf = io.BytesIO(); image.save(buf, format="PNG")
        return {"file_base64": base64.b64encode(buf.getvalue()).decode(), "ext": "png",
                "note": f"image {steps} steps"}
    if job.task == "video":
        secs = float(job.params.get("seconds", 2))
        frames = get_vid_pipe()(job.prompt, num_frames=max(8, int(secs*8))).frames[0]
        path = export_to_video(frames, "/content/out.mp4")
        return {"file_base64": base64.b64encode(open(path,"rb").read()).decode(), "ext": "mp4",
                "note": f"video ~{secs}s"}
    raise HTTPException(400, f"tache inconnue: {job.task}")

nest_asyncio.apply()
threading.Thread(target=lambda: uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning"),
                 daemon=True).start()
time.sleep(3)

conf.get_default().auth_token = NGROK_AUTHTOKEN
ngrok.kill()  # ferme les anciens tunnels
public = ngrok.connect(8000, domain=NGROK_DOMAIN)
print("\n" + "="*70)
print(f">>> WORKER PRET : https://{NGROK_DOMAIN}  | TOKEN : {TOKEN}")
print("="*70)
print("Garde cet onglet ouvert. DevLLMA connait deja cette URL : demande juste une image/video.")
```

---

### Cellule 4 (OPTIONNELLE) — Déléguer du CODE au GPU (qwen2.5-coder:14b)
> À ajouter seulement si tu veux que DevLLMA envoie les **gros dev / cas bloqués** au GPU.
> Colle-la AVANT la cellule 3 (le serveur doit connaître l'endpoint `/llm` au démarrage),
> ou relance la cellule 3 après. Le modèle (~9 Go) se télécharge à la 1re exécution.
```python
# Ollama + modele de code sur le GPU
!curl -fsSL https://ollama.com/install.sh | sh >/dev/null 2>&1
import subprocess, time, requests
subprocess.Popen(["ollama","serve"])
time.sleep(5)
print("Téléchargement de qwen2.5-coder:14b (~9 Go, une fois)...")
subprocess.run(["ollama","pull","qwen2.5-coder:14b"])

from fastapi import Body
@app.post("/llm")
def llm(body: dict = Body(...), authorization: str = Header(default="")):
    if authorization != f"Bearer {TOKEN}": raise HTTPException(401, "token invalide")
    r = requests.post("http://localhost:11434/api/generate", json={
        "model":"qwen2.5-coder:14b","system":body.get("system",""),
        "prompt":body.get("prompt",""),"stream":False,
        "options":{"temperature":0.2,"num_ctx":16384,"num_predict":4000}}, timeout=590)
    return {"response": r.json().get("response","")}
print("Endpoint /llm prêt (relance la cellule 3 si tu l'avais deja lancee).")
```

---

## Garder la session ACTIVE (anti-déconnexion) — À LIRE

Colab coupe le worker pour **deux raisons différentes** — ne pas les confondre :

| Cause | Délai | Solution |
|---|---|---|
| **Inactivité** : la cellule 3 *se termine*, donc plus aucune cellule ne « tourne » → Colab te croit oisif (le thread `uvicorn` daemon ne compte pas). | ~90 min | Cellule 6 + auto-clic navigateur (ci-dessous) |
| **Limite absolue** de session sur le tier gratuit. | ~12 h | ❌ Rien (voir « Limite dure » plus bas) |

### Cellule 6 — HEARTBEAT (à exécuter en DERNIER, laisse-la tourner)
> C'est **la** cellule qui empêche la coupure des ~90 min. Elle ne se termine **jamais** :
> tant qu'elle tourne, Colab considère le runtime comme **actif**. Lance-la après la cellule 3.
> Version prête à coller (avec l'auto-clic navigateur en commentaire) : **`Colab-Cellule-Heartbeat.py`**.
```python
# NE S'ARRETE JAMAIS : garde le runtime "occupe" (pas d'oisivete) + garde le GPU alloue.
import time, datetime, torch
n = 0
while True:
    n += 1
    try:
        # micro-calcul GPU : prouve l'activite ET garde la VRAM/le modele au chaud
        _ = (torch.randn(512, 512, device="cuda") @ torch.randn(512, 512, device="cuda")).sum().item()
        gpu = "GPU ok"
    except Exception as e:
        gpu = f"GPU KO: {e}"
    print(f"[{datetime.datetime.now():%H:%M:%S}] worker vivant — heartbeat #{n} ({gpu})", flush=True)
    time.sleep(60)
```

### Auto-clic navigateur (déjoue le « êtes-vous toujours là ? »)
Même avec la cellule 6, Colab affiche parfois une invite anti-bot après un long moment sans
interaction. Ouvre la console développeur (**F12** → onglet *Console*), colle ceci, Entrée :
```javascript
// Reclique le bouton "Connect/Reconnect" toutes les 60 s. Laisse l'onglet ouvert.
function _kaColab() {
  const sels = ['colab-connect-button', '#connect', '#comments > span'];
  for (const s of sels) {
    const el = document.querySelector(s);
    if (el) { (el.shadowRoot?.querySelector('#connect') || el).click(); }
  }
  console.log('[keep-alive Colab]', new Date().toLocaleTimeString());
}
setInterval(_kaColab, 60000);
```

### Limite dure (~12 h, tier gratuit) — pas de contournement
La cellule 6 + l'auto-clic éliminent la coupure d'inactivité, **pas** le plafond absolu de
session du tier gratuit (~12 h, et le quota GPU varie selon l'usage). Quand ça arrive :
- DevLLMA le détecte automatiquement (thread `_colab_keepalive` → log `[COLAB] worker INJOIGNABLE`)
  et **bascule seul en génération locale** — rien ne casse, c'est juste plus lent.
- Il te suffit de rouvrir le notebook et **Exécuter tout** : l'URL/token ne changent pas.
- Pour du vraiment persistant (24 h, exécution en arrière-plan) → **Colab Pro/Pro+**.

---

## Utilisation quotidienne (après config)
1. Ouvre le notebook, **Exécuter tout** (cellules 1→3, puis 6). Attends « WORKER PRET ».
2. **F12 → Console** → colle l'auto-clic ci-dessus (une fois par onglet ouvert).
3. Dans DevLLMA : *« génère une image d'un chat astronaute »* → c'est tout. ✅
4. Le log DevLLMA affiche `[COLAB] worker JOIGNABLE`. S'il passe à `INJOIGNABLE` (limite 12 h
   atteinte), re-**Exécuter tout** → rien à reconfigurer côté DevLLMA.
