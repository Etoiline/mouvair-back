import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from mouvair_api.forecast_service import ForecastService
from mouvair_api.models import (
    Conditions,
    ForecastResponse,
    HourlyForecast,
    PollenForecast,
    SlotConditions,
)
from mouvair_api.open_meteo import SLOT_COUNT, SPORTS, OpenMeteoError, _next_full_hour
from mouvair_api.scoring import calculate_score


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


class SequencedFetcher:
    def __init__(self, *outcomes: ForecastResponse | OpenMeteoError) -> None:
        self.outcomes = outcomes
        self.calls = 0

    async def __call__(self, latitude: float, longitude: float) -> ForecastResponse:
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, OpenMeteoError):
            raise outcome
        return outcome


def forecast_at(
    retrieved_at: datetime,
    *,
    latitude: float = 45.75,
    longitude: float = 4.85,
) -> ForecastResponse:
    first_start = _next_full_hour(retrieved_at)
    hours = []
    for offset in range(SLOT_COUNT):
        start = first_start + timedelta(hours=offset)
        end = start + timedelta(hours=1)
        score_conditions = Conditions(
            apparent_temperature=15,
            precipitation=0,
            wind_speed=5,
            wind_gusts=10,
            european_aqi=20,
            weather_codes=[1, 1],
        )
        hours.append(
            HourlyForecast(
                time=start.isoformat(timespec="minutes"),
                end_time=end.isoformat(timespec="minutes"),
                conditions=SlotConditions(
                    apparent_temperature=15,
                    apparent_temperature_start=15,
                    apparent_temperature_end=15,
                    precipitation=0,
                    wind_speed=5,
                    wind_gusts=10,
                    european_aqi=20,
                    weather_code=1,
                    weather_codes=[1, 1],
                ),
                pollen=PollenForecast(),
                scores={sport: calculate_score(score_conditions, sport) for sport in SPORTS},
            )
        )
    return ForecastResponse(
        latitude=latitude,
        longitude=longitude,
        timezone="UTC",
        generated_at=retrieved_at.isoformat(timespec="seconds"),
        hours=hours,
    )


def test_complete_forecast_is_reused_through_fifteen_minutes() -> None:
    retrieved_at = datetime(2026, 9, 8, 12, 5, tzinfo=UTC)
    clock = MutableClock(retrieved_at)
    fetcher = SequencedFetcher(forecast_at(retrieved_at))
    service = ForecastService(fetcher, clock=clock)

    async def scenario() -> tuple[ForecastResponse, ForecastResponse]:
        direct = await service.get_forecast(45.75, 4.85)
        clock.value += timedelta(minutes=15)
        cached = await service.get_forecast(45.75, 4.85)
        return direct, cached

    direct, cached = asyncio.run(scenario())

    assert fetcher.calls == 1
    assert direct.from_cache is False
    assert cached.from_cache is True
    assert cached.data_state == "fresh"
    assert cached.generated_at == direct.generated_at
    assert cached.recommendation_available is True
    assert len(cached.hours) == SLOT_COUNT


def test_cache_older_than_fifteen_minutes_is_refreshed() -> None:
    first_retrieval = datetime(2026, 9, 8, 12, 5, tzinfo=UTC)
    second_retrieval = first_retrieval + timedelta(minutes=16)
    clock = MutableClock(first_retrieval)
    fetcher = SequencedFetcher(
        forecast_at(first_retrieval),
        forecast_at(second_retrieval),
    )
    service = ForecastService(fetcher, clock=clock)

    async def scenario() -> ForecastResponse:
        await service.get_forecast(45.75, 4.85)
        clock.value = second_retrieval
        return await service.get_forecast(45.75, 4.85)

    refreshed = asyncio.run(scenario())

    assert fetcher.calls == 2
    assert refreshed.from_cache is False
    assert refreshed.data_state == "fresh"
    assert refreshed.generated_at == second_retrieval.isoformat(timespec="seconds")


