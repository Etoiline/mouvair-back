# Mouv’Air — API

Cette API récupère les données Open-Meteo et produit les scores horaires des quatre activités.

## Commandes

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m uvicorn mouvair_api.main:app --app-dir src --reload
```

Tests et qualité :

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
```

## Endpoints

- `GET /health`
- `GET /api/locations?q=Lyon`
- `GET /api/forecast?latitude=45.75&longitude=4.85`
