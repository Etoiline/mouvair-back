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
| `open_meteo.py` | Appels HTTP fournisseur, géocodage, paramètres, erreurs réseau et quota. |
| `forecast_slots.py` | Assemblage pur des séries, mesures, pollens, créneaux et horaires locaux. |
| `errors.py` | Erreur de disponibilité commune aux services et aux adaptateurs de données. |
| `scoring.py` | Profils sportifs, courbes, notes précises, plafonds, arrondis et explications. |
| `forecast_service.py` | Cache mémoire L1, fraîcheur, fenêtre de 24 départs et réponse de secours. |
| `forecast_cache.py` | Sérialisation des prévisions dans le L2 Redis et verrou distribué. |
| `geocoding_service.py` | Normalisation des recherches, caches positif et négatif, mutualisation. |
| `rate_limit.py` | Fenêtres glissantes, réservations atomiques, attente fournisseur et limites client. |
| `client_identity.py` | Adresse réseau fiable derrière les proxies puis identifiant HMAC. |
| `redis_store.py` | Connexions, commandes, verrous à jeton et délai de repli après une panne Redis. |

Pour suivre une requête de prévision, commencer par `main.py`, puis `ForecastService.get_forecast`,
`OpenMeteoGateway.get_forecast`, l’assemblage des créneaux dans `forecast_slots.py` et enfin le moteur
`scoring.py`. `models.py` décrit les objets échangés tout au long de ce parcours.

`forecast_slots.py` ne réalise aucun accès HTTP ou Redis. Son interface publique expose
`assemble_forecast` pour construire la réponse à partir des séries déjà récupérées et
`next_full_hour` pour calculer les départs locaux. Le service de prévision utilise directement
cette règle horaire commune, sans importer de fonction privée du transport Open-Meteo.

Les erreurs attendues dérivent de `DataUnavailableError`, défini dans `errors.py`. L’adaptateur
conserve ses exceptions spécialisées pour les erreurs HTTP et les quotas. Les services déclenchent
leur repli sur le contrat commun ; les routes distinguent toujours les limitations `503` des
autres indisponibilités `502`.

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
- `compose.yaml` démarre l’API et Redis local avec son volume persistant ; le `Dockerfile`
  construit le serveur API, exécuté avec un utilisateur dédié.
- `.gitignore` et `.dockerignore` expliquent les fichiers locaux ou générés à exclure.
- `.github/workflows/checks.yml` exécute Ruff et les tests avec Redis sur GitHub Actions.
- `scripts/release.py` vérifie la cohérence des versions et du changelog avant de préparer les
  fichiers puis de créer le commit de release local. `--dry-run` conserve le choix interactif.
- `CHANGELOG.md` conserve l’historique des versions ; `AGENTS.md` décrit les conventions du dépôt.

### Organisation des tests

Chaque fichier de test explique son périmètre et le comportement attendu des scénarios.

| Suite | Vérifications principales |
|---|---|
| `test_scoring.py` | Courbes, frontières des catégories, arrondis, plafonds et données incomplètes. |
| `test_open_meteo.py` | Paramètres HTTP et passage des réponses météo et air à l’assemblage. |
| `test_forecast_slots.py` | Bornes des créneaux, jointure par timestamp, pollens et changements d’heure. |
| `test_forecast_service.py` | Fraîcheur, repli, LRU, requêtes concurrentes et partage entre instances. |
| `test_forecast_cache.py` | Identité, clés v1/v2, JSON, fuseaux, dates, créneaux et attente bornée. |
| `test_geocoding_service.py` | Normalisation, cache négatif, partage et fonctionnement sans L2. |
| `test_open_meteo_gateway.py` | Coûts réservés, erreurs de quota et propagation de Retry-After. |
| `test_rate_limit.py` | Réservations locales indivisibles, idempotence et politiques de repli. |
| `test_client_identity.py` | Proxy de confiance et résistance aux en-têtes d’adresse forgés. |
| `test_config.py` | Valeurs par défaut et refus des configurations incohérentes. |
| `test_main.py` | Validation des entrées et traduction HTTP des états de service. |
| `test_forecast_api.py` | Contrat JSON, 24 créneaux, cache, repli, validation, quotas et CORS. |
| `test_redis_integration.py` | Atomicité, verrous, TTL, corruption et rafraîchissements concurrents. |

`conftest.py` fournit un stockage mémoire indexé par clé pour exercer le véritable
`RedisForecastCache` dans les tests unitaires. Le partage entre deux services est vérifié avec
des coordonnées fournisseur identiques ou différentes de celles de la demande.
`forecast_payloads.py` fournit les séries simulées communes aux tests d’assemblage et d’adaptation
HTTP. `forecast_fixtures.py` partage l’horloge contrôlée et le fournisseur séquencé entre les
scénarios HTTP et de service. Les tests HTTP conservent le middleware, la sérialisation FastAPI,
le vrai service de prévision et l’adaptateur de cache ; les données fournisseur sont simulées.
Les tests ne consomment donc aucun quota Open-Meteo.

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

