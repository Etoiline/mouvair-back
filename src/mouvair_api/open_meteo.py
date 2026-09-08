import asyncio
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .models import (
    Conditions,
    ForecastResponse,
    HourlyForecast,
    Location,
    PollenForecast,
    PollenId,
    SlotConditions,
    SportId,
)
from .scoring import calculate_score, select_apparent_temperature

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
FORECAST_HOURS = 27
SLOT_COUNT = 24

SPORTS: tuple[SportId, ...] = ("easy_run", "hard_run", "cycling", "walking")
WEATHER_FIELDS = (
    "apparent_temperature",
    "precipitation",
    "wind_speed_10m",
    "wind_gusts_10m",
    "weather_code",
)
POLLEN_FIELDS: dict[PollenId, str] = {
    "alder": "alder_pollen",
    "birch": "birch_pollen",
    "grass": "grass_pollen",
    "mugwort": "mugwort_pollen",
    "olive": "olive_pollen",
    "ragweed": "ragweed_pollen",
}
AIR_FIELDS = ("european_aqi", *POLLEN_FIELDS.values())


class OpenMeteoError(RuntimeError):
    pass


async def _get_json(
    client: httpx.AsyncClient, url: str, params: dict[str, Any]
) -> dict[str, Any]:
    try:
        response = await client.get(url, params=params)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OpenMeteoError("Open-Meteo est momentanément indisponible") from exc

    if not isinstance(payload, dict):
        raise OpenMeteoError("Réponse Open-Meteo invalide")
    return payload


async def search_locations(query: str, count: int = 5) -> list[Location]:
    async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "MouvAir/0.2"}) as client:
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


