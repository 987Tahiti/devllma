# ══════════════════════════════════════════════════════════════════════════
#  DevLLMA — CELLULE HEARTBEAT (anti-déconnexion Colab)
#  À COLLER dans une nouvelle cellule Colab, À EXÉCUTER EN DERNIER (après la
#  cellule 3 "WORKER PRET"). Laisse-la tourner : elle ne s'arrête jamais.
#
#  POURQUOI : la cellule 3 lance le serveur dans un thread daemon PUIS se
#  termine. Une fois toutes les cellules finies, Colab considère le runtime
#  "oisif" (le thread daemon ne compte pas) et coupe au bout de ~90 min.
#  Cette cellule garde une exécution AU PREMIER PLAN en permanence -> le
#  runtime n'est jamais oisif -> pas de coupure d'inactivité.
#
#  ⚠️ Ça n'empêche PAS la limite absolue ~12 h du tier gratuit. Quand elle
#  tombe, DevLLMA bascule seul en local et affiche "Colab hors ligne" ; il
#  suffit de re-faire "Exécuter tout".
#
#  ⚠️ Complète-la par l'auto-clic navigateur (F12 -> Console), voir plus bas.
# ══════════════════════════════════════════════════════════════════════════

import time, datetime, torch

n = 0
while True:
    n += 1
    try:
        # micro-calcul GPU : prouve l'activité ET garde la VRAM / le modèle au chaud
        _ = (torch.randn(512, 512, device="cuda") @ torch.randn(512, 512, device="cuda")).sum().item()
        gpu = "GPU ok"
    except Exception as e:
        gpu = f"GPU KO: {e}"
    print(f"[{datetime.datetime.now():%H:%M:%S}] worker vivant — heartbeat #{n} ({gpu})", flush=True)
    time.sleep(60)


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
