from typing import Literal

from pydantic import BaseModel, Field

SportId = Literal["easy_run", "hard_run", "cycling", "walking"]


class Location(BaseModel):
    id: int
    name: str
    latitude: float
    longitude: float
    country: str | None = None
    admin1: str | None = None
    timezone: str | None = None


class Conditions(BaseModel):
    apparent_temperature: float
    precipitation: float
    wind_speed: float
    wind_gusts: float
    european_aqi: float
    weather_code: int


class ScoreFactor(BaseModel):
    id: Literal["air", "temperature", "rain", "wind"]
    label: str
    score: int = Field(ge=0, le=100)
    value: float
    unit: str


class SportScore(BaseModel):
    sport: SportId
    label: str
    score: int | None = Field(default=None, ge=0, le=100)
    status: Literal["favorable", "correct", "mitigé", "défavorable", "incomplet"]
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

