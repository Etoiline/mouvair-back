"""Vérifie les appels météo et air puis leur passage à l’assemblage des créneaux.

MockTransport exerce les paramètres réellement envoyés par httpx sans réseau.
Les règles temporelles et les données partielles sont testées séparément dans
test_forecast_slots ; les budgets et les erreurs HTTP ont leur suite de passerelle.
"""

import asyncio
from datetime import UTC, datetime

import httpx

from forecast_payloads import air_payload, weather_payload
from mouvair_api.forecast_slots import AIR_FIELDS, WEATHER_FIELDS
from mouvair_api.open_meteo import (
    AIR_QUALITY_URL,
    FORECAST_HOURS,
    FORECAST_URL,
    get_forecast,
)


def test_get_forecast_requests_unix_times_and_all_pollens() -> None:
    """Vérifie les deux GET et l’obtention de notes après assemblage de leurs réponses."""
    base = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    captured: dict[str, dict[str, str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        """Répond à chaque URL prévue en conservant ses paramètres effectivement encodés."""
        url = str(request.url).split("?", 1)[0]
        assert url in {FORECAST_URL, AIR_QUALITY_URL}
        captured[url] = dict(request.url.params)
        payload = weather_payload(base) if url == FORECAST_URL else air_payload(base)
        return httpx.Response(200, json=payload, request=request)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await get_forecast(45.75, 4.85, client=client)

    result = asyncio.run(scenario())

    assert len(captured) == 2
    for params in captured.values():
        assert params["latitude"] == "45.75"
        assert params["longitude"] == "4.85"
        assert params["timezone"] == "auto"
        assert params["timeformat"] == "unixtime"
        assert int(params["forecast_hours"]) == FORECAST_HOURS
    assert set(captured[FORECAST_URL]["hourly"].split(",")) == set(WEATHER_FIELDS)
    assert set(captured[AIR_QUALITY_URL]["hourly"].split(",")) == set(AIR_FIELDS)
    assert len(result.hours) == 24
    assert result.hours[0].scores["easy_run"].weather.score == 100
    assert result.hours[0].scores["easy_run"].air.score == 80
    assert result.hours[0].scores["easy_run"].overall.score == 93
