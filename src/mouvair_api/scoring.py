"""Calcule des notes explicables de conditions extérieures pour quatre activités.

Chaque profil transforme température ressentie, pluie et vent en sous-notes par
interpolation. La météo et l’air sont combinés avec des plafonds empêchant un
facteur très dégradé d’être masqué par les autres. Les calculs gardent leurs
décimales ; seules les valeurs publiques sont arrondies. Ce module est indépendant
du réseau et du cache, ce qui permet de vérifier les règles avec de simples données.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from math import isfinite

from .models import (
    AirQualityCategory,
    AirRecommendations,
    AirScore,
    Conditions,
    ScoreFactor,
    ScoreStatus,
    ScoreSummary,
    SportId,
    SportScore,
    WeatherFactorId,
    WeatherScore,
)

# Chaque point associe une mesure physique à une note ; les mesures sont croissantes.
Curve = tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class SportProfile:
    """Paramètres métier d’une activité d’une heure.

    weather_weights pondère les trois facteurs météo. weather_share et air_share
    pondèrent les deux familles dans la globale. Les courbes utilisent les °C,
    mm et km/h ; snow_cap ajoute la limite propre à l’activité en cas de neige.
    """
    id: SportId
    label: str
    activity_name: str
    weather_weights: dict[WeatherFactorId, float]
    weather_share: float
    air_share: float
    snow_cap: int
    temperature_curve: Curve
    rain_curve: Curve
    wind_curve: Curve


@dataclass(frozen=True)
class RawWeatherScore:
    """Sépare la météo publique arrondie de sa valeur précise et des plafonds d’événements.

    La globale doit utiliser score, et non public.score, pour éviter qu’un arrondi
    intermédiaire change le résultat final. Les plafonds d’événements y sont réappliqués.
    """
    public: WeatherScore
    score: float | None
    event_caps: tuple[int, ...]


@dataclass(frozen=True)
class RawAirScore:
    """Garde l’AQI converti sans arrondi en complément de la section air affichable."""
    public: AirScore
    score: float | None


# Les règles spécifiques sont rassemblées ici pour modifier un sport sans toucher
# à l’algorithme. Un plateau à 100 décrit sa plage idéale ; hors des extrêmes,
# la première ou la dernière note de la courbe reste appliquée.
PROFILES: dict[SportId, SportProfile] = {
    "easy_run": SportProfile(
        id="easy_run",
        label="Course tranquille · 1 h",
        activity_name="la course tranquille",
        weather_weights={"temperature": 0.50, "rain": 0.25, "wind": 0.25},
        weather_share=0.65,
        air_share=0.35,
        snow_cap=40,
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
        activity_name="la course intense",
        weather_weights={"temperature": 0.60, "rain": 0.20, "wind": 0.20},
        weather_share=0.60,
        air_share=0.40,
        snow_cap=40,
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
        activity_name="le vélo",
        weather_weights={"temperature": 0.35, "rain": 0.30, "wind": 0.35},
        weather_share=0.70,
        air_share=0.30,
        snow_cap=20,
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
        activity_name="la marche",
        weather_weights={"temperature": 0.40, "rain": 0.30, "wind": 0.30},
        weather_share=0.70,
        air_share=0.30,
        snow_cap=60,
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

# Un code inconnu ou une borne absente rend la météo incomplète : les alertes
# déjà reconnues restent visibles, mais une absence de code ne vaut pas « ciel clair ».
VALID_WEATHER_CODES = {
    0,
    1,
    2,
    3,
    45,
    48,
    51,
    53,
    55,
    56,
    57,
    61,
    63,
    65,
    66,
    67,
    71,
    73,
    75,
    77,
    80,
    81,
    82,
    85,
    86,
    95,
    96,
    99,
}
THUNDERSTORM_CODES = {95, 96, 99}
FREEZING_PRECIPITATION_CODES = {56, 57, 66, 67}
SNOW_CODES = {71, 73, 75, 77, 85, 86}

THUNDERSTORM_ALERT = "Orage prévu sur ce créneau : reportez votre sortie."
FREEZING_PRECIPITATION_ALERT = (
    "Précipitations verglaçantes prévues : risque de surfaces glissantes, "
    "reportez votre sortie."
)
SNOW_ALERT = "Chutes de neige prévues : adaptez votre sortie aux conditions du terrain."


def interpolate(value: float, curve: Curve) -> float:
    """Calcule une sous-note par interpolation linéaire sur une courbe ordonnée.

    Entre deux points, la note évolue proportionnellement à la mesure. Au-delà
    des bornes, la note de l’extrémité est conservée sans extrapolation.
    """
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

    raise ValueError("La courbe de score doit être triée par valeur")


def clamp(value: float, minimum: float = 0, maximum: float = 100) -> float:
    """Borne une valeur dans un intervalle fermé, par défaut l’échelle de notation."""
    return max(minimum, min(value, maximum))


def round_half_up(value: float) -> int:
    """Arrondit à l’entier le plus proche en envoyant les demis vers le haut.

    Decimal reçoit une chaîne pour conserver la représentation décimale de la
    valeur ; ROUND_HALF_UP évite l’arrondi des demis vers l’entier pair de round.
    """
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def score_status(score: int) -> ScoreStatus:
    """Classe une note affichée entière avec des seuils inclusifs 80, 60, 40 et 20."""
    if score >= 80:
        return "favorable"
    if score >= 60:
        return "correct"
    if score >= 40:
        return "mitigé"
    if score >= 20:
        return "défavorable"
    return "très défavorable"


def _is_finite(value: float | None) -> bool:
    """Refuse les absences, NaN et infinis avant d’évaluer une courbe."""
    return value is not None and isfinite(value)


def _is_non_negative(value: float | None) -> bool:
    """Ajoute la contrainte physique positive ou nulle pour pluie, vent et AQI."""
    return _is_finite(value) and value >= 0


def select_apparent_temperature(
    start: float | None, end: float | None, sport: SportId
) -> float | None:
    """Retient la borne donnant la sous-note thermique la plus basse pour l’activité.

    Choisir simplement le maximum de température serait faux en période froide.
    Les deux bornes doivent être connues ; en cas d’égalité, le départ est retenu.
    """
    if not _is_finite(start) or not _is_finite(end):
        return None
    curve = PROFILES[sport].temperature_curve
    start_score = interpolate(start, curve)
    end_score = interpolate(end, curve)
    return start if start_score <= end_score else end


def _wind_exposure(conditions: Conditions, sport: SportId) -> float | None:
    """Combine vent moyen et rafales avec une sensibilité plus élevée pour le vélo."""
    if not _is_non_negative(conditions.wind_speed) or not _is_non_negative(
        conditions.wind_gusts
    ):
        return None
    gust_ratio = 0.75 if sport == "cycling" else 0.60
    return max(conditions.wind_speed, conditions.wind_gusts * gust_ratio)


def _condition_codes(conditions: Conditions) -> tuple[int | None, ...]:
    """Réunit les codes sans doublons en conservant les bornes explicitement inconnues."""
    codes: list[int | None] = []
    if conditions.weather_code is not None:
        codes.append(conditions.weather_code)
    codes.extend(conditions.weather_codes)
    return tuple(dict.fromkeys(codes))


def _event_alerts_and_caps(
    codes: tuple[int | None, ...], profile: SportProfile
) -> tuple[list[str], tuple[int, ...]]:
    """Détecte les événements et conserve l’ordre d’alerte orage, verglas, puis neige.

    Tous les plafonds détectés sont retournés ; prendre leur minimum fera
    prévaloir le plus sévère, même si plusieurs événements coexistent.
    """
    alerts: list[str] = []
    caps: list[int] = []
    code_set = set(codes)

    if code_set & THUNDERSTORM_CODES:
        alerts.append(THUNDERSTORM_ALERT)
        caps.append(10)
    if code_set & FREEZING_PRECIPITATION_CODES:
        alerts.append(FREEZING_PRECIPITATION_ALERT)
        caps.append(10)
    if code_set & SNOW_CODES:
        alerts.append(SNOW_ALERT)
        caps.append(profile.snow_cap)

    return alerts, tuple(caps)


def _factor_reason(
    factor_id: WeatherFactorId, profile: SportProfile, *, strongly: bool
) -> str:
    """Produit un message français stable pour un facteur pénalisant et son activité."""
    emphasis = " fortement" if strongly else ""
    if factor_id == "temperature":
        return (
            f"La température ressentie pénalise{emphasis} ce créneau "
            f"pour {profile.activity_name}."
        )
    if factor_id == "rain":
        return (
            f"Les précipitations prévues pénalisent{emphasis} ce créneau "
            f"pour {profile.activity_name}."
        )
    return (
        f"Le vent et les rafales prévus pénalisent{emphasis} ce créneau "
        f"pour {profile.activity_name}."
    )


def _weather_reasons(
    factor_scores: dict[WeatherFactorId, float],
    profile: SportProfile,
    weighted_score: float,
    alerts: list[str],
) -> list[str]:
    """Choisit au plus deux facteurs expliquant la dégradation de la météo.

    Un facteur qui impose le plafond le plus bas est prioritaire. Les suivants
    sont classés par perte pondérée ; l’ordre température, pluie, vent départage
    les égalités pour garantir des explications déterministes.
    """
    factor_order: tuple[WeatherFactorId, ...] = ("temperature", "rain", "wind")
    progressive_caps = {
        factor_id: min(100, factor_scores[factor_id] + 20) for factor_id in factor_order
    }
    binding_factors = [
        factor_id
        for factor_id in factor_order
        if progressive_caps[factor_id] < weighted_score
    ]

    selected: list[tuple[WeatherFactorId, bool]] = []
    if binding_factors:
        lowest_cap = min(progressive_caps[factor_id] for factor_id in binding_factors)
        first = next(
            factor_id
            for factor_id in binding_factors
            if progressive_caps[factor_id] == lowest_cap
        )
        selected.append((first, True))

    losses = sorted(
        factor_order,
        key=lambda factor_id: (
            -profile.weather_weights[factor_id] * (100 - factor_scores[factor_id]),
            factor_order.index(factor_id),
        ),
    )
    for factor_id in losses:
        loss = profile.weather_weights[factor_id] * (100 - factor_scores[factor_id])
        if loss <= 0 or any(chosen == factor_id for chosen, _ in selected):
            continue
        selected.append((factor_id, False))
        if len(selected) == 2:
            break

    if not selected and not alerts:
        return ["Conditions météo favorables pour cette activité."]
    return [
        _factor_reason(factor_id, profile, strongly=strongly)
        for factor_id, strongly in selected
    ]


def _calculate_weather(conditions: Conditions, profile: SportProfile) -> RawWeatherScore:
    """Calcule les sous-notes, exige la complétude, puis applique tous les plafonds.

    Les facteurs disponibles et les alertes sont construits avant le contrôle de
    complétude pour rester consultables même quand aucune note météo n’est possible.
    """
    temperature_score = (
        interpolate(conditions.apparent_temperature, profile.temperature_curve)
        if _is_finite(conditions.apparent_temperature)
        else None
    )
    rain_score = (
        interpolate(conditions.precipitation, profile.rain_curve)
        if _is_non_negative(conditions.precipitation)
        else None
    )
    wind_exposure = _wind_exposure(conditions, profile.id)
    wind_score = (
        interpolate(wind_exposure, profile.wind_curve) if wind_exposure is not None else None
    )

    # Les mesures et les sous-notes publiques rendent chaque contribution inspectable.
    factors = [
        ScoreFactor(
            id="temperature",
            label="Température ressentie",
            score=round_half_up(temperature_score) if temperature_score is not None else None,
            value=(
                conditions.apparent_temperature
                if _is_finite(conditions.apparent_temperature)
                else None
            ),
            unit="°C",
        ),
        ScoreFactor(
            id="rain",
            label="Précipitations",
            score=round_half_up(rain_score) if rain_score is not None else None,
            value=conditions.precipitation if _is_non_negative(conditions.precipitation) else None,
            unit="mm",
        ),
        ScoreFactor(
            id="wind",
            label="Vent et rafales — valeur de calcul",
            score=round_half_up(wind_score) if wind_score is not None else None,
            value=wind_exposure,
            unit="km/h",
        ),
    ]

    codes = _condition_codes(conditions)
    alerts, event_caps = _event_alerts_and_caps(codes, profile)
    # Une seule borne météo inconnue suffit à suspendre la note complète du créneau.
    complete_codes = bool(codes) and all(code in VALID_WEATHER_CODES for code in codes)
    raw_factor_scores = (temperature_score, rain_score, wind_score)
    if not complete_codes or any(score is None for score in raw_factor_scores):
        return RawWeatherScore(
            public=WeatherScore(
                score=None,
                status="incomplet",
                factors=factors,
                reasons=[],
                alerts=alerts,
            ),
            score=None,
            event_caps=event_caps,
        )

    factor_scores: dict[WeatherFactorId, float] = {
        "temperature": temperature_score,
        "rain": rain_score,
        "wind": wind_score,
    }
    weighted_score = sum(
        factor_scores[factor_id] * weight
        for factor_id, weight in profile.weather_weights.items()
    )
    # Aucun facteur ne peut être compensé de plus de 20 points par les autres.
    progressive_caps = tuple(min(100, score + 20) for score in factor_scores.values())
    raw_score = min(weighted_score, *progressive_caps, *event_caps, 100)
    displayed_score = round_half_up(raw_score)

    return RawWeatherScore(
        public=WeatherScore(
            score=displayed_score,
            status=score_status(displayed_score),
            factors=factors,
            reasons=_weather_reasons(factor_scores, profile, weighted_score, alerts),
            alerts=alerts,
        ),
        score=raw_score,
        event_caps=event_caps,
    )


def _air_category(aqi: float) -> AirQualityCategory:
    """Classe l’AQI d’origine avec bornes supérieures incluses, avant conversion en note."""
    if aqi <= 20:
        return "Bonne"
    if aqi <= 40:
        return "Acceptable"
    if aqi <= 60:
        return "Modérée"
    if aqi <= 80:
        return "Mauvaise"
    if aqi <= 100:
        return "Très mauvaise"
    return "Extrêmement mauvaise"


def _air_recommendations(category: AirQualityCategory) -> AirRecommendations:
    """Associe la catégorie AQI à des messages généraux, indépendants du sport choisi."""
    if category in {"Bonne", "Acceptable"}:
        return AirRecommendations(
            general_population="Activités extérieures habituelles.",
            sensitive_people="Activités extérieures habituelles.",
        )
    if category == "Modérée":
        return AirRecommendations(
            general_population="Activités habituelles.",
            sensitive_people=(
                "Envisager de réduire les efforts intenses si des symptômes apparaissent."
            ),
        )
    if category == "Mauvaise":
        return AirRecommendations(
            general_population="Envisager de réduire les efforts intenses en cas de symptômes.",
            sensitive_people=(
                "Envisager de réduire l’activité physique, surtout dehors et en cas de symptômes."
            ),
        )
    if category == "Très mauvaise":
        return AirRecommendations(
            general_population="Envisager de réduire les efforts intenses en cas de symptômes.",
            sensitive_people=(
                "Réduire l’activité physique, surtout dehors et en cas de symptômes."
            ),
        )
    return AirRecommendations(
        general_population="Réduire l’activité physique extérieure.",
        sensitive_people="Éviter l’activité physique extérieure.",
    )


def _calculate_air(aqi: float | None) -> RawAirScore:
    """Convertit un AQI valide en max(0, 100 - AQI) et conserve sa catégorie d’origine.

    Un indice au-delà de 100 produit toujours zéro, mais garde sa valeur et sa
    catégorie pour ne pas perdre l’information décrivant la pollution prévue.
    """
    if not _is_non_negative(aqi):
        return RawAirScore(public=AirScore(score=None), score=None)

    raw_score = max(0, 100 - aqi)
    category = _air_category(aqi)
    reason = None
    if raw_score < 100:
        reason = f"Qualité de l’air {category.lower()} (indice européen {aqi:g})."

    return RawAirScore(
        public=AirScore(
            score=round_half_up(raw_score),
            aqi=aqi,
            category=category,
            recommendations=_air_recommendations(category),
            reason=reason,
        ),
        score=raw_score,
    )


def _calculate_overall(
    weather: RawWeatherScore, air: RawAirScore, profile: SportProfile
) -> ScoreSummary:
    """Combine les valeurs précises et limite la compensation entre météo et air.

    Les deux notes sont nécessaires. La moyenne propre au sport est limitée à
    chaque famille + 20 et aux événements dangereux, puis seulement arrondie.
    """
    if weather.score is None or air.score is None:
        return ScoreSummary(score=None, status="incomplet")

    weighted_score = weather.score * profile.weather_share + air.score * profile.air_share
    raw_score = min(
        weighted_score,
        min(100, weather.score + 20),
        min(100, air.score + 20),
        *weather.event_caps,
        100,
    )
    displayed_score = round_half_up(raw_score)
    return ScoreSummary(score=displayed_score, status=score_status(displayed_score))


def _build_sport_score(
    conditions: Conditions, profile: SportProfile, air: RawAirScore
) -> SportScore:
    """Assemble les trois sections publiques en gardant les calculs précis en interne."""
    weather = _calculate_weather(conditions, profile)
    overall = _calculate_overall(weather, air, profile)
    return SportScore(
        sport=profile.id,
        label=profile.label,
        weather=weather.public,
        air=air.public,
        overall=overall,
    )


def calculate_score(conditions: Conditions, sport: SportId) -> SportScore:
    """Évalue une activité dont la température représentative a déjà été sélectionnée."""
    air = _calculate_air(conditions.european_aqi)
    return _build_sport_score(conditions, PROFILES[sport], air)


def calculate_all_scores(conditions: Conditions) -> dict[SportId, SportScore]:
    """Évalue les quatre sports pour un même jeu de mesures en mutualisant le calcul air.

    L’assemblage horaire appelle plutôt calculate_score pour chaque activité,
    car la température la plus pénalisante peut différer entre deux profils.
    """
    air = _calculate_air(conditions.european_aqi)
    return {
        sport: _build_sport_score(conditions, profile, air)
        for sport, profile in PROFILES.items()
    }
