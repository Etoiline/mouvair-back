import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass
from math import ceil
from time import monotonic, time
from uuid import uuid4

from .config import Settings
from .redis_store import RedisStore, RedisUnavailable

MONTH_WINDOW_SECONDS = 31 * 24 * 60 * 60

SLIDING_WINDOW_SCRIPT = """
local key = KEYS[1]
local cost = tonumber(ARGV[1])
local reservation = ARGV[2]
local window_count = tonumber(ARGV[3])
local redis_time = redis.call('TIME')
local now = tonumber(redis_time[1]) + tonumber(redis_time[2]) / 1000000
local longest = 0

if redis.call('ZSCORE', key, reservation .. ':1') then
    return {1, 0}
end

for index = 1, window_count do
    local duration = tonumber(ARGV[4 + (index - 1) * 2])
    if duration > longest then
        longest = duration
    end
end

redis.call('ZREMRANGEBYSCORE', key, '-inf', now - longest)

local allowed = 1
local retry_after = 0
for index = 1, window_count do
    local duration = tonumber(ARGV[4 + (index - 1) * 2])
    local limit = tonumber(ARGV[5 + (index - 1) * 2])
    local minimum = '(' .. tostring(now - duration)
    local count = redis.call('ZCOUNT', key, minimum, '+inf')
    if count + cost > limit then
        allowed = 0
        local excess = count + cost - limit
        local entries = redis.call(
            'ZRANGEBYSCORE', key, minimum, '+inf', 'WITHSCORES', 'LIMIT', 0, excess
        )
        local wait = duration
        if #entries > 0 then
            wait = tonumber(entries[#entries]) + duration - now
        end
        if wait > retry_after then
            retry_after = wait
        end
    end
end

if allowed == 0 then
    return {0, math.max(1, math.ceil(retry_after))}
end

for index = 1, cost do
    redis.call('ZADD', key, now, reservation .. ':' .. tostring(index))
end
redis.call('EXPIRE', key, math.ceil(longest) + 60)
return {1, 0}
"""


@dataclass(frozen=True)
class RateWindow:
    seconds: int
    limit: int


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    retry_after: int = 0


class RateLimitExceeded(RuntimeError):
    def __init__(self, retry_after: int) -> None:
        super().__init__("Limite de requêtes atteinte")
        self.retry_after = max(1, retry_after)


class RateLimitBackendUnavailable(RuntimeError):
    pass


class RedisSlidingWindowLimiter:
    def __init__(self, store: RedisStore) -> None:
        self._store = store

    async def reserve(
        self,
        key: str,
        windows: tuple[RateWindow, ...],
        *,
        cost: int = 1,
        reservation_id: str | None = None,
    ) -> RateDecision:
        _validate_request(windows, cost)
        arguments: list[object] = [cost, reservation_id or uuid4().hex, len(windows)]
        for window in windows:
            arguments.extend((window.seconds, window.limit))
        result = await self._store.eval(SLIDING_WINDOW_SCRIPT, [key], arguments)
        return RateDecision(allowed=bool(int(result[0])), retry_after=int(result[1]))


class LocalSlidingWindowLimiter:
    """Repli explicite, limité au processus et donc non adapté au multi-instance."""

    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._reservations: dict[tuple[str, str], float] = {}
        self._lock = asyncio.Lock()

    async def reserve(
        self,
        key: str,
        windows: tuple[RateWindow, ...],
        *,
        cost: int = 1,
        reservation_id: str | None = None,
    ) -> RateDecision:
        _validate_request(windows, cost)
        now = time()
        reservation = reservation_id or uuid4().hex
        longest = max(window.seconds for window in windows)

        async with self._lock:
            known_until = self._reservations.get((key, reservation))
            if known_until is not None and known_until > now:
                return RateDecision(allowed=True)

            events = self._events[key]
            while events and events[0] <= now - longest:
                events.popleft()

            retry_after = 0
            for window in windows:
                relevant = [event for event in events if event > now - window.seconds]
                if len(relevant) + cost <= window.limit:
                    continue
                excess = len(relevant) + cost - window.limit
                retry_after = max(
                    retry_after,
                    ceil(relevant[excess - 1] + window.seconds - now),
                )

            if retry_after > 0:
                return RateDecision(allowed=False, retry_after=max(1, retry_after))

            events.extend(now for _ in range(cost))
            self._reservations[(key, reservation)] = now + longest
            expired = [item for item, expires_at in self._reservations.items() if expires_at <= now]
            for item in expired:
                del self._reservations[item]
            return RateDecision(allowed=True)


