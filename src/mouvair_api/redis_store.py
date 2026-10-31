"""Fournit les primitives Redis communes aux caches, verrous et compteurs.

Les erreurs du client Redis deviennent RedisUnavailable, comprise par les
services. Après un échec, un délai court évite de solliciter continuellement
une connexion indisponible ; une opération réussie rétablit l’état normal.
"""

import logging
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Any, TypeVar
from uuid import uuid4

import redis.asyncio as redis
from redis.exceptions import RedisError

from .config import Settings
from .diagnostics import error_fields, log_event

Result = TypeVar("Result")

# La comparaison du jeton et la suppression doivent être indivisibles : un ancien
# propriétaire ne doit pas libérer un verrou expiré puis repris par une autre tâche.
RELEASE_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

# Un signal de limitation court ne doit jamais raccourcir une attente déjà plus longue.
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
    """Signale qu’une primitive Redis ne peut pas être garantie à cet instant."""

    pass


class RedisStore:
    """Centralise l’accès Redis et coupe brièvement les tentatives après une panne.

    Une instance est partagée par tous les services d’une application. Le client
    peut être injecté pour les tests ; sans URL ni client, les opérations signalent
    une indisponibilité plutôt que de simuler silencieusement Redis.
    """

    def __init__(self, settings: Settings, client: redis.Redis | None = None) -> None:
        """Prépare le pool et les états locaux sans effectuer encore d’appel réseau."""
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
        """Indique la présence d’un client, indépendamment de sa connectivité actuelle."""
        return self._client is not None

    @property
    def available(self) -> bool:
        """Expose le résultat connu de la dernière opération, sans sonder le réseau."""
        return self._available

    def key(self, *parts: str) -> str:
        """Construit une clé préfixée commune aux composants d’un même déploiement."""
        clean_parts = [self._settings.redis_key_prefix.strip(":")]
        clean_parts.extend(part.strip(":") for part in parts)
        return ":".join(clean_parts)

    async def connect(self) -> bool:
        """Sonde Redis au démarrage sans empêcher l’API de démarrer en mode dégradé."""
        return await self.ping(force=True)

    async def close(self) -> None:
        """Ferme le pool possédé par cette application lors de son arrêt."""
        if self._client is not None:
            await self._client.aclose()

    async def ping(self, *, force: bool = False) -> bool:
        """Transforme le diagnostic réseau en booléen, avec contournement du délai local."""
        try:
            await self._execute(lambda client: client.ping(), force=force)
        except RedisUnavailable:
            return False
        return True

    async def get(self, key: str) -> str | None:
        """Lit une chaîne ; None signifie une clé absente, pas une panne de Redis."""
        return await self._execute(lambda client: client.get(key))

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        """Écrit une chaîne avec son expiration dans la même commande Redis."""
        await self._execute(lambda client: client.set(key, value, ex=ttl_seconds))

    async def delete(self, key: str) -> None:
        """Retire notamment une entrée de cache dont le contenu n’est plus valide."""
        await self._execute(lambda client: client.delete(key))

    async def pttl(self, key: str) -> int:
        """Lit la durée restante en millisecondes pour les périodes d’attente partagées."""
        return int(await self._execute(lambda client: client.pttl(key)))

    async def eval(self, script: str, keys: list[str], arguments: list[object]) -> Any:
        """Exécute atomiquement un script serveur, notamment pour réserver un quota."""
        return await self._execute(
            lambda client: client.eval(script, len(keys), *keys, *arguments)
        )

    async def acquire_lock(self, key: str, ttl_seconds: int) -> str | None:
        """Prend un verrou expirant et renvoie son jeton, ou None s’il est déjà pris.

        NX évite d’écraser le propriétaire courant ; PX libère automatiquement
        le verrou si le processus disparaît avant sa restitution explicite.
        """
        token = uuid4().hex
        acquired = await self._execute(
            lambda client: client.set(key, token, nx=True, px=ttl_seconds * 1_000)
        )
        return token if acquired else None

    async def release_lock(self, key: str, token: str) -> None:
        """Libère le verrou seulement si le jeton correspond encore à son propriétaire."""
        await self.eval(RELEASE_LOCK_SCRIPT, [key], [token])

    async def extend_cooldown(self, key: str, seconds: int) -> int:
        """Prolonge sans la réduire une interdiction temporaire d’appel fournisseur."""
        milliseconds = max(1, seconds) * 1_000
        result = await self.eval(EXTEND_COOLDOWN_SCRIPT, [key], [milliseconds])
        return max(1, int(result) // 1_000)

    async def _execute(
        self,
        operation: Callable[[redis.Redis], Awaitable[Result]],
        *,
        force: bool = False,
    ) -> Result:
        """Encadre les commandes avec le délai de repli et la normalisation des erreurs.

        Le délai utilise une horloge monotone, indépendante d’un changement
        d’heure système. force est réservé aux sondes explicites comme /health.
        """
        if self._client is None:
            raise RedisUnavailable("Redis n’est pas configuré")
        if not force and monotonic() < self._retry_at:
            raise RedisUnavailable("Redis est temporairement indisponible")

        try:
            result = await operation(self._client)
        except (RedisError, OSError, TimeoutError) as exc:
            self._available = False
            self._retry_at = monotonic() + self._settings.redis_retry_delay_seconds
            # Le journal signale les transitions, sans répéter le même avertissement par GET.
            if not self._reported_unavailable:
                log_event("redis.unavailable", level=logging.WARNING, **error_fields(exc))
                self._reported_unavailable = True
            raise RedisUnavailable("Redis est temporairement indisponible") from exc

        if self._reported_unavailable:
            log_event("redis.restored")
        self._reported_unavailable = False
        self._available = True
        self._retry_at = 0.0
        return result
