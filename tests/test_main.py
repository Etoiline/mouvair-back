"""Vérifie le contrat HTTP avec le cycle de vie réel de l’application FastAPI.

TestClient démarre et arrête les ressources dans son bloc with. Les doubles
de géocodage sont installés dans app.state après le démarrage. Les tests de
l’adaptateur configurent le transport sortant avant l’ouverture du client.
Les configurations sans Redis exercent les modes local et closed.
"""

from datetime import UTC, datetime
from functools import partial
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import mouvair_api.main as api_main
from mouvair_api.config import Settings
from mouvair_api.errors import DataUnavailableError
from mouvair_api.forecast_service import ForecastService
from mouvair_api.forecast_slots import assemble_forecast
from mouvair_api.main import create_app
from mouvair_api.models import ForecastResponse, Location
from mouvair_api.open_meteo import GEOCODING_URL, OpenMeteoError, OpenMeteoQuotaError

LYON_LOCATION_JSON = {
    "id": 1,
    "name": "Lyon",
    "latitude": 45.75,
    "longitude": 4.85,
    "country": "France",
    "admin1": "Auvergne-Rhône-Alpes",
    "timezone": "Europe/Paris",
}


class StubGeocoding:
    """Fournit un résultat de recherche ou une erreur de disponibilité au niveau HTTP.

    Par défaut, le double renvoie Lyon avec ses seuls champs obligatoires.
    Une liste vide représente une recherche réussie sans ville correspondante.
    """

    def __init__(
        self,
        error: DataUnavailableError | None = None,
        *,
        locations: list[Location] | None = None,
    ) -> None:
        self.error = error
        self.locations = (
            [Location(id=1, name="Lyon", latitude=45.75, longitude=4.85)]
            if locations is None
            else locations
        )

    async def search(self, query: str, count: int) -> list[Location]:
        """Ignore la recherche et produit uniquement l’issue configurée par le test."""
        del query, count
        if self.error is not None:
            raise self.error
        return self.locations


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


def test_location_search_returns_matching_city_as_json() -> None:
    """Les coordonnées sont présentes et les champs facultatifs absents valent null dans le JSON."""
    app = create_app(Settings(redis_failure_mode="local"))
    with TestClient(app) as client:
        app.state.geocoding_service = StubGeocoding()
        response = client.get("/api/locations", params={"q": "Lyon"})

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": 1,
            "name": "Lyon",
            "latitude": 45.75,
            "longitude": 4.85,
            "country": None,
            "admin1": None,
            "timezone": None,
        }
    ]


def test_location_search_returns_empty_list_without_matches() -> None:
    """Un tableau vide permet au frontend de traiter la recherche comme un succès sans résultat."""
    app = create_app(Settings(redis_failure_mode="local"))
    with TestClient(app) as client:
        app.state.geocoding_service = StubGeocoding(locations=[])
        response = client.get("/api/locations", params={"q": "Ville introuvable"})

    assert response.status_code == 200
    assert response.json() == []


def test_location_search_returns_502_when_provider_is_unavailable() -> None:
    """La route traduit l’indisponibilité en HTTP 502 et conserve le message dans detail."""
    message = "Open-Meteo est momentanément indisponible"
    app = create_app(Settings(redis_failure_mode="local"))
    with TestClient(app) as client:
        app.state.geocoding_service = StubGeocoding(OpenMeteoError(message))
        response = client.get("/api/locations", params={"q": "Lyon"})

    assert response.status_code == 502
    assert response.json() == {"detail": message}


@pytest.mark.parametrize(
    ("results", "expected_status", "expected_json"),
    [
        pytest.param(
            [{**LYON_LOCATION_JSON, "population": 500_000}],
            200,
            [LYON_LOCATION_JSON],
            id="valid-location",
        ),
        pytest.param(
            {"name": "Lyon"},
            502,
            {"detail": "Réponse Open-Meteo invalide"},
            id="invalid-results",
        ),
        pytest.param(
            [{"id": 1, "name": "Lyon"}],
            502,
            {"detail": "Réponse Open-Meteo invalide"},
            id="missing-coordinates",
        ),
    ],
)
def test_location_search_translates_open_meteo_response(
    monkeypatch: pytest.MonkeyPatch,
    results: object,
    expected_status: int,
    expected_json: object,
) -> None:
    """Exerce l’adaptateur Open-Meteo depuis la route de recherche de villes.

    La population sert à vérifier le filtrage des champs hors contrat. Le fournisseur répond
    toujours 200, de sorte que les 502 proviennent de la validation du contenu.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url).split("?", 1)[0] == GEOCODING_URL
        assert dict(request.url.params) == {
            "name": "Lyon",
            "count": "3",
            "language": "fr",
            "format": "json",
        }
        return httpx.Response(200, json={"results": results})

    # Le remplacement dans main réserve MockTransport au client Open-Meteo créé au démarrage.
    provider_client_factory = partial(httpx.AsyncClient, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(api_main, "httpx", SimpleNamespace(AsyncClient=provider_client_factory))
    with TestClient(create_app(Settings(redis_failure_mode="local"))) as client:
        response = client.get("/api/locations", params={"q": "Lyon", "count": 3})

    assert response.status_code == expected_status
    assert response.json() == expected_json


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
