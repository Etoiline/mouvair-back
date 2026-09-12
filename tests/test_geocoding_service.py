import asyncio
from collections.abc import Awaitable, Callable

from mouvair_api.config import Settings
from mouvair_api.geocoding_service import GeocodingService, normalize_location_query
from mouvair_api.models import Location
from mouvair_api.redis_store import RedisUnavailable


class MemoryStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.locks: dict[str, str] = {}

    def key(self, *parts: str) -> str:
        return ":".join(("test", *parts))

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        self.values[key] = value
        self.ttls[key] = ttl_seconds

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)

    async def acquire_lock(self, key: str, ttl_seconds: int) -> str | None:
        del ttl_seconds
        if key in self.locks:
            return None
        self.locks[key] = "token"
        return "token"

    async def release_lock(self, key: str, token: str) -> None:
        if self.locks.get(key) == token:
            del self.locks[key]


class UnavailableStore(MemoryStore):
    async def get(self, key: str) -> str | None:
        del key
        raise RedisUnavailable("indisponible")

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        del key, value, ttl_seconds
        raise RedisUnavailable("indisponible")

    async def acquire_lock(self, key: str, ttl_seconds: int) -> str | None:
        del key, ttl_seconds
        raise RedisUnavailable("indisponible")


class ToggleStore(MemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.available = True

    async def get(self, key: str) -> str | None:
        if not self.available:
            raise RedisUnavailable("indisponible")
        return await super().get(key)

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        if not self.available:
            raise RedisUnavailable("indisponible")
        await super().set(key, value, ttl_seconds)

    async def acquire_lock(self, key: str, ttl_seconds: int) -> str | None:
        if not self.available:
            raise RedisUnavailable("indisponible")
        return await super().acquire_lock(key, ttl_seconds)


def lyon() -> Location:
    return Location(id=1, name="Lyon", latitude=45.75, longitude=4.85, country="France")


def service_with(
    fetcher: Callable[[str, int], Awaitable[list[Location]]],
    store: MemoryStore,
) -> GeocodingService:
    return GeocodingService(fetcher, store, Settings(redis_failure_mode="local"))


def test_query_normalization_preserves_accents_and_collapses_spaces() -> None:
    assert normalize_location_query("  SAINT-Étienne  ") == "SAINT-Étienne"


def test_queries_shorter_than_three_characters_never_reach_the_provider() -> None:
    calls = 0

    async def fetcher(query: str, count: int) -> list[Location]:
        nonlocal calls
        calls += 1
        return [lyon()]

    result = asyncio.run(service_with(fetcher, MemoryStore()).search(" Ly "))

    assert result == []
    assert calls == 0


def test_normalized_queries_share_the_positive_cache() -> None:
    calls: list[tuple[str, int]] = []

    async def fetcher(query: str, count: int) -> list[Location]:
        calls.append((query, count))
        return [lyon()]

    service = service_with(fetcher, MemoryStore())

    async def scenario() -> tuple[list[Location], list[Location]]:
        first = await service.search("  LYON ", 5)
        second = await service.search("lyon", 5)
        return first, second

    first, second = asyncio.run(scenario())

    assert first == second == [lyon()]
    assert calls == [("LYON", 5)]


def test_two_instances_share_the_redis_cache() -> None:
    store = MemoryStore()
    calls = 0

    async def fetcher(query: str, count: int) -> list[Location]:
        nonlocal calls
        calls += 1
        return [lyon()]

    first = service_with(fetcher, store)
    second = service_with(fetcher, store)

    async def scenario() -> None:
        assert await first.search("Lyon") == [lyon()]
        assert await second.search("LYON") == [lyon()]

    asyncio.run(scenario())
    assert calls == 1


def test_empty_results_use_the_short_negative_ttl() -> None:
    store = MemoryStore()

    async def fetcher(query: str, count: int) -> list[Location]:
        del query, count
        return []

    asyncio.run(service_with(fetcher, store).search("Inconnue"))

    assert list(store.ttls.values()) == [300]


def test_local_cache_remains_available_during_a_redis_failure() -> None:
    store = ToggleStore()
    calls = 0

    async def fetcher(query: str, count: int) -> list[Location]:
        nonlocal calls
        del query, count
        calls += 1
        return [lyon()]

    service = service_with(fetcher, store)

    async def scenario() -> list[Location]:
        await service.search("Lyon")
        store.available = False
        return await service.search("LYON")

    assert asyncio.run(scenario()) == [lyon()]
    assert calls == 1


def test_simultaneous_queries_share_one_local_fetch() -> None:
    store = UnavailableStore()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def fetcher(query: str, count: int) -> list[Location]:
        nonlocal calls
        del query, count
        calls += 1
        started.set()
        await release.wait()
        return [lyon()]

    service = service_with(fetcher, store)

    async def scenario() -> None:
        first = asyncio.create_task(service.search("Lyon"))
        await started.wait()
        second = asyncio.create_task(service.search("Lyon"))
        await asyncio.sleep(0)
        release.set()
        assert await asyncio.gather(first, second) == [[lyon()], [lyon()]]

    asyncio.run(scenario())
    assert calls == 1
