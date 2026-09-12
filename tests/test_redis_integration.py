import asyncio
import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from mouvair_api.config import Settings
from mouvair_api.main import create_app
from mouvair_api.rate_limit import (
    OpenMeteoQuota,
    RateLimitExceeded,
    RateWindow,
    RedisSlidingWindowLimiter,
)
from mouvair_api.redis_store import RedisStore

TEST_REDIS_URL = os.getenv("TEST_REDIS_URL")

pytestmark = [
    pytest.mark.redis_integration,
    pytest.mark.skipif(TEST_REDIS_URL is None, reason="TEST_REDIS_URL n’est pas configurée"),
]


def test_real_redis_never_overspends_a_concurrent_window() -> None:
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


def test_health_reports_a_real_redis_connection() -> None:
    settings = Settings(
        redis_url=TEST_REDIS_URL,
        redis_key_prefix=f"test-{uuid4().hex}",
        client_id_hmac_secret="test-secret",
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "redis": "ok"}
