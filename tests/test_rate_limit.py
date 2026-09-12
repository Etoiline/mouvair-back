import asyncio

import pytest

from mouvair_api.config import Settings
from mouvair_api.rate_limit import (
    ClientRateLimiter,
    LocalSlidingWindowLimiter,
    OpenMeteoQuota,
    RateLimitBackendUnavailable,
    RateLimitExceeded,
    RateWindow,
)
from mouvair_api.redis_store import RedisStore


def test_local_limiter_reserves_a_cost_atomically() -> None:
    limiter = LocalSlidingWindowLimiter()
    windows = (RateWindow(60, 3),)

    async def scenario() -> tuple[bool, bool, int]:
        first = await limiter.reserve("provider", windows, cost=2)
        second = await limiter.reserve("provider", windows, cost=2)
        third = await limiter.reserve("provider", windows, cost=1)
        return first.allowed, second.allowed, third.retry_after

    first_allowed, second_allowed, third_retry = asyncio.run(scenario())

    assert first_allowed is True
    assert second_allowed is False
    assert third_retry == 0


def test_local_limiter_reuses_an_idempotent_reservation() -> None:
    limiter = LocalSlidingWindowLimiter()
    windows = (RateWindow(60, 2),)

    async def scenario() -> tuple[bool, bool, bool]:
        first = await limiter.reserve("provider", windows, cost=2, reservation_id="same")
        repeated = await limiter.reserve("provider", windows, cost=2, reservation_id="same")
        other = await limiter.reserve("provider", windows, reservation_id="other")
        return first.allowed, repeated.allowed, other.allowed

    assert asyncio.run(scenario()) == (True, True, False)


def test_global_quota_fails_closed_without_redis() -> None:
    settings = Settings(redis_failure_mode="closed")
    quota = OpenMeteoQuota(RedisStore(settings), settings)

    with pytest.raises(RateLimitBackendUnavailable):
        asyncio.run(quota.reserve(1))


def test_explicit_local_mode_keeps_a_single_process_budget() -> None:
    settings = Settings(
        redis_failure_mode="local",
        open_meteo_quota_minute=2,
        open_meteo_quota_hour=2,
        open_meteo_quota_day=2,
        open_meteo_quota_month=2,
    )
    quota = OpenMeteoQuota(RedisStore(settings), settings)

    async def scenario() -> None:
        await quota.reserve(2)
        with pytest.raises(RateLimitExceeded):
            await quota.reserve(1)

    asyncio.run(scenario())


def test_client_limiter_uses_a_local_fallback_when_redis_is_unavailable() -> None:
    settings = Settings(
        location_rate_limit_minute=2,
        location_rate_limit_hour=2,
    )
    limiter = ClientRateLimiter(RedisStore(settings), settings)

    async def scenario() -> None:
        await limiter.check("locations", "client-a")
        await limiter.check("locations", "client-a")
        with pytest.raises(RateLimitExceeded) as caught:
            await limiter.check("locations", "client-a")
        assert caught.value.retry_after > 0
        await limiter.check("locations", "client-b")

    asyncio.run(scenario())
