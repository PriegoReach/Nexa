"""Logging estructurado JSON, formato unificado para API y worker."""
import json
import logging
from datetime import datetime, timezone
from logging.config import dictConfig

from app.core.log_context import ContextFilter

# Claves propias de LogRecord que NO repetimos como 'extra'.
_RESERVED = set(logging.makeLogRecord({}).__dict__) | {
    "request_id", "conversation_id", "message", "asctime", "taskName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "conversation_id": getattr(record, "conversation_id", "-"),
        }
        # Campos arbitrarios pasados por logger.info(..., extra={...})
        for key, val in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = val
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # ensure_ascii=False preserva acentos (misma lección que la Parte 8).
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO") -> None:
    dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"context": {"()": ContextFilter}},
        "formatters": {"json": {"()": JsonFormatter}},
        "handlers": {
            "default": {
                "class": "logging.StreamHandler",
                "formatter": "json",
                "filters": ["context"],
            },
        },
        "root": {"handlers": ["default"], "level": level},
        "loggers": {
            # uvicorn trae sus handlers; los vaciamos y dejamos que propaguen
            # al root para un único formato JSON.
            "uvicorn": {"handlers": [], "propagate": True},
            "uvicorn.error": {"handlers": [], "propagate": True},
            "uvicorn.access": {"handlers": [], "propagate": True},
        },
    })