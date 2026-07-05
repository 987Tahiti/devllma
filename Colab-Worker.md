# DevLLMA — Worker GPU sur Google Colab

Ce worker fait tourner les **tâches lourdes** (génération d'image, de vidéo) sur le
**GPU gratuit de Google Colab**, et les expose en HTTP à ton DevLLMA local via un tunnel.
DevLLMA (outil `colab_run`) appelle ce serveur automatiquement.

## Mode d'emploi (2 min)

1. Va sur https://colab.research.google.com → **Nouveau notebook**.
2. Menu **Exécution → Modifier le type d'exécution → T4 GPU** → Enregistrer.
3. Colle les **3 cellules** ci-dessous (une par cellule) et exécute-les dans l'ordre.
4. La dernière cellule affiche une ligne du type :
   `>>> URL POUR DEVLLMA : https://xxxx-xxxx.trycloudflare.com  | TOKEN : ab12cd34`
5. Dans DevLLMA, écris une fois :
   `génère une image d'un chat astronaute` — l'agent dira que Colab n'est pas configuré.
   Réponds-lui : `utilise cette url https://xxxx.trycloudflare.com et ce token ab12cd34`
   (ou l'agent te le redemandera). L'URL + le token sont ensuite **mémorisés** : les fois
   suivantes, il suffit de demander l'image/vidéo, DevLLMA s'en charge tout seul.

⚠️ Colab est éphémère : à chaque nouvelle session, relance les cellules et redonne la
nouvelle URL à DevLLMA. Garde l'onglet Colab ouvert pendant l'utilisation.

---

## Cellule 1 — Dépendances

```python
!pip -q install diffusers transformers accelerate safetensors fastapi uvicorn nest-asyncio pyngrok >/dev/null 2>&1
# cloudflared (tunnel sans compte) :
!wget -q -O /usr/local/bin/cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
!chmod +x /usr/local/bin/cloudflared
print("Dépendances installées.")
```

## Cellule 2 — Charger les modèles GPU (image + vidéo)

```python
import torch
from diffusers import StableDiffusionPipeline, DiffusionPipeline

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
assert DEVICE == "cuda", "Active le GPU T4 : Exécution > Modifier le type d'exécution."

# Image (Stable Diffusion 1.5, léger et rapide sur T4)
img_pipe = StableDiffusionPipeline.from_pretrained(
    "runwayml/stable-diffusion-v1-5", torch_dtype=torch.float16, safety_checker=None
).to(DEVICE)

# Vidéo (text-to-video court ; charge à la 1re demande pour économiser la RAM)
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

## Cellule 3 — Serveur HTTP + tunnel

```python
import base64, io, secrets, threading, subprocess, time, nest_asyncio, uvicorn
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from diffusers.utils import export_to_video

TOKEN = secrets.token_hex(4)   # jeton simple pour éviter les appels non désirés
app = FastAPI()

class Job(BaseModel):
    task: str = "image"
    prompt: str = ""
    params: dict = {}

@app.get("/")
def health():
    return {"ok": True, "gpu": torch.cuda.get_device_name(0)}

@app.post("/run")
def run(job: Job, authorization: str = Header(default="")):
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(401, "token invalide")
    if not job.prompt.strip():
        raise HTTPException(400, "prompt vide")
    if job.task == "image":
        steps = int(job.params.get("steps", 25))
        image = img_pipe(job.prompt, num_inference_steps=steps).images[0]
        buf = io.BytesIO(); image.save(buf, format="PNG")
        return {"file_base64": base64.b64encode(buf.getvalue()).decode(), "ext": "png",
                "note": f"image {steps} steps (GPU {torch.cuda.get_device_name(0)})"}
    elif job.task == "video":
        secs = float(job.params.get("seconds", 2))
        frames = get_vid_pipe()(job.prompt, num_frames=max(8, int(secs*8))).frames[0]
        path = export_to_video(frames, "/content/out.mp4")
        data = open(path, "rb").read()
        return {"file_base64": base64.b64encode(data).decode(), "ext": "mp4",
                "note": f"vidéo ~{secs}s (GPU)"}
    raise HTTPException(400, f"tâche inconnue: {job.task}")

# Lancer le serveur en arrière-plan
nest_asyncio.apply()
threading.Thread(target=lambda: uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning"),
                 daemon=True).start()
time.sleep(3)

# Ouvrir le tunnel cloudflared et afficher l'URL
proc = subprocess.Popen(["cloudflared","tunnel","--url","http://localhost:8000","--no-autoupdate"],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
url = None
for line in proc.stdout:
    if "trycloudflare.com" in line:
        import re as _re
        m = _re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
        if m: url = m.group(0); break
print("\n" + "="*70)
print(f">>> URL POUR DEVLLMA : {url}  | TOKEN : {TOKEN}")
print("="*70)
print("Garde cet onglet ouvert. Donne l'URL et le TOKEN à DevLLMA.")
```