def _unix_timestamp(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not isfinite(numeric) or not numeric.is_integer():
        return None
    return int(numeric)


def _hourly_rows(
    payload: dict[str, Any], fields: tuple[str, ...]
) -> dict[int, dict[str, object]]:
    """Indexe les séries par timestamp et invalide les horaires dupliqués."""
    hourly = payload.get("hourly")
    if not isinstance(hourly, dict):
        return {}
    times = hourly.get("time")
    if not isinstance(times, list):
        return {}

    rows: dict[int, dict[str, object]] = {}
    duplicates: set[int] = set()
    for index, raw_time in enumerate(times):
        timestamp = _unix_timestamp(raw_time)
        if timestamp is None or timestamp in duplicates:
            continue
        if timestamp in rows:
            rows.pop(timestamp)
            duplicates.add(timestamp)
            continue

        row: dict[str, object] = {}
        for field in fields:
            values = hourly.get(field)
            row[field] = values[index] if isinstance(values, list) and index < len(values) else None
        rows[timestamp] = row
    return rows


def _number(value: object, *, non_negative: bool) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not isfinite(number) or (non_negative and number < 0):
        return None
    return number


def _maximum(left: object, right: object, *, non_negative: bool = True) -> float | None:
    left_number = _number(left, non_negative=non_negative)
    right_number = _number(right, non_negative=non_negative)
    if left_number is None or right_number is None:
        return None
    return max(left_number, right_number)


def _weather_code(value: object) -> int | None:
    return value if type(value) is int else None


def _required_coordinate(payload: dict[str, Any], field: str) -> float:
    value = _number(payload.get(field), non_negative=False)
    if value is None:
        raise OpenMeteoError("Coordonnées absentes de la réponse météo")
    return value


def _timezone(payload: dict[str, Any]) -> tuple[str, ZoneInfo]:
    name = payload.get("timezone")
    if not isinstance(name, str):
        raise OpenMeteoError("Fuseau horaire absent de la réponse météo")
    try:
        return name, ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise OpenMeteoError("Fuseau horaire invalide dans la réponse météo") from exc


def _next_full_hour(moment: datetime, timezone: ZoneInfo) -> datetime:
    """Trouve la prochaine heure pleine locale et renvoie son instant UTC."""
    if moment.tzinfo is None:
        raise ValueError("L’instant de récupération doit inclure un fuseau horaire")
    utc_moment = moment.astimezone(UTC)
    candidate = utc_moment.replace(second=0, microsecond=0)
    if candidate < utc_moment:
        candidate += timedelta(minutes=1)

    # La chronologie UTC gère correctement les heures locales répétées ou inexistantes.
    for _ in range(181):
        if candidate.astimezone(timezone).minute == 0:
            return candidate
        candidate += timedelta(minutes=1)
    raise ValueError("Impossible de déterminer la prochaine heure pleine locale")


def _row_value(rows: dict[int, dict[str, object]], timestamp: int, field: str) -> object:
    return rows.get(timestamp, {}).get(field)


def _build_pollen(
    air_rows: dict[int, dict[str, object]], start_timestamp: int, end_timestamp: int
) -> PollenForecast:
    values = {
        pollen_id: _maximum(
            _row_value(air_rows, start_timestamp, provider_field),
            _row_value(air_rows, end_timestamp, provider_field),
        )
        for pollen_id, provider_field in POLLEN_FIELDS.items()
    }
    return PollenForecast.model_validate(values)


def _build_slot(
    start: datetime,
    timezone: ZoneInfo,
    weather_rows: dict[int, dict[str, object]],
    air_rows: dict[int, dict[str, object]],
) -> HourlyForecast:
    end = start + timedelta(hours=1)
    start_timestamp = int(start.timestamp())
    end_timestamp = int(end.timestamp())

    temperature_start = _number(
        _row_value(weather_rows, start_timestamp, "apparent_temperature"),
        non_negative=False,
    )
    temperature_end = _number(
        _row_value(weather_rows, end_timestamp, "apparent_temperature"),
        non_negative=False,
    )
    precipitation = _number(
        _row_value(weather_rows, end_timestamp, "precipitation"),
        non_negative=True,
    )
    wind_speed = _maximum(
        _row_value(weather_rows, start_timestamp, "wind_speed_10m"),
        _row_value(weather_rows, end_timestamp, "wind_speed_10m"),
    )
    wind_gusts = _number(
        _row_value(weather_rows, end_timestamp, "wind_gusts_10m"),
        non_negative=True,
    )
    weather_codes = [
        _weather_code(_row_value(weather_rows, start_timestamp, "weather_code")),
        _weather_code(_row_value(weather_rows, end_timestamp, "weather_code")),
    ]
    european_aqi = _maximum(
        _row_value(air_rows, start_timestamp, "european_aqi"),
        _row_value(air_rows, end_timestamp, "european_aqi"),
    )

    scores = {}
    for sport in SPORTS:
        apparent_temperature = select_apparent_temperature(
            temperature_start, temperature_end, sport
        )
        score_conditions = Conditions(
            apparent_temperature=apparent_temperature,
            precipitation=precipitation,
            wind_speed=wind_speed,
            wind_gusts=wind_gusts,
            european_aqi=european_aqi,
            weather_codes=weather_codes,
        )
        scores[sport] = calculate_score(score_conditions, sport)

    default_temperature = select_apparent_temperature(
        temperature_start, temperature_end, "easy_run"
    )
    first_weather_code = next((code for code in weather_codes if code is not None), None)
    conditions = SlotConditions(
        apparent_temperature=default_temperature,
        apparent_temperature_start=temperature_start,
        apparent_temperature_end=temperature_end,
        precipitation=precipitation,
        wind_speed=wind_speed,
        wind_gusts=wind_gusts,
        european_aqi=european_aqi,
        weather_code=first_weather_code,
        weather_codes=weather_codes,
    )

    return HourlyForecast(
        time=start.astimezone(timezone).isoformat(timespec="minutes"),
        end_time=end.astimezone(timezone).isoformat(timespec="minutes"),
        conditions=conditions,
        pollen=_build_pollen(air_rows, start_timestamp, end_timestamp),
        scores=scores,
    )


def _has_relevant_data(
    weather_rows: dict[int, dict[str, object]],
    air_rows: dict[int, dict[str, object]],
    timestamps: set[int],
) -> bool:
    for rows in (weather_rows, air_rows):
        for timestamp in timestamps:
            row = rows.get(timestamp)
            if row is not None and any(value is not None for value in row.values()):
                return True
    return False


def _assemble_forecast(
    weather: dict[str, Any], air: dict[str, Any], retrieved_at: datetime
) -> ForecastResponse:
    timezone_name, timezone = _timezone(weather)
    weather_rows = _hourly_rows(weather, WEATHER_FIELDS)
    air_rows = _hourly_rows(air, AIR_FIELDS)
    first_start = _next_full_hour(retrieved_at, timezone)
    timestamps = {
        int((first_start + timedelta(hours=offset)).timestamp())
        for offset in range(SLOT_COUNT + 1)
    }
    if not _has_relevant_data(weather_rows, air_rows, timestamps):
        raise OpenMeteoError("Prévisions horaires absentes ou mal horodatées")

    hours = [
        _build_slot(first_start + timedelta(hours=offset), timezone, weather_rows, air_rows)
        for offset in range(SLOT_COUNT)
    ]
    return ForecastResponse(
        latitude=_required_coordinate(weather, "latitude"),
        longitude=_required_coordinate(weather, "longitude"),
        timezone=timezone_name,
        generated_at=retrieved_at.astimezone(timezone).isoformat(timespec="seconds"),
        hours=hours,
    )


async def get_forecast(latitude: float, longitude: float) -> ForecastResponse:
    common_params: dict[str, Any] = {
        "latitude": latitude,
        "longitude": longitude,
        "timezone": "auto",
        "timeformat": "unixtime",
        "forecast_hours": FORECAST_HOURS,
    }
    weather_params = {**common_params, "hourly": ",".join(WEATHER_FIELDS)}
    air_params = {**common_params, "hourly": ",".join(AIR_FIELDS)}

    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "MouvAir/0.2"}) as client:
        weather, air = await asyncio.gather(
            _get_json(client, FORECAST_URL, weather_params),
            _get_json(client, AIR_QUALITY_URL, air_params),
        )

    return _assemble_forecast(weather, air, datetime.now(UTC))