class SharedRateLimiter:
    def __init__(
        self,
        store: RedisStore,
        *,
        allow_local_fallback: bool,
        local: LocalSlidingWindowLimiter | None = None,
    ) -> None:
        self._redis = RedisSlidingWindowLimiter(store)
        self._local = local or LocalSlidingWindowLimiter()
        self._allow_local_fallback = allow_local_fallback

    async def reserve(
        self,
        key: str,
        windows: tuple[RateWindow, ...],
        *,
        cost: int = 1,
        reservation_id: str | None = None,
    ) -> RateDecision:
        reservation = reservation_id or uuid4().hex
        try:
            return await self._redis.reserve(
                key,
                windows,
                cost=cost,
                reservation_id=reservation,
            )
        except RedisUnavailable as exc:
            if not self._allow_local_fallback:
                raise RateLimitBackendUnavailable(
                    "Le quota partagé est temporairement indisponible"
                ) from exc
            return await self._local.reserve(
                key,
                windows,
                cost=cost,
                reservation_id=reservation,
            )


class OpenMeteoQuota:
    def __init__(self, store: RedisStore, settings: Settings) -> None:
        self._store = store
        self._limiter = SharedRateLimiter(
            store,
            allow_local_fallback=settings.redis_failure_mode == "local",
        )
        scope = settings.open_meteo_quota_scope
        self._events_key = store.key("quota", f"{{open-meteo-{scope}}}", "events")
        self._cooldown_key = store.key("quota", f"{{open-meteo-{scope}}}", "cooldown")
        self._windows = (
            RateWindow(60, settings.open_meteo_quota_minute),
            RateWindow(60 * 60, settings.open_meteo_quota_hour),
            RateWindow(24 * 60 * 60, settings.open_meteo_quota_day),
            RateWindow(MONTH_WINDOW_SECONDS, settings.open_meteo_quota_month),
        )
        self._allow_local_fallback = settings.redis_failure_mode == "local"
        self._local_cooldown_until = 0.0

    async def reserve(self, cost: int, reservation_id: str | None = None) -> None:
        retry_after = await self._cooldown_retry_after()
        if retry_after > 0:
            raise RateLimitExceeded(retry_after)
        decision = await self._limiter.reserve(
            self._events_key,
            self._windows,
            cost=cost,
            reservation_id=reservation_id,
        )
        if not decision.allowed:
            raise RateLimitExceeded(decision.retry_after)

    async def block_for(self, seconds: int) -> None:
        duration = max(1, seconds)
        self._local_cooldown_until = max(
            self._local_cooldown_until, monotonic() + duration
        )
        try:
            await self._store.extend_cooldown(self._cooldown_key, duration)
        except RedisUnavailable:
            if not self._allow_local_fallback:
                return

    async def _cooldown_retry_after(self) -> int:
        local_retry = ceil(self._local_cooldown_until - monotonic())
        try:
            milliseconds = await self._store.pttl(self._cooldown_key)
        except RedisUnavailable as exc:
            if self._allow_local_fallback:
                return max(0, local_retry)
            raise RateLimitBackendUnavailable(
                "Le quota partagé est temporairement indisponible"
            ) from exc
        return max(0, local_retry, ceil(milliseconds / 1_000))


class ClientRateLimiter:
    def __init__(self, store: RedisStore, settings: Settings) -> None:
        self._store = store
        self._limiter = SharedRateLimiter(store, allow_local_fallback=True)
        self._windows = {
            "locations": (
                RateWindow(60, settings.location_rate_limit_minute),
                RateWindow(60 * 60, settings.location_rate_limit_hour),
            ),
            "forecast": (
                RateWindow(60, settings.forecast_rate_limit_minute),
                RateWindow(60 * 60, settings.forecast_rate_limit_hour),
            ),
        }

    async def check(self, endpoint: str, client_id: str) -> None:
        decision = await self._limiter.reserve(
            self._store.key("client-limit", endpoint, client_id),
            self._windows[endpoint],
        )
        if not decision.allowed:
            raise RateLimitExceeded(decision.retry_after)


def _validate_request(windows: tuple[RateWindow, ...], cost: int) -> None:
    if not windows:
        raise ValueError("Au moins une fenêtre de limitation est requise")
    if cost < 1:
        raise ValueError("Le coût doit être strictement positif")
    for window in windows:
        if window.seconds < 1 or window.limit < cost:
            raise ValueError("Fenêtre ou limite invalide")
