import pytest

from mouvair_api.models import Conditions, SportId, SportScore, WeatherFactorId
from mouvair_api.scoring import (
    FREEZING_PRECIPITATION_ALERT,
    SNOW_ALERT,
    THUNDERSTORM_ALERT,
    calculate_all_scores,
    calculate_score,
    interpolate,
    round_half_up,
    score_status,
)


def conditions(**overrides: object) -> Conditions:
    values: dict[str, object] = {
        "apparent_temperature": 15,
        "precipitation": 0,
        "wind_speed": 8,
        "wind_gusts": 12,
        "european_aqi": 15,
        "weather_code": 1,
    }
    values.update(overrides)
    return Conditions.model_validate(values)


def factor(result: SportScore, factor_id: WeatherFactorId):
    return next(item for item in result.weather.factors if item.id == factor_id)


def test_interpolate_between_two_thresholds() -> None:
    assert interpolate(5, ((0, 100), (10, 0))) == 50


@pytest.mark.parametrize(
    ("value", "expected"),
    [(54.49, 54), (54.5, 55), (92.5, 93), (100, 100)],
)
def test_round_half_up(value: float, expected: int) -> None:
    assert round_half_up(value) == expected


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (100, "favorable"),
        (80, "favorable"),
        (79, "correct"),
        (60, "correct"),
        (59, "mitigé"),
        (40, "mitigé"),
        (39, "défavorable"),
        (20, "défavorable"),
        (19, "très défavorable"),
        (0, "très défavorable"),
    ],
)
def test_score_status_boundaries(score: int, expected: str) -> None:
    assert score_status(score) == expected


@pytest.mark.parametrize(
    ("sport", "temperature", "expected_weather"),
    [
        ("easy_run", 21, 93),
        ("hard_run", 24, 58),
        ("cycling", 34, 33),
        ("walking", 35, 38),
    ],
)
def test_documented_temperature_examples(
    sport: SportId, temperature: float, expected_weather: int
) -> None:
    result = calculate_score(
        conditions(apparent_temperature=temperature, wind_speed=0, wind_gusts=0),
        sport,
    )
    assert result.weather.score == expected_weather


@pytest.mark.parametrize(
    ("sport", "rain", "expected_factor"),
    [
        ("easy_run", 1, 67),
        ("hard_run", 3, 27),
        ("cycling", 1, 35),
        ("walking", 3, 42),
    ],
)
def test_rain_curves(sport: SportId, rain: float, expected_factor: int) -> None:
    result = calculate_score(conditions(precipitation=rain), sport)
    assert factor(result, "rain").score == expected_factor


@pytest.mark.parametrize(
    ("sport", "wind", "gusts", "expected_exposure", "expected_factor"),
    [
        ("easy_run", 20, 40, 24, 61),
        ("hard_run", 20, 40, 24, 61),
        ("cycling", 20, 40, 30, 20),
        ("walking", 20, 60, 36, 54),
    ],
)
def test_wind_and_gust_combination(
    sport: SportId,
    wind: float,
    gusts: float,
    expected_exposure: float,
    expected_factor: int,
) -> None:
    result = calculate_score(conditions(wind_speed=wind, wind_gusts=gusts), sport)
    wind_factor = factor(result, "wind")
    assert wind_factor.value == expected_exposure
    assert wind_factor.score == expected_factor


def test_progressive_rain_cap_limits_cycling_weather() -> None:
    result = calculate_score(
        conditions(
            apparent_temperature=20,
            precipitation=1,
            wind_speed=0,
            wind_gusts=0,
        ),
        "cycling",
    )
    assert result.weather.score == 55
    assert result.weather.reasons[0] == (
        "Les précipitations prévues pénalisent fortement ce créneau pour le vélo."
    )


@pytest.mark.parametrize(
    ("aqi", "expected_score", "expected_category"),
    [
        (0, 100, "Bonne"),
        (20, 80, "Bonne"),
        (20.1, 80, "Acceptable"),
        (40, 60, "Acceptable"),
        (40.1, 60, "Modérée"),
        (60, 40, "Modérée"),
        (60.1, 40, "Mauvaise"),
        (80, 20, "Mauvaise"),
        (80.1, 20, "Très mauvaise"),
        (100, 0, "Très mauvaise"),
        (100.1, 0, "Extrêmement mauvaise"),
    ],
)
def test_air_score_and_category_boundaries(
    aqi: float, expected_score: int, expected_category: str
) -> None:
    air = calculate_score(conditions(european_aqi=aqi), "easy_run").air
    assert air.score == expected_score
    assert air.category == expected_category


