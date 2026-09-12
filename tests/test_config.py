import pytest

from mouvair_api.config import Settings


def test_quota_defaults_keep_a_ten_percent_safety_margin() -> None:
    settings = Settings()

    assert settings.open_meteo_quota_minute == 540
    assert settings.open_meteo_quota_hour == 4_500
    assert settings.open_meteo_quota_day == 9_000
    assert settings.open_meteo_quota_month == 270_000


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("open_meteo_quota_minute", 600),
        ("open_meteo_quota_hour", 5_000),
        ("open_meteo_quota_day", 10_000),
        ("open_meteo_quota_month", 300_000),
    ],
)
def test_quota_configuration_cannot_reach_the_official_limit(
    field: str, value: int
) -> None:
    with pytest.raises(ValueError, match="doit être compris"):
        Settings(**{field: value})


def test_each_provider_window_can_reserve_a_complete_forecast() -> None:
    with pytest.raises(ValueError, match="compris entre 2"):
        Settings(open_meteo_quota_minute=1)


def test_environment_configuration_rejects_an_unknown_failure_mode(monkeypatch) -> None:
    monkeypatch.setenv("REDIS_FAILURE_MODE", "open")

    with pytest.raises(ValueError, match="closed ou local"):
        Settings.from_env()
