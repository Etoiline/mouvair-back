import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .models import (
    Conditions,
    ForecastDataState,
    ForecastResponse,
    HourlyForecast,
    PollenForecast,
    SlotConditions,
)
from .open_meteo import SLOT_COUNT, SPORTS, OpenMeteoError, _next_full_hour, get_forecast
from .scoring import calculate_score

CACHE_TTL = timedelta(minutes=15)
STALE_AFTER = timedelta(hours=1)
CACHE_MAX_ENTRIES = 256

ForecastFetcher = Callable[[float, float], Awaitable[ForecastResponse]]
Clock = Callable[[], datetime]
CacheKey = tuple[float, float]


@dataclass(frozen=True)
class CacheEntry:
    forecast: ForecastResponse
    retrieved_at: datetime


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("L’horloge du cache doit fournir un instant avec fuseau horaire")
    return value.astimezone(UTC)


def _retrieved_at(forecast: ForecastResponse, fallback: datetime) -> datetime:
    try:
        parsed = datetime.fromisoformat(forecast.generated_at)
    except ValueError:
        return fallback
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else fallback


def _hour_timestamp(hour: HourlyForecast) -> int | None:
    try:
        moment = datetime.fromisoformat(hour.time)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return int(moment.timestamp())


def _requested_timestamps(now: datetime, timezone: ZoneInfo) -> list[int]:
    first_start = _next_full_hour(now, timezone)
    return [
        int((first_start + timedelta(hours=offset)).timestamp())
        for offset in range(SLOT_COUNT)
    ]


def _covers_requested_window(forecast: ForecastResponse, now: datetime) -> bool:
    timezone = ZoneInfo(forecast.timezone)
    available = {
        timestamp
        for hour in forecast.hours
        if (timestamp := _hour_timestamp(hour)) is not None
    }
    return all(timestamp in available for timestamp in _requested_timestamps(now, timezone))


def _unavailable_hour(start: datetime, timezone: ZoneInfo) -> HourlyForecast:
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
    """Replace les données conservées dans la fenêtre courante de 24 créneaux."""
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
            "recommendation_available": data_state != "stale",
        },
    )


class ForecastService:
    """Gère la fraîcheur, le repli et la mutualisation des prévisions par position."""

    def __init__(
        self,
        fetcher: ForecastFetcher | None = None,
        *,
        clock: Clock | None = None,
        max_entries: int = CACHE_MAX_ENTRIES,
    ) -> None:
        if max_entries < 1:
            raise ValueError("Le cache doit accepter au moins une position")
        self._fetcher = fetcher or get_forecast
        self._clock = clock or _utc_now
        self._max_entries = max_entries
        self._cache: OrderedDict[CacheKey, CacheEntry] = OrderedDict()
        self._inflight: dict[CacheKey, asyncio.Task[ForecastResponse]] = {}

    def _now(self) -> datetime:
        return _as_utc(self._clock())

    def _cached(self, key: CacheKey) -> CacheEntry | None:
        entry = self._cache.get(key)
        if entry is not None:
            self._cache.move_to_end(key)
        return entry

    def _store(self, key: CacheKey, forecast: ForecastResponse, now: datetime) -> CacheEntry:
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

    @staticmethod
    def _age(entry: CacheEntry, now: datetime) -> timedelta:
        return max(now - entry.retrieved_at, timedelta())

    def _fresh_cache_response(
        self, entry: CacheEntry, now: datetime
    ) -> ForecastResponse | None:
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
        try:
            forecast = await self._fetcher(latitude, longitude)
        except OpenMeteoError:
            if previous is None:
                raise
            return self._fallback(previous, self._now())

        now = self._now()
        entry = self._store(key, forecast, now)
        return _windowed_response(
            entry.forecast,
            now,
            data_state="fresh",
            from_cache=False,
        )

    def _clear_inflight(self, key: CacheKey, task: asyncio.Task[ForecastResponse]) -> None:
        if self._inflight.get(key) is task:
            del self._inflight[key]
        if not task.cancelled():
            task.exception()

    async def get_forecast(self, latitude: float, longitude: float) -> ForecastResponse:
        key = (latitude, longitude)
        now = self._now()
        previous = self._cached(key)
        if previous is not None:
            cached_response = self._fresh_cache_response(previous, now)
            if cached_response is not None:
                return cached_response

        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._refresh(key, latitude, longitude, previous))
            self._inflight[key] = task
            task.add_done_callback(lambda finished: self._clear_inflight(key, finished))
        return await asyncio.shield(task)


forecast_service = ForecastService()
