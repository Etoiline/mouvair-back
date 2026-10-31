"""Orchestre les prévisions : L1, L2, fraîcheur, concurrence et réponse de secours.

Une position est recherchée en mémoire puis dans le cache partagé. Si les données
ne couvrent plus la fenêtre demandée ou sont trop anciennes, une récupération est
tentée. En cas d’échec fournisseur, les anciennes heures sont replacées dans les
24 départs futurs, avec des créneaux incomplets pour les heures inconnues.
"""

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .forecast_cache import SharedForecastCache
from .models import (
    Conditions,
    ForecastDataState,
    ForecastResponse,
    HourlyForecast,
    PollenForecast,
    SlotConditions,
)
from .open_meteo import SLOT_COUNT, SPORTS, OpenMeteoError, _next_full_hour
from .redis_store import RedisUnavailable
from .scoring import calculate_score

# La fraîcheur est indépendante du TTL de conservation Redis, plus long pour le repli.
CACHE_TTL = timedelta(minutes=15)
STALE_AFTER = timedelta(hours=1)
# Le L1 est un LRU : dépasser cette borne retire la position la moins récemment utilisée.
CACHE_MAX_ENTRIES = 256

ForecastFetcher = Callable[[float, float], Awaitable[ForecastResponse]]
Clock = Callable[[], datetime]
CacheKey = tuple[float, float]


@dataclass(frozen=True)
class CacheEntry:
    """Prévision conservée et instant UTC réel de sa récupération chez le fournisseur."""
    forecast: ForecastResponse
    retrieved_at: datetime


def _utc_now() -> datetime:
    """Horloge de production, remplaçable par une horloge contrôlée dans les tests."""
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    """Exige une date consciente de son fuseau avant toute comparaison de fraîcheur."""
    if value.tzinfo is None:
        raise ValueError("L’horloge du cache doit fournir un instant avec fuseau horaire")
    return value.astimezone(UTC)


def _retrieved_at(forecast: ForecastResponse, fallback: datetime) -> datetime:
    """Préserve l’âge fournisseur d’une entrée L2 au lieu de dater sa lecture du cache."""
    try:
        parsed = datetime.fromisoformat(forecast.generated_at)
    except ValueError:
        return fallback
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else fallback


def _hour_timestamp(hour: HourlyForecast) -> int | None:
    """Identifie un départ par son instant réel, même si son heure locale est répétée."""
    try:
        moment = datetime.fromisoformat(hour.time)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return int(moment.timestamp())


def _requested_timestamps(now: datetime, timezone: ZoneInfo) -> list[int]:
    """Détermine les 24 départs futurs à partir de l’heure pleine locale suivante."""
    first_start = _next_full_hour(now, timezone)
    return [
        int((first_start + timedelta(hours=offset)).timestamp())
        for offset in range(SLOT_COUNT)
    ]


def _covers_requested_window(forecast: ForecastResponse, now: datetime) -> bool:
    """Vérifie la présence des départs demandés, indépendamment des mesures manquantes.

    Même encore récente, une prévision peut perdre sa couverture au passage
    d’une heure : le dernier créneau demandé se décale alors d’une heure aussi.
    """
    timezone = ZoneInfo(forecast.timezone)
    available = {
        timestamp
        for hour in forecast.hours
        if (timestamp := _hour_timestamp(hour)) is not None
    }
    return all(timestamp in available for timestamp in _requested_timestamps(now, timezone))


def _unavailable_hour(start: datetime, timezone: ZoneInfo) -> HourlyForecast:
    """Crée un vrai intervalle d’une heure dont les mesures et les notes sont absentes.

    Réutiliser le moteur garantit les mêmes statuts incomplets que pour les
    réponses fournisseur partielles, sans inventer de conditions favorables.
    """
    end = start + timedelta(hours=1)
    score_conditions = Conditions(weather_codes=[None, None])
    return HourlyForecast(
        time=start.astimezone(timezone).isoformat(timespec="minutes"),
        end_time=end.astimezone(timezone).isoformat(timespec="minutes"),
        conditions=SlotConditions(weather_codes=[None, None]),
        pollen=PollenForecast(),
        scores={sport: calculate_score(score_conditions, sport) for sport in SPORTS},
    )


