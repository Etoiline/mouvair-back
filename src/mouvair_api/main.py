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
from .forecast_cache import RedisForecastCache
from .forecast_service import ForecastService
from .geocoding_service import GeocodingService
from .models import ForecastResponse, Location
from .open_meteo import OpenMeteoError, OpenMeteoGateway, OpenMeteoQuotaError
from .rate_limit import ClientRateLimiter, OpenMeteoQuota, RateLimitExceeded
from .redis_store import RedisStore


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        store = RedisStore(resolved_settings)
        await store.connect()
        http_client = httpx.AsyncClient(
            timeout=15,
            headers={"User-Agent": f"MouvAir/{__version__}"},
        )
        quota = OpenMeteoQuota(store, resolved_settings)
        gateway = OpenMeteoGateway(quota, http_client)
        forecast_cache = RedisForecastCache(store, resolved_settings)

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
            await http_client.aclose()
            await store.close()

    application = FastAPI(
        title="Mouv’Air API",
        version=__version__,
        description="Conditions horaires pour la course, le vélo et la marche.",
        lifespan=lifespan,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(resolved_settings.frontend_origins),
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    @application.middleware("http")
    async def apply_client_limit(request: Request, call_next):
        endpoint = {
            "/api/locations": "locations",
            "/api/forecast": "forecast",
        }.get(request.url.path)
        if endpoint is not None and request.method == "GET":
            identity = client_identity(request, resolved_settings)
            try:
                await request.app.state.client_limiter.check(endpoint, identity)
            except RateLimitExceeded as exc:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Trop de requêtes, veuillez réessayer plus tard"},
                    headers={"Retry-After": str(exc.retry_after)},
                )
        return await call_next(request)

    @application.get("/health")
    async def health(request: Request) -> dict[str, str]:
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
        try:
            return await request.app.state.geocoding_service.search(q, count)
        except OpenMeteoQuotaError as exc:
            raise HTTPException(
                status_code=503,
                detail=str(exc),
                headers={"Retry-After": str(exc.retry_after)},
            ) from exc
        except OpenMeteoError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @application.get("/api/forecast", response_model=ForecastResponse)
    async def forecast(
        request: Request,
        latitude: Annotated[float, Query(ge=-90, le=90)],
        longitude: Annotated[float, Query(ge=-180, le=180)],
    ) -> ForecastResponse:
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
        except OpenMeteoError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        if not result.hours:
            raise HTTPException(status_code=502, detail="Prévisions horaires indisponibles")
        return result

    return application


app = create_app()
