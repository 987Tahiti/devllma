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

## Utilisation quotidienne (après config)
1. Ouvre le notebook, **Exécuter tout** (cellules 1→3). Attends « WORKER PRET ».
2. Dans DevLLMA : *« génère une image d'un chat astronaute »* → c'est tout. ✅
3. Quand Colab se déconnecte (~90 min d'inactivité) : re-**Exécuter tout**. L'URL et le token
   ne changent pas → rien à reconfigurer côté DevLLMA.
