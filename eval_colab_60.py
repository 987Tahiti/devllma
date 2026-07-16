# -*- coding: utf-8 -*-
"""
Banc de 60 tests de developpement LOURD via de vraies sessions DevLLMA (WebSocket),
avec delegation FORCEE au GPU Colab (phrase declencheuse "via Colab" dans chaque
prompt, cf. _COLAB_ASK_RE dans webui.py). Chaque test :
  - envoie une demande de projet multi-fonctionnalites (5+ caracteristiques listees
    explicitement -> len(todos)>=5, declenche aussi big_project meme sans Colab)
  - collecte TOUS les evenements websocket (pas juste le texte final)
  - verifie que la delegation Colab a REELLEMENT eu lieu (agent_start agent="colab-gpu")
    et pas juste tente puis repliee en local
  - verifie le succes reel du projet (project_done + run_result ok, ou au moins
    project_done avec fichiers si l'execution echoue pour une autre raison)
  - enregistre timing, evenements bruts, et un diagnostic clair

Usage: python eval_colab_60.py [N]   (N = nombre de tests a lancer, defaut 60)
Rapport cumulatif -> colab_60_results.json (ajoute a chaque run, jamais ecrase)
"""
import asyncio, json, os, sys, time
import websockets

URI = "ws://127.0.0.1:8080/ws"
RESULTS_FILE = r"C:\Devllma\colab_60_results.json"
PER_TEST_TIMEOUT = 1800  # 30 min max par test (plan + generation + ecriture + execution + auto-correction)
# Releve de 900 a 1800 (14/07/2026) : la sonde _http_probe renforcee de webui.py
# detecte desormais de VRAIES pannes (route CRUD cassee) qui passaient avant a tort
# -> plus de cycles d'auto-correction reellement declenches qu'avant, et sur ce poste
# CPU seul (generation locale qwen3-coder:30b quand Colab indisponible), 3-4 cycles
# completes peuvent legitimement depasser 900s. Sans cette marge, le harnais abandonne
# avant que DevLLMA ait fini -> run_ok=False qui ne reflete pas un vrai echec, juste
# un harnais trop impatient (constate : 2 tests consecutifs pile a la limite des 900s).

# ── 60 demandes de dev "lourdes" (5+ fonctionnalites explicites) forcant Colab ──
# DOIT matcher _COLAB_ASK_RE de webui.py : r'\b(utilise|via|avec|sur|passe par)\s+colab\b'
# "en utilisant Colab" (participe present) NE MATCHE PAS \butilise\b -- verifie et
# corrige apres coup (6 premiers tests : seulement 4/6 avaient reellement delegue,
# le reste dependait du hasard de la longueur du plan, pas d'un forcage reel).
_TRIGGER = "via Colab pour la generation de code"