Lorsqu’une autre instance rafraîchit déjà la position, l’attente ignore la génération connue,
identifiée par son `generated_at`. L’ancienne entrée peut rester présente dans Redis pendant le
chargement : elle ne doit pas être confondue avec le nouveau résultat. Si aucune nouvelle
génération n’arrive avant le délai configuré, le service utilise son repli habituel ou signale
l’indisponibilité lorsqu’il ne possède aucun historique.

À chaque lecture du L2, le JSON doit respecter le modèle de prévision et ses métadonnées
temporelles doivent être utilisables : le fuseau doit être reconnu, `generated_at`, `time` et
`end_time` doivent être des dates ISO avec décalage UTC, et chaque créneau doit durer une heure
réelle. La comparaison en UTC conserve les créneaux valides lors des changements d’heure.
Les chaînes et leurs décalages restent inchangés dans la réponse publique.

Une entrée invalide est supprimée puis traitée comme une absence de cache : le service tente
une récupération fournisseur, ou utilise un historique local valide si son repli est nécessaire.
Une date invalide n’est donc pas remplacée par l’heure courante pour lui attribuer une fausse
fraîcheur. Les mesures absentes et les notes `null` restent autorisées ; l’âge des données est
toujours évalué séparément par le service de prévision.

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

Le middleware CORS enveloppe la limitation client : les réponses `429` conservent les en-têtes
d’autorisation pour les origines configurées dans `FRONTEND_ORIGINS`. Le frontend peut ainsi lire
le message JSON plutôt que recevoir une erreur réseau liée à CORS. `Retry-After` est également
exposé au JavaScript du navigateur sur les réponses `429` et `503`. Les prérequêtes `OPTIONS`
ne consomment pas le budget client ; les origines absentes de la configuration ne reçoivent pas
d’autorisation CORS.

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

### API et Redis avec Docker

Pour tester le backend localement sans installer Python sur le poste :

```powershell
cd E:\projects\mouvair\mouvair-back
docker compose up -d --build --wait
docker compose ps
```

Compose construit l’image de l’API, attend que Redis soit disponible, puis vérifie que l’API
répond avec un état `ok` et une connexion Redis `ok`. Les deux services doivent apparaître
avec l’état `healthy` dans Docker. L’API est accessible sur `http://localhost:8000`, sa
documentation sur `http://localhost:8000/docs` et son diagnostic sur `http://localhost:8000/health`.

Le fichier `.env` est facultatif dans ce mode. S’il existe, ses valeurs sont transmises au
conteneur ; Compose fixe le port interne à `8000`, l’adresse Redis à `redis://redis:6379/0`
et le mode de panne à `closed`. Les ports publiés restent limités au poste local.

Pour utiliser l’interface, lancer le frontend dans un autre terminal :

```powershell
cd E:\projects\mouvair\mouvair-front
pnpm install
pnpm dev
```

Le site est disponible sur `http://localhost:5173` et utilise l’API locale par défaut.
Après une modification du backend, relancer `docker compose up -d --build --wait` pour
reconstruire son image. Pour consulter ses journaux :

```powershell
docker compose logs -f api
```

Pour arrêter l’environnement en conservant les données Redis :

```powershell
docker compose stop
```

### Développement avec Uvicorn sur le poste hôte

Ce mode facilite le rechargement automatique du code. Si l’API Docker est déjà lancée,
arrêter son service avec `docker compose stop api` pour libérer le port `8000`.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
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
nettoient uniquement leurs propres clés et vérifient les réservations concurrentes, les verrous,
le délai partagé, `/health`, l’expiration réelle du cache, son rétablissement après corruption et
le partage d’un rafraîchissement entre deux connexions. Ils couvrent aussi le cas où une ancienne
prévision reste présente pendant le chargement de la suivante.

Sans `TEST_REDIS_URL`, ces tests d’intégration sont ignorés ; les autres tests restent exécutables
sans Redis. Lorsque la variable est définie, une connexion Redis impossible fait échouer les
tests concernés. `docker compose stop redis` arrête le service sans supprimer le volume persistant,
tandis que `docker compose down -v` supprimerait aussi les données Redis locales.

### Vérifications automatiques sur GitHub

Le workflow `.github/workflows/checks.yml` démarre à chaque push et pull request ; il peut aussi
être déclenché manuellement depuis l’onglet Actions. Il utilise Python 3.12 comme le `Dockerfile`,
installe le package avec son extra `dev`, puis lance Ruff et toute la suite Pytest.

Un conteneur Redis 8.2 dédié au job est attendu jusqu’à son état sain. `TEST_REDIS_URL` pointe
explicitement vers sa base 15 pour activer les tests Redis en CI. Ce service est éphémère et
n’utilise ni le Redis de développement ni celui de production. La configuration suit le
[fonctionnement des services Redis dans GitHub Actions](https://docs.github.com/en/actions/tutorials/use-containerized-services/create-redis-service-containers).

Les actions sont épinglées à leurs SHA de release. Le workflow possède uniquement le droit de
lecture du dépôt ; il ne déploie pas l’application. Sa première exécution sur GitHub aura lieu
après publication de la branche contenant ce fichier.

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
