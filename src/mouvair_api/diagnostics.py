"""Produit les événements JSON du backend et mesure les requêtes HTTP.

Un identifiant interne relie les événements d’une requête, y compris ceux des
tâches asyncio qu’elle déclenche. Les champs sont choisis explicitement : ni
chaîne de requête, ni coordonnées, ni corps de réponse ne sont recopiés.
"""

import asyncio
import json
import logging
from contextvars import ContextVar
from datetime import UTC, datetime
from time import perf_counter
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

DiagnosticValue = str | int | float | bool | None
request_id: ContextVar[str | None] = ContextVar("mouvair_request_id", default=None)
logger = logging.getLogger("mouvair_api")


class JsonDiagnosticFormatter(logging.Formatter):
    """Sérialise une ligne exploitable par Docker ou un collecteur de journaux."""

    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "diagnostic_fields", {})
        timestamp = datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds")
        payload = {
            **fields,
            "timestamp": timestamp,
            "level": record.levelname,
            "event": record.getMessage(),
        }
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    """Rend les INFO visibles sous Uvicorn sans modifier ses loggers ni le logger racine.

    Le gestionnaire est installé une seule fois. Une configuration fournie par
    l’hébergeur sur le logger mouvair_api reste prioritaire.
    """
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonDiagnosticFormatter())
        logger.addHandler(handler)
    if logger.level == logging.NOTSET:
        logger.setLevel(logging.INFO)


def log_event(event: str, *, level: int = logging.INFO, **fields: DiagnosticValue) -> None:
    """Capture le contexte au moment du log, avant un éventuel traitement différé."""
    logger.log(
        level,
        event,
        extra={"diagnostic_fields": {**fields, "request_id": request_id.get()}},
    )


def error_fields(error: Exception) -> dict[str, DiagnosticValue]:
    """Décrit l’échec sans recopier un message pouvant contenir une URL fournisseur."""
    fields: dict[str, DiagnosticValue] = {"error_type": type(error).__name__}
    if error.__cause__ is not None:
        fields["cause_type"] = type(error.__cause__).__name__
    retry_after = getattr(error, "retry_after", None)
    if isinstance(retry_after, int):
        fields["retry_after"] = retry_after
    return fields


class RequestDiagnosticsMiddleware:
    """Mesure le traitement HTTP complet et observe aussi CORS et les refus 429.

    Ce middleware ASGI transmet les messages sans consommer le corps ni modifier
    les réponses. Le contexte est restauré même après une erreur ou une annulation.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        token = request_id.set(uuid4().hex)
        started = perf_counter()
        status_code: int | None = None
        outcome = "completed"
        failure_type: str | None = None

        async def observe_response(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, observe_response)
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception as exc:
            outcome = "error"
            failure_type = type(exc).__name__
            status_code = status_code if status_code is not None else 500
            raise
        finally:
            level = logging.INFO
            if outcome == "error":
                level = logging.ERROR
            elif outcome == "cancelled" or (status_code is not None and status_code >= 400):
                level = logging.WARNING
            elif scope["path"] == "/health" or scope["method"] == "OPTIONS":
                # Les sondes Docker et les prérequêtes ne doivent pas masquer les appels métier.
                level = logging.DEBUG
            try:
                log_event(
                    "http.request",
                    level=level,
                    method=scope["method"],
                    path=scope["path"],
                    status_code=status_code,
                    duration_ms=round((perf_counter() - started) * 1_000, 3),
                    outcome=outcome,
                    error_type=failure_type,
                )
            finally:
                request_id.reset(token)
