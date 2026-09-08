import os
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .forecast_service import forecast_service
from .models import ForecastResponse, Location
from .open_meteo import OpenMeteoError, search_locations

app = FastAPI(
    title="Mouv’Air API",
    version=__version__,
    description="Conditions horaires pour la course, le vélo et la marche.",
)

origins = [
    origin.strip()
    for origin in os.getenv("FRONTEND_ORIGINS", "http://localhost:5173").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/locations", response_model=list[Location])
async def locations(
    q: Annotated[str, Query(min_length=2, max_length=100)],
    count: Annotated[int, Query(ge=1, le=10)] = 5,
) -> list[Location]:
    try:
        return await search_locations(q, count)
    except OpenMeteoError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/forecast", response_model=ForecastResponse)
async def forecast(
    latitude: Annotated[float, Query(ge=-90, le=90)],
    longitude: Annotated[float, Query(ge=-180, le=180)],
) -> ForecastResponse:
    try:
        result = await forecast_service.get_forecast(latitude, longitude)
    except OpenMeteoError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if not result.hours:
        raise HTTPException(status_code=502, detail="Prévisions horaires indisponibles")
    return result
