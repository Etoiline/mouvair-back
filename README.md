# Mouv’Air — API

Cette API récupère les données Open-Meteo et produit les scores horaires des quatre activités.

Pour chaque activité et chaque créneau, le moteur sépare désormais :

- `weather` : note météo, niveau, trois sous-notes, explications et alertes ;
- `air` : note air, indice européen AQI, catégorie et recommandations générales ;
- `overall` : note globale combinant météo et air selon l’activité.

Une note impossible à calculer vaut `null` et porte le statut `incomplet`. Une note disponible est
arrondie à l’entier pour l’affichage, après conservation des décimales pendant tous les calculs.
Les anciens champs plats `score`, `status`, `factors` et `reasons` restent temporairement présents
pour maintenir l’interface actuelle ; ils reflètent la note globale et les nouveaux résultats.

## Créneaux et données utilisées

L’API renvoie 24 créneaux consécutifs d’une heure à partir de la prochaine heure pleine. `time` et
`end_time` contiennent le décalage UTC du lieu afin de distinguer les heures répétées lors d’un
changement d’heure. L’heure pleine est calculée dans le fuseau local, y compris lorsque son décalage
UTC comporte 30 ou 45 minutes.

Chaque créneau utilise les deux bornes horaires correspondantes :

- la température ressentie la plus pénalisante est choisie séparément pour chaque activité ;
- le vent moyen et l’AQI retiennent la valeur maximale des deux bornes ;
- les précipitations et rafales utilisent la valeur de fin, qui décrit l’heure précédente ;
- les codes météo des deux bornes sont conservés pour les alertes ;
- les concentrations maximales d’aulne, bouleau, graminées, armoise, olivier et ambroisie sont
  exposées dans `pollen`.

Les séries météo et air sont associées par leur timestamp Unix. Un champ absent ou invalide reste
indisponible et le créneau demeure présent dans la réponse.

## Cache et fraîcheur

Les prévisions d’une même position sont conservées en mémoire pendant 15 minutes lorsqu’elles
couvrent encore les 24 créneaux demandés. Une réponse mise en cache garde son heure réelle de
récupération dans `generated_at`. Le cache contient au maximum 256 positions et est vidé au
redémarrage du serveur.

La réponse expose les champs suivants :

- `data_state` vaut `fresh` pour des données actuelles, `refresh_failed` lorsqu’une actualisation a
  échoué avec des données âgées d’au plus une heure, puis `stale` au-delà d’une heure ;
- `from_cache` indique si les données proviennent de la mémoire du serveur ;
- `recommendation_available` passe à `false` lorsque les données sont anciennes afin de suspendre
  le meilleur créneau.

Après un échec d’actualisation, les départs passés sont retirés. Les créneaux futurs encore connus
sont conservés et les heures manquantes sont ajoutées avec des notes incomplètes. Les demandes
simultanées pour une même position partagent une seule récupération auprès du fournisseur.

## Commandes

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m uvicorn mouvair_api.main:app --app-dir src --reload
```

Tests et qualité :

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
```

## Créer une release

Le dépôt doit être propre et `CHANGELOG.md` doit contenir au moins une ligne de changement non
publiée sous son titre. Le script demande s’il faut incrémenter la version majeure, mineure ou
corrective, synchronise `pyproject.toml`, la version exposée par l’API et le changelog, puis crée le
commit `🔖 Release x.y.z` :

```powershell
python scripts/release.py
```

Pour prévisualiser la prochaine version sans modifier le dépôt ni créer de commit :

```powershell
python scripts/release.py --dry-run
```

## Endpoints

- `GET /health`
- `GET /api/locations?q=Lyon`
- `GET /api/forecast?latitude=45.75&longitude=4.85`
