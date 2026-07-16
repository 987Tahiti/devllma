# ══════════════════════════════════════════════════════════════════════════
#  DevLLMA — CELLULE KEEP-ALIVE AUTO-RÉPARANTE (anti-déconnexion + auto-reconnexion)
#  À COLLER dans une nouvelle cellule, À EXÉCUTER EN DERNIER (après la cellule 3
#  "WORKER PRET"). Laisse-la tourner : elle ne s'arrête jamais et se répare seule.
#
#  CE QU'ELLE FAIT :
#   1. Garde une exécution AU PREMIER PLAN en permanence + un micro-calcul GPU
#      -> le runtime n'est jamais "oisif" -> pas de coupure d'inactivité (~90 min).
#   2. SURVEILLE toutes les 20 s que (a) le serveur uvicorn local répond et (b) le
#      tunnel ngrok est bien ouvert sur le domaine fixe. Si le TUNNEL est tombé (cas
#      le plus fréquent : ngrok se déconnecte alors que le runtime vit encore), elle
#      le RECONNECTE automatiquement sur le même domaine -> DevLLMA rebranché sans
#      que tu touches à rien.
#
#   ⚠️ Ce qu'elle NE peut PAS sauver : si Google COUPE la VM (préemption / limite
#      ~12 h du tier gratuit), tout le runtime meurt et aucune cellule ne survit.
#      Dans ce cas seul un "Exécuter tout" relance. DevLLMA bascule alors en local.
#
#   ⚠️ Complète-la par l'auto-clic navigateur (F12 → Console), voir tout en bas.
#
#  Pré-requis : la cellule 3 doit avoir été exécutée (elle définit NGROK_DOMAIN,
#  ngrok, et lance uvicorn sur le port 8000).
# ══════════════════════════════════════════════════════════════════════════

import time, datetime, requests, torch
from pyngrok import ngrok

PORT = 8000
CHECK_EVERY = 20  # secondes


def _tunnel_ok():
    """Le tunnel ngrok est-il ouvert sur notre port ?"""
    try:
        return any(str(PORT) in (t.config.get("addr", "") or "") for t in ngrok.get_tunnels())
    except Exception:
        return False


def _server_ok():
    """Le serveur uvicorn local répond-il ?"""
    try:
        return requests.get(f"http://127.0.0.1:{PORT}/", timeout=5).status_code == 200
    except Exception:
        return False


def _reconnect_tunnel():
    """Rétablit le tunnel ngrok sur le domaine FIXE (le worker DevLLMA le retrouve seul)."""
    try:
        ngrok.kill()  # ferme les tunnels morts/fantômes
        time.sleep(2)
        ngrok.connect(PORT, domain=NGROK_DOMAIN)  # NGROK_DOMAIN vient de la cellule 3
        return True
    except Exception as e:
        print(f"   ✗ échec reconnexion ngrok : {e}", flush=True)
        return False


print("=" * 64)
print(">>> KEEP-ALIVE AUTO-RÉPARANT ACTIF — surveille serveur + tunnel toutes",
      CHECK_EVERY, "s")
print("=" * 64, flush=True)

n = 0
while True:
    n += 1
    # (1) micro-calcul GPU : prouve l'activité ET garde le modèle/VRAM au chaud
    try:
        _ = (torch.randn(256, 256, device="cuda") @ torch.randn(256, 256, device="cuda")).sum().item()
        gpu = "GPU ok"
    except Exception as e:
        gpu = f"GPU KO: {e}"

    # (2) auto-réparation du tunnel s'il est tombé (runtime encore vivant)
    srv = _server_ok()
    tun = _tunnel_ok()
    healed = ""
    if srv and not tun:
        print(f"[{datetime.datetime.now():%H:%M:%S}] tunnel ngrok TOMBÉ → reconnexion…", flush=True)
        if _reconnect_tunnel():
            healed = " → tunnel RECONNECTÉ ✅"
            tun = True
    elif not srv:
        # Le serveur uvicorn lui-même est mort : il tourne dans un thread daemon de la
        # cellule 3, on ne peut pas le relancer d'ici de façon fiable. On le signale.
        healed = " → ⚠️ serveur uvicorn éteint : ré-exécute la cellule 3 (WORKER)"

    if n % 3 == 0 or healed:  # log concis : ~1 ligne/min, + toute réparation
        etat = "🟢 EN LIGNE" if (srv and tun) else "🔴 DÉGRADÉ"
        print(f"[{datetime.datetime.now():%H:%M:%S}] {etat} | serveur={srv} tunnel={tun} "
              f"| {gpu} | heartbeat #{n}{healed}", flush=True)

    time.sleep(CHECK_EVERY)


# ══════════════════════════════════════════════════════════════════════════
#  AUTO-CLIC NAVIGATEUR (à coller dans la console F12 de l'onglet Colab, PAS ici)
#  Déjoue l'invite anti-bot "êtes-vous toujours là ?". Une fois par onglet ouvert.
#
#  function _kaColab(){
#    const sels=['colab-connect-button','#connect','#comments > span'];
#    for(const s of sels){const el=document.querySelector(s);
#      if(el){(el.shadowRoot?.querySelector('#connect')||el).click();}}
#    console.log('[keep-alive Colab]', new Date().toLocaleTimeString());
#  }
#  setInterval(_kaColab, 60000);
# ══════════════════════════════════════════════════════════════════════════
