"""Intención pendiente de confirmación (Fase C — confirmación asistida).

Una acción IRREVERSIBLE propuesta (hoy solo delete_task) se guarda aquí en Redis,
APARTE del historial (clave nexa:pending:<conv_id>, no nexa:memory:<id>), con TTL
corto (efímera: o se confirma pronto, o caduca). El turno siguiente la lee para
saber que es una respuesta a la propuesta y no un mensaje normal.

Degradación idéntica al patrón de memory.py: si Redis cae, se loguea WARNING y se
degrada con gracia (NO revienta). Y para una acción DESTRUCTIVA, la degradación es
FAIL-SAFE: sin intención visible, el turno de confirmación no ejecuta nada -> ante
Redis caído, NO se borra. Fallar hacia no-actuar es lo correcto aquí.
"""
import json
import logging
from contextlib import asynccontextmanager

import redis.asyncio as redis
from redis.exceptions import RedisError

from app.core.config import settings

logger = logging.getLogger("nexa.pending")

_TTL = 600  # 10 min: ventana para confirmar; luego caduca sola


@asynccontextmanager
async def _redis():
    """Cliente Redis EFÍMERO por llamada, atado al loop activo.

    pending se invoca desde DOS loops: el principal de FastAPI (get/clear en el
    orquestador) y un loop nuevo en un ThreadPoolExecutor (set_pending desde la
    tool, vía _run_async). Un cliente async Redis NO cruza loops ('Future attached
    to a different loop'). Crear+cerrar por llamada lo evita — el mismo patrón que
    worker_db.py usa con el engine NullPool para SQLAlchemy.
    """
    client = redis.from_url(settings.redis_url, decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


def _key(conversation_id: int) -> str:
    return f"nexa:pending:{conversation_id}"


async def set_pending(conversation_id: int, intent: dict) -> None:
    try:
        async with _redis() as client:
            await client.set(_key(conversation_id), json.dumps(intent), ex=_TTL)
    except RedisError as exc:
        # Fail-safe: sin intención guardada, el siguiente "sí" se trata como
        # mensaje normal -> NO se borra. Para lo destructivo, eso es lo correcto.
        logger.warning(
            "redis unavailable on set_pending, intent NOT stored (fail-safe: no delete)",
            extra={"event": "redis_degraded", "op": "set_pending", "exc_type": type(exc).__name__},
        )


async def get_pending(conversation_id: int) -> dict | None:
    try:
        async with _redis() as client:
            raw = await client.get(_key(conversation_id))
        return json.loads(raw) if raw else None
    except RedisError as exc:
        # Degrada a "no hay pendiente" -> flujo normal -> no ejecuta acción guardada.
        logger.warning(
            "redis unavailable on get_pending, degrading to no-pending",
            extra={"event": "redis_degraded", "op": "get_pending", "exc_type": type(exc).__name__},
        )
        return None


async def clear_pending(conversation_id: int) -> None:
    try:
        async with _redis() as client:
            await client.delete(_key(conversation_id))
    except RedisError as exc:
        # No relanza: si no se borra la clave, el TTL la limpia; y reintentar
        # perform_delete sobre algo ya borrado es no-op benigno.
        logger.warning(
            "redis unavailable on clear_pending, will expire via TTL",
            extra={"event": "redis_degraded", "op": "clear_pending", "exc_type": type(exc).__name__},
        )
