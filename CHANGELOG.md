# Changelog

- Separate forecast assembly and time rules from Open-Meteo transport, introduce shared data errors, and reorganize tests by responsibility.

- Fix shared forecast cache identity to use requested coordinates, preserve provider metadata, and isolate legacy entries and locks in a v2 namespace, with regression and Redis integration coverage.

- Add detailed French comments across backend modules, tests, release tooling and configuration, and document the project structure and request flow in the README.

## 0.4.0 - 2026-09-13

- Add a production Docker image and safely support Railway's trusted client IP header.

- Document the complete local Redis verification workflow with Docker and persistent volumes.

- Raise per-client request limits to 120 location searches and 40 forecast requests per minute, with proportional hourly ceilings.

- Add Redis-backed Open-Meteo quota reservations, shared geocoding and forecast caches, distributed locks, per-client rate limits, fail-closed degradation, and real Redis integration coverage.

## 0.3.0 - 2026-09-09

- Remove deprecated flat sport-score fields and expose weather, air, and overall results only through their nested sections.

- Calculate forecast windows from full hours in the location timezone, including fractional UTC offsets and daylight-saving transitions, and apply the same alignment to cached data.

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
