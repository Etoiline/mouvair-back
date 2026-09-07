import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .models import Conditions, ForecastResponse, HourlyForecast, Location
from .scoring import calculate_all_scores

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"


class OpenMeteoError(RuntimeError):
    pass


async def _get_json(client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> dict:
    try:
        response = await client.get(url, params=params)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OpenMeteoError("Open-Meteo est momentanément indisponible") from exc


async def search_locations(query: str, count: int = 5) -> list[Location]:
    async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "MouvAir/0.1"}) as client:
        payload = await _get_json(
            client,
            GEOCODING_URL,
            {"name": query, "count": count, "language": "fr", "format": "json"},
        )

    return [
        Location(
            id=item["id"],
            name=item["name"],
            latitude=item["latitude"],
            longitude=item["longitude"],
            country=item.get("country"),
            admin1=item.get("admin1"),
            timezone=item.get("timezone"),
        )
        for item in payload.get("results", [])
    ]


async def get_forecast(latitude: float, longitude: float) -> ForecastResponse:
    common_params: dict[str, Any] = {
        "latitude": latitude,
        "longitude": longitude,
        "timezone": "auto",
        "forecast_days": 2,
    }
    weather_params = {
        **common_params,
        "hourly": ",".join(
            [
                "apparent_temperature",
                "precipitation",
                "wind_speed_10m",
                "wind_gusts_10m",
                "weather_code",
            ]
        ),
    }
    air_params = {**common_params, "hourly": "european_aqi"}

    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "MouvAir/0.1"}) as client:
        weather, air = await asyncio.gather(
            _get_json(client, FORECAST_URL, weather_params),
            _get_json(client, AIR_QUALITY_URL, air_params),
        )

    timezone_name = weather.get("timezone", "UTC")
    utc_offset = int(weather.get("utc_offset_seconds", 0))
    local_now = (datetime.now(UTC) + timedelta(seconds=utc_offset)).replace(tzinfo=None)
    current_hour = local_now.replace(minute=0, second=0, microsecond=0)
    air_by_time = dict(zip(air["hourly"]["time"], air["hourly"]["european_aqi"], strict=True))
    hourly = weather["hourly"]
    hours: list[HourlyForecast] = []

    for index, time_value in enumerate(hourly["time"]):
        local_time = datetime.fromisoformat(time_value)
        if local_time < current_hour:
            continue
        if len(hours) == 24:
            break

        values = {
            "apparent_temperature": hourly["apparent_temperature"][index],
            "precipitation": hourly["precipitation"][index],
            "wind_speed": hourly["wind_speed_10m"][index],
            "wind_gusts": hourly["wind_gusts_10m"][index],
            "weather_code": hourly["weather_code"][index],
            "european_aqi": air_by_time.get(time_value),
        }
        if any(value is None for value in values.values()):
            continue

        conditions = Conditions.model_validate(values)
        hours.append(
            HourlyForecast(
                time=time_value,
                conditions=conditions,
                scores=calculate_all_scores(conditions),
            )
        )

    return ForecastResponse(
        latitude=weather["latitude"],
        longitude=weather["longitude"],
        timezone=timezone_name,
        generated_at=local_now.isoformat(timespec="seconds"),
        hours=hours,
    )

