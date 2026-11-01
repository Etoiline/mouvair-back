"""Gère les recherches de villes et évite les appels fournisseur redondants.

Un LRU en mémoire précède le cache Redis partagé. Les résultats positifs et
négatifs ont des durées distinctes ; les tâches locales et les verrous Redis
regroupent les recherches identiques. Le quota reste géré par le fetcher injecté.
"""

import asyncio
import hashlib
import json
import logging
import unicodedata
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from time import monotonic, perf_counter
from typing import Protocol

from .config import Settings
from .diagnostics import error_fields, log_event
from .errors import DataUnavailableError
from .models import Location
from .redis_store import RedisUnavailable

LocationFetcher = Callable[[str, int], Awaitable[list[Location]]]
Clock = Callable[[], float]
CacheKey = tuple[str, int]


class GeocodingStore(Protocol):
    """Primitives de stockage suffisantes au service, simulables sans serveur Redis."""
    def key(self, *parts: str) -> str: ...

    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, ttl_seconds: int) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def acquire_lock(self, key: str, ttl_seconds: int) -> str | None: ...

    async def release_lock(self, key: str, token: str) -> None: ...


@dataclass(frozen=True)
class LocalGeocodingEntry:
    """Résultats et échéance monotone, indépendants des changements d’heure système."""
    locations: list[Location]
    expires_at: float


def normalize_location_query(query: str) -> str:
    """Uniformise Unicode et les espaces sans confondre volontairement les accents.

    La casse est normalisée uniquement pour la clé ; la requête envoyée au
    fournisseur conserve son écriture nettoyée pour la recherche des toponymes.
    """
    normalized = unicodedata.normalize("NFKC", query)
    return " ".join(normalized.split())


