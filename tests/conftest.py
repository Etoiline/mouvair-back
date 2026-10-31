"""Doubles partagés pour tester le vrai cache de prévision sans serveur Redis.

Le stockage conserve les clés et la possession des verrous pour exercer le calcul
des clés et la sérialisation de RedisForecastCache. Les TTL sont enregistrés sans
simuler le passage du temps ; leur application réelle est vérifiée en intégration.
"""

from uuid import uuid4

import pytest

from mouvair_api.config import Settings
from mouvair_api.redis_store import RedisStore


class MemoryRedisStore(RedisStore):
    """Implémente en mémoire les primitives utilisées par le cache de prévision."""

    def __init__(self) -> None:
        super().__init__(Settings(redis_key_prefix="test"))
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.locks: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        self.values[key] = value
        self.ttls[key] = ttl_seconds

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)
        self.ttls.pop(key, None)

    async def acquire_lock(self, key: str, ttl_seconds: int) -> str | None:
        del ttl_seconds
        if key in self.locks:
            return None
        token = uuid4().hex
        self.locks[key] = token
        return token

    async def release_lock(self, key: str, token: str) -> None:
        if self.locks.get(key) == token:
            del self.locks[key]


@pytest.fixture
def memory_redis_store() -> RedisStore:
    """Fournit un stockage isolé par test, partageable entre plusieurs adaptateurs L2."""
    return MemoryRedisStore()
