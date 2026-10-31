"""Définit les données internes et le contrat JSON exposé au frontend.

Pydantic valide les objets construits et sérialise les réponses HTTP ou le cache.
Les valeurs None représentent une information indisponible, jamais une mesure
nulle. Les familles météo, air et globale restent séparées dans chaque score.
"""

from typing import Literal

from pydantic import BaseModel, Field

# Ces identifiants servent de clés stables dans la réponse et dans les types frontend.
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
    """Ville issue du géocodage, avec les coordonnées nécessaires aux prévisions.

    Le pays et la région permettent de distinguer les villes homonymes. Le fuseau
    peut être absent du géocodage ; celui des prévisions vient de la réponse météo.
    """
    id: int
    name: str
    latitude: float
    longitude: float
    country: str | None = None
    admin1: str | None = None
    timezone: str | None = None


class Conditions(BaseModel):
    """Mesures fournies au moteur de score pour une activité et un créneau.

    Unités : température ressentie en °C, précipitations en mm, vent et rafales
    en km/h. L’AQI est un indice. weather_codes conserve les deux bornes horaires
    afin de détecter un événement même s’il n’apparaît qu’à l’une d’elles.
    """
    apparent_temperature: float | None = None
    precipitation: float | None = None
    wind_speed: float | None = None
    wind_gusts: float | None = None
    european_aqi: float | None = None
    weather_code: int | None = None
    weather_codes: list[int | None] = Field(default_factory=list)


class PollenForecast(BaseModel):
    """Concentrations de six pollens en grains/m³, indépendantes des trois notes."""
    alder: float | None = None
    birch: float | None = None
    grass: float | None = None
    mugwort: float | None = None
    olive: float | None = None
    ragweed: float | None = None


class SlotConditions(Conditions):
    """Mesures affichables du créneau, avec les deux températures d’origine.

    La température héritée correspond au profil de course tranquille. Pour une
    autre activité, sa valeur de calcul figure dans weather.factors.
    """
    apparent_temperature_start: float | None = None
    apparent_temperature_end: float | None = None


class ScoreFactor(BaseModel):
    """Sous-note météo et mesure utilisée, pour rendre le calcul explicable.

    La valeur du facteur vent inclut l’effet pondéré des rafales ; elle peut donc
    différer du seul vent moyen présenté dans les conditions du créneau.
    """
    id: WeatherFactorId
    label: str
    score: int | None = Field(default=None, ge=0, le=100)
    value: float | None = None
    unit: str


class ScoreSummary(BaseModel):
    """Note entière sur 100 et appréciation, ou note absente et statut incomplet."""
    score: int | None = Field(default=None, ge=0, le=100)
    status: ScoreStatus


class WeatherScore(ScoreSummary):
    """Résultat météo avec facteurs numériques, explications et alertes d’événements."""
    factors: list[ScoreFactor]
    reasons: list[str]
    alerts: list[str]


class AirRecommendations(BaseModel):
    """Messages généraux liés à la catégorie AQI pour deux groupes de population."""
    general_population: str
    sensitive_people: str


class AirScore(BaseModel):
    """Note air et catégorie dérivée de l’AQI d’origine, avant conversion en note.

    L’absence d’AQI laisse les champs descriptifs à None. Cette section possède
    une catégorie de qualité de l’air plutôt qu’un statut de note météo.
    """
    score: int | None = Field(default=None, ge=0, le=100)
    aqi: float | None = None
    category: AirQualityCategory | None = None
    recommendations: AirRecommendations | None = None
    reason: str | None = None


class SportScore(BaseModel):
    """Résultats d’une activité : météo spécifique, air commun et combinaison globale."""
    # Le contrat public conserve chaque famille de données dans sa section, sans copie à plat.
    sport: SportId
    label: str
    weather: WeatherScore
    air: AirScore
    overall: ScoreSummary


class HourlyForecast(BaseModel):
    """Créneau d’une heure réelle et résultats des quatre activités.

    time et end_time sont des dates ISO avec décalage UTC, ce qui distingue les
    heures locales répétées lors d’un changement d’heure.
    """
    time: str
    end_time: str
    conditions: SlotConditions
    pollen: PollenForecast
    scores: dict[SportId, SportScore]


class ForecastResponse(BaseModel):
    """Prévision d’une position accompagnée des informations de fraîcheur.

    generated_at reste l’instant réel de récupération, même depuis un cache.
    data_state décrit la fraîcheur, indépendamment de la complétude des notes.
    recommendation_available autorise la recherche du meilleur créneau côté
    frontend ; celui-ci doit encore trouver au moins une note globale disponible.
    """
    latitude: float
    longitude: float
    timezone: str
    generated_at: str
    hours: list[HourlyForecast]
    data_state: ForecastDataState = "fresh"
    from_cache: bool = False
    recommendation_available: bool = True
