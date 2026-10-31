"""Vérifie l’identité et la sérialisation du véritable adaptateur de cache L2.

Les coordonnées demandées doivent rester la clé même si le fournisseur retourne
une autre position. Le stockage mémoire conserve les vraies clés et permet aussi
de vérifier l’isolation des anciennes entrées et des anciens verrous v1.
"""

import asyncio

import pytest

from mouvair_api.config import Settings
from mouvair_api.forecast_cache import RedisForecastCache
from mouvair_api.models import ForecastResponse
from mouvair_api.redis_store import RedisStore


@pytest.fixture
def provider_forecast() -> ForecastResponse:
    """Fournit un contenu à sérialiser ; les créneaux sont testés au niveau du service."""
    return ForecastResponse(
        latitude=45.76,
        longitude=4.86,
        timezone="Europe/Paris",
        generated_at="2026-09-08T14:05:00+02:00",
        hours=[],
    )


def test_cache_uses_requested_position_and_preserves_provider_metadata(
    memory_redis_store: RedisStore,
    provider_forecast: ForecastResponse,
) -> None:
    """La prévision de B se retrouve avec la demande A, sans modifier son contenu."""
    cache = RedisForecastCache(memory_redis_store, Settings())

    async def scenario() -> None:
        await cache.set(latitude=45.75, longitude=4.85, forecast=provider_forecast)

        cached = await cache.get(45.75, 4.85)
        assert cached == provider_forecast
        assert cached is not None
        assert (cached.latitude, cached.longitude) == (45.76, 4.86)
        assert cached.generated_at == "2026-09-08T14:05:00+02:00"
        assert (provider_forecast.latitude, provider_forecast.longitude) == (45.76, 4.86)
        assert await cache.get(45.76, 4.86) is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("other_latitude", "other_longitude"),
    [(45.7500001, 4.85), (45.75, 4.8500001)],
)
def test_distinct_requests_keep_separate_entries_for_the_same_provider_position(
    memory_redis_store: RedisStore,
    provider_forecast: ForecastResponse,
    other_latitude: float,
    other_longitude: float,
) -> None:
    """Deux positions même très proches restent distinctes, sans arrondi de leur identité."""
    cache = RedisForecastCache(memory_redis_store, Settings())
    other_forecast = provider_forecast.model_copy(
        update={"generated_at": "2026-09-08T14:06:00+02:00"}
    )

    async def scenario() -> None:
        await cache.set(45.75, 4.85, provider_forecast)
        await cache.set(other_latitude, other_longitude, other_forecast)

        assert await cache.get(45.75, 4.85) == provider_forecast
        assert await cache.get(other_latitude, other_longitude) == other_forecast

    asyncio.run(scenario())


def test_v2_preserves_and_ignores_legacy_cache_entries_and_locks(
    memory_redis_store: RedisStore,
    provider_forecast: ForecastResponse,
) -> None:
    """Les données et verrous v1 ne bloquent pas le nouveau cache et restent intacts."""
    settings = Settings()
    cache = RedisForecastCache(memory_redis_store, settings)
    digest = cache._digest(45.75, 4.85)
    legacy_cache_key = memory_redis_store.key("cache", "forecast", "v1", digest)
    legacy_lock_key = memory_redis_store.key("lock", "forecast", "v1", digest)
    current_cache_key = memory_redis_store.key("cache", "forecast", "v2", digest)
    current_lock_key = memory_redis_store.key("lock", "forecast", "v2", digest)
    raw_forecast = provider_forecast.model_dump_json()

    async def scenario() -> None:
        await memory_redis_store.set(legacy_cache_key, raw_forecast, 3_600)
        legacy_token = await memory_redis_store.acquire_lock(legacy_lock_key, 20)
        assert legacy_token is not None
        assert await cache.get(45.75, 4.85) is None

        token = await cache.acquire_lock(45.75, 4.85)
        assert token is not None
        assert await memory_redis_store.acquire_lock(current_lock_key, 20) is None
        await cache.set(45.75, 4.85, provider_forecast)
        assert await memory_redis_store.get(current_cache_key) == raw_forecast
        assert await memory_redis_store.get(legacy_cache_key) == raw_forecast

        await cache.release_lock(45.75, 4.85, token)
        assert await memory_redis_store.acquire_lock(legacy_lock_key, 20) is None
        await memory_redis_store.release_lock(legacy_lock_key, legacy_token)

    asyncio.run(scenario())


@pytest.mark.parametrize("raw_value", ["{invalid-json", "{}", '["invalid-model"]'])
def test_corrupt_forecast_is_deleted_without_removing_another_position(
    memory_redis_store: RedisStore,
    provider_forecast: ForecastResponse,
    raw_value: str,
) -> None:
    """Un JSON illisible ou incompatible devient un cache absent, sans toucher aux voisins."""
    cache = RedisForecastCache(memory_redis_store, Settings())
    corrupt_key = cache._cache_key(45.75, 4.85)

    async def scenario() -> None:
        await memory_redis_store.set(corrupt_key, raw_value, 60)
        await cache.set(45.76, 4.86, provider_forecast)

        assert await cache.get(45.75, 4.85) is None
        assert await memory_redis_store.get(corrupt_key) is None
        assert await cache.get(45.76, 4.86) == provider_forecast

    asyncio.run(scenario())


@pytest.mark.parametrize("with_previous_forecast", [False, True])
def test_wait_returns_none_when_no_new_generation_arrives_before_the_deadline(
    memory_redis_store: RedisStore,
    provider_forecast: ForecastResponse,
    with_previous_forecast: bool,
) -> None:
    """L’attente reste bornée si le cache est vide ou si sa seule entrée est déjà connue."""
    cache = RedisForecastCache(memory_redis_store, Settings(forecast_lock_wait_seconds=0.05))

    async def scenario() -> None:
        if with_previous_forecast:
            await cache.set(45.75, 4.85, provider_forecast)
        async with asyncio.timeout(1):
            assert await cache.wait(
                45.75, 4.85, previous_generated_at=provider_forecast.generated_at,
            ) is None
        if with_previous_forecast:
            # L’ancienne génération reste conservée pour le repli du service.
            assert await cache.get(45.75, 4.85) == provider_forecast

    asyncio.run(scenario())
