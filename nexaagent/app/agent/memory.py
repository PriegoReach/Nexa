import json
import logging

import redis.asyncio as redis
from redis.exceptions import RedisError

from app.core.config import settings

logger = logging.getLogger("nexa.memory")

_client: redis.Redis | None = None
_MAX_TURNS = 20  # how many recent messages to keep in short-term memory


def _redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(settings.redis_url, decode_responses=True)
    return _client


def _key(conversation_id: int) -> str:
    return f"nexa:memory:{conversation_id}"


async def ping() -> None:
    """Readiness: lanza si Redis no responde. Reusa el cliente/pool ya abierto
    por memory.py (un solo pool por proceso), para que la sonda de /health/ready
    no abra una conexión nueva en cada chequeo."""
    await _redis().ping()


async def load_history(conversation_id: int) -> list[dict]:
    try:
        raw = await _redis().lrange(_key(conversation_id), 0, -1)
        return [json.loads(item) for item in raw]
    except RedisError as exc:
        # Degradación: el agente responde sin historial reciente, no revienta.
        logger.warning(
            "redis unavailable on load, degrading to empty history",
            extra={
                "event": "redis_degraded",
                "op": "load_history",
                "exc_type": type(exc).__name__,
            },
        )
        return []


async def append(conversation_id: int, role: str, content: str) -> None:
    key = _key(conversation_id)
    try:
        client = _redis()
        await client.rpush(key, json.dumps({"role": role, "content": content}))
        await client.ltrim(key, -_MAX_TURNS, -1)
        await client.expire(key, 60 * 60 * 24)  # 24h TTL for short-term memory
    except RedisError as exc:
        # No relanza: Postgres ya tiene el mensaje durable. Solo se pierde la copia
        # rápida en Redis de este turno.
        logger.warning(
            "redis unavailable on append, skipping short-term cache",
            extra={
                "event": "redis_degraded",
                "op": "append",
                "exc_type": type(exc).__name__,
            },
        )