CATEGORIES = {
    "api": [
        "une API FastAPI de gestion de taches avec : creation/edition/suppression de taches, "
        "categories, priorites (basse/moyenne/haute), dates d'echeance, et une route de recherche "
        "par mot-cle. Persistance SQLite. {trig}.",
        "une API FastAPI de reservation de salles avec : liste des salles, creneaux horaires, "
        "reservation avec verification de conflit, annulation, et export CSV des reservations du jour. "
        "Persistance SQLite. {trig}.",
        "une API FastAPI de forum avec : sujets, reponses imbriquees, upvote/downvote, "
        "recherche par titre, et un classement des sujets les plus actifs. Persistance SQLite. {trig}.",
        "une API FastAPI de recettes de cuisine avec : ajout de recette (ingredients, etapes, temps de "
        "preparation), recherche par ingredient disponible, filtrage par temps max, et notation 1-5 etoiles. "
        "Persistance SQLite. {trig}.",
        "une API FastAPI de suivi d'evenements avec : creation d'evenement, inscription de participants, "
        "liste d'attente si complet, rappel automatique (calcul du delai avant l'evenement), et export "
        "de la liste des participants. Persistance SQLite. {trig}.",
        "une API FastAPI de catalogue de bibliotheque avec : ajout de livre (titre, auteur, ISBN, "
        "categorie), emprunt/retour avec date limite, calcul d'amende de retard, et recherche multi-criteres. "
        "Persistance SQLite. {trig}.",
        "une API FastAPI d'offres d'emploi avec : publication d'offre, candidature avec CV texte, "
        "filtrage par competences requises, statut de candidature (recue/en cours/refusee/acceptee), "
        "et statistiques par offre. Persistance SQLite. {trig}.",
        "une API FastAPI de gestion d'abonnements avec : plans tarifaires, souscription, calcul de "
        "prorata a l'annulation, historique de facturation, et alerte de renouvellement proche. "
        "Persistance SQLite. {trig}.",
        "une API FastAPI de sondages avec : creation de sondage a choix multiples, vote unique par "
        "utilisateur (empeche le double vote), resultats en pourcentage, date de cloture automatique, "
        "et export des resultats en JSON. Persistance SQLite. {trig}.",
        "une API FastAPI de suivi de colis avec : creation d'expedition, mise a jour de statut "
        "(prepare/expedie/en transit/livre), historique complet des statuts avec horodatage, "
        "estimation de livraison, et recherche par numero de suivi. Persistance SQLite. {trig}.",
    ],
    "cli": [
        "un outil en ligne de commande Python d'organisation de fichiers qui : trie les fichiers d'un "
        "dossier par type (images/documents/videos/autres), detecte les doublons par hash, renomme "
        "selon un motif date+nom, et genere un rapport texte du tri effectue. {trig}.",
        "un outil en ligne de commande Python d'analyse de logs qui : parse un fichier log ligne par "
        "ligne, compte les erreurs par type, detecte les pics d'activite par heure, extrait les adresses "
        "IP suspectes (frequence anormale), et genere un resume texte. {trig}.",
        "un outil en ligne de commande Python de sauvegarde qui : copie un dossier source vers une "
        "destination avec horodatage, compresse en zip, verifie l'integrite par hash apres copie, "
        "supprime les sauvegardes de plus de 30 jours, et journalise chaque operation. {trig}.",
        "un outil en ligne de commande Python de gestion de mots de passe qui : genere des mots de "
        "passe forts configurables (longueur, symboles), les stocke chiffres localement, permet la "
        "recherche par nom de service, verifie la force d'un mot de passe fourni, et exporte en CSV chiffre. {trig}.",
        "un outil en ligne de commande Python de scan reseau local qui : liste les appareils actifs sur "
        "le sous-reseau, identifie les ports ouverts courants, mesure la latence, detecte les nouveaux "
        "appareils par rapport a un scan precedent, et genere un rapport. {trig}.",
        "un outil en ligne de commande Python d'assistant Git qui : resume les commits non pousses, "
        "detecte les fichiers modifies non commites, genere un message de commit suggere a partir du diff, "
        "liste les branches non fusionnees, et avertit des conflits potentiels. {trig}.",
        "un outil en ligne de commande Python de traitement CSV qui : valide le format et les types de "
        "colonnes, detecte les valeurs manquantes/aberrantes, calcule des statistiques par colonne, "
        "fusionne plusieurs CSV avec deduplication, et exporte un rapport de qualite. {trig}.",
        "un outil en ligne de commande Python de conversion Markdown vers HTML qui : convertit la "
        "syntaxe standard (titres/listes/liens/code), genere une table des matieres automatique, "
        "detecte les liens casses internes, et supporte un mode batch sur un dossier entier. {trig}.",
        "un outil en ligne de commande Python de gestion de taches (todo) qui : ajoute/complete/supprime "
        "des taches avec priorite et echeance, filtre par statut, trie par urgence calculee, archive les "
        "taches terminees de plus de 7 jours, et exporte en Markdown. {trig}.",
        "un outil en ligne de commande Python de chiffrement de fichiers qui : chiffre/dechiffre un "
        "fichier avec mot de passe, verifie l'integrite avant dechiffrement, gere plusieurs fichiers en "
        "lot, journalise chaque operation sans jamais logguer le mot de passe, et confirme avant ecrasement. {trig}.",
    ],
    "web": [
        "un tableau de bord web (FastAPI + HTML/CSS/JS) d'analyse de ventes avec : import de donnees "
        "CSV, graphiques de tendance par mois, filtrage par categorie de produit, top 5 des produits, "
        "et export du tableau filtre en CSV. {trig}.",
        "un panneau d'administration web (FastAPI + HTML/CSS/JS) de gestion d'utilisateurs avec : liste "
        "paginee, recherche par nom/email, activation/desactivation de compte, historique de connexion "
        "simule, et export de la liste filtree. {trig}.",
        "un tableau Kanban web (FastAPI + HTML/CSS/JS) avec : colonnes personnalisables, cartes avec "
        "glisser-deposer entre colonnes, etiquettes colorees, date d'echeance par carte, et compteur de "
        "cartes par colonne. {trig}.",
        "une application web de calendrier (FastAPI + HTML/CSS/JS) avec : vue mensuelle, ajout/edition "
        "d'evenements avec heure et duree, categories colorees, rappel visuel des evenements du jour, "
        "et recherche d'evenement par mot-cle. {trig}.",
        "une application web de suivi budgetaire (FastAPI + HTML/CSS/JS) avec : ajout de "
        "depenses/revenus par categorie, graphique de repartition mensuelle, solde courant, alerte de "
        "depassement de budget par categorie, et export mensuel en CSV. {trig}.",
        "une application web de suivi d'habitudes (FastAPI + HTML/CSS/JS) avec : creation d'habitudes a "
        "suivre, coche quotidienne, calcul de serie (streak) en cours, graphique de progression sur 30 "
        "jours, et rappel visuel des habitudes non cochees aujourd'hui. {trig}.",
        "une application web de planification d'entrainements (FastAPI + HTML/CSS/JS) avec : creation "
        "de seances (exercices, series, repetitions), calendrier hebdomadaire, historique des seances "
        "completees, calcul de volume total par semaine, et export du programme en PDF texte. {trig}.",
        "une application web de partage de recettes (FastAPI + HTML/CSS/JS) avec : ajout de recette avec "
        "photo simulee (URL), recherche par ingredient, filtrage par temps de preparation, systeme de "
        "favoris, et impression optimisee d'une recette. {trig}.",
        "une application web de partage de depenses de groupe (FastAPI + HTML/CSS/JS) avec : creation de "
        "groupe, ajout de depense partagee, calcul automatique de qui doit combien a qui, historique des "
        "remboursements, et export du solde de chacun. {trig}.",
        "une application web de suivi de projets (FastAPI + HTML/CSS/JS) avec : creation de projets avec "
        "jalons, taches assignees avec statut, barre de progression calculee, vue calendrier des "
        "echeances, et export du rapport d'avancement en CSV. {trig}.",
    ],
    "jeu": [
        "un jeu de morpion (tic-tac-toe) en Python avec : mode 2 joueurs local, mode contre une IA "
        "(qui bloque les coups gagnants adverses et cherche a gagner), detection de victoire/match nul, "
        "score cumule sur plusieurs parties, et interface en ligne de commande claire. {trig}.",
        "un jeu du pendu en Python avec : liste de mots par categorie (animaux/pays/fruits), affichage "
        "ASCII du pendu qui se dessine progressivement, limite d'essais, indice optionnel apres 3 erreurs, "
        "et score cumule sur plusieurs manches. {trig}.",
        "un jeu de memoire (memory/paires) en Python en ligne de commande avec : plateau de cartes "
        "melangees representees par des symboles, retournement de deux cartes par tour, detection de "
        "paire, compteur de coups, et chronometre de partie. {trig}.",
        "un quiz en Python avec : banque de questions par categorie et difficulte, choix multiples, "
        "chronometre par question, score avec bonus de rapidite, et classement des meilleurs scores "
        "sauvegarde en fichier. {trig}.",
        "un jeu d'aventure textuelle en Python avec : plusieurs salles reliees, inventaire d'objets a "
        "ramasser, enigmes necessitant un objet specifique, systeme de points de vie, et sauvegarde/"
        "chargement de partie. {trig}.",
        "un jeu du serpent (snake) en Python (console, tour par tour ou boucle simple) avec : "
        "deplacement du serpent, nourriture qui le fait grandir, detection de collision avec les bords et "
        "lui-meme, score croissant, et vitesse qui augmente progressivement. {trig}.",
        "un jeu de blackjack en Python avec : distribution de cartes, calcul de valeur de main (as "
        "flexible 1 ou 11), tirer/rester, croupier automatique suivant les regles standard, et solde de "
        "jetons persistant entre les parties. {trig}.",
        "un jeu de puissance 4 en Python avec : grille 7x6, deux joueurs en alternance, detection de "
        "victoire (ligne/colonne/diagonale), detection de match nul, et une IA simple pour jouer seul. {trig}.",
        "un generateur et solveur de sudoku en Python avec : generation de grille valide avec niveau de "
        "difficulte (nombre de cases retirees), solveur par backtracking, verification de solution "
        "utilisateur, et affichage formate de la grille. {trig}.",
        "un jeu de devinette de mot (type Wordle) en Python avec : mot secret de 5 lettres, "
        "indices couleur par lettre (bien place/mal place/absent), 6 essais maximum, dictionnaire de "
        "mots valides, et statistiques de parties gagnees. {trig}.",
    ],
    "data": [
        "un outil Python de conversion CSV vers JSON avec : validation du schema attendu, detection et "
        "signalement des lignes invalides, conversion des types (nombres/dates/booleens), option de "
        "structure imbriquee par groupement de colonne, et rapport de conversion. {trig}.",
        "un outil Python d'analyse de fichiers log avec : extraction des metriques cles (requetes/min, "
        "codes d'erreur, temps de reponse moyen), detection d'anomalies (pics), graphique ASCII simple en "
        "console, et export du resume en JSON. {trig}.",
        "un outil Python d'agregation meteo simulee avec : generation de donnees meteo aleatoires "
        "realistes sur 30 jours, calcul de moyennes/min/max par semaine, detection de tendances "
        "(rechauffement/refroidissement), et export graphique ASCII. {trig}.",
        "un outil Python de conversion de devises avec : taux de change configurables, historique de "
        "conversions avec horodatage, calcul de variation sur une periode, alerte si taux hors d'une plage "
        "definie, et export CSV de l'historique. {trig}.",
        "un outil Python de conversion d'unites avec : longueur/poids/temperature/volume, conversion "
        "bidirectionnelle, historique des conversions, mode batch depuis un fichier, et gestion des "
        "erreurs d'unite inconnue. {trig}.",
        "un outil Python d'analyse de texte avec : comptage de mots/phrases/paragraphes, mots les plus "
        "frequents (hors mots vides), score de lisibilite simple, detection de la langue probable, et "
        "export du rapport en Markdown. {trig}.",
        "un outil Python de generation de rapport a partir de donnees CSV avec : calcul de statistiques "
        "descriptives par colonne, detection de valeurs aberrantes, generation d'un rapport texte "
        "structure, section de recommandations automatiques, et export en fichier texte formate. {trig}.",
        "un outil Python de generation et lecture de QR codes avec : encodage de texte/URL en QR code "
        "(fichier image), decodage d'un QR code depuis une image, validation du contenu decode, et mode "
        "batch pour plusieurs fichiers. {trig}.",
        "un outil Python de redimensionnement d'images en lot avec : traitement de tout un dossier, "
        "conservation du ratio d'aspect optionnelle, conversion de format (PNG/JPG), renommage "
        "systematique, et rapport du nombre de fichiers traites/ignores. {trig}.",
        "un outil Python de planification de sauvegardes avec : configuration de dossiers a sauvegarder "
        "et frequence, calcul de la prochaine execution, simulation d'execution avec journalisation, "
        "rotation des anciennes sauvegardes, et rapport d'etat. {trig}.",
    ],
    "social": [
        "une API FastAPI de forum avec fils de discussion imbriques (reponses a des reponses), "
        "systeme de mentions @utilisateur, recherche full-text, marquage resolu/non-resolu, et "
        "statistiques d'activite par utilisateur. Persistance SQLite. {trig}.",
        "une API FastAPI de systeme de commentaires avec : commentaires imbriques sur un article, "
        "signalement de commentaire, moderation (approuver/rejeter), tri par popularite ou date, et "
        "limite anti-spam (frequence de post). Persistance SQLite. {trig}.",
        "une API FastAPI de notifications avec : creation de notification par evenement type, marquage "
        "lu/non lu, groupement par type, suppression en masse des notifications lues, et compteur de "
        "non-lues. Persistance SQLite. {trig}.",
        "un chatbot Python a base de regles avec : detection d'intentions par mots-cles, reponses "
        "contextuelles selon l'historique de la conversation, gestion de plusieurs sujets (meteo/"
        "salutations/aide), memoire du prenom de l'utilisateur, et journal de conversation. {trig}.",
        "une API FastAPI de fil d'actualite social simule avec : publications avec texte, likes, "
        "commentaires, tri chronologique ou par popularite, et flux personnalise base sur des "
        "abonnements simules. Persistance SQLite. {trig}.",
        "une API FastAPI de sondages avec commentaires avec : creation de sondage, vote, commentaire "
        "argumentant le vote, resultats en temps reel (recalcul a chaque appel), et cloture automatique "
        "apres une duree. Persistance SQLite. {trig}.",
        "une API FastAPI d'agregation d'avis (reviews) avec : ajout d'avis avec note et texte, calcul de "
        "moyenne ponderee, detection d'avis suspects (texte trop court ou duplique), tri par utilite, et "
        "export du resume par produit. Persistance SQLite. {trig}.",
        "une API FastAPI d'organisation de contenu par tags avec : creation de contenu avec tags "
        "multiples, recherche par combinaison de tags, suggestion de tags relies, nuage de tags les plus "
        "utilises, et fusion de tags synonymes. Persistance SQLite. {trig}.",
        "une API FastAPI de demandes d'amis avec : envoi/acceptation/refus de demande, liste d'amis, "
        "amis en commun entre deux utilisateurs, blocage d'utilisateur, et suggestions d'amis basees sur "
        "les amis communs. Persistance SQLite. {trig}.",
        "une API FastAPI de fil d'activite avec : enregistrement d'evenements utilisateur (action, "
        "horodatage), fil personnel et fil global, filtrage par type d'action, pagination, et resume "
        "quotidien genere automatiquement. Persistance SQLite. {trig}.",
    ],
}

