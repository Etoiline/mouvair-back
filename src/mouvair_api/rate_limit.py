"""Protège les appels entrants et le budget sortant avec des fenêtres glissantes.

Redis enregistre une unité par appel dans un ensemble trié par instant. Un script
atomique vérifie toutes les fenêtres avant de débiter le coût complet. Le repli
local est autorisé pour les clients et, sur choix explicite, pour le fournisseur.
Une attente partagée propage les limitations signalées par Open-Meteo.
"""

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass
from math import ceil
from time import monotonic, time
from uuid import uuid4

from .config import Settings
from .redis_store import RedisStore, RedisUnavailable

# Une durée de 31 jours fournit la fenêtre longue commune à toutes les réservations.
MONTH_WINDOW_SECONDS = 31 * 24 * 60 * 60

# Étapes du script : reconnaître une réservation déjà effectuée, retirer les unités
# expirées, vérifier chaque fenêtre, puis ajouter tout le coût seulement si autorisé.
# TIME utilise l’horloge Redis pour que les instances partagent la même référence.
# Chaque unité a un membre distinct : une prévision de coût 2 occupe deux places.
# En cas de refus, les plus anciens événements déterminent le délai de libération.
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
    """Plafond d’unités sur une durée glissante exprimée en secondes."""
    seconds: int
    limit: int


@dataclass(frozen=True)
class RateDecision:
    """Résultat d’une réservation avec attente minimale en secondes si elle est refusée."""
    allowed: bool
    retry_after: int = 0


class RateLimitExceeded(RuntimeError):
    """Refus temporaire utilisable par le middleware ou la passerelle fournisseur."""

    def __init__(self, retry_after: int) -> None:
        """Normalise le délai pour ne jamais suggérer une reprise immédiate."""
        super().__init__("Limite de requêtes atteinte")
        self.retry_after = max(1, retry_after)


class RateLimitBackendUnavailable(RuntimeError):
    """Indique qu’aucun compteur autorisé ne peut garantir la réservation demandée."""

    pass


class RedisSlidingWindowLimiter:
    """Adapte le script atomique aux structures Python de fenêtres et de décision."""

    def __init__(self, store: RedisStore) -> None:
        """Réutilise la connexion et le traitement d’erreur Redis de l’application."""
        self._store = store

    async def reserve(
        self,
        key: str,
        windows: tuple[RateWindow, ...],
        *,
        cost: int = 1,
        reservation_id: str | None = None,
    ) -> RateDecision:
        """Réserve un coût indivisible et identifie l’opération pour éviter un double débit."""
        _validate_request(windows, cost)
        arguments: list[object] = [cost, reservation_id or uuid4().hex, len(windows)]
        for window in windows:
            arguments.extend((window.seconds, window.limit))
        result = await self._store.eval(SLIDING_WINDOW_SCRIPT, [key], arguments)
        return RateDecision(allowed=bool(int(result[0])), retry_after=int(result[1]))


class LocalSlidingWindowLimiter:
    """Repli explicite, limité au processus et donc non adapté au multi-instance.

    Une file d’instants représente les unités consommées pour chaque clé. Un
    verrou asyncio conserve l’indivisibilité du contrôle et du débit dans le
    processus ; son historique reste indépendant de celui de Redis.
    """

    def __init__(self) -> None:
        """Prépare les événements, les identifiants déjà réservés et leur verrou commun."""
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
        """Applique toutes les fenêtres avant de débiter, avec la même idempotence que Redis."""
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

            # Refuser ici ne consomme aucune unité, même si une autre fenêtre aurait accepté.
            if retry_after > 0:
                return RateDecision(allowed=False, retry_after=max(1, retry_after))

            events.extend(now for _ in range(cost))
            self._reservations[(key, reservation)] = now + longest
            expired = [item for item, expires_at in self._reservations.items() if expires_at <= now]
            for item in expired:
                del self._reservations[item]
            return RateDecision(allowed=True)


class SharedRateLimiter:
    """Privilégie Redis et n’autorise le compteur local que selon la politique choisie."""
    def __init__(
        self,
        store: RedisStore,
        *,
        allow_local_fallback: bool,
        local: LocalSlidingWindowLimiter | None = None,
    ) -> None:
        """Injecte éventuellement le compteur local pour contrôler son comportement en test."""
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
        """Conserve le même identifiant d’opération lors d’un éventuel repli local."""
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
    """Partage les quatre budgets fournisseur et les périodes d’attente entre instances."""

    def __init__(self, store: RedisStore, settings: Settings) -> None:
        """Construit les fenêtres et isole les clés par périmètre de consommation partagé."""
        self._store = store
        self._limiter = SharedRateLimiter(
            store,
            allow_local_fallback=settings.redis_failure_mode == "local",
        )
        scope = settings.open_meteo_quota_scope
        # Le segment entre accolades regroupe ces clés dans le même hash slot Redis Cluster.
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
        """Vérifie l’attente fournisseur puis réserve le budget de toutes les fenêtres.

        Les unités ne sont pas remboursées après un échec réseau : le fournisseur
        peut avoir reçu et comptabilisé l’appel malgré l’absence de réponse locale.
        """
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
        """Mémorise et partage une attente en conservant toujours la plus longue connue."""
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
        """Combine l’attente monotone locale et la durée Redis arrondie vers le haut.

        Sans Redis, le mode closed bloque la réservation plutôt que d’ignorer
        une éventuelle attente imposée par une autre instance.
        """
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
    """Applique des budgets distincts par route et identité réseau, même sur cache hit."""

    def __init__(self, store: RedisStore, settings: Settings) -> None:
        """Active un repli local pour maintenir une limitation entrante pendant une panne."""
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
        """Compte une requête avant son traitement ou lève une erreur avec son délai."""
        decision = await self._limiter.reserve(
            self._store.key("client-limit", endpoint, client_id),
            self._windows[endpoint],
        )
        if not decision.allowed:
            raise RateLimitExceeded(decision.retry_after)


def _validate_request(windows: tuple[RateWindow, ...], cost: int) -> None:
    """Refuse une réservation impossible avant tout accès à Redis ou au compteur local."""
    if not windows:
        raise ValueError("Au moins une fenêtre de limitation est requise")
    if cost < 1:
        raise ValueError("Le coût doit être strictement positif")
    for window in windows:
        if window.seconds < 1 or window.limit < cost:
            raise ValueError("Fenêtre ou limite invalide")
