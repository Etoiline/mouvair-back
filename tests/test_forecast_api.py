"""Vérifie le contrat de prévision à travers FastAPI, le service et le cache L2.

Les séries fournisseur sont assemblées normalement ; seul leur chargement est
simulé. L’horloge contrôlée permet de vérifier le JSON reçu par le frontend,
les états de repli et les limites HTTP sans appeler Open-Meteo.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from forecast_fixtures import MutableClock, SequencedFetcher
from forecast_payloads import air_payload, weather_payload
from mouvair_api.config import Settings
from mouvair_api.forecast_cache import RedisForecastCache
from mouvair_api.forecast_service import ForecastService
from mouvair_api.forecast_slots import assemble_forecast
from mouvair_api.main import create_app
from mouvair_api.open_meteo import OpenMeteoQuotaError
from mouvair_api.redis_store import RedisStore


@pytest.fixture
def forecast_clock() -> MutableClock:
    """Fixe un instant éloigné d’un changement d’heure pour isoler le contrat HTTP."""
    return MutableClock(datetime(2026, 9, 8, 12, 5, tzinfo=UTC))


@pytest.fixture
def forecast_fetcher(forecast_clock: MutableClock) -> SequencedFetcher:
    """Prépare une réponse calculée, puis un refus de quota pour tester le repli."""
    start = forecast_clock.value.replace(minute=0)
    weather = weather_payload(start)
    # Les métadonnées fournisseur ne doivent pas déplacer l’identité du cache HTTP.
    weather.update(latitude=45.76, longitude=4.86)
    response = assemble_forecast(weather, air_payload(start), forecast_clock.value)
    return SequencedFetcher(response, OpenMeteoQuotaError("quota épuisé", 42))


@pytest.fixture
def forecast_client(
    forecast_clock: MutableClock,
    forecast_fetcher: SequencedFetcher,
    memory_redis_store: RedisStore,
) -> Iterator[TestClient]:
    """Conserve le vrai middleware et injecte un service muni du véritable adaptateur L2."""
    settings = Settings(redis_failure_mode="local")
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.forecast_service = ForecastService(
            forecast_fetcher,
            clock=forecast_clock,
            shared_cache=RedisForecastCache(memory_redis_store, settings),
        )
        yield client


def test_forecast_returns_twenty_four_scored_slots_and_provider_metadata(
    forecast_client: TestClient,
    forecast_fetcher: SequencedFetcher,
) -> None:
    """Le JSON public contient les mesures, les intervalles et les quatre familles de notes."""
    response = forecast_client.get(
        "/api/forecast", params={"latitude": 45.75, "longitude": 4.85}
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    body = response.json()
    assert set(body) == {
        "latitude", "longitude", "timezone", "generated_at", "hours",
        "data_state", "from_cache", "recommendation_available",
    }
    assert (body["latitude"], body["longitude"]) == (45.76, 4.86)
    assert body["timezone"] == "Europe/Paris"
    assert body["generated_at"] == "2026-09-08T14:05:00+02:00"
    assert body["data_state"] == "fresh"
    assert body["from_cache"] is False
    assert body["recommendation_available"] is True
    assert len(body["hours"]) == 24
    assert body["hours"][0]["time"] == "2026-09-08T15:00+02:00"
    for index, hour in enumerate(body["hours"]):
        start = datetime.fromisoformat(hour["time"])
        end = datetime.fromisoformat(hour["end_time"])
        assert end - start == timedelta(hours=1)
        if index > 0:
            assert hour["time"] == body["hours"][index - 1]["end_time"]
        assert set(hour["scores"]) == {"easy_run", "hard_run", "cycling", "walking"}
        assert hour["conditions"]["apparent_temperature"] == 15
        assert set(hour["pollen"]) == {"alder", "birch", "grass", "mugwort", "olive", "ragweed"}
        for sport, scores in hour["scores"].items():
            assert scores["sport"] == sport
            assert scores["label"]
            assert scores["weather"]["score"] == 100
            assert scores["air"]["score"] == 80
            assert 0 <= scores["overall"]["score"] <= 100
    assert body["hours"][0]["scores"]["easy_run"]["overall"]["score"] == 93
    assert forecast_fetcher.calls == 1


def test_forecast_reuses_the_requested_position_without_changing_generation_time(
    forecast_client: TestClient,
    forecast_clock: MutableClock,
    forecast_fetcher: SequencedFetcher,
) -> None:
    """Une seconde requête sert le cache malgré des coordonnées fournisseur différentes."""
    params = {"latitude": 45.75, "longitude": 4.85}
    first = forecast_client.get("/api/forecast", params=params)
    forecast_clock.value += timedelta(minutes=15)
    second = forecast_client.get("/api/forecast", params=params)

    assert first.status_code == second.status_code == 200
    assert first.json()["from_cache"] is False
    assert second.json() == {**first.json(), "from_cache": True}
    assert forecast_fetcher.calls == 1


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"longitude": 4.85},
        {"latitude": 45.75},
        {"latitude": -90.01, "longitude": 4.85},
        {"latitude": 90.01, "longitude": 4.85},
        {"latitude": 45.75, "longitude": -180.01},
        {"latitude": 45.75, "longitude": 180.01},
        {"latitude": "Lyon", "longitude": 4.85},
        {"latitude": "nan", "longitude": 4.85},
        {"latitude": 45.75, "longitude": "inf"},
    ],
)
def test_forecast_rejects_invalid_coordinates_before_calling_the_provider(
    forecast_client: TestClient,
    forecast_fetcher: SequencedFetcher,
    params: dict[str, float | str],
) -> None:
    """Les coordonnées manquantes, hors limites ou non finies ne déclenchent aucun fetch."""
    response = forecast_client.get("/api/forecast", params=params)

    assert response.status_code == 422
    assert response.json()["detail"]
    assert forecast_fetcher.calls == 0


@pytest.mark.parametrize(("latitude", "longitude"), [(-90, -180), (90, 180)])
def test_forecast_accepts_coordinate_boundaries(
    forecast_client: TestClient, latitude: float, longitude: float
) -> None:
    """Les bornes géographiques elles-mêmes appartiennent au domaine accepté par la route."""
    response = forecast_client.get(
        "/api/forecast", params={"latitude": latitude, "longitude": longitude}
    )

    assert response.status_code == 200


def test_forecast_quota_error_without_cache_preserves_retry_after(
    forecast_client: TestClient,
    forecast_fetcher: SequencedFetcher,
) -> None:
    """Sans historique, le refus fournisseur traverse le service et devient un HTTP 503."""
    forecast_fetcher.outcomes = (OpenMeteoQuotaError("quota épuisé", 42),)

    response = forecast_client.get(
        "/api/forecast", params={"latitude": 45.75, "longitude": 4.85}
    )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "42"
    assert response.json() == {"detail": "quota épuisé"}
    assert forecast_fetcher.calls == 1


@pytest.mark.parametrize(
    ("age", "state", "recommendation_available"),
    [(timedelta(minutes=16), "refresh_failed", True), (timedelta(minutes=61), "stale", False)],
)
def test_forecast_exposes_failed_refresh_and_stale_states_in_json(
    forecast_client: TestClient,
    forecast_clock: MutableClock,
    forecast_fetcher: SequencedFetcher,
    age: timedelta,
    state: str,
    recommendation_available: bool,
) -> None:
    """Le frontend reçoit l’âge réel et les marqueurs de repli malgré un quota épuisé."""
    params = {"latitude": 45.75, "longitude": 4.85}
    first = forecast_client.get("/api/forecast", params=params)
    forecast_clock.value += age
    fallback = forecast_client.get("/api/forecast", params=params)

    assert first.status_code == fallback.status_code == 200
    body = fallback.json()
    assert body["generated_at"] == first.json()["generated_at"]
    assert body["from_cache"] is True
    assert body["data_state"] == state
    assert body["recommendation_available"] is recommendation_available
    assert len(body["hours"]) == 24
    if state == "stale":
        assert body["hours"][-1]["conditions"]["apparent_temperature"] is None
        assert body["hours"][-1]["scores"]["walking"]["overall"] == {
            "score": None, "status": "incomplet",
        }
    assert forecast_fetcher.calls == 2


def test_forecast_missing_air_measurements_remain_null_in_http_response(
    forecast_client: TestClient,
    forecast_clock: MutableClock,
    forecast_fetcher: SequencedFetcher,
) -> None:
    """Une mesure manquante reste null et ne devient pas une note favorable à la sérialisation."""
    start = forecast_clock.value.replace(minute=0)
    air = air_payload(start)
    air["hourly"]["european_aqi"][1] = None
    partial = assemble_forecast(weather_payload(start), air, forecast_clock.value)
    forecast_fetcher.outcomes = (partial,)

    response = forecast_client.get(
        "/api/forecast", params={"latitude": 45.75, "longitude": 4.85}
    )

    assert response.status_code == 200
    first = response.json()["hours"][0]
    assert first["conditions"]["european_aqi"] is None
    for scores in first["scores"].values():
        assert scores["air"]["score"] is None
        assert scores["overall"] == {"score": None, "status": "incomplet"}


def test_forecast_client_limit_applies_to_cache_hits_and_allows_cors_preflight(
    forecast_clock: MutableClock,
    forecast_fetcher: SequencedFetcher,
) -> None:
    """Les OPTIONS ne consomment pas le budget ; un GET répété est limité même sur le cache."""
    settings = Settings(
        redis_failure_mode="local", forecast_rate_limit_minute=1, forecast_rate_limit_hour=1,
    )
    app = create_app(settings)
    params = {"latitude": 45.75, "longitude": 4.85}
    origin = {"Origin": "http://localhost:5173"}
    with TestClient(app) as client:
        app.state.forecast_service = ForecastService(forecast_fetcher, clock=forecast_clock)
        for _ in range(2):
            preflight = client.options(
                "/api/forecast", headers={**origin, "Access-Control-Request-Method": "GET"}
            )
            assert preflight.status_code == 200
            assert preflight.headers["Access-Control-Allow-Origin"] == origin["Origin"]
        first = client.get("/api/forecast", params=params, headers=origin)
        limited = client.get("/api/forecast", params=params, headers=origin)

    assert first.status_code == 200
    assert first.headers["Access-Control-Allow-Origin"] == origin["Origin"]
    assert limited.status_code == 429
    assert limited.headers["Access-Control-Allow-Origin"] == origin["Origin"]
    assert "Retry-After" in limited.headers["Access-Control-Expose-Headers"]
    assert "Origin" in limited.headers["Vary"]
    assert int(limited.headers["Retry-After"]) > 0
    assert limited.json() == {"detail": "Trop de requêtes, veuillez réessayer plus tard"}
    assert forecast_fetcher.calls == 1
