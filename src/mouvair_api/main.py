"""Assemble l’API HTTP et relie ses routes aux services du backend.

Ce module gère le cycle de vie des connexions, les contrôles d’entrée et la
traduction des erreurs métier en réponses HTTP. Les règles de score, de cache
et de quota restent dans leurs modules respectifs pour être testées séparément.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__
from .client_identity import client_identity
from .config import Settings
from .diagnostics import RequestDiagnosticsMiddleware, configure_logging, log_event
from .errors import DataUnavailableError
from .forecast_cache import RedisForecastCache
from .forecast_service import ForecastService
from .geocoding_service import GeocodingService
from .models import ForecastResponse, Location
from .open_meteo import OpenMeteoGateway, OpenMeteoQuotaError
from .rate_limit import ClientRateLimiter, OpenMeteoQuota, RateLimitExceeded
from .redis_store import RedisStore


def create_app(settings: Settings | None = None) -> FastAPI:
    """Construit une application isolée, avec une configuration injectable en test.

    La configuration est résolue une fois par application. Les ressources réseau
    sont créées plus tard, dans lifespan, au démarrage effectif du serveur.
    """
    resolved_settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        """Ouvre les ressources partagées du processus et les ferme à son arrêt."""
        configure_logging()
        store = RedisStore(resolved_settings)
        await store.connect()
        # Le pool HTTP est réutilisé par les recherches et les deux API de prévision.
        # Une panne Redis laisse démarrer l’API ; la politique de quota décide du repli.
        http_client = httpx.AsyncClient(
            timeout=15,
            headers={"User-Agent": f"MouvAir/{__version__}"},
        )
        quota = OpenMeteoQuota(store, resolved_settings)
        gateway = OpenMeteoGateway(quota, http_client)
        forecast_cache = RedisForecastCache(store, resolved_settings)

        # Les routes accèdent aux mêmes services pendant toute la vie de l’application.
        # Chaque worker possède son propre L1 ; Redis partage le L2 et les compteurs.
        application.state.settings = resolved_settings
        application.state.redis_store = store
        application.state.client_limiter = ClientRateLimiter(store, resolved_settings)
        application.state.geocoding_service = GeocodingService(
            gateway.search_locations,
            store,
            resolved_settings,
        )
        application.state.forecast_service = ForecastService(
            gateway.get_forecast,
            shared_cache=forecast_cache,
        )
        try:
            yield
        finally:
            # La fermeture a lieu aussi si le serveur s’arrête après une exception.
            await http_client.aclose()
            await store.close()

    application = FastAPI(
        title="Mouv’Air API",
        version=__version__,
        description="Conditions horaires pour la course, le vélo et la marche.",
        lifespan=lifespan,
    )

    @application.middleware("http")
    async def apply_client_limit(request: Request, call_next):
        """Compte les GET métier avant validation et avant toute lecture de cache.

        Les contrôles de santé et les prérequêtes CORS restent accessibles. Le
        quota du fournisseur, distinct, n’est consommé qu’en cas d’appel sortant.
        """
        endpoint = {
            "/api/locations": "locations",
            "/api/forecast": "forecast",
        }.get(request.url.path)
        if endpoint is not None and request.method == "GET":
            identity = client_identity(request, resolved_settings)
            try:
                await request.app.state.client_limiter.check(endpoint, identity)
            except RateLimitExceeded as exc:
                log_event(
                    "http.client_limited", level=logging.WARNING,
                    endpoint=endpoint, retry_after=exc.retry_after,
                )
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Trop de requêtes, veuillez réessayer plus tard"},
                    headers={"Retry-After": str(exc.retry_after)},
                )
        return await call_next(request)

    # Le dernier middleware ajouté s’exécute en premier : CORS doit envelopper
    # le limiteur pour traiter aussi ses réponses 429, produites sans appeler la route.
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(resolved_settings.frontend_origins),
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["*"],
        # Le navigateur peut lire le délai de reprise sur les réponses 429 et 503.
        expose_headers=["Retry-After"],
    )
    # À l’extérieur de CORS et du limiteur pour observer leurs réponses anticipées aussi.
    application.add_middleware(RequestDiagnosticsMiddleware)

    @application.get("/health")
    async def health(request: Request) -> dict[str, str]:
        """Signale Redis sans rendre le processus HTTP inaccessible en mode dégradé."""
        # force=True teste la connexion même pendant le délai de repli Redis.
        redis_available = await request.app.state.redis_store.ping(force=True)
        return {
            "status": "ok" if redis_available else "degraded",
            "redis": "ok" if redis_available else "unavailable",
        }

    @application.get("/api/locations", response_model=list[Location])
    async def locations(
        request: Request,
        q: Annotated[str, Query(min_length=3, max_length=100)],
        count: Annotated[int, Query(ge=1, le=10)] = 5,
    ) -> list[Location]:
        """Recherche des villes après validation de la longueur et du nombre demandé."""
        try:
            return await request.app.state.geocoding_service.search(q, count)
        except OpenMeteoQuotaError as exc:
            # Une indisponibilité du budget est temporaire : le client reçoit un délai.
            raise HTTPException(
                status_code=503,
                detail=str(exc),
                headers={"Retry-After": str(exc.retry_after)},
            ) from exc
        except DataUnavailableError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @application.get("/api/forecast", response_model=ForecastResponse)
    async def forecast(
        request: Request,
        latitude: Annotated[float, Query(ge=-90, le=90)],
        longitude: Annotated[float, Query(ge=-180, le=180)],
    ) -> ForecastResponse:
        """Renvoie les créneaux calculés ou le repli déjà préparé par le service.

        La route ne recalcule pas les notes et ne décide pas de la fraîcheur :
        elle conserve le contrat HTTP et traduit seulement les échecs propagés.
        """
        try:
            result = await request.app.state.forecast_service.get_forecast(
                latitude, longitude
            )
        except OpenMeteoQuotaError as exc:
            raise HTTPException(
                status_code=503,
                detail=str(exc),
                headers={"Retry-After": str(exc.retry_after)},
            ) from exc
        except DataUnavailableError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        if not result.hours:
            # Une réponse vide ne permettrait pas au frontend de présenter sa chronologie.
            raise HTTPException(status_code=502, detail="Prévisions horaires indisponibles")
        return result

    return application


app = create_app()
