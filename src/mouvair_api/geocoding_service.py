import asyncio
import hashlib
import json
import unicodedata
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from time import monotonic
from typing import Protocol

from .config import Settings
from .models import Location
from .open_meteo import OpenMeteoError
from .redis_store import RedisUnavailable

LocationFetcher = Callable[[str, int], Awaitable[list[Location]]]
Clock = Callable[[], float]
CacheKey = tuple[str, int]


class GeocodingStore(Protocol):
    def key(self, *parts: str) -> str: ...

    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, ttl_seconds: int) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def acquire_lock(self, key: str, ttl_seconds: int) -> str | None: ...

    async def release_lock(self, key: str, token: str) -> None: ...


@dataclass(frozen=True)
class LocalGeocodingEntry:
    locations: list[Location]
    expires_at: float


def normalize_location_query(query: str) -> str:
    """Uniformise les variantes sans confondre volontairement les accents."""
    normalized = unicodedata.normalize("NFKC", query)
    return " ".join(normalized.split())


class GeocodingService:
    def __init__(
        self,
        fetcher: LocationFetcher,
        store: GeocodingStore,
        settings: Settings,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._store = store
        self._positive_ttl = settings.geocoding_cache_ttl_seconds
        self._negative_ttl = settings.geocoding_negative_cache_ttl_seconds
        self._max_entries = settings.geocoding_local_cache_max_entries
        self._lock_ttl = settings.geocoding_lock_ttl_seconds
        self._lock_wait = settings.geocoding_lock_wait_seconds
        self._clock = clock or monotonic
        self._cache: OrderedDict[CacheKey, LocalGeocodingEntry] = OrderedDict()
        self._inflight: dict[CacheKey, asyncio.Task[list[Location]]] = {}

    async def search(self, query: str, count: int = 5) -> list[Location]:
        normalized = normalize_location_query(query)
        if len(normalized) < 3:
            return []
        key = (normalized.casefold(), count)
        cached = self._local(key)
        if cached is not None:
            return cached

        shared = await self._shared(key)
        if shared is not None:
            self._store_local(key, shared)
            return shared

        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._load(key, normalized, count))
            self._inflight[key] = task
            task.add_done_callback(lambda finished: self._clear_inflight(key, finished))
        return await asyncio.shield(task)

    def _local(self, key: CacheKey) -> list[Location] | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        if entry.expires_at <= self._clock():
            del self._cache[key]
            return None
        self._cache.move_to_end(key)
        return [location.model_copy(deep=True) for location in entry.locations]

    def _store_local(self, key: CacheKey, locations: list[Location]) -> None:
        ttl = self._positive_ttl if locations else self._negative_ttl
        copies = [location.model_copy(deep=True) for location in locations]
        self._cache[key] = LocalGeocodingEntry(copies, self._clock() + ttl)
        self._cache.move_to_end(key)
        while len(self._cache) > self._max_entries:
            self._cache.popitem(last=False)

    async def _load(self, key: CacheKey, query: str, count: int) -> list[Location]:
        lock_key = self._lock_key(key)
        try:
            token = await self._store.acquire_lock(lock_key, self._lock_ttl)
        except RedisUnavailable:
            return await self._fetch_and_cache(key, query, count)

        if token is None:
            shared = await self._wait_for_shared(key)
            if shared is None:
                raise OpenMeteoError(
                    "Une recherche identique est déjà en cours, veuillez réessayer"
                )
            self._store_local(key, shared)
            return shared

        try:
            shared = await self._shared(key)
            if shared is not None:
                self._store_local(key, shared)
                return shared
            return await self._fetch_and_cache(key, query, count)
        finally:
            with suppress(RedisUnavailable):
                await self._store.release_lock(lock_key, token)

    async def _fetch_and_cache(
        self, key: CacheKey, query: str, count: int
    ) -> list[Location]:
        locations = await self._fetcher(query, count)
        self._store_local(key, locations)
        ttl = self._positive_ttl if locations else self._negative_ttl
        with suppress(RedisUnavailable):
            await self._store.set(
                self._shared_key(key),
                json.dumps([location.model_dump(mode="json") for location in locations]),
                ttl,
            )
        return [location.model_copy(deep=True) for location in locations]

    async def _shared(self, key: CacheKey) -> list[Location] | None:
        shared_key = self._shared_key(key)
        try:
            raw_value = await self._store.get(shared_key)
        except RedisUnavailable:
            return None
        if raw_value is None:
            return None
        try:
            payload = json.loads(raw_value)
            if not isinstance(payload, list):
                raise ValueError("Le cache de géocodage doit contenir une liste")
            return [Location.model_validate(item) for item in payload]
        except (TypeError, ValueError):
            with suppress(RedisUnavailable):
                await self._store.delete(shared_key)
            return None

    async def _wait_for_shared(self, key: CacheKey) -> list[Location] | None:
        deadline = monotonic() + self._lock_wait
        while monotonic() < deadline:
            await asyncio.sleep(0.05)
            shared = await self._shared(key)
            if shared is not None:
                return shared
        return None

    def _shared_key(self, key: CacheKey) -> str:
        digest = hashlib.sha256(f"{key[0]}\0{key[1]}\0fr".encode()).hexdigest()
        return self._store.key("cache", "geocoding", "v1", digest)

    def _lock_key(self, key: CacheKey) -> str:
        digest = hashlib.sha256(f"{key[0]}\0{key[1]}\0fr".encode()).hexdigest()
        return self._store.key("lock", "geocoding", "v1", digest)

    def _clear_inflight(self, key: CacheKey, task: asyncio.Task[list[Location]]) -> None:
        if self._inflight.get(key) is task:
            del self._inflight[key]
        if not task.cancelled():
            task.exception()
