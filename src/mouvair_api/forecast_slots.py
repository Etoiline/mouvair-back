"""Construit les créneaux horaires à partir de séries déjà récupérées.

Ce module sans accès réseau définit les séries attendues, valide les mesures et
associe météo, air et pollens par timestamp Unix. Les deux points d’entrée publics
sont assemble_forecast et next_full_hour, également utilisé par le cache pour
aligner sa fenêtre. Les appels HTTP et les quotas appartiennent à open_meteo.
"""

from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import DataUnavailableError
from .models import (
    Conditions,
    ForecastResponse,
    HourlyForecast,
    PollenForecast,
    PollenId,
    SlotConditions,
    SportId,
)
from .scoring import calculate_score, select_apparent_temperature

# Le schéma des séries consommées guide aussi les champs demandés par l’adaptateur HTTP.
SLOT_COUNT = 24

SPORTS: tuple[SportId, ...] = ("easy_run", "hard_run", "cycling", "walking")
# Les unités par défaut du fournisseur correspondent aux °C, mm et km/h du moteur.
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


def _unix_timestamp(value: object) -> int | None:
    """Accepte un instant Unix entier et fini, en excluant explicitement les booléens."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not isfinite(numeric) or not numeric.is_integer():
        return None
    return int(numeric)


def _hourly_rows(
    payload: dict[str, Any], fields: tuple[str, ...]
) -> dict[int, dict[str, object]]:
    """Indexe les séries par timestamp et invalide les horaires dupliqués.

    Les tableaux de mesures peuvent être incomplets ou désordonnés. Une valeur
    absente devient None ; un timestamp dupliqué est supprimé entièrement plutôt
    que de choisir arbitrairement une de ses mesures contradictoires.
    """
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
    """Nettoie une mesure sans transformer une absence ou une anomalie en zéro."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not isfinite(number) or (non_negative and number < 0):
        return None
    return number


def _maximum(left: object, right: object, *, non_negative: bool = True) -> float | None:
    """Retient le maximum seulement si les deux bornes sont disponibles et valides.

    Une seule borne connue ne suffit pas pour représenter le maximum du créneau.
    """
    left_number = _number(left, non_negative=non_negative)
    right_number = _number(right, non_negative=non_negative)
    if left_number is None or right_number is None:
        return None
    return max(left_number, right_number)


def _weather_code(value: object) -> int | None:
    """Vérifie le type du code ; sa présence dans la nomenclature est contrôlée au score."""
    return value if type(value) is int else None


def _required_coordinate(payload: dict[str, Any], field: str) -> float:
    """Exige les coordonnées numériques renvoyées par le fournisseur météo."""
    value = _number(payload.get(field), non_negative=False)
    if value is None:
        raise DataUnavailableError("Coordonnées absentes de la réponse météo")
    return value


def _timezone(payload: dict[str, Any]) -> tuple[str, ZoneInfo]:
    """Valide le nom IANA nécessaire à l’affichage local et à l’alignement des départs."""
    name = payload.get("timezone")
    if not isinstance(name, str):
        raise DataUnavailableError("Fuseau horaire absent de la réponse météo")
    try:
        return name, ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise DataUnavailableError("Fuseau horaire invalide dans la réponse météo") from exc


def next_full_hour(moment: datetime, timezone: ZoneInfo) -> datetime:
    """Trouve la prochaine heure pleine locale et renvoie son instant UTC.

    Une heure déjà pleine est conservée. La recherche progresse en minutes UTC
    pour respecter aussi les décalages de 30 ou 45 minutes et les transitions
    saisonnières, sans créer d’heure locale inexistante.
    """
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
    """Lit une mesure par instant exact, avec absence explicite si sa ligne manque."""
    return rows.get(timestamp, {}).get(field)


def _build_pollen(
    air_rows: dict[int, dict[str, object]], start_timestamp: int, end_timestamp: int
) -> PollenForecast:
    """Expose les maxima des deux bornes pour les six pollens, sans les noter."""
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
    """Agrège les deux bornes selon le sens physique des champs puis calcule les sports.

    Le départ est en UTC : ajouter une heure conserve une durée réelle d’une
    heure même lors d’un changement d’heure locale. Les mesures communes sont
    calculées une fois ; la borne thermique est ensuite choisie pour chaque sport.
    """
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
    # Ces champs de fin décrivent l’heure précédente, donc l’intervalle start -> end.
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

    # La borne thermique la plus sévère dépend de la courbe de tolérance de l’activité.
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

    # Le résumé commun utilise la course tranquille ; chaque facteur sportif reste exact.
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
    """Distingue une réponse partielle d’une absence totale de champs dans la fenêtre.

    Ce contrôle ne garantit pas encore des mesures valides : leur nettoyage et
    l’incomplétude des notes sont traités à la construction de chaque créneau.
    """
    for rows in (weather_rows, air_rows):
        for timestamp in timestamps:
            row = rows.get(timestamp)
            if row is not None and any(value is not None for value in row.values()):
                return True
    return False


def assemble_forecast(
    weather: dict[str, Any], air: dict[str, Any], retrieved_at: datetime
) -> ForecastResponse:
    """Joint météo et air par instant réel et garde les créneaux partiellement connus."""
    timezone_name, timezone = _timezone(weather)
    weather_rows = _hourly_rows(weather, WEATHER_FIELDS)
    air_rows = _hourly_rows(air, AIR_FIELDS)
    first_start = next_full_hour(retrieved_at, timezone)
    timestamps = {
        int((first_start + timedelta(hours=offset)).timestamp())
        for offset in range(SLOT_COUNT + 1)
    }
    if not _has_relevant_data(weather_rows, air_rows, timestamps):
        raise DataUnavailableError("Prévisions horaires absentes ou mal horodatées")

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
