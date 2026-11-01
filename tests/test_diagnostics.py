"""Vérifie les événements utiles, leur corrélation et leur contenu réellement journalisé.

Les requêtes concurrentes doivent conserver des identifiants distincts. Les
scénarios contrôlent les unités de durée, les sources de données, les motifs de
repli et l’absence de paramètres ou de messages d’exception dans les événements.
"""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from forecast_fixtures import MutableClock, SequencedFetcher
from forecast_payloads import air_payload, weather_payload
from mouvair_api import diagnostics
from mouvair_api.config import Settings
from mouvair_api.diagnostics import (
    JsonDiagnosticFormatter,
    RequestDiagnosticsMiddleware,
    configure_logging,
    error_fields,
    log_event,
    request_id,
)
from mouvair_api.forecast_cache import RedisForecastCache
from mouvair_api.forecast_service import ForecastService
from mouvair_api.forecast_slots import assemble_forecast
from mouvair_api.geocoding_service import GeocodingService
from mouvair_api.main import create_app
from mouvair_api.models import Location
from mouvair_api.open_meteo import OpenMeteoError, OpenMeteoQuotaError, search_locations
from mouvair_api.redis_store import RedisStore


def events(caplog: pytest.LogCaptureFixture, event: str) -> list[dict]:
    """Extrait les champs structurés en conservant l’ordre des événements du backend."""
    return [
        record.diagnostic_fields for record in caplog.records
        if record.name == "mouvair_api" and record.getMessage() == event
    ]


def test_logging_configuration_is_idempotent_and_leaves_root_handlers_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plusieurs cycles de vie FastAPI ne doivent pas dupliquer les lignes de journal."""
    root_handlers = list(logging.getLogger().handlers)
    monkeypatch.setattr(diagnostics.logger, "handlers", [])
    monkeypatch.setattr(diagnostics.logger, "level", logging.NOTSET)
    configure_logging()
    try:
        first_handler = diagnostics.logger.handlers[0]
        configure_logging()
        assert diagnostics.logger.handlers == [first_handler]
        assert isinstance(first_handler.formatter, JsonDiagnosticFormatter)
        assert diagnostics.logger.level == logging.INFO
        assert logging.getLogger().handlers == root_handlers
    finally:
        diagnostics.logger.handlers[0].close()


def test_logging_configuration_preserves_an_existing_host_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un hébergeur peut fournir son propre gestionnaire et son niveau sans être écrasé."""
    handler = logging.NullHandler()
    monkeypatch.setattr(diagnostics.logger, "handlers", [handler])
    monkeypatch.setattr(diagnostics.logger, "level", logging.WARNING)

    configure_logging()

    assert diagnostics.logger.handlers == [handler]
    assert diagnostics.logger.level == logging.WARNING


@pytest.mark.parametrize(
    ("endpoint", "params"),
    [
        ("/api/locations", {"q": "private-town", "count": "0"}),
        ("/api/forecast", {"latitude": "private-coordinate", "longitude": "4.85"}),
    ],
)
def test_http_events_measure_422_and_429_without_logging_query_parameters(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    params: dict[str, str],
) -> None:
    """La durée est en millisecondes, et le middleware observe les refus avant la route."""
    caplog.set_level(logging.INFO, logger="mouvair_api")
    moments = iter([10.0, 10.125, 20.0, 20.25])
    monkeypatch.setattr(diagnostics, "perf_counter", lambda: next(moments))
    settings = Settings(
        redis_failure_mode="local",
        location_rate_limit_minute=1, location_rate_limit_hour=1,
        forecast_rate_limit_minute=1, forecast_rate_limit_hour=1,
    )
    with TestClient(create_app(settings)) as client:
        first = client.get(endpoint, params=params)
        limited = client.get(endpoint, params=params, headers={"Origin": "http://localhost:5173"})

    assert first.status_code == 422
    assert limited.status_code == 429
    assert limited.headers["Access-Control-Allow-Origin"] == "http://localhost:5173"
    recorded = events(caplog, "http.request")
    assert [item["status_code"] for item in recorded] == [422, 429]
    assert [item["duration_ms"] for item in recorded] == [125.0, 250.0]
    assert {item["path"] for item in recorded} == {endpoint}
    assert {item["method"] for item in recorded} == {"GET"}
    assert all(item["outcome"] == "completed" for item in recorded)
    assert recorded[0]["request_id"] != recorded[1]["request_id"]
    assert events(caplog, "http.client_limited")[0]["request_id"] == recorded[1]["request_id"]
    serialized = "\n".join(
        JsonDiagnosticFormatter().format(record) for record in caplog.records
        if record.name == "mouvair_api"
    )
    assert "private-town" not in serialized
    assert "private-coordinate" not in serialized
    assert all("latitude" not in item and "longitude" not in item for item in recorded)


