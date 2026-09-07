from mouvair_api.models import Conditions, SportId
from mouvair_api.scoring import calculate_score, interpolate


def conditions(**overrides: float | int) -> Conditions:
    values: dict[str, float | int] = {
        "apparent_temperature": 15,
        "precipitation": 0,
        "wind_speed": 8,
        "wind_gusts": 12,
        "european_aqi": 15,
        "weather_code": 1,
    }
    values.update(overrides)
    return Conditions.model_validate(values)


def test_interpolate_between_two_thresholds() -> None:
    assert interpolate(5, ((0, 100), (10, 0))) == 50


def test_favourable_conditions_score_high_for_all_profiles() -> None:
    sports: tuple[SportId, ...] = ("easy_run", "hard_run", "cycling", "walking")
    for sport in sports:
        result = calculate_score(conditions(), sport)
        assert result.score is not None
        assert result.score >= 80


def test_bad_air_caps_every_sport() -> None:
    sports: tuple[SportId, ...] = ("easy_run", "hard_run", "cycling", "walking")
    for sport in sports:
        result = calculate_score(conditions(european_aqi=65), sport)
        assert result.score is not None
        assert result.score <= 39


def test_moderate_air_caps_intense_run() -> None:
    result = calculate_score(conditions(european_aqi=45), "hard_run")
    assert result.score is not None
    assert result.score <= 59


def test_heat_penalises_intense_run_more_than_walking() -> None:
    hot = conditions(apparent_temperature=28)
    hard_run = calculate_score(hot, "hard_run")
    walking = calculate_score(hot, "walking")
    assert hard_run.score is not None
    assert walking.score is not None
    assert hard_run.score < walking.score


def test_thunderstorm_has_priority_over_weighted_score() -> None:
    result = calculate_score(conditions(weather_code=95), "walking")
    assert result.score is not None
    assert result.score <= 10
    assert result.status == "défavorable"

