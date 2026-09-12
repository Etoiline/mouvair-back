# Mouv’Air — API

Cette API récupère les données Open-Meteo et produit les scores horaires des quatre activités.

Pour chaque activité et chaque créneau, le moteur sépare désormais :

- `weather` : note météo, niveau, trois sous-notes, explications et alertes ;
- `air` : note air, indice européen AQI, catégorie et recommandations générales ;
- `overall` : note globale combinant météo et air selon l’activité.

Une note impossible à calculer vaut `null` et porte le statut `incomplet`. Une note disponible est
arrondie à l’entier pour l’affichage, après conservation des décimales pendant tous les calculs.
Le contrat `SportScore` expose uniquement ces trois sections afin de conserver une source claire
pour chaque note, ses facteurs et ses explications.

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

Les prévisions d’une même position sont conservées pendant 15 minutes lorsqu’elles couvrent encore
les 24 créneaux demandés. Chaque processus garde un cache mémoire L1 de 256 positions et Redis sert
de cache L2 partagé entre les workers et les instances. Une réponse mise en cache garde son heure
réelle de récupération dans `generated_at`. Un verrou Redis empêche plusieurs instances de
rafraîchir simultanément la même position.

La recherche de ville ne contacte Open-Meteo qu’à partir de trois caractères normalisés. Les
résultats de géocodage sont conservés 24 heures dans Redis et dans un petit cache mémoire local. Une
recherche sans résultat est conservée 5 minutes. La langue, le nombre de résultats et la requête
normalisée participent à la clé, et un verrou distribué évite les appels identiques simultanés.

La réponse expose les champs suivants :

- `data_state` vaut `fresh` pour des données actuelles, `refresh_failed` lorsqu’une actualisation a
  échoué avec des données âgées d’au plus une heure, puis `stale` au-delà d’une heure ;
- `from_cache` indique si les données proviennent d’un cache du serveur ;
- `recommendation_available` passe à `false` lorsque les données sont anciennes afin de suspendre
  le meilleur créneau.

Après un échec d’actualisation, les départs passés sont retirés. Les créneaux futurs encore connus
sont conservés et les heures manquantes sont ajoutées avec des notes incomplètes. Les demandes
simultanées pour une même position partagent une seule récupération auprès du fournisseur, y
compris entre plusieurs instances lorsque Redis est disponible.

## Quotas et limites de requêtes

Tous les appels sortants vers le géocodage, la météo et la qualité de l’air partagent le même budget
Redis. Les seuils par défaut conservent une marge de sécurité de 10 % sous les limites gratuites
[publiées par Open-Meteo](https://open-meteo.com/en/terms) :

| Fenêtre glissante | Limite Open-Meteo | Seuil Mouv’Air |
|---|---:|---:|
| Minute | 600 | 540 |
| Heure | 5 000 | 4 500 |
| Jour | 10 000 | 9 000 |
| 31 jours | 300 000 par mois | 270 000 |

Un géocodage réserve une unité. Un chargement de prévisions réserve atomiquement deux unités avant
de lancer en parallèle les appels météo et qualité de l’air. Une réservation n’est pas remboursée
après un échec, car le fournisseur a pu comptabiliser la requête. Les réponses provenant du cache ne
consomment aucune unité.

Les limites entrantes par identité client sont de 120 recherches de villes par minute et 1 200 par
heure, puis 40 demandes de prévisions par minute et 240 par heure. Elles s’appliquent aussi aux cache
hits. L’identité est dérivée par HMAC de l’adresse du client ; `X-Forwarded-For` n’est utilisé que si
le proxy direct appartient à `TRUSTED_PROXY_CIDRS`.

Une limite client renvoie `429` avec `Retry-After`. Lorsque le budget global ou sa protection sont
indisponibles, une donnée déjà en cache reste servie ; une opération nécessitant Open-Meteo renvoie
`503` avec `Retry-After`. Une erreur ordinaire du fournisseur reste une réponse `502`.

## Redis et fonctionnement dégradé

En production, `REDIS_FAILURE_MODE=closed` empêche tout nouvel appel fournisseur lorsque Redis est
indisponible. Les caches mémoire continuent à répondre et le repli `refresh_failed` ou `stale` reste
appliqué aux prévisions connues. Le limiteur client utilise alors un compteur local conservateur.
`/health` reste accessible avec l’état `degraded` et `redis: unavailable`.

Le mode `local` doit être activé explicitement pour travailler sans Redis. Ses compteurs sont propres
au processus et ne garantissent donc pas les quotas lors d’un déploiement multi-instance. Tous les
déploiements partageant la même IP sortante ou clé Open-Meteo doivent utiliser le même
`OPEN_METEO_QUOTA_SCOPE` et le même stockage Redis.

Le service fourni dans `compose.yaml` active AOF avec `appendfsync everysec` et interdit l’éviction.
Une production doit employer des garanties de persistance équivalentes, superviser la mémoire et
utiliser Redis 7.2 ou ultérieur comme stockage faisant autorité plutôt qu’une réplication à
cohérence éventuelle.

## Commandes

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
docker compose up -d redis
.\.venv\Scripts\python.exe -m uvicorn mouvair_api.main:app --app-dir src --reload --env-file .env
```

Remplacer `CLIENT_ID_HMAC_SECRET` dans `.env` par une valeur aléatoire longue avant d’exposer
l’API. Redis n’est pas obligatoire uniquement si `.env` contient explicitement
`REDIS_FAILURE_MODE=local`.

Tests et qualité :

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
$env:TEST_REDIS_URL="redis://127.0.0.1:6379/15"
.\.venv\Scripts\python.exe -m pytest -m redis_integration
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
- `GET /api/locations?q=Lyon` — trois caractères minimum
- `GET /api/forecast?latitude=45.75&longitude=4.85`
