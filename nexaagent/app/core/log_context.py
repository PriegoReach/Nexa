"""Contextvars para correlación de logs (request_id, conversation_id)."""
import logging
from contextvars import ContextVar

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
conversation_id_var: ContextVar[str] = ContextVar("conversation_id", default="-")


class ContextFilter(logging.Filter):
    """Inyecta request_id y conversation_id en cada LogRecord desde los contextvars."""
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.conversation_id = conversation_id_var.get()
        return True