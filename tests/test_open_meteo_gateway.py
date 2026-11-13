"""Teste la frontière entre budget partagé, appels fournisseur et erreurs métier.

Le quota simulé enregistre les coûts et les attentes. monkeypatch remplace les
chargements de données ; MockTransport simule un vrai échange httpx pour vérifier
la lecture de Retry-After sans contacter un service externe.
"""

import asyncio
import logging
from datetime import UTC, datetime
from unittest.mock import Mock

import httpx
import pytest

import mouvair_api.open_meteo as open_meteo
from mouvair_api.models import Location
from mouvair_api.open_meteo import (
    OpenMeteoGateway,
    OpenMeteoProviderRateLimit,
    OpenMeteoQuotaError,
)
from mouvair_api.rate_limit import RateLimitBackendUnavailable, RateLimitExceeded


class FakeQuota:
    """Double enregistrant les réservations et les périodes de blocage demandées."""
    def __init__(self, error: Exception | None = None) -> None:
        self.costs: list[int] = []
        self.blocked: list[int] = []
        self.error = error

    async def reserve(self, cost: int) -> None:
        """Mémorise le coût même si le scénario doit ensuite refuser cette réservation."""
        self.costs.append(cost)
        if self.error is not None:
            raise self.error

    async def block_for(self, seconds: int) -> None:
        """Capture l’attente qui devrait être propagée aux autres instances."""
        self.blocked.append(seconds)


def test_gateway_reserves_one_geocoding_call_and_two_forecast_calls(monkeypatch) -> None:
    """Les opérations métier réservent respectivement un et deux appels avant leur exécution."""
    quota = FakeQuota()

    async def fake_locations(query: str, count: int, *, client) -> list[Location]:
        del query, count, client
        return []

    async def fake_forecast(latitude: float, longitude: float, *, client):
        del latitude, longitude, client
        return object()

    monkeypatch.setattr(open_meteo, "search_locations", fake_locations)
    monkeypatch.setattr(open_meteo, "get_forecast", fake_forecast)

    async def scenario() -> None:
        async with httpx.AsyncClient() as client:
            gateway = OpenMeteoGateway(quota, client)
            await gateway.search_locations("Lyon")
            await gateway.get_forecast(45.75, 4.85)

    asyncio.run(scenario())
    assert quota.costs == [1, 2]


@pytest.mark.parametrize(
    "error",
    [RateLimitExceeded(17), RateLimitBackendUnavailable("indisponible")],
)
def test_gateway_converts_quota_failures_without_calling_the_provider(
    monkeypatch, error: Exception, caplog: pytest.LogCaptureFixture,
) -> None:
    """Un refus ou une panne de quota doit remonter avant toute tentative d’appel fournisseur."""
    caplog.set_level(logging.INFO, logger="mouvair_api")
    called = False

    async def fake_locations(query: str, count: int, *, client) -> list[Location]:
        nonlocal called
        del query, count, client
        called = True
        return []

    monkeypatch.setattr(open_meteo, "search_locations", fake_locations)

    async def scenario() -> OpenMeteoQuotaError:
        async with httpx.AsyncClient() as client:
            gateway = OpenMeteoGateway(FakeQuota(error), client)
            with pytest.raises(OpenMeteoQuotaError) as caught:
                await gateway.search_locations("Lyon")
            return caught.value

    caught = asyncio.run(scenario())
    assert caught.retry_after == (17 if isinstance(error, RateLimitExceeded) else 5)
    assert called is False
    recorded = [
        record.diagnostic_fields for record in caplog.records
        if record.name == "mouvair_api" and record.getMessage() == "provider.quota_rejected"
    ]
    assert len(recorded) == 1
    assert recorded[0]["retry_after"] == caught.retry_after
    assert recorded[0]["cost"] == 1
    assert recorded[0]["reason"] == (
        "quota_or_cooldown" if isinstance(error, RateLimitExceeded) else "redis_unavailable"
    )


def test_provider_429_creates_a_shared_cooldown(monkeypatch, caplog) -> None:
    """Une limitation fournisseur de 23 secondes devient une attente partagée et une erreur 503."""
    caplog.set_level(logging.INFO, logger="mouvair_api")
    quota = FakeQuota()

    async def fake_locations(query: str, count: int, *, client) -> list[Location]:
        del query, count, client
        raise OpenMeteoProviderRateLimit(23)

    monkeypatch.setattr(open_meteo, "search_locations", fake_locations)

    async def scenario() -> None:
        async with httpx.AsyncClient() as client:
            gateway = OpenMeteoGateway(quota, client)
            with pytest.raises(OpenMeteoQuotaError) as caught:
                await gateway.search_locations("Lyon")
            assert caught.value.retry_after == 23

    asyncio.run(scenario())
    assert quota.blocked == [23]
    recorded = [
        record.diagnostic_fields for record in caplog.records
        if record.name == "mouvair_api" and record.getMessage() == "provider.rate_limited"
    ]
    assert len(recorded) == 1
    assert recorded[0]["operation"] == "locations"
    assert recorded[0]["retry_after"] == 23


@pytest.mark.parametrize(
    ("headers", "expected_retry_after"),
    [
        pytest.param({"Retry-After": "37"}, 37, id="seconds"),
        pytest.param({}, 60, id="missing-header"),
        pytest.param({"Retry-After": "invalid"}, 60, id="invalid-header"),
        pytest.param(
            {"Retry-After": "Tue, 08 Sep 2026 12:05:16 GMT"}, 16, id="future-http-date"
        ),
        pytest.param(
            {"Retry-After": "Tue, 08 Sep 2026 12:04:59 GMT"}, 1, id="past-http-date"
        ),
    ],
)
def test_http_429_uses_retry_after_or_default(
    monkeypatch: pytest.MonkeyPatch,
    headers: dict[str, str],
    expected_retry_after: int,
) -> None:
    """Vérifie le délai transmis par l’exception pour une réponse HTTP 429.

    Un en-tête absent ou illisible donne 60 secondes. Une date passée donne
    le délai minimal d’une seconde.
    """
    # Fige l’horloge à la demi-seconde pour vérifier que 15,5 s sont arrondies à 16 s.
    fixed_datetime = Mock()
    fixed_datetime.now.return_value = datetime(2026, 9, 8, 12, 5, 0, 500_000, tzinfo=UTC)
    monkeypatch.setattr(open_meteo, "datetime", fixed_datetime)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers=headers, request=request)

    async def scenario() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(OpenMeteoProviderRateLimit) as caught:
                await open_meteo._get_json(client, "https://example.test", {})
            assert caught.value.retry_after == expected_retry_after

    asyncio.run(scenario())
