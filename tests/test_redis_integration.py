"""Vérifie les garanties serveur contre une instance Redis réelle.

TEST_REDIS_URL active cette suite et doit pointer vers une base de test dédiée,
par convention la base 15 locale. Les clés utilisent des identifiants uniques et
sont supprimées dans finally ; aucune purge globale de la base n’est effectuée.
Les tests unitaires ne suffisent pas à démontrer l’atomicité des scripts Lua.
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from forecast_fixtures import MutableClock
from forecast_payloads import air_payload, weather_payload
from mouvair_api.config import Settings
from mouvair_api.forecast_cache import RedisForecastCache
from mouvair_api.forecast_service import ForecastService
from mouvair_api.forecast_slots import assemble_forecast
from mouvair_api.main import create_app
from mouvair_api.models import ForecastResponse
from mouvair_api.rate_limit import (
    OpenMeteoQuota,
    RateLimitExceeded,
    RateWindow,
    RedisSlidingWindowLimiter,
)
from mouvair_api.redis_store import RedisStore

# Une absence de variable ignore la suite au lieu de toucher à une base non choisie.
TEST_REDIS_URL = os.getenv("TEST_REDIS_URL")

pytestmark = [
    pytest.mark.redis_integration,
    pytest.mark.skipif(TEST_REDIS_URL is None, reason="TEST_REDIS_URL n’est pas configurée"),
]


def test_real_redis_never_overspends_a_concurrent_window() -> None:
    """Vingt réservations concurrentes doivent produire exactement cinq autorisations."""
    async def scenario() -> None:
        settings = Settings(redis_url=TEST_REDIS_URL, client_id_hmac_secret="test-secret")
        store = RedisStore(settings)
        assert await store.connect()
        key = store.key("tests", uuid4().hex)
        limiter = RedisSlidingWindowLimiter(store)
        try:
            decisions = await asyncio.gather(
                *(
                    limiter.reserve(key, (RateWindow(60, 5),), reservation_id=str(index))
                    for index in range(20)
                )
            )
            assert sum(decision.allowed for decision in decisions) == 5
        finally:
            await store.delete(key)
            await store.close()

    asyncio.run(scenario())


def test_real_redis_rejects_a_two_unit_reservation_without_partial_debit() -> None:
    """Avec une seule unité libre, refuser un coût de deux ne doit rien débiter."""
    async def scenario() -> None:
        settings = Settings(redis_url=TEST_REDIS_URL, client_id_hmac_secret="test-secret")
        store = RedisStore(settings)
        assert await store.connect()
        key = store.key("tests", uuid4().hex)
        limiter = RedisSlidingWindowLimiter(store)
        window = (RateWindow(60, 5),)
        try:
            assert (await limiter.reserve(key, window, cost=4)).allowed
            assert not (await limiter.reserve(key, window, cost=2)).allowed
            assert (await limiter.reserve(key, window, cost=1)).allowed
        finally:
            await store.delete(key)
            await store.close()

    asyncio.run(scenario())


def test_real_redis_lock_can_only_be_released_by_its_owner() -> None:
    """Un mauvais jeton ne supprime pas le verrou ; le bon jeton permet sa reprise."""
    async def scenario() -> None:
        settings = Settings(redis_url=TEST_REDIS_URL, client_id_hmac_secret="test-secret")
        store = RedisStore(settings)
        assert await store.connect()
        key = store.key("tests", uuid4().hex)
        try:
            token = await store.acquire_lock(key, 10)
            assert token is not None
            assert await store.acquire_lock(key, 10) is None
            await store.release_lock(key, "wrong-token")
            assert await store.acquire_lock(key, 10) is None
            await store.release_lock(key, token)
            assert await store.acquire_lock(key, 10) is not None
        finally:
            await store.delete(key)
            await store.close()

    asyncio.run(scenario())


def test_real_redis_shares_a_provider_cooldown_between_instances() -> None:
    """L’attente écrite par une connexion empêche une autre connexion de réserver."""
    async def scenario() -> None:
        scope = f"test-{uuid4().hex}"
        settings = Settings(
            redis_url=TEST_REDIS_URL,
            client_id_hmac_secret="test-secret",
            open_meteo_quota_scope=scope,
        )
        first_store = RedisStore(settings)
        second_store = RedisStore(settings)
        assert await first_store.connect()
        assert await second_store.connect()
        first = OpenMeteoQuota(first_store, settings)
        second = OpenMeteoQuota(second_store, settings)
        cooldown_key = first_store.key("quota", f"{{open-meteo-{scope}}}", "cooldown")
        events_key = first_store.key("quota", f"{{open-meteo-{scope}}}", "events")
        try:
            await first.block_for(10)
            with pytest.raises(RateLimitExceeded) as caught:
                await second.reserve(1)
            assert caught.value.retry_after > 0
        finally:
            await first_store.delete(cooldown_key)
            await first_store.delete(events_key)
            await first_store.close()
            await second_store.close()

    asyncio.run(scenario())


def test_real_forecast_cache_shares_requested_position_between_connections() -> None:
    """Deux caches L2 retrouvent A dans Redis même si la réponse décrit la position B."""
    async def scenario() -> None:
        settings = Settings(
            redis_url=TEST_REDIS_URL,
            redis_key_prefix=f"test-{uuid4().hex}",
            client_id_hmac_secret="test-secret",
        )
        first_store = RedisStore(settings)
        second_store = RedisStore(settings)
        first = RedisForecastCache(first_store, settings)
        second = RedisForecastCache(second_store, settings)
        forecast = ForecastResponse(
            latitude=45.76,
            longitude=4.86,
            timezone="Europe/Paris",
            generated_at="2026-09-08T14:05:00+02:00",
            hours=[],
        )
        cache_key = first._cache_key(45.75, 4.85)
        try:
            assert await first_store.connect()
            assert await second_store.connect()
            await first.set(45.75, 4.85, forecast)

            cached = await second.get(45.75, 4.85)
            assert cached == forecast
            assert cached is not None
            assert (cached.latitude, cached.longitude) == (45.76, 4.86)
            assert await second.get(45.76, 4.86) is None
            assert 0 < await second_store.pttl(cache_key) <= (
                settings.forecast_shared_cache_ttl_seconds * 1_000
            )
        finally:
            await first_store.delete(cache_key)
            await first_store.close()
            await second_store.close()

    asyncio.run(scenario())


def test_health_reports_a_real_redis_connection() -> None:
    """Le diagnostic HTTP est positif quand le cycle de vie joint effectivement Redis."""
    settings = Settings(
        redis_url=TEST_REDIS_URL,
        redis_key_prefix=f"test-{uuid4().hex}",
        client_id_hmac_secret="test-secret",
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "redis": "ok"}


def test_real_forecast_cache_expires_after_its_configured_ttl() -> None:
    """Le serveur applique réellement le TTL, sans simuler le temps dans un double mémoire."""
    async def scenario() -> None:
        settings = Settings(
            redis_url=TEST_REDIS_URL,
            redis_key_prefix=f"test-{uuid4().hex}",
            forecast_shared_cache_ttl_seconds=1,
        )
        store = RedisStore(settings)
        cache = RedisForecastCache(store, settings)
        moment = datetime(2026, 9, 8, 12, 5, tzinfo=UTC)
        start = moment.replace(minute=0)
        forecast = assemble_forecast(weather_payload(start), air_payload(start), moment)
        key = cache._cache_key(45.75, 4.85)
        try:
            assert await store.connect()
            await cache.set(45.75, 4.85, forecast)
            assert await cache.get(45.75, 4.85) == forecast
            assert 0 < await store.pttl(key) <= 1_000
            async with asyncio.timeout(3):
                while await cache.get(45.75, 4.85) is not None:
                    await asyncio.sleep(0.05)
            assert await store.pttl(key) == -2
        finally:
            await store.delete(key)
            await store.close()

    asyncio.run(scenario())


def test_real_forecast_service_rebuilds_a_corrupt_cache_entry() -> None:
    """Une entrée Redis invalide est remplacée par une prévision calculée et réutilisable."""
    async def scenario() -> None:
        settings = Settings(redis_url=TEST_REDIS_URL, redis_key_prefix=f"test-{uuid4().hex}")
        store = RedisStore(settings)
        cache = RedisForecastCache(store, settings)
        moment = datetime(2026, 9, 8, 12, 5, tzinfo=UTC)
        start = moment.replace(minute=0)
        calls = 0

        async def fetcher(latitude: float, longitude: float) -> ForecastResponse:
            nonlocal calls
            calls += 1
            return assemble_forecast(weather_payload(start), air_payload(start), moment)

        service = ForecastService(fetcher, clock=MutableClock(moment), shared_cache=cache)
        cache_key = cache._cache_key(45.75, 4.85)
        lock_key = cache._lock_key(45.75, 4.85)
        try:
            assert await store.connect()
            await store.set(cache_key, '{"hours": []}', 60)
            direct = await service.get_forecast(45.75, 4.85)
            # Une nouvelle instance ne doit pas dépendre du L1 de la première.
            other_service = ForecastService(fetcher, clock=MutableClock(moment), shared_cache=cache)
            cached = await other_service.get_forecast(45.75, 4.85)
            assert calls == 1
            assert len(direct.hours) == len(cached.hours) == 24
            assert direct.from_cache is False
            assert cached.from_cache is True
            assert cached.generated_at == direct.generated_at
            assert await cache.get(45.75, 4.85) == direct
        finally:
            await store.delete(cache_key)
            await store.delete(lock_key)
            await store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("with_previous_forecast", [False, True])
def test_real_forecast_refresh_is_shared_by_two_concurrent_services(
    with_previous_forecast: bool,
) -> None:
    """Deux connexions partagent la récupération, même si un ancien résultat occupe le L2."""
    async def scenario() -> None:
        settings = Settings(
            redis_url=TEST_REDIS_URL,
            redis_key_prefix=f"test-{uuid4().hex}",
            forecast_lock_wait_seconds=2,
        )
        first_store = RedisStore(settings)
        second_store = RedisStore(settings)
        provider_started = asyncio.Event()
        release_provider = asyncio.Event()
        waiter_polled = asyncio.Event()

        class ObservedCache(RedisForecastCache):
            """Signale une lecture réelle pendant l’attente, sans modifier sa réponse."""

            waiting = False

            async def wait(
                self,
                latitude: float,
                longitude: float,
                *,
                previous_generated_at: str | None = None,
            ) -> ForecastResponse | None:
                self.waiting = True
                return await super().wait(
                    latitude, longitude, previous_generated_at=previous_generated_at,
                )

            async def get(self, latitude: float, longitude: float) -> ForecastResponse | None:
                result = await super().get(latitude, longitude)
                if self.waiting:
                    waiter_polled.set()
                return result

        first_cache = RedisForecastCache(first_store, settings)
        second_cache = ObservedCache(second_store, settings)
        moment = datetime(2026, 9, 8, 12, 25, tzinfo=UTC)
        start = moment.replace(minute=0)
        expected = assemble_forecast(weather_payload(start), air_payload(start), moment)
        calls = 0

        async def fetcher(latitude: float, longitude: float) -> ForecastResponse:
            """Maintient le fournisseur ouvert jusqu’à la lecture Redis de l’autre instance."""
            nonlocal calls
            calls += 1
            provider_started.set()
            await release_provider.wait()
            return expected

        first = ForecastService(fetcher, clock=MutableClock(moment), shared_cache=first_cache)
        second = ForecastService(fetcher, clock=MutableClock(moment), shared_cache=second_cache)
        cache_key = first_cache._cache_key(45.75, 4.85)
        lock_key = first_cache._lock_key(45.75, 4.85)
        tasks: list[asyncio.Task[ForecastResponse]] = []
        try:
            assert await first_store.connect()
            assert await second_store.connect()
            if with_previous_forecast:
                previous = expected.model_copy(
                    update={"generated_at": (moment - timedelta(minutes=16)).isoformat()}
                )
                await first_cache.set(45.75, 4.85, previous)

            async with asyncio.timeout(5):
                tasks.append(asyncio.create_task(first.get_forecast(45.75, 4.85)))
                await provider_started.wait()
                tasks.append(asyncio.create_task(second.get_forecast(45.75, 4.85)))
                await waiter_polled.wait()
                release_provider.set()
                direct, cached = await asyncio.gather(*tasks)

            assert calls == 1
            assert direct.data_state == cached.data_state == "fresh"
            assert direct.from_cache is False
            assert cached.from_cache is True
            assert cached.generated_at == direct.generated_at == expected.generated_at
            assert len(direct.hours) == len(cached.hours) == 24
            assert await first_store.get(lock_key) is None
        finally:
            release_provider.set()
            # Laisser finir le travail protégé par shield avant de fermer les connexions.
            await asyncio.gather(*tasks, return_exceptions=True)
            pending = list(first._inflight.values()) + list(second._inflight.values())
            await asyncio.gather(*pending, return_exceptions=True)
            await first_store.delete(cache_key)
            await first_store.delete(lock_key)
            await first_store.close()
            await second_store.close()

    asyncio.run(scenario())
