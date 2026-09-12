from fastapi.testclient import TestClient

from mouvair_api.config import Settings
from mouvair_api.main import create_app
from mouvair_api.models import Location
from mouvair_api.open_meteo import OpenMeteoQuotaError


class StubGeocoding:
    def __init__(self, error: OpenMeteoQuotaError | None = None) -> None:
        self.error = error

    async def search(self, query: str, count: int) -> list[Location]:
        del query, count
        if self.error is not None:
            raise self.error
        return [Location(id=1, name="Lyon", latitude=45.75, longitude=4.85)]


def test_health_reports_degraded_without_redis() -> None:
    with TestClient(create_app(Settings(redis_failure_mode="local"))) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "degraded", "redis": "unavailable"}


def test_location_search_requires_three_characters() -> None:
    with TestClient(create_app(Settings(redis_failure_mode="local"))) as client:
        response = client.get("/api/locations", params={"q": "Ly"})

    assert response.status_code == 422


def test_provider_calls_fail_closed_when_redis_is_not_configured() -> None:
    with TestClient(create_app(Settings(redis_failure_mode="closed"))) as client:
        response = client.get("/api/locations", params={"q": "Lyon"})

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert "protection du quota" in response.json()["detail"]


def test_location_search_returns_a_quota_503_with_retry_after() -> None:
    app = create_app(Settings(redis_failure_mode="local"))
    with TestClient(app) as client:
        app.state.geocoding_service = StubGeocoding(OpenMeteoQuotaError("limite", 42))
        response = client.get("/api/locations", params={"q": "Lyon"})

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "42"
    assert response.json() == {"detail": "limite"}


def test_location_client_limit_returns_429_and_keeps_clients_separate() -> None:
    settings = Settings(
        redis_failure_mode="local",
        location_rate_limit_minute=1,
        location_rate_limit_hour=1,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.geocoding_service = StubGeocoding()
        first = client.get("/api/locations", params={"q": "Lyon"})
        limited = client.get("/api/locations", params={"q": "Paris"})

    assert first.status_code == 200
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) > 0
