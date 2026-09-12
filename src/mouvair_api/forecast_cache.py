import asyncio
import hashlib
from time import monotonic
from typing import Protocol

from pydantic import ValidationError

from .config import Settings
from .models import ForecastResponse
from .redis_store import RedisStore


class SharedForecastCache(Protocol):
    async def get(self, latitude: float, longitude: float) -> ForecastResponse | None: ...

    async def set(self, forecast: ForecastResponse) -> None: ...

    async def acquire_lock(self, latitude: float, longitude: float) -> str | None: ...

    async def release_lock(self, latitude: float, longitude: float, token: str) -> None: ...

    async def wait(self, latitude: float, longitude: float) -> ForecastResponse | None: ...


class RedisForecastCache:
    """Stocke les prévisions partagées sans décider de leur fraîcheur métier."""

    def __init__(self, store: RedisStore, settings: Settings) -> None:
        self._store = store
        self._ttl = settings.forecast_shared_cache_ttl_seconds
        self._lock_ttl = settings.forecast_lock_ttl_seconds
        self._lock_wait = settings.forecast_lock_wait_seconds

    async def get(self, latitude: float, longitude: float) -> ForecastResponse | None:
        key = self._cache_key(latitude, longitude)
        raw_value = await self._store.get(key)
        if raw_value is None:
            return None
        try:
            return ForecastResponse.model_validate_json(raw_value)
        except (ValidationError, ValueError):
            await self._store.delete(key)
            return None

    async def set(self, forecast: ForecastResponse) -> None:
        await self._store.set(
            self._cache_key(forecast.latitude, forecast.longitude),
            forecast.model_dump_json(),
            self._ttl,
        )

    async def acquire_lock(self, latitude: float, longitude: float) -> str | None:
        return await self._store.acquire_lock(
            self._lock_key(latitude, longitude),
            self._lock_ttl,
        )

    async def release_lock(self, latitude: float, longitude: float, token: str) -> None:
        await self._store.release_lock(self._lock_key(latitude, longitude), token)

    async def wait(self, latitude: float, longitude: float) -> ForecastResponse | None:
        deadline = monotonic() + self._lock_wait
        while monotonic() < deadline:
            await asyncio.sleep(0.05)
            forecast = await self.get(latitude, longitude)
            if forecast is not None:
                return forecast
        return None

    def _cache_key(self, latitude: float, longitude: float) -> str:
        return self._store.key("cache", "forecast", "v1", self._digest(latitude, longitude))

    def _lock_key(self, latitude: float, longitude: float) -> str:
        return self._store.key("lock", "forecast", "v1", self._digest(latitude, longitude))

    @staticmethod
    def _digest(latitude: float, longitude: float) -> str:
        coordinates = f"{latitude.hex()}\0{longitude.hex()}"
        return hashlib.sha256(coordinates.encode()).hexdigest()
