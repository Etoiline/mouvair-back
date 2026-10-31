# Image d’exécution du backend ; Redis est un service distinct, accessible par REDIS_URL.
FROM python:3.12-slim

# Évite les fichiers bytecode dans l’image et affiche les journaux sans mise en tampon.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Les chemins de construction et d’exécution restent indépendants du poste hôte.
WORKDIR /app

# Le package et ses métadonnées suffisent ; tests, .env et environnement local sont exclus.
COPY pyproject.toml README.md ./
COPY src ./src

# Installe seulement les dépendances d’exécution et crée un utilisateur sans privilèges root.
RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 mouvair

# Le serveur utilise cet utilisateur même si la construction s’est faite comme root.
USER mouvair

# Port documenté pour l’usage local ; l’hébergeur peut fournir PORT à l’exécution.
EXPOSE 8000

# Le shell développe PORT, puis exec transmet les signaux d’arrêt directement à Uvicorn.
CMD ["sh", "-c", "exec uvicorn mouvair_api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
