"""Vérifie le contrat HTTP avec le cycle de vie réel de l’application FastAPI.

TestClient démarre et arrête les ressources dans son bloc with. Les services
remplacés dans app.state isolent les traductions HTTP du réseau fournisseur ;
les configurations sans Redis exercent les modes local et closed.
"""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from mouvair_api.config import Settings
from mouvair_api.forecast_service import ForecastService
from mouvair_api.forecast_slots import assemble_forecast
from mouvair_api.main import create_app
from mouvair_api.models import ForecastResponse, Location
from mouvair_api.open_meteo import OpenMeteoQuotaError


class StubGeocoding:
    """Service prévisible pour tester une ville ou une erreur de quota au niveau HTTP."""
    def __init__(self, error: OpenMeteoQuotaError | None = None) -> None:
        self.error = error

    async def search(self, query: str, count: int) -> list[Location]:
        """Ignore la recherche et produit uniquement l’issue configurée par le test."""
        del query, count
        if self.error is not None:
            raise self.error
        return [Location(id=1, name="Lyon", latitude=45.75, longitude=4.85)]


def test_health_reports_degraded_without_redis() -> None:
    """L’API répond toujours 200, avec un diagnostic Redis explicite dans son corps JSON."""
    with TestClient(create_app(Settings(redis_failure_mode="local"))) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "degraded", "redis": "unavailable"}


def test_location_search_requires_three_characters() -> None:
    """La validation FastAPI doit refuser une saisie trop courte avant le service de recherche."""
    with TestClient(create_app(Settings(redis_failure_mode="local"))) as client:
        response = client.get("/api/locations", params={"q": "Ly"})

    assert response.status_code == 422


def test_provider_calls_fail_closed_when_redis_is_not_configured() -> None:
    """Sans protection partagée, le mode closed retourne 503 plutôt que d’appeler le fournisseur."""
    with TestClient(create_app(Settings(redis_failure_mode="closed"))) as client:
        response = client.get("/api/locations", params={"q": "Lyon"})

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert "protection du quota" in response.json()["detail"]


def test_location_search_returns_a_quota_503_with_retry_after() -> None:
    """La route conserve le message et le délai d’une erreur de budget fournie par le service."""
    app = create_app(Settings(redis_failure_mode="local"))
    with TestClient(app) as client:
        app.state.geocoding_service = StubGeocoding(OpenMeteoQuotaError("limite", 42))
        response = client.get("/api/locations", params={"q": "Lyon"})

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "42"
    assert response.json() == {"detail": "limite"}


def test_location_client_limit_returns_429_when_the_same_client_changes_city() -> None:
    """Changer la ville conserve le budget ; le navigateur peut lire le refus et son délai."""
    settings = Settings(
        redis_failure_mode="local",
        location_rate_limit_minute=1,
        location_rate_limit_hour=1,
    )
    app = create_app(settings)
    origin = {"Origin": "http://localhost:5173"}
    with TestClient(app) as client:
        app.state.geocoding_service = StubGeocoding()
        first = client.get("/api/locations", params={"q": "Lyon"}, headers=origin)
        limited = client.get("/api/locations", params={"q": "Paris"}, headers=origin)

    assert first.status_code == 200
    assert limited.status_code == 429
    assert limited.headers["Access-Control-Allow-Origin"] == origin["Origin"]
    assert "Retry-After" in limited.headers["Access-Control-Expose-Headers"]
    assert int(limited.headers["Retry-After"]) > 0


@pytest.mark.parametrize("endpoint", ["/api/locations", "/api/forecast"])
@pytest.mark.parametrize("origin", [None, "https://untrusted.example"])
def test_client_limit_does_not_grant_cors_to_an_unlisted_or_missing_origin(
    endpoint: str,
    origin: str | None,
) -> None:
    """Le traitement des 429 respecte la liste des origines autorisées sur les deux routes."""
    settings = Settings(
        redis_failure_mode="local",
        location_rate_limit_minute=1,
        location_rate_limit_hour=1,
        forecast_rate_limit_minute=1,
        forecast_rate_limit_hour=1,
    )
    headers = {"Origin": origin} if origin is not None else {}
    with TestClient(create_app(settings)) as client:
        # Les paramètres manquants consomment le budget sans appeler le fournisseur.
        first = client.get(endpoint, headers=headers)
        limited = client.get(endpoint, headers=headers)

    assert first.status_code == 422
    assert limited.status_code == 429
    assert "Access-Control-Allow-Origin" not in limited.headers
    assert int(limited.headers["Retry-After"]) > 0


def test_forecast_assembly_error_returns_502() -> None:
    """Une erreur d’assemblage traverse le service et conserve sa traduction HTTP 502."""
    async def invalid_forecast(latitude: float, longitude: float) -> ForecastResponse:
        """Simule des réponses fournisseur vides sans effectuer de requête réseau."""
        del latitude, longitude
        return assemble_forecast({}, {}, datetime(2026, 9, 8, 12, tzinfo=UTC))

    app = create_app(Settings(redis_failure_mode="local"))
    with TestClient(app) as client:
        app.state.forecast_service = ForecastService(invalid_forecast)
        response = client.get("/api/forecast", params={"latitude": 45.75, "longitude": 4.85})

    assert response.status_code == 502
    assert response.json() == {"detail": "Fuseau horaire absent de la réponse météo"}
