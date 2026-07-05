# Tests — Délégation des tâches lourdes au GPU Google Colab

Plan de test de l'intégration Colab (outil `colab_run` + routage auto image/vidéo).
À faire **après un redémarrage de la prod** (le code doit être chargé).

## ⚙️ Prérequis
1. Redémarrer la prod :
   ```
   powershell -ExecutionPolicy Bypass -File C:\Devllma\Reboot-Prod.ps1
   ```
2. Démarrer le worker Colab : ouvrir `Colab-Worker.md`, coller les 3 cellules dans un
   notebook Colab avec **GPU T4**, exécuter → noter l'`URL` et le `TOKEN` affichés.

## 🧪 Scénarios

| # | À taper dans DevLLMA | Attendu |
|---|----------------------|---------|
| 1 | `génère une image d'un chat astronaute` | Part vers l'agent (pas le pipeline) ; dit que Colab n'est pas configuré (repli propre) |
| 2 | `utilise cette url https://xxxx.trycloudflare.com et ce token ab12cd34` | Mémorise, génère l'image sur GPU, renvoie le chemin `workspace/colab_out/*.png` |
| 3 | `génère une image d'une ville futuriste la nuit` | Génère directement (URL déjà mémorisée) |
| 4 | `crée une vidéo courte d'une vague qui déferle` | `colab_run task=video` → fichier `.mp4` (plus lent : 1er chargement du modèle vidéo) |
| 5 | (fermer l'onglet Colab) `génère une image de montagne` | Message clair « GPU Colab injoignable », **pas** de plantage |
| 6 | `crée un site vitrine` puis `écris une fonction qui trie une liste` | Pipeline / fast-path normaux, **aucun** appel Colab (non-régression) |
| 7 | `peux-tu générer des vidéos ?` | **Répond** (et propose Colab) au lieu de construire un projet |

## 📌 Vérifications
- Fichiers produits : `C:\Devllma\workspace\colab_out\`
- Chaque appel Colab est tracé dans l'audit : `C:\Devllma\logs\agent_audit.jsonl`

## ℹ️ Rappels
- Colab est **éphémère** : à chaque nouvelle session Colab, relancer les cellules et
  redonner la nouvelle URL à DevLLMA (`utilise cette url ...`).
- L'URL et le token sont mémorisés en base (`memory`) → inutile de les redonner tant
  que la session Colab reste la même.
