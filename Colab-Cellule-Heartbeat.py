# ══════════════════════════════════════════════════════════════════════════
#  DevLLMA — CELLULE KEEP-ALIVE AUTO-RÉPARANTE v3 (à lancer EN DERNIER, après la cellule 3)
#
#  CE QU'ELLE FAIT :
#   1. Garde le runtime actif (heartbeat GPU au premier plan) -> pas de coupure d'inactivité.
#   2. Surveille le serveur local + le tunnel PUBLIC toutes les 20 s. Si le tunnel est tombé
#      alors que le serveur vit encore -> reconnecte ngrok automatiquement (domaine fixe).
#
#  CORRIGÉ (v3) :
#   - Ne fait PLUS ngrok.get_tunnels() (pyngrok démarrait un agent NON AUTHENTIFIÉ ->
#     ERR_NGROK_4018 en boucle). Le tunnel est vérifié en interrogeant l'URL PUBLIQUE.
#   - Applique l'authtoken UNE FOIS au démarrage, avant tout appel ngrok.
#
#  Pré-requis : la cellule 3 doit avoir été exécutée dans CE kernel (définit NGROK_DOMAIN,
#  NGROK_AUTHTOKEN, lance uvicorn sur le port 8000). Si "serveur=False" persiste -> ré-exécute
#  la cellule 3 (aucune cellule ne peut relancer uvicorn à sa place de façon fiable).
# ══════════════════════════════════════════════════════════════════════════

import time, datetime, requests, torch
from pyngrok import ngrok, conf

conf.get_default().auth_token = NGROK_AUTHTOKEN   # une fois : tout (re)démarrage d'agent est authentifié

PORT = 8000
PUB = f"https://{NGROK_DOMAIN}"
CHECK_EVERY = 20
_H = {"ngrok-skip-browser-warning": "1"}


def _server_ok():
    try:
        return requests.get(f"http://127.0.0.1:{PORT}/", timeout=5).status_code == 200
    except Exception:
        return False


def _tunnel_ok():
    # Interroge l'URL PUBLIQUE (surtout PAS ngrok.get_tunnels() qui auto-démarrerait un
    # agent non authentifié). 200 = le tunnel pointe bien sur notre serveur vivant.
    try:
        return requests.get(PUB + "/", headers=_H, timeout=8).status_code == 200
    except Exception:
        return False


def _reconnect_tunnel():
    try:
        conf.get_default().auth_token = NGROK_AUTHTOKEN
        ngrok.kill(); time.sleep(2)
        ngrok.connect(PORT, domain=NGROK_DOMAIN)
        return True
    except Exception as e:
        print(f"   ✗ échec reconnexion ngrok : {e}", flush=True)
        return False


print("=" * 64)
print(">>> KEEP-ALIVE v3 ACTIF — serveur + tunnel toutes", CHECK_EVERY, "s")
print("=" * 64, flush=True)

n = 0
while True:
    n += 1
    try:
        _ = (torch.randn(256, 256, device="cuda") @ torch.randn(256, 256, device="cuda")).sum().item()
        gpu = "GPU ok"
    except Exception as e:
        gpu = f"GPU KO: {e}"

    srv = _server_ok(); tun = _tunnel_ok(); healed = ""
    if srv and not tun:
        print(f"[{datetime.datetime.now():%H:%M:%S}] tunnel TOMBÉ → reconnexion…", flush=True)
        if _reconnect_tunnel():
            healed = " → tunnel RECONNECTÉ ✅"; tun = True
    elif not srv:
        healed = " → ⚠️ serveur éteint : ré-exécute la cellule 3 (WORKER)"

    if n % 3 == 0 or healed:
        etat = "🟢 EN LIGNE" if (srv and tun) else "🔴 DÉGRADÉ"
        print(f"[{datetime.datetime.now():%H:%M:%S}] {etat} | serveur={srv} tunnel={tun} "
              f"| {gpu} | #{n}{healed}", flush=True)

    time.sleep(CHECK_EVERY)

# ── Bonus anti pop-up (console navigateur F12, PAS ici) ──
#  function _kaColab(){const s=['colab-connect-button','#connect'];
#    for(const x of s){const e=document.querySelector(x);
#      if(e){(e.shadowRoot?.querySelector('#connect')||e).click();}}}
#  setInterval(_kaColab,60000);
