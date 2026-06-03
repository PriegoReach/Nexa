"""Endpoint /metrics — exposición Prometheus (texto plano)."""
from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from app.core import metrics

router = APIRouter(tags=["observability"])

# Content-Type del formato de exposición de Prometheus (v0.0.4).
_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


@router.get("/metrics")
async def metrics_endpoint() -> PlainTextResponse:
    return PlainTextResponse(content=metrics.render(), media_type=_CONTENT_TYPE)
