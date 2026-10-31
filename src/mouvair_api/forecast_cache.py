"""Implémente le stockage partagé des prévisions et leur verrou de récupération.

Ce cache L2 sérialise les modèles Pydantic et applique une durée de conservation.
La fraîcheur de 15 minutes et les états de repli relèvent de ForecastService :
une entrée conservée ici peut donc être trop ancienne pour être recommandée.
"""

import asyncio
import hashlib
from time import monotonic
from typing import Protocol

from pydantic import ValidationError

from .config import Settings
from .models import ForecastResponse
from .redis_store import RedisStore


class SharedForecastCache(Protocol):
    """Contrat injectable du L2 : lire, conserver et coordonner une récupération.

    Le service dépend de ces capacités plutôt que du client Redis concret, ce
    qui permet de simuler le partage entre instances dans les tests unitaires.
    """
    async def get(self, latitude: float, longitude: float) -> ForecastResponse | None: ...

    async def set(self, forecast: ForecastResponse) -> None: ...

    async def acquire_lock(self, latitude: float, longitude: float) -> str | None: ...

    async def release_lock(self, latitude: float, longitude: float, token: str) -> None: ...

    async def wait(self, latitude: float, longitude: float) -> ForecastResponse | None: ...


class RedisForecastCache:
    """Stocke les prévisions partagées sans décider de leur fraîcheur métier."""

    def __init__(self, store: RedisStore, settings: Settings) -> None:
        """Sépare les durées de conservation, de possession du verrou et d’attente."""
        self._store = store
        self._ttl = settings.forecast_shared_cache_ttl_seconds
        self._lock_ttl = settings.forecast_lock_ttl_seconds
        self._lock_wait = settings.forecast_lock_wait_seconds

    async def get(self, latitude: float, longitude: float) -> ForecastResponse | None:
        """Relit un modèle validé ou écarte une entrée corrompue comme un cache manquant."""
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
        """Conserve le résultat et son generated_at d’origine pendant le TTL configuré."""
        await self._store.set(
            self._cache_key(forecast.latitude, forecast.longitude),
            forecast.model_dump_json(),
            self._ttl,
        )

    async def acquire_lock(self, latitude: float, longitude: float) -> str | None:
        """Coordonne les récupérations d’une position entre workers et instances."""
        return await self._store.acquire_lock(
            self._lock_key(latitude, longitude),
            self._lock_ttl,
        )

    async def release_lock(self, latitude: float, longitude: float, token: str) -> None:
        """Restitue la possession avec le jeton attribué lors de l’acquisition."""
        await self._store.release_lock(self._lock_key(latitude, longitude), token)

    async def wait(self, latitude: float, longitude: float) -> ForecastResponse | None:
        """Attend brièvement une entrée partagée sans bloquer la boucle asynchrone.

        L’appelant doit encore vérifier la fraîcheur du résultat obtenu. La borne
        d’attente évite de retenir indéfiniment une requête derrière une autre instance.
        """
        deadline = monotonic() + self._lock_wait
        while monotonic() < deadline:
            await asyncio.sleep(0.05)
            forecast = await self.get(latitude, longitude)
            if forecast is not None:
                return forecast
        return None

    def _cache_key(self, latitude: float, longitude: float) -> str:
        """Versionne le format stocké pour distinguer les générations de contrat."""
        return self._store.key("cache", "forecast", "v1", self._digest(latitude, longitude))

    def _lock_key(self, latitude: float, longitude: float) -> str:
        """Utilise la même identité de position dans un espace de clés distinct du cache."""
        return self._store.key("lock", "forecast", "v1", self._digest(latitude, longitude))

    @staticmethod
    def _digest(latitude: float, longitude: float) -> str:
        """Encode exactement les flottants, sans arrondir ni fusionner deux positions.

        Le séparateur nul rend la paire non ambiguë avant son hachage SHA-256.
        Le hachage sert à construire une clé stable et compacte.
        """
        coordinates = f"{latitude.hex()}\0{longitude.hex()}"
        return hashlib.sha256(coordinates.encode()).hexdigest()
