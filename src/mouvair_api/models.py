from typing import Literal

from pydantic import BaseModel, Field

SportId = Literal["easy_run", "hard_run", "cycling", "walking"]
ScoreStatus = Literal[
    "favorable",
    "correct",
    "mitigé",
    "défavorable",
    "très défavorable",
    "incomplet",
]
LegacyScoreStatus = Literal["favorable", "correct", "mitigé", "défavorable", "incomplet"]
WeatherFactorId = Literal["temperature", "rain", "wind"]
ScoreFactorId = Literal["air", "temperature", "rain", "wind"]
AirQualityCategory = Literal[
    "Bonne",
    "Acceptable",
    "Modérée",
    "Mauvaise",
    "Très mauvaise",
    "Extrêmement mauvaise",
]


class Location(BaseModel):
    id: int
    name: str
    latitude: float
    longitude: float
    country: str | None = None
    admin1: str | None = None
    timezone: str | None = None


class Conditions(BaseModel):
    apparent_temperature: float | None = None
    precipitation: float | None = None
    wind_speed: float | None = None
    wind_gusts: float | None = None
    european_aqi: float | None = None
    weather_code: int | None = None
    weather_codes: list[int] = Field(default_factory=list)


class ScoreFactor(BaseModel):
    id: ScoreFactorId
    label: str
    score: int | None = Field(default=None, ge=0, le=100)
    value: float | None = None
    unit: str


class ScoreSummary(BaseModel):
    score: int | None = Field(default=None, ge=0, le=100)
    status: ScoreStatus


class WeatherScore(ScoreSummary):
    factors: list[ScoreFactor]
    reasons: list[str]
    alerts: list[str]


class AirRecommendations(BaseModel):
    general_population: str
    sensitive_people: str


class AirScore(BaseModel):
    score: int | None = Field(default=None, ge=0, le=100)
    aqi: float | None = None
    category: AirQualityCategory | None = None
    recommendations: AirRecommendations | None = None
    reason: str | None = None


class SportScore(BaseModel):
    sport: SportId
    label: str
    weather: WeatherScore
    air: AirScore
    overall: ScoreSummary
    score: int | None = Field(default=None, ge=0, le=100)
    status: LegacyScoreStatus
    factors: list[ScoreFactor]
    reasons: list[str]


class HourlyForecast(BaseModel):
    time: str
    conditions: Conditions
    scores: dict[SportId, SportScore]


class ForecastResponse(BaseModel):
    latitude: float
    longitude: float
    timezone: str
    generated_at: str
    hours: list[HourlyForecast]