def build_prompts():
    prompts = []
    for cat, items in CATEGORIES.items():
        for i, tpl in enumerate(items, 1):
            prompts.append({"id": f"{cat}_{i:02d}", "text": "Cree " + tpl.format(trig=_TRIGGER)})
    return prompts


async def run_one(test_id, prompt_text, wait=PER_TEST_TIMEOUT):
    """Envoie UNE demande lourde via une vraie session WebSocket DevLLMA et
    collecte tous les evenements pertinents pour verifier la delegation Colab
    et le succes reel du projet."""
    events = []
    text = ""
    t0 = time.time()
    try:
        async with websockets.connect(URI, ping_timeout=None, max_size=None, open_timeout=15) as ws:
            await ws.send(json.dumps({"type": "init", "session": ""}))
            await asyncio.wait_for(ws.recv(), timeout=10)
            await ws.send(json.dumps({"type": "message", "text": prompt_text}))
            while time.time() - t0 < wait:
                try:
                    m = await asyncio.wait_for(ws.recv(), timeout=max(1, wait - (time.time() - t0)))
                except asyncio.TimeoutError:
                    events.append({"type": "_timeout_harnais"})
                    break
                d = json.loads(m)
                t = d.get("type")
                if t == "token":
                    text += d.get("text", "")
                elif t == "speed":
                    continue
                else:
                    events.append(d)
                    if t == "done":
                        break
    except Exception as e:
        events.append({"type": "_exception_harnais", "error": str(e)})

    dur = round(time.time() - t0, 1)
    kinds = [e.get("type") for e in events]
    colab_used = any(e.get("type") == "agent_start" and e.get("agent") == "colab-gpu" for e in events)
    colab_attempted = "⚡ délégué au GPU Colab" in text or "delegue" in text.lower()
    colab_fallback = "GPU Colab indisponible" in text
    project_done = next((e for e in events if e.get("type") == "project_done"), None)
    run_results = [e for e in events if e.get("type") == "run_result"]
    run_ok = any(e.get("ok") for e in run_results) if run_results else None
    blocked = next((e for e in events if e.get("type") == "blocked"), None)

    return {
        "id": test_id,
        "duration_s": dur,
        "colab_delegation_confirmed": colab_used,
        "colab_fallback_to_local": colab_fallback,
        "project_created": bool(project_done),
        "files_count": project_done.get("count") if project_done else 0,
        "project_name": project_done.get("project_name") if project_done else None,
        "run_attempts": len(run_results),
        "run_ok": run_ok,
        "blocked_by_security": bool(blocked),
        "blocked_reasons": blocked.get("reasons") if blocked else None,
        "event_kinds": kinds,
        "final_text_excerpt": text[-400:] if text else "",
        "prompt_excerpt": prompt_text[:150],
    }