class GeocodingService:
    """Orchestre le géocodage en français avec deux caches et une coordination partagée."""
    def __init__(
        self,
        fetcher: LocationFetcher,
        store: GeocodingStore,
        settings: Settings,
        *,
        clock: Clock | None = None,
    ) -> None:
        """Injecte le fournisseur, le stockage et une horloge monotone remplaçable en test."""
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
        """Cherche une requête normalisée, puis partage une tâche unique en cas de manque.

        Le seuil s’applique après nettoyage : trois caractères bruts ne suffisent
        pas si la requête ne contient en réalité qu’une lettre et des espaces.
        """
        normalized = normalize_location_query(query)
        if len(normalized) < 3:
            return []
        # casefold réunit les variantes de casse ; count distingue les tailles de réponse.
        key = (normalized.casefold(), count)
        cached = self._local(key)
        if cached is not None:
            log_event("geocoding.cache_hit", source="memory", result_count=len(cached))
            return cached

        shared = await self._shared(key)
        if shared is not None:
            self._store_local(key, shared)
            log_event("geocoding.cache_hit", source="redis", result_count=len(shared))
            return shared

        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._load(key, normalized, count))
            self._inflight[key] = task
            task.add_done_callback(lambda finished: self._clear_inflight(key, finished))
        else:
            log_event("geocoding.coalesced", source="memory")
        # Un demandeur annulé ne doit pas interrompre la recherche partagée par les autres.
        return await asyncio.shield(task)

    def _local(self, key: CacheKey) -> list[Location] | None:
        """Écarte les entrées expirées et renvoie des copies protégeant le cache local."""
        entry = self._cache.get(key)
        if entry is None:
            return None
        if entry.expires_at <= self._clock():
            del self._cache[key]
            return None
        self._cache.move_to_end(key)
        return [location.model_copy(deep=True) for location in entry.locations]

    def _store_local(self, key: CacheKey, locations: list[Location]) -> None:
        """Applique le TTL positif ou négatif et maintient la capacité maximale du LRU."""
        ttl = self._positive_ttl if locations else self._negative_ttl
        copies = [location.model_copy(deep=True) for location in locations]
        self._cache[key] = LocalGeocodingEntry(copies, self._clock() + ttl)
        self._cache.move_to_end(key)
        while len(self._cache) > self._max_entries:
            self._cache.popitem(last=False)

    async def _load(self, key: CacheKey, query: str, count: int) -> list[Location]:
        """Prend le verrou distribué ou attend le résultat d’une recherche identique."""
        lock_key = self._lock_key(key)
        try:
            token = await self._store.acquire_lock(lock_key, self._lock_ttl)
        except RedisUnavailable:
            return await self._fetch_and_cache(key, query, count)

        if token is None:
            started = perf_counter()
            shared = await self._wait_for_shared(key)
            log_event(
                "geocoding.shared_wait", source="redis",
                level=logging.INFO if shared is not None else logging.WARNING,
                outcome="ready" if shared is not None else "timeout",
                duration_ms=round((perf_counter() - started) * 1_000, 3),
            )
            if shared is None:
                raise DataUnavailableError(
                    "Une recherche identique est déjà en cours, veuillez réessayer"
                )
            self._store_local(key, shared)
            log_event("geocoding.cache_hit", source="redis", result_count=len(shared))
            return shared

        try:
            # Une instance a pu remplir le cache entre sa première lecture et ce verrou.
            shared = await self._shared(key)
            if shared is not None:
                self._store_local(key, shared)
                log_event("geocoding.cache_hit", source="redis", result_count=len(shared))
                return shared
            return await self._fetch_and_cache(key, query, count)
        finally:
            with suppress(RedisUnavailable):
                await self._store.release_lock(lock_key, token)

    async def _fetch_and_cache(
        self, key: CacheKey, query: str, count: int
    ) -> list[Location]:
        """Appelle le fournisseur puis conserve aussi une liste vide pendant son TTL court.

        L’écriture Redis est facultative pour servir un résultat déjà obtenu ; la
        copie retournée empêche l’appelant d’altérer les objets gardés en mémoire.
        """
        started = perf_counter()
        try:
            locations = await self._fetcher(query, count)
        except DataUnavailableError as exc:
            log_event(
                "geocoding.fetch_failed", level=logging.WARNING, source="provider",
                duration_ms=round((perf_counter() - started) * 1_000, 3), **error_fields(exc),
            )
            raise
        provider_duration_ms = round((perf_counter() - started) * 1_000, 3)
        self._store_local(key, locations)
        ttl = self._positive_ttl if locations else self._negative_ttl
        with suppress(RedisUnavailable):
            await self._store.set(
                self._shared_key(key),
                json.dumps([location.model_dump(mode="json") for location in locations]),
                ttl,
            )
        log_event(
            "geocoding.fetched", source="provider", result_count=len(locations),
            duration_ms=provider_duration_ms,
        )
        return [location.model_copy(deep=True) for location in locations]

    async def _shared(self, key: CacheKey) -> list[Location] | None:
        """Valide le JSON Redis et distingue une liste vide valide d’une absence de cache."""
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
        except (TypeError, ValueError) as exc:
            log_event(
                "geocoding.cache_invalid", level=logging.WARNING,
                source="redis", **error_fields(exc),
            )
            with suppress(RedisUnavailable):
                await self._store.delete(shared_key)
            return None

    async def _wait_for_shared(self, key: CacheKey) -> list[Location] | None:
        """Interroge brièvement le L2 sans immobiliser la boucle asynchrone."""
        deadline = monotonic() + self._lock_wait
        while monotonic() < deadline:
            await asyncio.sleep(0.05)
            shared = await self._shared(key)
            if shared is not None:
                return shared
        return None

    def _shared_key(self, key: CacheKey) -> str:
        """Inclut requête, taille et langue dans une clé versionnée de résultats."""
        digest = hashlib.sha256(f"{key[0]}\0{key[1]}\0fr".encode()).hexdigest()
        return self._store.key("cache", "geocoding", "v1", digest)

    def _lock_key(self, key: CacheKey) -> str:
        """Coordonne exactement le même périmètre de recherche que la clé de cache."""
        digest = hashlib.sha256(f"{key[0]}\0{key[1]}\0fr".encode()).hexdigest()
        return self._store.key("lock", "geocoding", "v1", digest)

    def _clear_inflight(self, key: CacheKey, task: asyncio.Task[list[Location]]) -> None:
        """Oublie une tâche terminée sans retirer une éventuelle nouvelle tâche de même clé."""
        if self._inflight.get(key) is task:
            del self._inflight[key]
        if not task.cancelled():
            task.exception()
