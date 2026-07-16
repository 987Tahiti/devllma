# ══════════════════════════════════════════════════════════════════════════════
#  DevLLMA — WORKER GPU COLAB : TOUT EN UNE SEULE CELLULE
#  ------------------------------------------------------------------------------
#  À coller dans UNE cellule d'un notebook Colab avec GPU (Exécution → Modifier le
#  type d'exécution → GPU T4), remplir NGROK_AUTHTOKEN ci-dessous, puis ▶️ Exécuter.
#  Fait tout, dans l'ordre : installe → charge le modèle image → lance le serveur →
#  ouvre le tunnel ngrok (domaine fixe) → garde le runtime en vie et reconnecte le
#  tunnel tout seul s'il tombe. LA CELLULE NE S'ARRÊTE JAMAIS (c'est voulu).
# ══════════════════════════════════════════════════════════════════════════════

# ── 0) CONFIG — À REMPLIR ─────────────────────────────────────────────────────
NGROK_AUTHTOKEN = "COLLE_TON_AUTHTOKEN_NGROK_ICI"        # https://dashboard.ngrok.com/get-started/your-authtoken
NGROK_DOMAIN    = "panama-regular-alike.ngrok-free.dev"  # ton domaine réservé (fixe à vie)
TOKEN           = "dl_2290d14d01fc"                      # mot de passe du worker (DOIT matcher celui de DevLLMA)

# ── 1) INSTALLS ───────────────────────────────────────────────────────────────
import subprocess, sys
subprocess.run([sys.executable, "-m", "pip", "install", "-q",
                "diffusers", "transformers", "accelerate", "safetensors",
                "fastapi", "uvicorn", "nest-asyncio", "pyngrok", "requests"], check=False)

# ── 2) CHARGEMENT DES PIPELINES (image tout de suite, vidéo à la demande) ──────
import torch
from diffusers import AutoPipelineForText2Image, DiffusionPipeline
print("Chargement du modèle image (SDXL, ~1 min la 1re fois)…", flush=True)
img_pipe = AutoPipelineForText2Image.from_pretrained(
    "stabilityai/stable-diffusion-xl-base-1.0",
    torch_dtype=torch.float16, variant="fp16", use_safetensors=True).to("cuda")
img_pipe.enable_attention_slicing()   # tient dans les 16 Go du T4

_vid_pipe = None
def get_vid_pipe():
    """Charge le modèle vidéo seulement au 1er appel /run video (économise la VRAM)."""
    global _vid_pipe
    if _vid_pipe is None:
        print("Chargement du modèle vidéo…", flush=True)
        _vid_pipe = DiffusionPipeline.from_pretrained(
            "damo-vilab/text-to-video-ms-1.7b", torch_dtype=torch.float16).to("cuda")
        _vid_pipe.enable_attention_slicing()
    return _vid_pipe
print("Modèle image prêt.", flush=True)

# ── 3) SERVEUR FastAPI :  /  (santé)  +  /run  (image / vidéo) ─────────────────
import base64, io, threading, time, nest_asyncio, uvicorn
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from diffusers.utils import export_to_video

app = FastAPI()
class Job(BaseModel):
    task: str = "image"; prompt: str = ""; params: dict = {}

@app.get("/")
def health():
    return {"ok": True, "gpu": torch.cuda.get_device_name(0)}

