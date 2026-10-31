"""Adapte les trois API Open-Meteo au contrat de Mouv’Air.

Le géocodage fournit des villes. Les API météo et air sont appelées en parallèle,
puis leurs séries sont indexées par timestamp pour construire 24 créneaux d’une
heure réelle. La passerelle publique réserve le budget avant tout appel sortant ;
les fonctions de lecture et d’assemblage restent utilisables avec un client simulé.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from math import ceil, isfinite
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .models import (
    Conditions,
    ForecastResponse,
    HourlyForecast,
    Location,
    PollenForecast,
    PollenId,
    SlotConditions,
    SportId,
)
from .rate_limit import RateLimitBackendUnavailable, RateLimitExceeded
from .scoring import calculate_score, select_apparent_temperature

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
# On demande une marge de données pour former les deux bornes des 24 départs futurs.
FORECAST_HOURS = 27
SLOT_COUNT = 24

SPORTS: tuple[SportId, ...] = ("easy_run", "hard_run", "cycling", "walking")
# Les unités par défaut du fournisseur correspondent aux °C, mm et km/h du moteur.
WEATHER_FIELDS = (
    "apparent_temperature",
    "precipitation",
    "wind_speed_10m",
    "wind_gusts_10m",
    "weather_code",
)
POLLEN_FIELDS: dict[PollenId, str] = {
    "alder": "alder_pollen",
    "birch": "birch_pollen",
    "grass": "grass_pollen",
    "mugwort": "mugwort_pollen",
    "olive": "olive_pollen",
    "ragweed": "ragweed_pollen",
}
AIR_FIELDS = ("european_aqi", *POLLEN_FIELDS.values())


class OpenMeteoError(RuntimeError):
    """Erreur fournisseur attendue, traduite en HTTP ou en repli par les services."""

    pass


class OpenMeteoQuotaError(OpenMeteoError):
    """Refus temporaire de l’opération avec un délai exploitable dans Retry-After."""

    def __init__(self, message: str, retry_after: int) -> None:
        """Conserve une attente strictement positive pour la traduction en HTTP 503."""
        super().__init__(message)
        self.retry_after = max(1, retry_after)


class OpenMeteoProviderRateLimit(OpenMeteoError):
    """Distingue un HTTP 429 fournisseur pour propager une attente entre instances."""

    def __init__(self, retry_after: int) -> None:
        """Mémorise le délai demandé par le fournisseur avant de le partager."""
        super().__init__("Open-Meteo limite temporairement les appels")
        self.retry_after = max(1, retry_after)


class QuotaGuard(Protocol):
    """Capacités requises par la passerelle : réserver des unités et imposer une attente."""
    async def reserve(self, cost: int) -> None: ...

    async def block_for(self, seconds: int) -> None: ...


def _provider_retry_after(response: httpx.Response) -> int:
    """Interprète Retry-After comme un nombre de secondes ou une date HTTP.

    Une valeur absente ou inutilisable entraîne une attente de 60 secondes.
    L’arrondi supérieur évite de reprendre avant l’échéance annoncée.
    """
    value = response.headers.get("Retry-After")
    if value is None:
        return 60
    try:
        return max(1, int(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return 60
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return max(1, ceil((retry_at - datetime.now(UTC)).total_seconds()))


async def _get_json(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, Any],
    *,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Effectue un GET et convertit les échecs réseau, HTTP ou JSON en erreurs métier.

    Le cas 429 reste identifiable par la passerelle. Un JSON valide doit encore
    être un objet ; les champs attendus sont contrôlés lors de leur utilisation.
    """
    try:
        if timeout is None:
            response = await client.get(url, params=params)
        else:
            response = await client.get(url, params=params, timeout=timeout)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            raise OpenMeteoProviderRateLimit(_provider_retry_after(exc.response)) from exc
        raise OpenMeteoError("Open-Meteo est momentanément indisponible") from exc
    except httpx.HTTPError as exc:
        raise OpenMeteoError("Open-Meteo est momentanément indisponible") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise OpenMeteoError("Réponse Open-Meteo invalide") from exc

    if not isinstance(payload, dict):
        raise OpenMeteoError("Réponse Open-Meteo invalide")
    return payload


