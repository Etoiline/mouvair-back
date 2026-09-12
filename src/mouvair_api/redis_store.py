import logging
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Any, TypeVar
from uuid import uuid4

import redis.asyncio as redis
from redis.exceptions import RedisError

from .config import Settings

logger = logging.getLogger(__name__)
Result = TypeVar("Result")

RELEASE_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

EXTEND_COOLDOWN_SCRIPT = """
local requested = tonumber(ARGV[1])
local current = redis.call('PTTL', KEYS[1])
if current < requested then
    redis.call('SET', KEYS[1], '1', 'PX', requested)
    return requested
end
return current
"""


class RedisUnavailable(RuntimeError):
    pass


class RedisStore:
    """Centralise l’accès Redis et coupe brièvement les tentatives après une panne."""

    def __init__(self, settings: Settings, client: redis.Redis | None = None) -> None:
        self._settings = settings
        self._client = client
        if self._client is None and settings.redis_url is not None:
            self._client = redis.Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_timeout=settings.redis_socket_timeout_seconds,
                socket_connect_timeout=settings.redis_connect_timeout_seconds,
                max_connections=settings.redis_max_connections,
                health_check_interval=30,
            )
        self._available = False
        self._retry_at = 0.0
        self._reported_unavailable = False

    @property
    def configured(self) -> bool:
        return self._client is not None

    @property
    def available(self) -> bool:
        return self._available

    def key(self, *parts: str) -> str:
        clean_parts = [self._settings.redis_key_prefix.strip(":")]
        clean_parts.extend(part.strip(":") for part in parts)
        return ":".join(clean_parts)

    async def connect(self) -> bool:
        return await self.ping(force=True)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def ping(self, *, force: bool = False) -> bool:
        try:
            await self._execute(lambda client: client.ping(), force=force)
        except RedisUnavailable:
            return False
        return True

    async def get(self, key: str) -> str | None:
        return await self._execute(lambda client: client.get(key))

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        await self._execute(lambda client: client.set(key, value, ex=ttl_seconds))

    async def delete(self, key: str) -> None:
        await self._execute(lambda client: client.delete(key))

    async def pttl(self, key: str) -> int:
        return int(await self._execute(lambda client: client.pttl(key)))

    async def eval(self, script: str, keys: list[str], arguments: list[object]) -> Any:
        return await self._execute(
            lambda client: client.eval(script, len(keys), *keys, *arguments)
        )

    async def acquire_lock(self, key: str, ttl_seconds: int) -> str | None:
        token = uuid4().hex
        acquired = await self._execute(
            lambda client: client.set(key, token, nx=True, px=ttl_seconds * 1_000)
        )
        return token if acquired else None

    async def release_lock(self, key: str, token: str) -> None:
        await self.eval(RELEASE_LOCK_SCRIPT, [key], [token])

    async def extend_cooldown(self, key: str, seconds: int) -> int:
        milliseconds = max(1, seconds) * 1_000
        result = await self.eval(EXTEND_COOLDOWN_SCRIPT, [key], [milliseconds])
        return max(1, int(result) // 1_000)

    async def _execute(
        self,
        operation: Callable[[redis.Redis], Awaitable[Result]],
        *,
        force: bool = False,
    ) -> Result:
        if self._client is None:
            raise RedisUnavailable("Redis n’est pas configuré")
        if not force and monotonic() < self._retry_at:
            raise RedisUnavailable("Redis est temporairement indisponible")

        try:
            result = await operation(self._client)
        except (RedisError, OSError, TimeoutError) as exc:
            self._available = False
            self._retry_at = monotonic() + self._settings.redis_retry_delay_seconds
            if not self._reported_unavailable:
                logger.warning("Redis est indisponible : %s", exc)
                self._reported_unavailable = True
            raise RedisUnavailable("Redis est temporairement indisponible") from exc

        if self._reported_unavailable:
            logger.info("La connexion Redis est rétablie")
        self._reported_unavailable = False
        self._available = True
        self._retry_at = 0.0
        return result