@app.post("/run")
def run(job: Job, authorization: str = Header(default="")):
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(401, "token invalide")
    if not job.prompt.strip():
        raise HTTPException(400, "prompt vide")
    p = job.params or {}
    if job.task == "image":
        # Utilise le prompt NÉGATIF + réglages envoyés par DevLLMA (meilleure qualité :
        # mains/détails) ; valeurs par défaut si absents.
        img = img_pipe(
            job.prompt,
            negative_prompt=(p.get("negative_prompt") or None),
            num_inference_steps=int(p.get("steps", 30)),
            guidance_scale=float(p.get("guidance", 7.0)),
            width=int(p.get("width", 1024)),
            height=int(p.get("height", 1024)),
        ).images[0]
        buf = io.BytesIO(); img.save(buf, format="PNG")
        return {"file_base64": base64.b64encode(buf.getvalue()).decode(), "ext": "png"}
    if job.task == "video":
        secs = float(p.get("seconds", 2))
        frames = get_vid_pipe()(job.prompt, num_frames=max(8, int(secs * 8))).frames[0]
        path = export_to_video(frames, "/content/out.mp4")
        return {"file_base64": base64.b64encode(open(path, "rb").read()).decode(), "ext": "mp4"}
    raise HTTPException(400, "tache inconnue")

nest_asyncio.apply()
threading.Thread(
    target=lambda: uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning"),
    daemon=True).start()
time.sleep(3)

# ── 4) TUNNEL ngrok (domaine FIXE) ─────────────────────────────────────────────
from pyngrok import ngrok, conf
conf.get_default().auth_token = NGROK_AUTHTOKEN
ngrok.kill()                                   # ferme d'éventuels tunnels fantômes
ngrok.connect(8000, domain=NGROK_DOMAIN)
print("=" * 64)
print(">>> WORKER PRÊT :", NGROK_DOMAIN, "| TOKEN :", TOKEN)
print("=" * 64, flush=True)

# ── 5) KEEP-ALIVE AUTO-RÉPARANT (garde le runtime + reconnecte le tunnel) ──────
#  Ne fait PAS ngrok.get_tunnels() (ça démarrait un agent non authentifié -> ERR_4018).
#  Vérifie le tunnel via l'URL PUBLIQUE. NE S'ARRÊTE JAMAIS.
import datetime, requests
PUB = f"https://{NGROK_DOMAIN}"; _H = {"ngrok-skip-browser-warning": "1"}

def _srv_ok():
    try: return requests.get("http://127.0.0.1:8000/", timeout=5).status_code == 200
    except Exception: return False

def _tun_ok():
    try: return requests.get(PUB + "/", headers=_H, timeout=8).status_code == 200
    except Exception: return False

n = 0
while True:
    n += 1
    try:
        _ = (torch.randn(256, 256, device="cuda") @ torch.randn(256, 256, device="cuda")).sum().item()
        gpu = "GPU ok"
    except Exception as e:
        gpu = f"GPU KO: {e}"
    srv = _srv_ok(); tun = _tun_ok(); healed = ""
    if srv and not tun:
        print(f"[{datetime.datetime.now():%H:%M:%S}] tunnel TOMBÉ → reconnexion…", flush=True)
        try:
            conf.get_default().auth_token = NGROK_AUTHTOKEN
            ngrok.kill(); time.sleep(2); ngrok.connect(8000, domain=NGROK_DOMAIN)
            healed = " → RECONNECTÉ ✅"; tun = True
        except Exception as e:
            print("   ✗ reconnexion:", e, flush=True)
    if n % 3 == 0 or healed:
        etat = "🟢 EN LIGNE" if (srv and tun) else "🔴 DÉGRADÉ"
        print(f"[{datetime.datetime.now():%H:%M:%S}] {etat} | serveur={srv} tunnel={tun} "
              f"| {gpu} | #{n}{healed}", flush=True)
    time.sleep(20)

# ══════════════════════════════════════════════════════════════════════════════
#  BONUS (facultatif) — anti pop-up "êtes-vous toujours là ?"
#  À coller dans la CONSOLE du navigateur (F12 → Console), PAS dans une cellule :
#
#   function _kaColab(){const s=['colab-connect-button','#connect'];
#     for(const x of s){const e=document.querySelector(x);
#       if(e){(e.shadowRoot?.querySelector('#connect')||e).click();}}}
#   setInterval(_kaColab,60000);
# ══════════════════════════════════════════════════════════════════════════════