async def _search_locations(
    client: httpx.AsyncClient, query: str, count: int
) -> list[Location]:
    """Demande des villes en français et valide les champs nécessaires à leur sélection."""
    payload = await _get_json(
        client,
        GEOCODING_URL,
        {"name": query, "count": count, "language": "fr", "format": "json"},
        timeout=10,
    )

    results = payload.get("results", [])
    if not isinstance(results, list):
        raise OpenMeteoError("Réponse Open-Meteo invalide")
    try:
        return [
            Location(
                id=item["id"],
                name=item["name"],
                latitude=item["latitude"],
                longitude=item["longitude"],
                country=item.get("country"),
                admin1=item.get("admin1"),
                timezone=item.get("timezone"),
            )
            for item in results
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise OpenMeteoError("Réponse Open-Meteo invalide") from exc


async def search_locations(
    query: str,
    count: int = 5,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[Location]:
    """Réutilise un client injecté ou possède un client fermé à la fin de l’appel.

    Dans l’API, OpenMeteoGateway fournit le client partagé et contrôle le quota.
    L’appel direct de cette fonction ne réserve pas lui-même de budget.
    """
    if client is not None:
        return await _search_locations(client, query, count)
    async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "MouvAir/0.3"}) as owned:
        return await _search_locations(owned, query, count)


def _unix_timestamp(value: object) -> int | None:
    """Accepte un instant Unix entier et fini, en excluant explicitement les booléens."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not isfinite(numeric) or not numeric.is_integer():
        return None
    return int(numeric)


def _hourly_rows(
    payload: dict[str, Any], fields: tuple[str, ...]
) -> dict[int, dict[str, object]]:
    """Indexe les séries par timestamp et invalide les horaires dupliqués.

    Les tableaux de mesures peuvent être incomplets ou désordonnés. Une valeur
    absente devient None ; un timestamp dupliqué est supprimé entièrement plutôt
    que de choisir arbitrairement une de ses mesures contradictoires.
    """
    hourly = payload.get("hourly")
    if not isinstance(hourly, dict):
        return {}
    times = hourly.get("time")
    if not isinstance(times, list):
        return {}

    rows: dict[int, dict[str, object]] = {}
    duplicates: set[int] = set()
    for index, raw_time in enumerate(times):
        timestamp = _unix_timestamp(raw_time)
        if timestamp is None or timestamp in duplicates:
            continue
        if timestamp in rows:
            rows.pop(timestamp)
            duplicates.add(timestamp)
            continue

        row: dict[str, object] = {}
        for field in fields:
            values = hourly.get(field)
            row[field] = values[index] if isinstance(values, list) and index < len(values) else None
        rows[timestamp] = row
    return rows


def _number(value: object, *, non_negative: bool) -> float | None:
    """Nettoie une mesure sans transformer une absence ou une anomalie en zéro."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not isfinite(number) or (non_negative and number < 0):
        return None
    return number


def _maximum(left: object, right: object, *, non_negative: bool = True) -> float | None:
    """Retient le maximum seulement si les deux bornes sont disponibles et valides.

    Une seule borne connue ne suffit pas pour représenter le maximum du créneau.
    """
    left_number = _number(left, non_negative=non_negative)
    right_number = _number(right, non_negative=non_negative)
    if left_number is None or right_number is None:
        return None
    return max(left_number, right_number)


def _weather_code(value: object) -> int | None:
    """Vérifie le type du code ; sa présence dans la nomenclature est contrôlée au score."""
    return value if type(value) is int else None


def _required_coordinate(payload: dict[str, Any], field: str) -> float:
    """Exige les coordonnées numériques renvoyées par le fournisseur météo."""
    value = _number(payload.get(field), non_negative=False)
    if value is None:
        raise OpenMeteoError("Coordonnées absentes de la réponse météo")
    return value


def _timezone(payload: dict[str, Any]) -> tuple[str, ZoneInfo]:
    """Valide le nom IANA nécessaire à l’affichage local et à l’alignement des départs."""
    name = payload.get("timezone")
    if not isinstance(name, str):
        raise OpenMeteoError("Fuseau horaire absent de la réponse météo")
    try:
        return name, ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise OpenMeteoError("Fuseau horaire invalide dans la réponse météo") from exc


def _next_full_hour(moment: datetime, timezone: ZoneInfo) -> datetime:
    """Trouve la prochaine heure pleine locale et renvoie son instant UTC.

    Une heure déjà pleine est conservée. La recherche progresse en minutes UTC
    pour respecter aussi les décalages de 30 ou 45 minutes et les transitions
    saisonnières, sans créer d’heure locale inexistante.
    """
    if moment.tzinfo is None:
        raise ValueError("L’instant de récupération doit inclure un fuseau horaire")
    utc_moment = moment.astimezone(UTC)
    candidate = utc_moment.replace(second=0, microsecond=0)
    if candidate < utc_moment:
        candidate += timedelta(minutes=1)

    # La chronologie UTC gère correctement les heures locales répétées ou inexistantes.
    for _ in range(181):
        if candidate.astimezone(timezone).minute == 0:
            return candidate
        candidate += timedelta(minutes=1)
    raise ValueError("Impossible de déterminer la prochaine heure pleine locale")