def test_failed_refresh_keeps_recent_slots_and_fills_the_missing_hour() -> None:
    retrieved_at = datetime(2026, 9, 8, 12, 50, tzinfo=UTC)
    clock = MutableClock(retrieved_at)
    fetcher = SequencedFetcher(
        forecast_at(retrieved_at),
        OpenMeteoError("indisponible"),
    )
    service = ForecastService(fetcher, clock=clock)

    async def scenario() -> ForecastResponse:
        await service.get_forecast(45.75, 4.85)
        clock.value = datetime(2026, 9, 8, 13, 5, tzinfo=UTC)
        return await service.get_forecast(45.75, 4.85)

    fallback = asyncio.run(scenario())

    assert fetcher.calls == 2
    assert fallback.data_state == "refresh_failed"
    assert fallback.from_cache is True
    assert fallback.recommendation_available is True
    assert len(fallback.hours) == SLOT_COUNT
    assert fallback.hours[0].time == "2026-09-08T14:00+00:00"
    assert fallback.hours[-1].conditions.apparent_temperature is None
    assert fallback.hours[-1].scores["walking"].overall.status == "incomplet"
    assert fallback.generated_at == retrieved_at.isoformat(timespec="seconds")


@pytest.mark.parametrize(
    ("age", "expected_state", "recommendation_available"),
    [
        (timedelta(hours=1), "refresh_failed", True),
        (timedelta(hours=1, seconds=1), "stale", False),
    ],
)
def test_failed_refresh_applies_the_one_hour_threshold(
    age: timedelta,
    expected_state: str,
    recommendation_available: bool,
) -> None:
    retrieved_at = datetime(2026, 9, 8, 12, 5, tzinfo=UTC)
    clock = MutableClock(retrieved_at)
    fetcher = SequencedFetcher(
        forecast_at(retrieved_at),
        OpenMeteoError("indisponible"),
    )
    service = ForecastService(fetcher, clock=clock)

    async def scenario() -> ForecastResponse:
        await service.get_forecast(45.75, 4.85)
        clock.value = retrieved_at + age
        return await service.get_forecast(45.75, 4.85)

    fallback = asyncio.run(scenario())

    assert fallback.data_state == expected_state
    assert fallback.from_cache is True
    assert fallback.recommendation_available is recommendation_available
    assert fallback.generated_at == retrieved_at.isoformat(timespec="seconds")


def test_provider_error_without_cached_data_is_propagated() -> None:
    moment = datetime(2026, 9, 8, 12, 5, tzinfo=UTC)
    service = ForecastService(
        SequencedFetcher(OpenMeteoError("indisponible")),
        clock=MutableClock(moment),
    )

    with pytest.raises(OpenMeteoError, match="indisponible"):
        asyncio.run(service.get_forecast(45.75, 4.85))


def test_simultaneous_requests_share_one_provider_call() -> None:
    moment = datetime(2026, 9, 8, 12, 5, tzinfo=UTC)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def fetcher(latitude: float, longitude: float) -> ForecastResponse:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return forecast_at(moment)

    service = ForecastService(fetcher, clock=MutableClock(moment))

    async def scenario() -> tuple[ForecastResponse, ForecastResponse]:
        first = asyncio.create_task(service.get_forecast(45.75, 4.85))
        await started.wait()
        second = asyncio.create_task(service.get_forecast(45.75, 4.85))
        await asyncio.sleep(0)
        release.set()
        return await asyncio.gather(first, second)

    first, second = asyncio.run(scenario())

    assert calls == 1
    assert first.generated_at == second.generated_at


def test_cache_discards_the_least_recently_used_position() -> None:
    moment = datetime(2026, 9, 8, 12, 5, tzinfo=UTC)

    async def fetcher(latitude: float, longitude: float) -> ForecastResponse:
        return forecast_at(moment, latitude=latitude, longitude=longitude)

    service = ForecastService(fetcher, clock=MutableClock(moment), max_entries=2)

    async def scenario() -> None:
        await service.get_forecast(1, 1)
        await service.get_forecast(2, 2)
        await service.get_forecast(1, 1)
        await service.get_forecast(3, 3)

    asyncio.run(scenario())

    assert list(service._cache) == [(1, 1), (3, 3)]
