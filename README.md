# Mouv’Air — API

Cette API récupère les données Open-Meteo et produit les scores horaires des quatre activités.

Pour chaque activité et chaque créneau, le moteur sépare désormais :

- `weather` : note météo, niveau, trois sous-notes, explications et alertes ;
- `air` : note air, indice européen AQI, catégorie et recommandations générales ;
- `overall` : note globale combinant météo et air selon l’activité.

Une note impossible à calculer vaut `null` ; les sections `weather` et `overall` portent alors le
statut `incomplet`. Une note disponible est arrondie à l’entier pour l’affichage, après conservation
des décimales pendant tous les calculs. Le contrat `SportScore` contient les métadonnées `sport` et
`label`, puis ces trois sections pour chaque note, ses facteurs et ses explications.

## Organisation et lecture du code

Le package Python se trouve dans `src/mouvair_api`. Ses modules sont commentés en français :
les docstrings expliquent les contrats et les commentaires détaillent les choix de traitement,
notamment les plafonds, les bornes temporelles et la coordination entre requêtes.

| Module | Responsabilité |
|---|---|
| `__init__.py` | Version du package, synchronisée par le script de release. |
| `main.py` | Construction de FastAPI, cycle de vie des connexions, CORS, routes et erreurs HTTP. |
| `config.py` | Configuration immuable, lecture de l’environnement et validation des limites. |
| `models.py` | Contrat Pydantic des villes, mesures, pollens, scores et prévisions. |
| `open_meteo.py` | Appels fournisseur, nettoyage des séries, jointure temporelle et créneaux. |
| `scoring.py` | Profils sportifs, courbes, notes précises, plafonds, arrondis et explications. |
| `forecast_service.py` | Cache mémoire L1, fraîcheur, fenêtre de 24 départs et réponse de secours. |
| `forecast_cache.py` | Sérialisation des prévisions dans le L2 Redis et verrou distribué. |
| `geocoding_service.py` | Normalisation des recherches, caches positif et négatif, mutualisation. |
| `rate_limit.py` | Fenêtres glissantes, réservations atomiques, attente fournisseur et limites client. |
| `client_identity.py` | Adresse réseau fiable derrière les proxies puis identifiant HMAC. |
| `redis_store.py` | Connexions, commandes, verrous à jeton et délai de repli après une panne Redis. |

Pour suivre une requête de prévision, commencer par `main.py`, puis `ForecastService.get_forecast`,
`OpenMeteoGateway.get_forecast`, l’assemblage des créneaux dans `open_meteo.py` et enfin le moteur
`scoring.py`. `models.py` décrit les objets échangés tout au long de ce parcours.

Les ressources réseau sont créées au démarrage dans le gestionnaire `lifespan` et partagées par
les routes via `app.state`. Les services acceptent des dépendances injectées, par exemple une
horloge contrôlée ou un fournisseur simulé, pour tester les comportements sans appel externe.

Le backend renvoie les notes des quatre activités dans chaque réponse. Le frontend peut donc
changer d’activité et sélectionner le meilleur créneau sans redemander les données. Les pollens
restent informatifs et ne participent à aucune note.

### Fichiers de configuration et maintenance

- `pyproject.toml` décrit le package, ses dépendances, la découverte des sources, Pytest et Ruff.
- `.env.example` explique chaque groupe de variables ; `.env` contient la configuration locale
  chargée par l’option `--env-file` d’Uvicorn et reste exclu de Git et de l’image Docker.
- `compose.yaml` fournit Redis local avec son volume persistant ; le `Dockerfile` construit le
  serveur API de production, exécuté avec un utilisateur dédié.
- `.gitignore` et `.dockerignore` expliquent les fichiers locaux ou générés à exclure.
- `scripts/release.py` vérifie la cohérence des versions et du changelog avant de préparer les
  fichiers puis de créer le commit de release local. `--dry-run` conserve le choix interactif.
- `CHANGELOG.md` conserve l’historique des versions ; `AGENTS.md` décrit les conventions du dépôt.

### Organisation des tests

Chaque fichier de test explique son périmètre et le comportement attendu des scénarios.

