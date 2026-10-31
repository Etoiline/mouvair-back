"""Teste la politique de prévision sans serveur Redis ni fournisseur externe.

Une horloge mutable fait franchir les seuils de fraîcheur sans attente réelle.
Des fetchers séquencés simulent succès et pannes ; le véritable cache L2 utilise
un stockage mémoire partagé pour vérifier les échanges entre deux instances.
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from mouvair_api.config import Settings
from mouvair_api.errors import DataUnavailableError
from mouvair_api.forecast_cache import RedisForecastCache
from mouvair_api.forecast_service import ForecastService
from mouvair_api.forecast_slots import SLOT_COUNT, SPORTS, next_full_hour
from mouvair_api.models import (
    Conditions,
    ForecastResponse,
    HourlyForecast,
    PollenForecast,
    SlotConditions,
)
from mouvair_api.redis_store import RedisStore
from mouvair_api.scoring import calculate_score


@dataclass
class MutableClock:
    """Horloge injectée que chaque scénario avance jusqu’à une frontière précise."""
    value: datetime

    def __call__(self) -> datetime:
        """Expose l’instant courant au service avec la même interface que l’horloge réelle."""
        return self.value


class SequencedFetcher:
    """Fournisseur déterministe renvoyant successivement des prévisions ou des erreurs."""

    def __init__(self, *outcomes: ForecastResponse | DataUnavailableError) -> None:
        """Conserve les issues et compte les vrais rafraîchissements demandés."""
        self.outcomes = outcomes
        self.calls = 0

    async def __call__(self, latitude: float, longitude: float) -> ForecastResponse:
        """Consomme une issue par appel pour révéler un rafraîchissement superflu."""
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, DataUnavailableError):
            raise outcome
        return outcome


def forecast_at(
    retrieved_at: datetime,
    *,
    latitude: float = 45.75,
    longitude: float = 4.85,
    timezone_name: str = "UTC",
) -> ForecastResponse:
    """Construit 24 créneaux cohérents à la date donnée, avec leurs vraies notes.

    Les mesures constantes isolent les tests de cache : seuls le temps, les
    coordonnées, le fuseau et les résultats du fournisseur varient selon le test.
    """
    timezone = ZoneInfo(timezone_name)
    first_start = next_full_hour(retrieved_at, timezone)
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
                time=start.astimezone(timezone).isoformat(timespec="minutes"),
                end_time=end.astimezone(timezone).isoformat(timespec="minutes"),
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
        timezone=timezone_name,
        generated_at=retrieved_at.astimezone(timezone).isoformat(timespec="seconds"),
        hours=hours,
    )


def test_complete_forecast_is_reused_through_fifteen_minutes() -> None:
    """À exactement 15 minutes, une fenêtre couverte reste fraîche et garde generated_at."""
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
    """À 16 minutes, une nouvelle récupération remplace la date et les marqueurs du cache."""
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


def test_fractional_timezone_cache_reuses_the_local_hour_window() -> None:
    """La vérification de couverture suit l’heure locale même avec un décalage de 30 minutes."""
    retrieved_at = datetime(2026, 9, 8, 13, 10, tzinfo=UTC)
    clock = MutableClock(retrieved_at)
    fetcher = SequencedFetcher(forecast_at(retrieved_at, timezone_name="Asia/Kolkata"))
    service = ForecastService(fetcher, clock=clock)

    async def scenario() -> ForecastResponse:
        await service.get_forecast(28.61, 77.21)
        clock.value += timedelta(minutes=5)
        return await service.get_forecast(28.61, 77.21)

    cached = asyncio.run(scenario())

    assert fetcher.calls == 1
    assert cached.from_cache is True
    assert cached.hours[0].time == "2026-09-08T19:00+05:30"


def test_failed_refresh_keeps_recent_slots_and_fills_the_missing_hour() -> None:
    """Après le changement d’heure de départ, le repli conserve 24 places dont la fin inconnue."""
    retrieved_at = datetime(2026, 9, 8, 12, 50, tzinfo=UTC)
    clock = MutableClock(retrieved_at)
    fetcher = SequencedFetcher(
        forecast_at(retrieved_at),
        DataUnavailableError("indisponible"),
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
    """L’âge d’une heure autorise encore le meilleur créneau ; une seconde de plus le suspend."""
    retrieved_at = datetime(2026, 9, 8, 12, 5, tzinfo=UTC)
    clock = MutableClock(retrieved_at)
    fetcher = SequencedFetcher(
        forecast_at(retrieved_at),
        DataUnavailableError("indisponible"),
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
    """Sans historique disponible, le service doit laisser la route présenter l’erreur."""
    moment = datetime(2026, 9, 8, 12, 5, tzinfo=UTC)
    service = ForecastService(
        SequencedFetcher(DataUnavailableError("indisponible")),
        clock=MutableClock(moment),
    )

    with pytest.raises(DataUnavailableError, match="indisponible"):
        asyncio.run(service.get_forecast(45.75, 4.85))


def test_simultaneous_requests_share_one_provider_call() -> None:
    """Deux demandeurs en attente doivent partager un unique fetcher encore inachevé."""
    moment = datetime(2026, 9, 8, 12, 5, tzinfo=UTC)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def fetcher(latitude: float, longitude: float) -> ForecastResponse:
        """Maintient la récupération ouverte jusqu’à l’arrivée du second demandeur."""
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
        # Cède un tour de boucle pour laisser la seconde requête rejoindre la tâche partagée.
        await asyncio.sleep(0)
        release.set()
        return await asyncio.gather(first, second)

    first, second = asyncio.run(scenario())

    assert calls == 1
    assert first.generated_at == second.generated_at


def test_cache_discards_the_least_recently_used_position() -> None:
    """Relire la première position protège son entrée : c’est la deuxième qui doit sortir."""
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


@pytest.mark.parametrize(
    ("provider_latitude", "provider_longitude"),
    [(45.75, 4.85), (45.76, 4.86)],
)
def test_two_instances_share_a_forecast_through_the_distributed_cache(
    memory_redis_store: RedisStore,
    provider_latitude: float,
    provider_longitude: float,
) -> None:
    """Le L2 reste partagé si les coordonnées fournisseur diffèrent de celles demandées."""
    moment = datetime(2026, 9, 8, 12, 5, tzinfo=UTC)
    settings = Settings()
    calls = 0

    async def fetcher(latitude: float, longitude: float) -> ForecastResponse:
        nonlocal calls
        calls += 1
        assert (latitude, longitude) == (45.75, 4.85)
        return forecast_at(moment, latitude=provider_latitude, longitude=provider_longitude)

    first = ForecastService(
        fetcher,
        clock=MutableClock(moment),
        shared_cache=RedisForecastCache(memory_redis_store, settings),
    )
    second = ForecastService(
        fetcher,
        clock=MutableClock(moment),
        shared_cache=RedisForecastCache(memory_redis_store, settings),
    )

    async def scenario() -> tuple[ForecastResponse, ForecastResponse]:
        direct = await first.get_forecast(45.75, 4.85)
        cached = await second.get_forecast(45.75, 4.85)
        return direct, cached

    direct, cached = asyncio.run(scenario())

    assert calls == 1
    assert direct.from_cache is False
    assert cached.from_cache is True
    assert cached.generated_at == direct.generated_at
    assert (cached.latitude, cached.longitude) == (provider_latitude, provider_longitude)