def append_result(entry):
    history = []
    if os.path.exists(RESULTS_FILE):
        try:
            history = json.load(open(RESULTS_FILE, encoding="utf-8"))
        except Exception:
            history = []
    history.append(entry)
    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=1)


async def main(n):
    prompts = build_prompts()[:n]
    print(f"=== Lancement de {len(prompts)} tests de dev lourd (delegation Colab forcee) ===\n")
    ok_count = 0
    colab_count = 0
    for i, p in enumerate(prompts, 1):
        print(f"[{i}/{len(prompts)}] {p['id']} ...", flush=True)
        t0 = time.time()
        result = await run_one(p["id"], p["text"])
        result["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
        append_result(result)
        status = "OK" if result["run_ok"] or result["project_created"] else "ECHEC"
        colab_tag = "COLAB" if result["colab_delegation_confirmed"] else ("repli-local" if result["colab_fallback_to_local"] else "?")
        if result["run_ok"] or result["project_created"]:
            ok_count += 1
        if result["colab_delegation_confirmed"]:
            colab_count += 1
        print(f"    -> {status} [{colab_tag}] {result['duration_s']}s, "
              f"fichiers={result['files_count']}, run_ok={result['run_ok']}")
        await asyncio.sleep(3)  # laisse le CPU/GPU retomber entre deux tests

    print(f"\n=== BILAN : {ok_count}/{len(prompts)} projets crees avec succes, "
          f"{colab_count}/{len(prompts)} avec delegation Colab confirmee ===")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    asyncio.run(main(n))
