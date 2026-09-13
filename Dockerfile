FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 mouvair

USER mouvair

EXPOSE 8000

CMD ["sh", "-c", "exec uvicorn mouvair_api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