def test_air_recommendations_use_original_aqi_category() -> None:
    air = calculate_score(conditions(european_aqi=112), "walking").air
    assert air.score == 0
    assert air.aqi == 112
    assert air.category == "Extrêmement mauvaise"
    assert air.recommendations is not None
    assert air.recommendations.general_population == (
        "Réduire l’activité physique extérieure."
    )
    assert air.recommendations.sensitive_people == "Éviter l’activité physique extérieure."


def test_global_score_uses_air_progressive_cap() -> None:
    result = calculate_score(
        conditions(
            apparent_temperature=15,
            precipitation=0,
            wind_speed=0,
            wind_gusts=0,
            european_aqi=88,
        ),
        "easy_run",
    )
    assert result.weather.score == 100
    assert result.air.score == 12
    assert result.overall.score == 32


def test_global_score_uses_unrounded_weather_value() -> None:
    result = calculate_score(
        conditions(
            apparent_temperature=21,
            precipitation=0,
            wind_speed=0,
            wind_gusts=0,
            european_aqi=17,
        ),
        "easy_run",
    )
    assert result.weather.score == 93
    assert result.air.score == 83
    assert result.overall.score == 89


@pytest.mark.parametrize(
    ("sport", "expected_cap"),
    [
        ("easy_run", 40),
        ("hard_run", 40),
        ("cycling", 20),
        ("walking", 60),
    ],
)
def test_snow_caps_weather_and_overall(sport: SportId, expected_cap: int) -> None:
    result = calculate_score(conditions(weather_code=71), sport)
    assert result.weather.score == expected_cap
    assert result.overall.score == expected_cap
    assert result.weather.alerts == [SNOW_ALERT]


@pytest.mark.parametrize("weather_code", [95, 66])
def test_dangerous_weather_caps_weather_and_overall(weather_code: int) -> None:
    result = calculate_score(conditions(weather_code=weather_code), "walking")
    assert result.weather.score == 10
    assert result.weather.status == "très défavorable"
    assert result.overall.score == 10
    assert result.overall.status == "très défavorable"


def test_event_alerts_keep_the_validated_priority_order() -> None:
    result = calculate_score(
        conditions(weather_codes=[71, 66, 95]),
        "walking",
    )
    assert result.weather.alerts == [
        THUNDERSTORM_ALERT,
        FREEZING_PRECIPITATION_ALERT,
        SNOW_ALERT,
    ]
    assert result.weather.score == 10
    assert result.overall.score == 10


def test_missing_air_keeps_weather_but_blocks_overall() -> None:
    result = calculate_score(conditions(european_aqi=None), "easy_run")
    assert result.weather.score == 100
    assert result.air.score is None
    assert result.overall.score is None
    assert result.overall.status == "incomplet"


def test_missing_weather_keeps_air_and_detected_alerts() -> None:
    result = calculate_score(
        conditions(precipitation=None, weather_code=95),
        "easy_run",
    )
    assert result.weather.score is None
    assert factor(result, "rain").score is None
    assert result.weather.alerts == [THUNDERSTORM_ALERT]
    assert result.air.score == 85
    assert result.overall.score is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"precipitation": -0.1},
        {"wind_speed": -1},
        {"wind_gusts": -1},
        {"weather_code": 999},
    ],
)
def test_invalid_weather_value_makes_weather_incomplete(overrides: dict[str, object]) -> None:
    result = calculate_score(conditions(**overrides), "walking")
    assert result.weather.score is None
    assert result.air.score == 85
    assert result.overall.score is None


@pytest.mark.parametrize("aqi", [-1, float("nan"), float("inf")])
def test_invalid_air_value_makes_air_incomplete(aqi: float) -> None:
    result = calculate_score(conditions(european_aqi=aqi), "walking")
    assert result.weather.score is not None
    assert result.air.score is None
    assert result.overall.score is None


def test_air_score_is_identical_for_all_activities() -> None:
    scores = calculate_all_scores(conditions(european_aqi=43.5))
    assert {result.air.score for result in scores.values()} == {57}
    assert {result.air.category for result in scores.values()} == {"Modérée"}


def test_legacy_fields_keep_the_current_frontend_compatible() -> None:
    result = calculate_score(conditions(weather_code=95), "easy_run")
    assert result.score == result.overall.score == 10
    assert result.status == "défavorable"
    assert [item.id for item in result.factors] == ["air", "temperature", "rain", "wind"]
    assert result.reasons[0] == THUNDERSTORM_ALERT
