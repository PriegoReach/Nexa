"""Middleware de observabilidad por petición (Parte 16, sobre la base de la 15).

Hace tres cosas, a propósito en UN único middleware:
  1. Asigna/propaga `X-Request-ID` y lo fija en el contextvar (correlación de logs).
  2. Cronometra la petición y emite una línea `request_end` (método, ruta, status,
     duración_ms) — automatiza la latencia que antes se leía a mano de los timestamps.
  3. Alimenta las métricas Prometheus de /metrics (contador + histograma + gauge).

¿Por qué todo junto y no un middleware aparte para latencia/métricas? Porque la
línea `request_end` y las métricas deben emitirse con el contextvar del request_id
AÚN fijado. En un middleware separado, el orden de `add_middleware` decidiría si
la correlación sobrevive (un footgun silencioso: el contextvar se resetea al salir
del middleware que lo fijó). Aquí el reset ocurre al final del `finally`, después
de medir y loguear, así que la correlación está garantizada.
"""
import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.core import metrics
from app.core.log_context import request_id_var

logger = logging.getLogger("nexa.access")

# Sondas de infra: NO generan línea `request_end` (si no, inundan el log a cada
# intervalo de readiness/liveness/scrape). Siguen contando en métricas, salvo
# /metrics, que no se cuenta a sí mismo.
_QUIET_PATHS = frozenset({"/health", "/health/ready", "/metrics"})


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        token = request_id_var.set(rid)

        method = request.method
        raw_path = request.url.path
        # /metrics no se cuenta a sí mismo (cada scrape inflaría todas las series).
        record = raw_path != "/metrics"
        if record:
            metrics.inc_in_progress(method)

        start = time.perf_counter()
        status = 500  # si call_next lanza (excepción no tratada → 500 de Starlette)
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = rid
            return response
        finally:
            duration = time.perf_counter() - start
            if record:
                metrics.dec_in_progress(method)
                # Plantilla de ruta para acotar la cardinalidad de la etiqueta:
                # el router ya emparejó scope["route"] dentro de call_next. Sin
                # match (404 a una URL inventada) cae a "<unmatched>", nunca a la
                # ruta cruda — que reventaría la cardinalidad con cada id distinto.
                route = request.scope.get("route")
                metric_path = getattr(route, "path", None) or "<unmatched>"
                metrics.observe(method, metric_path, status, duration)

            if raw_path not in _QUIET_PATHS:
                logger.info(
                    "request end",
                    extra={
                        "event": "request_end",
                        "method": method,
                        "path": raw_path,           # cruda: más legible para un humano
                        "status": status,
                        "duration_ms": round(duration * 1000, 2),
                    },
                )
            request_id_var.reset(token)
