from dataclasses import dataclass
from typing import Literal

from .models import Conditions, ScoreFactor, SportId, SportScore

Curve = tuple[tuple[float, float], ...]
FactorId = Literal["air", "temperature", "rain", "wind"]


@dataclass(frozen=True)
class SportProfile:
    id: SportId
    label: str
    weights: dict[FactorId, float]
    temperature_curve: Curve
    rain_curve: Curve
    wind_curve: Curve


PROFILES: dict[SportId, SportProfile] = {
    "easy_run": SportProfile(
        id="easy_run",
        label="Course tranquille · 1 h",
        weights={"air": 0.35, "temperature": 0.35, "rain": 0.15, "wind": 0.15},
        temperature_curve=(
            (-10, 0),
            (0, 40),
            (5, 75),
            (10, 100),
            (18, 100),
            (23, 75),
            (28, 40),
            (35, 0),
        ),
        rain_curve=((0, 100), (0.5, 80), (2, 40), (5, 0)),
        wind_curve=((0, 100), (10, 100), (20, 75), (30, 40), (50, 0)),
    ),
    "hard_run": SportProfile(
        id="hard_run",
        label="Course intense · 1 h",
        weights={"air": 0.40, "temperature": 0.40, "rain": 0.10, "wind": 0.10},
        temperature_curve=(
            (-10, 0),
            (0, 30),
            (5, 70),
            (8, 100),
            (15, 100),
            (20, 70),
            (25, 30),
            (30, 0),
        ),
        rain_curve=((0, 100), (0.5, 80), (2, 40), (5, 0)),
        wind_curve=((0, 100), (10, 100), (20, 75), (30, 40), (50, 0)),
    ),
    "cycling": SportProfile(
        id="cycling",
        label="Vélo · 1 h",
        weights={"air": 0.30, "temperature": 0.25, "rain": 0.20, "wind": 0.25},
        temperature_curve=(
            (-10, 0),
            (0, 35),
            (7, 75),
            (12, 100),
            (22, 100),
            (27, 70),
            (32, 25),
            (36, 0),
        ),
        rain_curve=((0, 100), (0.2, 75), (1, 35), (3, 0)),
        wind_curve=((0, 100), (8, 100), (15, 70), (25, 30), (40, 0)),
    ),
    "walking": SportProfile(
        id="walking",
        label="Marche · 1 h",
        weights={"air": 0.30, "temperature": 0.30, "rain": 0.20, "wind": 0.20},
        temperature_curve=(
            (-10, 0),
            (0, 50),
            (5, 75),
            (10, 100),
            (22, 100),
            (28, 70),
            (33, 30),
            (38, 0),
        ),
        rain_curve=((0, 100), (0.5, 85), (2, 55), (5, 15), (8, 0)),
        wind_curve=((0, 100), (15, 100), (30, 70), (45, 30), (65, 0)),
    ),
}

THUNDERSTORM_CODES = {95, 96, 99}


def interpolate(value: float, curve: Curve) -> float:
    """Return a linearly interpolated score from an ordered threshold curve."""
    if value <= curve[0][0]:
        return curve[0][1]
    if value >= curve[-1][0]:
        return curve[-1][1]

    for (left_value, left_score), (right_value, right_score) in zip(
        curve, curve[1:], strict=False
    ):
        if left_value <= value <= right_value:
            position = (value - left_value) / (right_value - left_value)
            return left_score + position * (right_score - left_score)

    raise ValueError("The score curve must be sorted by value")


def clamp(value: float, minimum: float = 0, maximum: float = 100) -> float:
    return max(minimum, min(value, maximum))


def _status(score: int) -> Literal["favorable", "correct", "mitigé", "défavorable"]:
    if score >= 80:
        return "favorable"
    if score >= 60:
        return "correct"
    if score >= 40:
        return "mitigé"
    return "défavorable"


def _wind_exposure(conditions: Conditions, sport: SportId) -> float:
    """Blend sustained wind and gusts; gusts matter more when cycling."""
    gust_ratio = 0.75 if sport == "cycling" else 0.60
    return max(conditions.wind_speed, conditions.wind_gusts * gust_ratio)


