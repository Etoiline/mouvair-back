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

## Créer une release

Le dépôt doit être propre et `CHANGELOG.md` doit contenir au moins une ligne de changement non
publiée sous son titre. Le script demande s’il faut incrémenter la version majeure, mineure ou
corrective, synchronise `pyproject.toml`, la version exposée par l’API et le changelog, puis crée le
commit `🔖 Release x.y.z` :

```powershell
python scripts/release.py
```

Pour prévisualiser la prochaine version sans modifier le dépôt ni créer de commit :

```powershell
python scripts/release.py --dry-run
```

## Endpoints

- `GET /health`
- `GET /api/locations?q=Lyon`
- `GET /api/forecast?latitude=45.75&longitude=4.85`
