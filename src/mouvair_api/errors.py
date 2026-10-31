"""Erreurs attendues communes aux adaptateurs de données et aux services métier.

Ce contrat permet aux services de déclencher un repli et aux routes de produire
une réponse HTTP sans importer le transport concret à l’origine du problème.
"""


class DataUnavailableError(RuntimeError):
    """Signale des données indisponibles, invalides ou une récupération non aboutie."""