def _windowed_response(
    forecast: ForecastResponse,
    now: datetime,
    *,
    data_state: ForecastDataState,
    from_cache: bool,
) -> ForecastResponse:
    """Replace les données conservées dans la fenêtre courante de 24 créneaux.

    Les anciens départs disparaissent et les nouveaux départs inconnus restent
    visibles avec des valeurs nulles. La copie profonde évite qu’un consommateur
    modifie ensuite la prévision de référence conservée dans le cache.
    """
    timezone = ZoneInfo(forecast.timezone)
    by_timestamp = {
        timestamp: hour
        for hour in forecast.hours
        if (timestamp := _hour_timestamp(hour)) is not None
    }
    first_start = _next_full_hour(now, timezone)
    hours = []
    for offset in range(SLOT_COUNT):
        start = first_start + timedelta(hours=offset)
        timestamp = int(start.timestamp())
        hours.append(by_timestamp.get(timestamp) or _unavailable_hour(start, timezone))

    return forecast.model_copy(
        deep=True,
        update={
            "hours": hours,
            "data_state": data_state,
            "from_cache": from_cache,
            # Une actualisation échouée reste exploitable jusqu’à une heure d’âge.
            "recommendation_available": data_state != "stale",
        },
    )


class ForecastService:
    """Gère la fraîcheur, le repli et la mutualisation des prévisions par position.

    fetcher représente l’opération fournisseur déjà protégée par son quota.
    clock et shared_cache sont injectables pour vérifier le temps et le partage
    entre instances sans dépendre du réseau dans les tests unitaires.
    """

    def __init__(
        self,
        fetcher: ForecastFetcher,
        *,
        clock: Clock | None = None,
        max_entries: int = CACHE_MAX_ENTRIES,
        shared_cache: SharedForecastCache | None = None,
    ) -> None:
        """Prépare un LRU borné et le registre des récupérations locales en cours."""
        if max_entries < 1:
            raise ValueError("Le cache doit accepter au moins une position")
        self._fetcher = fetcher
        self._clock = clock or _utc_now
        self._max_entries = max_entries
        self._shared_cache = shared_cache
        self._cache: OrderedDict[CacheKey, CacheEntry] = OrderedDict()
        self._inflight: dict[CacheKey, asyncio.Task[ForecastResponse]] = {}

    def _now(self) -> datetime:
        """Normalise chaque lecture de l’horloge injectée en UTC."""
        return _as_utc(self._clock())

    def _cached(self, key: CacheKey) -> CacheEntry | None:
        """Lit le L1 et marque une entrée consultée comme récemment utilisée."""
        entry = self._cache.get(key)
        if entry is not None:
            self._cache.move_to_end(key)
        return entry

    def _store(self, key: CacheKey, forecast: ForecastResponse, now: datetime) -> CacheEntry:
        """Conserve une copie canonique et retire les positions excédant la capacité.

        Les marqueurs de réponse seront recalculés à chaque lecture. La date
        générée par le fournisseur reste la référence de fraîcheur, y compris au L2.
        """
        fresh = forecast.model_copy(
            deep=True,
            update={
                "data_state": "fresh",
                "from_cache": False,
                "recommendation_available": True,
            },
        )
        entry = CacheEntry(forecast=fresh, retrieved_at=_retrieved_at(fresh, now))
        self._cache[key] = entry
        self._cache.move_to_end(key)
        while len(self._cache) > self._max_entries:
            self._cache.popitem(last=False)
        return entry

    def _prefer_shared(
        self,
        key: CacheKey,
        shared: ForecastResponse,
        previous: CacheEntry | None,
        now: datetime,
    ) -> CacheEntry:
        """Choisit la récupération la plus récente entre le L1 connu et le résultat L2."""
        shared_retrieved_at = _retrieved_at(shared, now)
        if previous is not None and previous.retrieved_at >= shared_retrieved_at:
            return previous
        return self._store(key, shared, now)

    @staticmethod
    def _age(entry: CacheEntry, now: datetime) -> timedelta:
        """Mesure l’âge sans produire une durée négative si l’horloge a reculé."""
        return max(now - entry.retrieved_at, timedelta())

    def _fresh_cache_response(
        self, entry: CacheEntry, now: datetime
    ) -> ForecastResponse | None:
        """Réutilise seulement les données de 15 minutes au plus couvrant la fenêtre."""
        if self._age(entry, now) > CACHE_TTL:
            return None
        if not _covers_requested_window(entry.forecast, now):
            return None
        return _windowed_response(
            entry.forecast,
            now,
            data_state="fresh",
            from_cache=True,
        )

    def _fallback(self, entry: CacheEntry, now: datetime) -> ForecastResponse:
        """Classe le repli selon le seuil inclusif d’une heure et réajuste ses départs."""
        state: ForecastDataState = (
            "refresh_failed" if self._age(entry, now) <= STALE_AFTER else "stale"
        )
        return _windowed_response(
            entry.forecast,
            now,
            data_state=state,
            from_cache=True,
        )

    async def _refresh(
        self,
        key: CacheKey,
        latitude: float,
        longitude: float,
        previous: CacheEntry | None,
    ) -> ForecastResponse:
        """Récupère, conserve et présente une prévision, ou réutilise la dernière connue.

        Seules les erreurs attendues du fournisseur déclenchent le repli. Sans
        donnée antérieure, elles remontent jusqu’à la traduction HTTP de la route.
        """
        try:
            forecast = await self._fetcher(latitude, longitude)
        except OpenMeteoError:
            if previous is None:
                raise
            return self._fallback(previous, self._now())

        now = self._now()
        entry = self._store(key, forecast, now)
        await self._store_shared(key, entry.forecast)
        return _windowed_response(
            entry.forecast,
            now,
            data_state="fresh",
            from_cache=False,
        )

    async def _refresh_shared(
        self,
        key: CacheKey,
        latitude: float,
        longitude: float,
        previous: CacheEntry | None,
    ) -> ForecastResponse:
        """Coordonne le rafraîchissement avec les autres instances grâce au verrou L2.

        Une panne Redis laisse la passerelle décider si le quota autorise encore
        un appel. En mode closed, ce refus devient lui-même une erreur fournisseur
        attendue et peut donc servir une ancienne prévision disponible.
        """
        if self._shared_cache is None:
            return await self._refresh(key, latitude, longitude, previous)

        try:
            token = await self._shared_cache.acquire_lock(latitude, longitude)
        except RedisUnavailable:
            return await self._refresh(key, latitude, longitude, previous)

        if token is None:
            # Une autre instance possède le verrou ; attendre son résultat évite un doublon.
            try:
                shared = await self._shared_cache.wait(latitude, longitude)
            except RedisUnavailable:
                shared = None
            if shared is None:
                if previous is not None:
                    return self._fallback(previous, self._now())
                raise OpenMeteoError(
                    "Une récupération identique est déjà en cours, veuillez réessayer"
                )
            now = self._now()
            entry = self._prefer_shared(key, shared, previous, now)
            cached = self._fresh_cache_response(entry, now)
            return cached or self._fallback(entry, self._now())

        try:
            # Relire après acquisition ferme la course entre la première lecture et le verrou.
            shared = await self._load_shared(latitude, longitude)
            if shared is not None:
                previous = self._prefer_shared(key, shared, previous, self._now())
                cached = self._fresh_cache_response(previous, self._now())
                if cached is not None:
                    return cached
            return await self._refresh(key, latitude, longitude, previous)
        finally:
            with suppress(RedisUnavailable):
                await self._shared_cache.release_lock(latitude, longitude, token)

    async def _load_shared(
        self, latitude: float, longitude: float
    ) -> ForecastResponse | None:
        """Traite une panne de lecture L2 comme un cache absent pour laisser agir le repli."""
        if self._shared_cache is None:
            return None
        try:
            return await self._shared_cache.get(latitude, longitude)
        except RedisUnavailable:
            return None

    async def _store_shared(self, key: CacheKey, forecast: ForecastResponse) -> None:
        """Écrit le L2 avec la même position demandée que le L1 et le verrou.

        La clé d’origine accompagne la réponse, dont les coordonnées fournisseur
        peuvent différer. Une panne Redis ne fait pas perdre ce résultat valide.
        """
        if self._shared_cache is None:
            return
        with suppress(RedisUnavailable):
            await self._shared_cache.set(latitude=key[0], longitude=key[1], forecast=forecast)

    def _clear_inflight(self, key: CacheKey, task: asyncio.Task[ForecastResponse]) -> None:
        """Retire uniquement la tâche terminée et récupère son éventuelle exception.

        La lecture de l’exception évite un avertissement asyncio si tous les
        demandeurs se sont déconnectés ; elle ne change pas le résultat de la tâche.
        """
        if self._inflight.get(key) is task:
            del self._inflight[key]
        if not task.cancelled():
            task.exception()

    async def get_forecast(self, latitude: float, longitude: float) -> ForecastResponse:
        """Parcourt L1, L2, puis une récupération mutualisée pour les coordonnées exactes."""
        key = (latitude, longitude)
        now = self._now()
        previous = self._cached(key)
        if previous is not None:
            cached_response = self._fresh_cache_response(previous, now)
            if cached_response is not None:
                return cached_response

        shared = await self._load_shared(latitude, longitude)
        if shared is not None:
            previous = self._prefer_shared(key, shared, previous, now)
        if previous is not None:
            cached_response = self._fresh_cache_response(previous, now)
            if cached_response is not None:
                return cached_response

        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(
                self._refresh_shared(key, latitude, longitude, previous)
            )
            self._inflight[key] = task
            task.add_done_callback(lambda finished: self._clear_inflight(key, finished))
        # La déconnexion d’un demandeur ne doit pas annuler le travail attendu par les autres.
        return await asyncio.shield(task)
