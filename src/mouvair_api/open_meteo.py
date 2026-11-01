"""Récupère les données Open-Meteo et protège leur budget d’appels.

Ce module gère les URL, paramètres HTTP, erreurs réseau, recherches de villes et
réservations de quota. Il transmet les réponses météo et air au module pur
forecast_slots pour construire les créneaux et leurs notes.
"""

import asyncio
import logging
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import ceil
from time import perf_counter
from typing import Any, Protocol

import httpx

from .diagnostics import error_fields, log_event
from .errors import DataUnavailableError
from .forecast_slots import AIR_FIELDS, WEATHER_FIELDS, assemble_forecast
from .models import ForecastResponse, Location
from .rate_limit import RateLimitBackendUnavailable, RateLimitExceeded

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
# On demande une marge de données pour former les deux bornes des 24 départs futurs.
FORECAST_HOURS = 27


class OpenMeteoError(DataUnavailableError):
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
    started = perf_counter()
    # Un nom fixe suffit au diagnostic ; l’URL encodée contiendrait les coordonnées ou la saisie.
    operation = {
        GEOCODING_URL: "locations", FORECAST_URL: "weather", AIR_QUALITY_URL: "air_quality",
    }.get(url, "unknown")
    try:
        if timeout is None:
            response = await client.get(url, params=params)
        else:
            response = await client.get(url, params=params, timeout=timeout)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        log_event(
            "provider.request_failed", level=logging.WARNING, operation=operation,
            reason="http_error", provider_status=exc.response.status_code,
            duration_ms=round((perf_counter() - started) * 1_000, 3), **error_fields(exc),
        )
        if exc.response.status_code == 429:
            raise OpenMeteoProviderRateLimit(_provider_retry_after(exc.response)) from exc
        raise OpenMeteoError("Open-Meteo est momentanément indisponible") from exc
    except httpx.HTTPError as exc:
        log_event(
            "provider.request_failed", level=logging.WARNING, operation=operation,
            reason="network_error", provider_status=None,
            duration_ms=round((perf_counter() - started) * 1_000, 3), **error_fields(exc),
        )
        raise OpenMeteoError("Open-Meteo est momentanément indisponible") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        log_event(
            "provider.request_failed", level=logging.WARNING, operation=operation,
            reason="invalid_json", provider_status=response.status_code,
            duration_ms=round((perf_counter() - started) * 1_000, 3), **error_fields(exc),
        )
        raise OpenMeteoError("Réponse Open-Meteo invalide") from exc

    if not isinstance(payload, dict):
        log_event(
            "provider.request_failed", level=logging.WARNING, operation=operation,
            reason="invalid_payload", provider_status=response.status_code,
            duration_ms=round((perf_counter() - started) * 1_000, 3),
        )
        raise OpenMeteoError("Réponse Open-Meteo invalide")
    log_event(
        "provider.request", level=logging.DEBUG, operation=operation,
        provider_status=response.status_code,
        duration_ms=round((perf_counter() - started) * 1_000, 3),
    )
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

    return assemble_forecast(weather, air, datetime.now(UTC))


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
            log_event(
                "provider.rate_limited", level=logging.WARNING,
                operation="locations", retry_after=exc.retry_after,
            )
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
            log_event(
                "provider.rate_limited", level=logging.WARNING,
                operation="forecast", retry_after=exc.retry_after,
            )
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
            log_event(
                "provider.quota_rejected", level=logging.WARNING,
                reason="quota_or_cooldown", cost=cost, retry_after=exc.retry_after,
            )
            raise OpenMeteoQuotaError(
                "Le budget Open-Meteo est temporairement atteint",
                exc.retry_after,
            ) from exc
        except RateLimitBackendUnavailable as exc:
            log_event(
                "provider.quota_rejected", level=logging.WARNING,
                reason="redis_unavailable", cost=cost, retry_after=5,
            )
            raise OpenMeteoQuotaError(
                "La protection du quota Open-Meteo est temporairement indisponible",
                5,
            ) from exc
