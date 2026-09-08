# Changelog

- Cache forecasts by position for 15 minutes, preserve stale data after provider failures, maintain 24 future slots, expose freshness metadata, and coalesce concurrent requests.

- Build 24 offset-aware one-hour forecast slots aligned by timestamp, aggregate endpoint values according to provider semantics, add six pollen series, preserve partial slots, and select the worst apparent-temperature endpoint for each activity.

- Replace the blended activity score with separate weather, European AQI, and overall scores using the validated weights, caps, alerts, rounding, explanations, and incomplete-data rules while preserving legacy response fields.

## 0.2.0 - 2026-09-08

- Add an interactive release script that requires a clean repository and pending changelog entries, updates the project version, dates the release, and creates the gitmoji release commit.
- Keep the FastAPI metadata version synchronized with the installed backend package.
- Add shared agent instructions for French code documentation and the validation, changelog, and gitmoji commit workflow.
- Initialize the changelog from the existing backend implementation.

## 0.1.0 - 2026-09-07

- Initialize the FastAPI backend with typed response models, configurable CORS, and local development instructions.
- Add health, city search, and coordinate-based forecast endpoints with input validation and upstream error handling.
- Fetch weather and European air quality forecasts concurrently from Open-Meteo, align hourly data, and return up to 24 complete hourly forecasts from the current local hour.
- Calculate activity-specific scores for easy running, intense running, cycling, and walking using air quality, apparent temperature, precipitation, and wind exposure.
- Apply score caps for thunderstorms, poor air quality, extreme temperatures, and adverse cycling conditions, with French explanations and factor details.
- Add scoring tests for interpolation, favorable conditions, air quality caps, heat sensitivity, and thunderstorm priority.
