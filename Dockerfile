# Les empreintes figent aussi les images si leurs tags sont republiés.
FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

# Même version Python que .python-version et la CI ; Redis reste un service distinct.
FROM python:3.12.16-slim@sha256:8523d6abeb48ceae0b7240d30c5808fc26cb79c1a635bacdf19cf3baecfbec07

# Évite les fichiers bytecode dans l’image et affiche les journaux sans mise en tampon.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

# Les chemins de construction et d’exécution restent indépendants du poste hôte.
WORKDIR /app

# Le package et ses métadonnées suffisent ; tests, .env et environnement local sont exclus.
COPY pyproject.toml uv.lock .python-version README.md ./
COPY src ./src

# --locked refuse un verrou périmé ; aucun outil de test ni binaire uv ne reste dans l’image.
# L’installation non éditable et son environnement isolé servent aussi à l’exécution.
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    uv sync --locked --no-dev --no-editable --no-cache \
    && useradd --create-home --uid 10001 mouvair

# Le serveur utilise cet utilisateur même si la construction s’est faite comme root.
USER mouvair

# Port documenté pour l’usage local ; l’hébergeur peut fournir PORT à l’exécution.
EXPOSE 8000

# Le shell développe PORT, puis exec transmet les signaux d’arrêt directement à Uvicorn.
# Les événements HTTP JSON remplacent le journal d’accès brut qui recopierait les paramètres.
CMD ["sh", "-c", "exec uvicorn mouvair_api.main:app --host 0.0.0.0 --port ${PORT:-8000} --no-access-log"]
