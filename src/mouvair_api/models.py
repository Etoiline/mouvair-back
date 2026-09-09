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
WeatherFactorId = Literal["temperature", "rain", "wind"]
AirQualityCategory = Literal[
    "Bonne",
    "Acceptable",
    "Modérée",
    "Mauvaise",
    "Très mauvaise",
    "Extrêmement mauvaise",
]
PollenId = Literal["alder", "birch", "grass", "mugwort", "olive", "ragweed"]
ForecastDataState = Literal["fresh", "refresh_failed", "stale"]


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
    weather_codes: list[int | None] = Field(default_factory=list)


class PollenForecast(BaseModel):
    alder: float | None = None
    birch: float | None = None
    grass: float | None = None
    mugwort: float | None = None
    olive: float | None = None
    ragweed: float | None = None


class SlotConditions(Conditions):
    apparent_temperature_start: float | None = None
    apparent_temperature_end: float | None = None


class ScoreFactor(BaseModel):
    id: WeatherFactorId
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
    # Le contrat public conserve chaque famille de données dans sa section, sans copie à plat.
    sport: SportId
    label: str
    weather: WeatherScore
    air: AirScore
    overall: ScoreSummary


class HourlyForecast(BaseModel):
    time: str
    end_time: str
    conditions: SlotConditions
    pollen: PollenForecast
    scores: dict[SportId, SportScore]


class ForecastResponse(BaseModel):
    latitude: float
    longitude: float
    timezone: str
    generated_at: str
    hours: list[HourlyForecast]
    data_state: ForecastDataState = "fresh"
    from_cache: bool = False
    recommendation_available: bool = True