def _row_value(rows: dict[int, dict[str, object]], timestamp: int, field: str) -> object:
    """Lit une mesure par instant exact, avec absence explicite si sa ligne manque."""
    return rows.get(timestamp, {}).get(field)


def _build_pollen(
    air_rows: dict[int, dict[str, object]], start_timestamp: int, end_timestamp: int
) -> PollenForecast:
    """Expose les maxima des deux bornes pour les six pollens, sans les noter."""
    values = {
        pollen_id: _maximum(
            _row_value(air_rows, start_timestamp, provider_field),
            _row_value(air_rows, end_timestamp, provider_field),
        )
        for pollen_id, provider_field in POLLEN_FIELDS.items()
    }
    return PollenForecast.model_validate(values)


def _build_slot(
    start: datetime,
    timezone: ZoneInfo,
    weather_rows: dict[int, dict[str, object]],
    air_rows: dict[int, dict[str, object]],
) -> HourlyForecast:
    """Agrège les deux bornes selon le sens physique des champs puis calcule les sports.

    Le départ est en UTC : ajouter une heure conserve une durée réelle d’une
    heure même lors d’un changement d’heure locale. Les mesures communes sont
    calculées une fois ; la borne thermique est ensuite choisie pour chaque sport.
    """
    end = start + timedelta(hours=1)
    start_timestamp = int(start.timestamp())
    end_timestamp = int(end.timestamp())

    temperature_start = _number(
        _row_value(weather_rows, start_timestamp, "apparent_temperature"),
        non_negative=False,
    )
    temperature_end = _number(
        _row_value(weather_rows, end_timestamp, "apparent_temperature"),
        non_negative=False,
    )
    # Ces champs de fin décrivent l’heure précédente, donc l’intervalle start -> end.
    precipitation = _number(
        _row_value(weather_rows, end_timestamp, "precipitation"),
        non_negative=True,
    )
    wind_speed = _maximum(
        _row_value(weather_rows, start_timestamp, "wind_speed_10m"),
        _row_value(weather_rows, end_timestamp, "wind_speed_10m"),
    )
    wind_gusts = _number(
        _row_value(weather_rows, end_timestamp, "wind_gusts_10m"),
        non_negative=True,
    )
    weather_codes = [
        _weather_code(_row_value(weather_rows, start_timestamp, "weather_code")),
        _weather_code(_row_value(weather_rows, end_timestamp, "weather_code")),
    ]
    european_aqi = _maximum(
        _row_value(air_rows, start_timestamp, "european_aqi"),
        _row_value(air_rows, end_timestamp, "european_aqi"),
    )

    # La borne thermique la plus sévère dépend de la courbe de tolérance de l’activité.
    scores = {}
    for sport in SPORTS:
        apparent_temperature = select_apparent_temperature(
            temperature_start, temperature_end, sport
        )
        score_conditions = Conditions(
            apparent_temperature=apparent_temperature,
            precipitation=precipitation,
            wind_speed=wind_speed,
            wind_gusts=wind_gusts,
            european_aqi=european_aqi,
            weather_codes=weather_codes,
        )
        scores[sport] = calculate_score(score_conditions, sport)

    # Le résumé commun utilise la course tranquille ; chaque facteur sportif reste exact.
    default_temperature = select_apparent_temperature(
        temperature_start, temperature_end, "easy_run"
    )
    first_weather_code = next((code for code in weather_codes if code is not None), None)
    conditions = SlotConditions(
        apparent_temperature=default_temperature,
        apparent_temperature_start=temperature_start,
        apparent_temperature_end=temperature_end,
        precipitation=precipitation,
        wind_speed=wind_speed,
        wind_gusts=wind_gusts,
        european_aqi=european_aqi,
        weather_code=first_weather_code,
        weather_codes=weather_codes,
    )

    return HourlyForecast(
        time=start.astimezone(timezone).isoformat(timespec="minutes"),
        end_time=end.astimezone(timezone).isoformat(timespec="minutes"),
        conditions=conditions,
        pollen=_build_pollen(air_rows, start_timestamp, end_timestamp),
        scores=scores,
    )


def _has_relevant_data(
    weather_rows: dict[int, dict[str, object]],
    air_rows: dict[int, dict[str, object]],
    timestamps: set[int],
) -> bool:
    """Distingue une réponse partielle d’une absence totale de champs dans la fenêtre.

    Ce contrôle ne garantit pas encore des mesures valides : leur nettoyage et
    l’incomplétude des notes sont traités à la construction de chaque créneau.
    """
    for rows in (weather_rows, air_rows):
        for timestamp in timestamps:
            row = rows.get(timestamp)
            if row is not None and any(value is not None for value in row.values()):
                return True
    return False