def test_health_checks_and_cors_preflight_do_not_create_info_request_events(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Les sondes de santé et OPTIONS restent consultables en DEBUG sans bruit en INFO."""
    caplog.set_level(logging.INFO, logger="mouvair_api")
    with TestClient(create_app(Settings(redis_failure_mode="local"))) as client:
        assert client.get("/health").status_code == 200
        assert client.options(
            "/api/forecast", headers={
                "Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET",
            },
        ).status_code == 200

    assert events(caplog, "http.request") == []


def test_concurrent_request_contexts_remain_isolated_and_json_is_parseable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Chaque requête garde son contexte après une suspension et le parent est restauré."""
    caplog.set_level(logging.INFO, logger="mouvair_api")

    async def scenario() -> None:
        started = 0
        ready = asyncio.Event()

        async def application(scope: Scope, receive: Receive, send: Send) -> None:
            nonlocal started
            log_event("inner.before", path=scope["path"])
            started += 1
            if started == 2:
                ready.set()
            await ready.wait()
            log_event("inner.after", path=scope["path"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        transport = httpx.ASGITransport(app=RequestDiagnosticsMiddleware(application))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            responses = await asyncio.gather(client.get("/first"), client.get("/second"))
        assert all(response.status_code == 200 for response in responses)
        assert request_id.get() == "parent-context"

    token = request_id.set("parent-context")
    try:
        asyncio.run(scenario())
    finally:
        request_id.reset(token)

    records = [record for record in caplog.records if record.name == "mouvair_api"]
    parsed = [json.loads(JsonDiagnosticFormatter().format(record)) for record in records]
    by_path = {
        path: {item["request_id"] for item in parsed if item["path"] == path}
        for path in ("/first", "/second")
    }
    assert all(len(identities) == 1 for identities in by_path.values())
    assert by_path["/first"] != by_path["/second"]
    assert all(len(item["request_id"]) == 32 for item in parsed)
    assert all(
        datetime.fromisoformat(item["timestamp"]).utcoffset() == timedelta() for item in parsed
    )


@pytest.mark.parametrize("cancelled", [False, True])
def test_http_failures_are_logged_and_propagated_without_leaking_error_messages(
    caplog: pytest.LogCaptureFixture,
    cancelled: bool,
) -> None:
    """Une erreur ou annulation garde son comportement et ne contamine pas le contexte suivant."""
    caplog.set_level(logging.INFO, logger="mouvair_api")
    error = asyncio.CancelledError("private-error") if cancelled else RuntimeError("private-error")

    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        raise error

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    async def send(message: Message) -> None:
        raise AssertionError("Aucune réponse ne doit être fabriquée par les diagnostics")

    async def scenario() -> None:
        token = request_id.set("parent-context")
        try:
            with pytest.raises(type(error)) as caught:
                await RequestDiagnosticsMiddleware(application)(
                    {"type": "http", "method": "GET", "path": "/broken"}, receive, send,
                )
            assert caught.value is error
            assert request_id.get() == "parent-context"
        finally:
            request_id.reset(token)

    asyncio.run(scenario())

    recorded = events(caplog, "http.request")
    assert len(recorded) == 1
    assert recorded[0]["outcome"] == ("cancelled" if cancelled else "error")
    assert recorded[0]["status_code"] == (None if cancelled else 500)
    assert "private-error" not in json.dumps(recorded)


def test_error_details_keep_the_type_and_retry_delay_without_the_message() -> None:
    """Les causes techniques et Retry-After suffisent sans exposer une URL ou un secret."""
    error = OpenMeteoQuotaError("https://provider.example/?secret=private", 42)
    error.__cause__ = TimeoutError("private-cause")

    assert error_fields(error) == {
        "error_type": "OpenMeteoQuotaError", "cause_type": "TimeoutError", "retry_after": 42,
    }


def test_forecast_events_distinguish_provider_memory_and_redis(
    caplog: pytest.LogCaptureFixture,
    memory_redis_store: RedisStore,
) -> None:
    """La provenance déclarée correspond à trois parcours réels, avec un seul appel fournisseur."""
    caplog.set_level(logging.INFO, logger="mouvair_api")
    moment = datetime(2026, 9, 8, 12, 5, tzinfo=UTC)
    start = moment.replace(minute=0)
    forecast = assemble_forecast(weather_payload(start), air_payload(start), moment)
    fetcher = SequencedFetcher(forecast)
    cache = RedisForecastCache(memory_redis_store, Settings())
    first = ForecastService(fetcher, clock=MutableClock(moment), shared_cache=cache)
    second = ForecastService(fetcher, clock=MutableClock(moment), shared_cache=cache)

    async def scenario() -> None:
        await first.get_forecast(45.75, 4.85)
        await first.get_forecast(45.75, 4.85)
        await second.get_forecast(45.75, 4.85)

    asyncio.run(scenario())

    assert fetcher.calls == 1
    assert events(caplog, "forecast.refreshed")[0]["source"] == "provider"
    assert [item["source"] for item in events(caplog, "forecast.cache_hit")] == ["memory", "redis"]
    assert all(item["age_seconds"] == 0 for item in events(caplog, "forecast.cache_hit"))


@pytest.mark.parametrize(("age", "state"), [(16, "refresh_failed"), (61, "stale")])
def test_forecast_fallback_events_explain_provider_failure_and_data_age(
    caplog: pytest.LogCaptureFixture,
    age: int,
    state: str,
) -> None:
    """Les logs permettent d’expliquer un HTTP 200 issu de données anciennes après un refus."""
    caplog.set_level(logging.INFO, logger="mouvair_api")
    moment = datetime(2026, 9, 8, 12, 5, tzinfo=UTC)
    clock = MutableClock(moment)
    start = moment.replace(minute=0)
    fetcher = SequencedFetcher(
        assemble_forecast(weather_payload(start), air_payload(start), moment),
        OpenMeteoQuotaError("private-provider-message", 42),
    )
    service = ForecastService(fetcher, clock=clock)

    async def scenario() -> None:
        await service.get_forecast(45.75, 4.85)
        clock.value += timedelta(minutes=age)
        assert (await service.get_forecast(45.75, 4.85)).data_state == state

    asyncio.run(scenario())

    failed = events(caplog, "forecast.refresh_failed")[0]
    assert failed["error_type"] == "OpenMeteoQuotaError"
    assert failed["retry_after"] == 42
    assert failed["has_previous"] is True
    fallback = events(caplog, "forecast.fallback")[0]
    assert fallback["reason"] == "provider_error"
    assert fallback["age_seconds"] == age * 60
    assert fallback["data_state"] == state
    assert "private-provider-message" not in json.dumps([failed, fallback])


@pytest.mark.parametrize("empty_result", [False, True])
def test_geocoding_events_distinguish_sources_and_keep_empty_results(
    caplog: pytest.LogCaptureFixture,
    memory_redis_store: RedisStore,
    empty_result: bool,
) -> None:
    """La recherche journalise le nombre de résultats, y compris zéro, sans la saisie."""
    caplog.set_level(logging.INFO, logger="mouvair_api")
    locations = (
        [] if empty_result else [Location(id=1, name="private-town", latitude=45, longitude=4)]
    )

    async def fetcher(query: str, count: int) -> list[Location]:
        return locations

    first = GeocodingService(fetcher, memory_redis_store, Settings())
    second = GeocodingService(fetcher, memory_redis_store, Settings())

    async def scenario() -> None:
        await first.search("private-town")
        await first.search("private-town")
        await second.search("private-town")

    asyncio.run(scenario())

    fetched = events(caplog, "geocoding.fetched")
    hits = events(caplog, "geocoding.cache_hit")
    assert len(fetched) == 1
    assert fetched[0]["source"] == "provider"
    assert [item["source"] for item in hits] == ["memory", "redis"]
    assert all(item["result_count"] == len(locations) for item in fetched + hits)
    assert "private-town" not in json.dumps(fetched + hits)


def test_redis_events_only_report_unavailability_and_recovery_transitions(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Une panne persistante n’inonde pas les journaux, et son message brut reste absent."""
    caplog.set_level(logging.INFO, logger="mouvair_api")

    class ToggleRedis:
        available = False

        async def ping(self) -> bool:
            if not self.available:
                raise OSError("private-redis-url")
            return True

    client = ToggleRedis()
    store = RedisStore(Settings(), client=client)

    async def scenario() -> None:
        assert not await store.ping(force=True)
        assert not await store.ping(force=True)
        client.available = True
        assert await store.ping(force=True)
        assert await store.ping(force=True)

    asyncio.run(scenario())

    assert len(events(caplog, "redis.unavailable")) == 1
    assert events(caplog, "redis.unavailable")[0]["error_type"] == "OSError"
    assert len(events(caplog, "redis.restored")) == 1
    assert "private-redis-url" not in json.dumps(events(caplog, "redis.unavailable"))


@pytest.mark.parametrize(
    ("reason", "provider_status"),
    [("http_error", 503), ("network_error", None), ("invalid_json", 200), ("invalid_payload", 200)],
)
def test_provider_failures_explain_transport_or_payload_errors_without_the_url(
    caplog: pytest.LogCaptureFixture,
    reason: str,
    provider_status: int | None,
) -> None:
    """Les causes réseau, HTTP et JSON restent distinctes sans recopier la requête sortante."""
    caplog.set_level(logging.INFO, logger="mouvair_api")

    def handler(request: httpx.Request) -> httpx.Response:
        if reason == "http_error":
            return httpx.Response(503)
        if reason == "network_error":
            raise httpx.ConnectError("private-network-message", request=request)
        if reason == "invalid_json":
            return httpx.Response(200, text="private-invalid-json")
        return httpx.Response(200, json=[])

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(OpenMeteoError):
                await search_locations("private-town", client=client)

    asyncio.run(scenario())

    recorded = events(caplog, "provider.request_failed")
    assert len(recorded) == 1
    assert recorded[0]["operation"] == "locations"
    assert recorded[0]["reason"] == reason
    assert recorded[0]["provider_status"] == provider_status
    assert recorded[0]["duration_ms"] >= 0
    assert "private" not in json.dumps(recorded)
    assert "https://" not in json.dumps(recorded)
