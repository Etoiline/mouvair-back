"""Doubles de temps et de fournisseur communs aux tests du service et de l’API.

Les scénarios avancent explicitement l’horloge et consomment des réponses déjà
assemblées. Aucun délai réel ni appel Open-Meteo n’est nécessaire pour tester
les seuils de fraîcheur et les erreurs après une première récupération.
"""

from dataclasses import dataclass
from datetime import datetime

from mouvair_api.errors import DataUnavailableError
from mouvair_api.models import ForecastResponse


@dataclass
class MutableClock:
    """Horloge injectable que chaque scénario avance jusqu’à une frontière précise."""

    value: datetime

    def __call__(self) -> datetime:
        return self.value


class SequencedFetcher:
    """Fournisseur déterministe renvoyant successivement des prévisions ou des erreurs."""

    def __init__(self, *outcomes: ForecastResponse | DataUnavailableError) -> None:
        self.outcomes = outcomes
        self.calls = 0

    async def __call__(self, latitude: float, longitude: float) -> ForecastResponse:
        """Compte les appels pour révéler une récupération supplémentaire ou évitable."""
        del latitude, longitude
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, DataUnavailableError):
            raise outcome
        return outcome
