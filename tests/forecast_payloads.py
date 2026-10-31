"""Jeux de séries horaires partagés par les tests d’assemblage et d’adaptation HTTP.

Les mesures favorables peuvent être modifiées par borne sans accès au fournisseur.
Les séries conservent le schéma des réponses pour exercer leur jointure temporelle.
"""

from datetime import datetime, timedelta
from typing import Any

from mouvair_api.forecast_slots import AIR_FIELDS


def timestamps(start: datetime, count: int = 30) -> list[int]:
    """Génère des instants horaires Unix pour simuler le format demandé au fournisseur."""
    return [int((start + timedelta(hours=index)).timestamp()) for index in range(count)]


def weather_payload(start: datetime, count: int = 30) -> dict[str, Any]:
    """Fournit une météo homogène favorable que chaque scénario peut modifier par borne."""
    return {
        "latitude": 45.75,
        "longitude": 4.85,
        "timezone": "Europe/Paris",
        "hourly": {
            "time": timestamps(start, count),
            "apparent_temperature": [15.0] * count,
            "precipitation": [0.0] * count,
            "wind_speed_10m": [5.0] * count,
            "wind_gusts_10m": [10.0] * count,
            "weather_code": [1] * count,
        },
    }


def air_payload(start: datetime, count: int = 30) -> dict[str, Any]:
    """Fournit un AQI et les six pollens, avec une série alignée sur les mêmes instants."""
    hourly: dict[str, list[int | float | None]] = {
        "time": timestamps(start, count),
        "european_aqi": [20.0] * count,
    }
    for field in AIR_FIELDS:
        if field != "european_aqi":
            hourly[field] = [0.0] * count
    return {
        "latitude": 45.75,
        "longitude": 4.85,
        "timezone": "Europe/Paris",
        "hourly": hourly,
    }
