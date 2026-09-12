import asyncio

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
    def __init__(self, error: Exception | None = None) -> None:
        self.costs: list[int] = []
        self.blocked: list[int] = []
        self.error = error

    async def reserve(self, cost: int) -> None:
        self.costs.append(cost)
        if self.error is not None:
            raise self.error

    async def block_for(self, seconds: int) -> None:
        self.blocked.append(seconds)


def test_gateway_reserves_one_geocoding_call_and_two_forecast_calls(monkeypatch) -> None:
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
    monkeypatch, error: Exception
) -> None:
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


def test_provider_429_creates_a_shared_cooldown(monkeypatch) -> None:
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


def test_http_429_retry_after_is_preserved() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "37"}, request=request)

    async def scenario() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(OpenMeteoProviderRateLimit) as caught:
                await open_meteo._get_json(client, "https://example.test", {})
            assert caught.value.retry_after == 37

    asyncio.run(scenario())
