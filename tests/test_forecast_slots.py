"""Vérifie l’assemblage pur des créneaux, sans client HTTP ni connexion Redis.

Les scénarios contrôlent les deux bornes, la jointure temporelle, les valeurs
manquantes et les transitions de fuseau. Chaque sortie dure une heure réelle.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from forecast_payloads import air_payload, weather_payload
from mouvair_api.errors import DataUnavailableError
from mouvair_api.forecast_slots import assemble_forecast, next_full_hour
from mouvair_api.scoring import SNOW_ALERT, THUNDERSTORM_ALERT


def weather_factor_value(hour, sport: str, factor_id: str) -> float | None:
    """Lit la mesure réellement choisie pour le sport dans ses facteurs explicatifs."""
    factors = hour.scores[sport].weather.factors
    return next(factor.value for factor in factors if factor.id == factor_id)


def test_assemble_uses_the_two_real_hour_boundaries() -> None:
    """Oppose les valeurs de début et de fin pour vérifier chaque règle d’agrégation."""
    base = datetime(2026, 9, 8, 12, tzinfo=UTC)
    weather = weather_payload(base)
    air = air_payload(base)

    weather_hourly = weather["hourly"]
    weather_hourly["apparent_temperature"][1:3] = [-5.0, 30.0]
    weather_hourly["precipitation"][1:3] = [9.0, 1.2]
    weather_hourly["wind_speed_10m"][1:3] = [10.0, 20.0]
    weather_hourly["wind_gusts_10m"][1:3] = [99.0, 50.0]
    weather_hourly["weather_code"][1:3] = [1, 95]

    air_hourly = air["hourly"]
    air_hourly["european_aqi"][1:3] = [30.0, 50.0]
    air_hourly["alder_pollen"][1:3] = [3.0, 7.0]
    air_hourly["birch_pollen"][1:3] = [None, 5.0]
    # Inverser les séries air doit rester sans effet sur la jointure par timestamp.
    for values in air_hourly.values():
        values.reverse()

    result = assemble_forecast(
        weather,
        air,
        datetime(2026, 9, 8, 12, 35, tzinfo=UTC),
    )

    assert len(result.hours) == 24
    first = result.hours[0]
    assert first.time == "2026-09-08T15:00+02:00"
    assert first.end_time == "2026-09-08T16:00+02:00"
    assert first.conditions.apparent_temperature_start == -5
    assert first.conditions.apparent_temperature_end == 30
    assert first.conditions.precipitation == 1.2
    assert first.conditions.wind_speed == 20
    assert first.conditions.wind_gusts == 50
    assert first.conditions.european_aqi == 50
    assert first.conditions.weather_codes == [1, 95]
    assert first.pollen.alder == 7
    assert first.pollen.birch is None
    assert weather_factor_value(first, "easy_run", "temperature") == -5
    assert weather_factor_value(first, "hard_run", "temperature") == 30
    assert first.scores["easy_run"].weather.alerts == [THUNDERSTORM_ALERT]
    assert first.scores["easy_run"].overall.score == 10
    assert result.generated_at == "2026-09-08T14:35:00+02:00"


def test_missing_endpoint_keeps_slot_and_known_alert() -> None:
    """Une borne inconnue conserve le créneau et la neige détectée, avec les notes partielles."""
    base = datetime(2026, 9, 8, 12, tzinfo=UTC)
    weather = weather_payload(base)
    air = air_payload(base)
    weather_hourly = weather["hourly"]
    weather_hourly["precipitation"][2] = None
    weather_hourly["weather_code"][1:3] = [71, None]
    air["hourly"]["grass_pollen"][1:3] = [10.0, None]

    result = assemble_forecast(
        weather,
        air,
        datetime(2026, 9, 8, 12, 35, tzinfo=UTC),
    )

    first = result.hours[0]
    assert first.conditions.precipitation is None
    assert first.conditions.weather_codes == [71, None]
    assert first.scores["walking"].weather.score is None
    assert first.scores["walking"].weather.alerts == [SNOW_ALERT]
    assert first.scores["walking"].air.score == 80
    assert first.scores["walking"].overall.score is None
    assert first.pollen.grass is None
    assert result.hours[1].scores["walking"].overall.score is None
    assert result.hours[2].scores["walking"].overall.score is not None


def test_air_series_is_not_stitched_by_array_position() -> None:
    """Retirer une heure air ne doit pas décaler les valeurs suivantes sur les heures météo."""
    base = datetime(2026, 9, 8, 12, tzinfo=UTC)
    weather = weather_payload(base)
    air = air_payload(base)
    air_hourly = air["hourly"]
    for values in air_hourly.values():
        values.pop(2)

    result = assemble_forecast(
        weather,
        air,
        datetime(2026, 9, 8, 12, 35, tzinfo=UTC),
    )

    first = result.hours[0]
    assert first.conditions.european_aqi is None
    assert first.scores["easy_run"].air.score is None
    assert first.scores["easy_run"].overall.status == "incomplet"


def test_dst_fallback_keeps_a_real_one_hour_interval() -> None:
    """Au passage à l’heure d’hiver, deux 02 h avec offsets distincts bornent une vraie heure."""
    base = datetime(2026, 10, 24, 23, tzinfo=UTC)
    result = assemble_forecast(
        weather_payload(base),
        air_payload(base),
        datetime(2026, 10, 24, 23, 30, tzinfo=UTC),
    )

    first = result.hours[0]
    assert first.time == "2026-10-25T02:00+02:00"
    assert first.end_time == "2026-10-25T02:00+01:00"
    start = datetime.fromisoformat(first.time)
    end = datetime.fromisoformat(first.end_time)
    assert end.timestamp() - start.timestamp() == 3600


def test_exact_full_hour_can_be_the_first_departure() -> None:
    """L’heure exacte reste proposée ; une seconde plus tard impose le départ suivant."""
    moment = datetime(2026, 9, 8, 13, tzinfo=UTC)
    timezone = ZoneInfo("Europe/Paris")
    assert next_full_hour(moment, timezone) == moment
    assert next_full_hour(moment + timedelta(seconds=1), timezone) == moment + timedelta(hours=1)


@pytest.mark.parametrize(
    ("timezone_name", "moment", "expected"),
    [
        (
            "Asia/Kolkata",
            datetime(2026, 9, 8, 13, 10, tzinfo=UTC),
            datetime(2026, 9, 8, 13, 30, tzinfo=UTC),
        ),
        (
            "Asia/Kathmandu",
            datetime(2026, 9, 8, 13, 10, tzinfo=UTC),
            datetime(2026, 9, 8, 13, 15, tzinfo=UTC),
        ),
    ],
)
def testnext_full_hour_uses_the_city_timezone(
    timezone_name: str,
    moment: datetime,
    expected: datetime,
) -> None:
    """L’alignement local tient compte des décalages de 30 et de 45 minutes par rapport à UTC."""
    assert next_full_hour(moment, ZoneInfo(timezone_name)) == expected


def test_half_hour_timezone_assembles_local_full_hour_slots() -> None:
    """Le départ à 19 h en Inde correspond à une demi-heure UTC et reste localement plein."""
    base = datetime(2026, 9, 8, 13, 30, tzinfo=UTC)
    weather = weather_payload(base)
    weather["timezone"] = "Asia/Kolkata"

    result = assemble_forecast(
        weather,
        air_payload(base),
        datetime(2026, 9, 8, 13, 10, tzinfo=UTC),
    )

    assert result.hours[0].time == "2026-09-08T19:00+05:30"
    assert result.hours[0].end_time == "2026-09-08T20:00+05:30"


def test_spring_transition_skips_the_nonexistent_local_hour() -> None:
    """Le saut du printemps ignore l’heure absente tout en gardant une durée de 3 600 secondes."""
    base = datetime(2026, 3, 28, 23, tzinfo=UTC)
    result = assemble_forecast(
        weather_payload(base),
        air_payload(base),
        datetime(2026, 3, 29, 0, 30, tzinfo=UTC),
    )

    first = result.hours[0]
    assert first.time == "2026-03-29T03:00+02:00"
    assert first.end_time == "2026-03-29T04:00+02:00"
    start = datetime.fromisoformat(first.time)
    end = datetime.fromisoformat(first.end_time)
    assert end.timestamp() - start.timestamp() == 3600


def test_malformed_timestamps_raise_a_data_error() -> None:
    """Des dates textuelles au lieu de timestamps Unix ne doivent pas produire de faux créneaux."""
    weather = weather_payload(datetime(2026, 9, 8, 12, tzinfo=UTC))
    air = air_payload(datetime(2026, 9, 8, 12, tzinfo=UTC))
    weather["hourly"]["time"] = ["2026-09-08T12:00"]
    air["hourly"]["time"] = ["2026-09-08T12:00"]

    with pytest.raises(DataUnavailableError, match="mal horodatées"):
        assemble_forecast(
            weather,
            air,
            datetime(2026, 9, 8, 12, 35, tzinfo=UTC),
        )