def _factor_reason(factor: ScoreFactor, conditions: Conditions) -> str:
    if factor.id == "air":
        return f"Qualité de l’air pénalisante (indice européen {conditions.european_aqi:.0f})."
    if factor.id == "temperature":
        qualifier = "élevée" if conditions.apparent_temperature > 20 else "basse"
        return f"Température ressentie {qualifier} ({conditions.apparent_temperature:.0f} °C)."
    if factor.id == "rain":
        return f"Pluie prévue ({conditions.precipitation:g} mm/h)."
    return (
        f"Vent soutenu ({conditions.wind_speed:.0f} km/h, "
        f"rafales à {conditions.wind_gusts:.0f} km/h)."
    )


def calculate_score(conditions: Conditions, sport: SportId) -> SportScore:
    profile = PROFILES[sport]
    wind_exposure = _wind_exposure(conditions, sport)

    factor_values: dict[FactorId, tuple[float, float, str, str]] = {
        "air": (
            clamp(100 - conditions.european_aqi),
            conditions.european_aqi,
            "Qualité de l’air",
            "AQI UE",
        ),
        "temperature": (
            interpolate(conditions.apparent_temperature, profile.temperature_curve),
            conditions.apparent_temperature,
            "Température ressentie",
            "°C",
        ),
        "rain": (
            interpolate(conditions.precipitation, profile.rain_curve),
            conditions.precipitation,
            "Précipitations",
            "mm/h",
        ),
        "wind": (
            interpolate(wind_exposure, profile.wind_curve),
            wind_exposure,
            "Vent",
            "km/h",
        ),
    }

    factors = [
        ScoreFactor(
            id=factor_id,
            label=label,
            score=round(score),
            value=value,
            unit=unit,
        )
        for factor_id, (score, value, label, unit) in factor_values.items()
    ]

    raw_score = sum(factor_values[key][0] * weight for key, weight in profile.weights.items())
    cap = 100
    priority_reasons: list[str] = []

    if conditions.weather_code in THUNDERSTORM_CODES:
        cap = min(cap, 10)
        priority_reasons.append("Orage prévu sur le créneau.")

    if conditions.european_aqi >= 100:
        cap = min(cap, 10)
        priority_reasons.append("Qualité de l’air extrêmement mauvaise.")
    elif conditions.european_aqi >= 80:
        cap = min(cap, 24)
        priority_reasons.append("Qualité de l’air très mauvaise.")
    elif conditions.european_aqi >= 60:
        cap = min(cap, 39)
        priority_reasons.append("Qualité de l’air mauvaise.")
    elif sport == "hard_run" and conditions.european_aqi >= 40:
        cap = min(cap, 59)
        priority_reasons.append("Air modéré pour un effort intense.")

    temperature_score = factor_values["temperature"][0]
    if temperature_score <= 10:
        cap = min(cap, 39)
        priority_reasons.append("Température ressentie extrême pour cette activité.")

    wind_score = factor_values["wind"][0]
    if sport == "cycling" and wind_score <= 10:
        cap = min(cap, 39)
        priority_reasons.append("Vent trop fort pour une sortie vélo confortable.")

    rain_score = factor_values["rain"][0]
    if sport == "cycling" and rain_score <= 10:
        cap = min(cap, 39)
        priority_reasons.append("Pluie forte pour une sortie vélo.")

    final_score = round(min(raw_score, cap))
    weak_factors = sorted(
        (factor for factor in factors if factor.score < 85),
        key=lambda factor: factor.score,
    )
    reasons = priority_reasons.copy()
    for factor in weak_factors:
        reason = _factor_reason(factor, conditions)
        if reason not in reasons:
            reasons.append(reason)
        if len(reasons) >= 2:
            break

    if not reasons:
        reasons.append("Conditions homogènes et favorables.")

    return SportScore(
        sport=sport,
        label=profile.label,
        score=final_score,
        status=_status(final_score),
        factors=factors,
        reasons=reasons,
    )


def calculate_all_scores(conditions: Conditions) -> dict[SportId, SportScore]:
    return {sport: calculate_score(conditions, sport) for sport in PROFILES}
