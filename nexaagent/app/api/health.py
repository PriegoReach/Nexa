"""Endpoints de salud.

- GET /health        → liveness: ¿el proceso responde? No toca dependencias.
                       (Lo usa el smoke test; serviría de liveness probe.)
- GET /health/ready  → readiness: ¿están alcanzables las dependencias para
                       servir de verdad? Comprueba DB, Redis y Ollama.

Criticidad (coherente con la resiliencia de la Parte 15):
  - DB y Ollama son CRÍTICAS: sin persistencia o sin inferencia el producto no
    funciona → su caída devuelve 503 (`not ready`).
  - Redis es DEGRADABLE: el historial de corto plazo y el encolado degradan con
    gracia (Parte 15, §4.3), así que su caída NO tumba la readiness → 200
    (`degraded`): el sistema sirve, solo sin caché de memoria reciente.

(Nota de despliegue: si Ollama fuese compartido entre varias instancias, marcarlo
crítico sacaría todas de rotación a la vez ante un hipo suyo; ahí convendría
degradarlo a no-crítico. Con un Ollama dedicado, crítico es lo correcto.)

Cada chequeo se cronometra, NUNCA lanza (captura y reporta) y corren en paralelo
con un timeout corto, para que la sonda sea rápida aunque una dependencia cuelgue.
"""
import asyncio
import logging
import time

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.agent import memory
from app.core.config import settings
from app.db.session import SessionLocal

logger = logging.getLogger("nexa.health")
router = APIRouter(tags=["health"])

# Timeouts por dependencia (segundos). Cortos a propósito: una sonda no se cuelga.
_DB_TIMEOUT = 2.0
_REDIS_TIMEOUT = 2.0
_OLLAMA_TIMEOUT = 3.0


@router.get("/health")
async def health() -> dict:
    """Liveness: el proceso está vivo. No toca dependencias a propósito."""
    return {"status": "ok"}


async def _ping_db() -> None:
    async with SessionLocal() as session:
        await session.execute(text("SELECT 1"))


async def _ping_ollama() -> None:
    async with httpx.AsyncClient(timeout=_OLLAMA_TIMEOUT) as client:
        resp = await client.get(f"{settings.ollama_base_url}/api/version")
        resp.raise_for_status()


async def _probe(name: str, fn, timeout: float) -> dict:
    """Ejecuta un chequeo con timeout, lo cronometra y NUNCA lanza."""
    start = time.perf_counter()
    try:
        await asyncio.wait_for(fn(), timeout)
        return {"status": "up", "latency_ms": round((time.perf_counter() - start) * 1000, 2)}
    except Exception as exc:
        logger.warning(
            "readiness check failed",
            extra={"event": "readiness_check", "check": name, "exc_type": type(exc).__name__},
        )
        return {
            "status": "down",
            "error": f"{type(exc).__name__}: {exc}",
            "latency_ms": round((time.perf_counter() - start) * 1000, 2),
        }


# Funciones de chequeo a nivel de módulo: así los tests pueden monkeypatchearlas
# para simular caídas sin necesitar Redis/Ollama reales (el servicio `tests` solo
# levanta la BD).
async def check_database() -> dict:
    return await _probe("database", _ping_db, _DB_TIMEOUT)


async def check_redis() -> dict:
    return await _probe("redis", memory.ping, _REDIS_TIMEOUT)


async def check_ollama() -> dict:
    return await _probe("ollama", _ping_ollama, _OLLAMA_TIMEOUT)


@router.get("/health/ready")
async def readiness() -> JSONResponse:
    db, redis_check, ollama_check = await asyncio.gather(
        check_database(), check_redis(), check_ollama()
    )
    checks = {
        "database": {**db, "critical": True},
        "ollama": {**ollama_check, "critical": True},
        "redis": {**redis_check, "critical": False},
    }
    critical_down = db["status"] != "up" or ollama_check["status"] != "up"
    if critical_down:
        status_label, code = "not ready", 503
    elif redis_check["status"] != "up":
        status_label, code = "degraded", 200   # sirve, sin caché de corto plazo
    else:
        status_label, code = "ready", 200
    return JSONResponse(status_code=code, content={"status": status_label, "checks": checks})