| Suite | Vérifications principales |
|---|---|
| `test_scoring.py` | Courbes, frontières des catégories, arrondis, plafonds et données incomplètes. |
| `test_open_meteo.py` | Bornes des créneaux, jointure par timestamp, pollens et changements d’heure. |
| `test_forecast_service.py` | Fraîcheur, repli, LRU, requêtes concurrentes et partage entre instances. |
| `test_forecast_cache.py` | Identité demandée, métadonnées fournisseur et isolation des clés v1/v2. |
| `test_geocoding_service.py` | Normalisation, cache négatif, partage et fonctionnement sans L2. |
| `test_open_meteo_gateway.py` | Coûts réservés, erreurs de quota et propagation de Retry-After. |
| `test_rate_limit.py` | Réservations locales indivisibles, idempotence et politiques de repli. |
| `test_client_identity.py` | Proxy de confiance et résistance aux en-têtes d’adresse forgés. |
| `test_config.py` | Valeurs par défaut et refus des configurations incohérentes. |
| `test_main.py` | Validation des entrées et traduction HTTP des états de service. |
| `test_redis_integration.py` | Scripts atomiques, verrous, attente et cache L2 sur un vrai Redis. |

`conftest.py` fournit un stockage mémoire indexé par clé pour exercer le véritable
`RedisForecastCache` dans les tests unitaires. Le partage entre deux services est vérifié avec
des coordonnées fournisseur identiques ou différentes de celles de la demande.

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

Les coordonnées exactes de la demande, sans arrondi supplémentaire, identifient le cache mémoire,
les entrées Redis et leurs verrous. Elles sont transmises explicitement à l’écriture du L2.
Les coordonnées renvoyées par le fournisseur restent dans le contenu de la réponse, même si elles
diffèrent de la position demandée ; elles ne servent pas à construire la clé de stockage.

Les clés de prévision et de verrou utilisent l’espace `v2`. Les anciennes entrées `v1`, dont la
clé d’écriture dépendait des coordonnées fournisseur, restent isolées et expirent naturellement
selon leur TTL. Les clés de géocodage et les compteurs de quota gardent leur espace existant.

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
le proxy direct appartient à `TRUSTED_PROXY_CIDRS`. L’en-tête unique `X-Real-IP`, fourni notamment
par Railway, est accepté selon la même règle et prend alors la priorité.

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

Tests complets avec une instance Redis locale :

```powershell
docker compose up -d redis
docker compose exec -T redis redis-cli ping
$env:TEST_REDIS_URL="redis://127.0.0.1:6379/15"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
Remove-Item Env:TEST_REDIS_URL
docker compose stop redis
```

La commande `ping` doit répondre `PONG`. Les tests marqués `redis_integration` utilisent la base 15,
nettoient leurs clés et vérifient les réservations concurrentes, les verrous, le délai partagé et
`/health`. `docker compose stop redis` arrête le service sans supprimer le volume persistant, tandis
que `docker compose down -v` supprimerait aussi les données Redis locales.

## Déploiement sur Railway

Le `Dockerfile` lance l’API sur le port injecté par Railway. Pour déployer la production :

1. créer un projet Railway et y ajouter un service Redis privé avec un volume monté sur `/data` ;
2. activer l’AOF avec `appendfsync everysec` et la politique `noeviction` sur ce service ;
3. connecter ce dépôt GitHub au service API ;
4. configurer `/health` comme chemin de vérification ;
5. générer un domaine public uniquement pour l’API.

Variables minimales du service API :

```dotenv
FRONTEND_ORIGINS=https://mouvair.example.dev
REDIS_URL=${{Redis.REDIS_URL}}
REDIS_FAILURE_MODE=closed
CLIENT_ID_HMAC_SECRET=replace-with-a-long-random-secret
TRUSTED_PROXY_CIDRS=10.0.0.0/8,100.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7
```

La valeur de `CLIENT_ID_HMAC_SECRET` doit être générée dans Railway et ne jamais être versionnée.
Les réseaux de confiance ci-dessus sont réservés au déploiement Railway, dont le proxy renseigne
`X-Real-IP`. Remplacer l’origine d’exemple par l’URL de production exacte du frontend.

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