def _assemble_forecast(
    weather: dict[str, Any], air: dict[str, Any], retrieved_at: datetime
) -> ForecastResponse:
    """Joint météo et air par instant réel et garde les créneaux partiellement connus."""
    timezone_name, timezone = _timezone(weather)
    weather_rows = _hourly_rows(weather, WEATHER_FIELDS)
    air_rows = _hourly_rows(air, AIR_FIELDS)
    first_start = _next_full_hour(retrieved_at, timezone)
    timestamps = {
        int((first_start + timedelta(hours=offset)).timestamp())
        for offset in range(SLOT_COUNT + 1)
    }
    if not _has_relevant_data(weather_rows, air_rows, timestamps):
        raise OpenMeteoError("Prévisions horaires absentes ou mal horodatées")

    hours = [
        _build_slot(first_start + timedelta(hours=offset), timezone, weather_rows, air_rows)
        for offset in range(SLOT_COUNT)
    ]
    return ForecastResponse(
        latitude=_required_coordinate(weather, "latitude"),
        longitude=_required_coordinate(weather, "longitude"),
        timezone=timezone_name,
        generated_at=retrieved_at.astimezone(timezone).isoformat(timespec="seconds"),
        hours=hours,
    )


async def _get_forecast(
    client: httpx.AsyncClient, latitude: float, longitude: float
) -> ForecastResponse:
    """Charge simultanément météo et air avec le même horizon et des temps Unix.

    Une erreur HTTP d’un des appels fait échouer cette récupération complète.
    Des champs manquants dans des réponses valides sont, eux, traités par créneau.
    """
    common_params: dict[str, Any] = {
        "latitude": latitude,
        "longitude": longitude,
        "timezone": "auto",
        "timeformat": "unixtime",
        "forecast_hours": FORECAST_HOURS,
    }
    weather_params = {**common_params, "hourly": ",".join(WEATHER_FIELDS)}
    air_params = {**common_params, "hourly": ",".join(AIR_FIELDS)}

    # L’attente des deux API est concurrente ; aucune donnée ne dépend de l’autre appel.
    weather, air = await asyncio.gather(
        _get_json(client, FORECAST_URL, weather_params),
        _get_json(client, AIR_QUALITY_URL, air_params),
    )

    return _assemble_forecast(weather, air, datetime.now(UTC))


async def get_forecast(
    latitude: float,
    longitude: float,
    *,
    client: httpx.AsyncClient | None = None,
) -> ForecastResponse:
    """Offre le chargement avec client partagé ou possédé, sans réserver le quota ici."""
    if client is not None:
        return await _get_forecast(client, latitude, longitude)
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "MouvAir/0.3"}) as owned:
        return await _get_forecast(owned, latitude, longitude)


class OpenMeteoGateway:
    """Réserve le budget partagé avant chaque opération auprès du fournisseur.

    Les services reçoivent ses méthodes comme fetchers. Cette frontière évite
    qu’un cache hit dépense du budget et transforme les refus fournisseur en
    délais partagés entre toutes les instances utilisant le même quota.
    """

    def __init__(self, quota: QuotaGuard, client: httpx.AsyncClient) -> None:
        """Associe le protecteur de quota et le pool HTTP préparés au démarrage."""
        self._quota = quota
        self._client = client

    async def search_locations(self, query: str, count: int = 5) -> list[Location]:
        """Réserve une unité avant le géocodage et partage un éventuel 429 fournisseur."""
        await self._reserve(1)
        try:
            return await search_locations(query, count, client=self._client)
        except OpenMeteoProviderRateLimit as exc:
            await self._quota.block_for(exc.retry_after)
            raise OpenMeteoQuotaError(
                "La recherche de villes est temporairement limitée",
                exc.retry_after,
            ) from exc

    async def get_forecast(self, latitude: float, longitude: float) -> ForecastResponse:
        """Réserve ensemble les deux appels, puis traduit une limitation fournisseur."""
        # Les deux unités sont réservées ensemble pour ne jamais lancer une prévision partielle.
        await self._reserve(2)
        try:
            return await get_forecast(latitude, longitude, client=self._client)
        except OpenMeteoProviderRateLimit as exc:
            await self._quota.block_for(exc.retry_after)
            raise OpenMeteoQuotaError(
                "Les prévisions sont temporairement limitées",
                exc.retry_after,
            ) from exc

    async def _reserve(self, cost: int) -> None:
        """Convertit les refus de budget et les pannes de protection en erreurs temporaires."""
        try:
            await self._quota.reserve(cost)
        except RateLimitExceeded as exc:
            raise OpenMeteoQuotaError(
                "Le budget Open-Meteo est temporairement atteint",
                exc.retry_after,
            ) from exc
        except RateLimitBackendUnavailable as exc:
            raise OpenMeteoQuotaError(
                "La protection du quota Open-Meteo est temporairement indisponible",
                5,
            ) from exc
