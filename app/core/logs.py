"""Structured logging: JSON lines carrying the current request ID."""

import json
import logging
import os
from contextvars import ContextVar
from datetime import UTC, datetime

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

_HANDLER_MARK = "_llmgw_handler"


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Extra data goes in record.fields (a dict)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id:
            payload["request_id"] = request_id
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            for key, value in fields.items():
                payload.setdefault(key, value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


class TextFormatter(logging.Formatter):
    """Human-friendly format for local development (LOG_FORMAT=text)."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        request_id = request_id_var.get()
        if request_id:
            text += f" request_id={request_id}"
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            text += "".join(f" {key}={value}" for key, value in fields.items())
        return text


def configure_logging(level: str | None = None, fmt: str | None = None) -> None:
    """Set up root logging once. Safe to call repeatedly.

    LOG_LEVEL (default INFO) and LOG_FORMAT (json or text, default json)
    can come from the environment.
    """
    level_name = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    log_format = (fmt or os.environ.get("LOG_FORMAT", "json")).lower()

    root = logging.getLogger()
    handler = next((h for h in root.handlers if getattr(h, _HANDLER_MARK, False)), None)
    if handler is None:
        handler = logging.StreamHandler()
        setattr(handler, _HANDLER_MARK, True)
        root.addHandler(handler)
    handler.setFormatter(JsonFormatter() if log_format == "json" else TextFormatter())
    root.setLevel(logging.getLevelNamesMapping().get(level_name, logging.INFO))

    # Our request log line replaces uvicorn's access log, and httpx logs a
    # line per provider call that we do not need at INFO.
    for noisy in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
