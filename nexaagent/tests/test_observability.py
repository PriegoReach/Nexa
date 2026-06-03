"""Tests de observabilidad (Parte 16): readiness y /metrics.

Los chequeos de readiness se monkeypatchean para simular caídas sin Redis/Ollama
reales (el servicio `tests` del compose solo levanta la BD). El test de la BD real
sí ejercita el probe verdadero, porque la BD está garantizada en ese servicio.
"""
from app.api import health
from app.core import metrics


# --- Dobles de chequeo (coroutines, como las reales) ------------------------
async def _up() -> dict:
    return {"status": "up", "latency_ms": 0.1}


async def _down() -> dict:
    return {"status": "down", "error": "RuntimeError: boom", "latency_ms": 0.1}


# --- Liveness ----------------------------------------------------------------
async def test_liveness_unchanged(client):
    """El /health de siempre (liveness) no cambia su contrato."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# --- Readiness ---------------------------------------------------------------
async def test_readiness_all_up(client, monkeypatch):
    monkeypatch.setattr(health, "check_database", _up)
    monkeypatch.setattr(health, "check_redis", _up)
    monkeypatch.setattr(health, "check_ollama", _up)

    resp = await client.get("/health/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert set(body["checks"]) == {"database", "redis", "ollama"}
    # La criticidad queda explícita en el cuerpo.
    assert body["checks"]["database"]["critical"] is True
    assert body["checks"]["ollama"]["critical"] is True
    assert body["checks"]["redis"]["critical"] is False


async def test_readiness_db_down_is_503(client, monkeypatch):
    monkeypatch.setattr(health, "check_database", _down)
    monkeypatch.setattr(health, "check_redis", _up)
    monkeypatch.setattr(health, "check_ollama", _up)

    resp = await client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json()["status"] == "not ready"


async def test_readiness_ollama_down_is_503(client, monkeypatch):
    monkeypatch.setattr(health, "check_database", _up)
    monkeypatch.setattr(health, "check_redis", _up)
    monkeypatch.setattr(health, "check_ollama", _down)

    resp = await client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json()["status"] == "not ready"


async def test_readiness_redis_down_is_degraded_200(client, monkeypatch):
    """Redis es degradable: caído NO tumba la readiness, solo la marca 'degraded'."""
    monkeypatch.setattr(health, "check_database", _up)
    monkeypatch.setattr(health, "check_redis", _down)
    monkeypatch.setattr(health, "check_ollama", _up)

    resp = await client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "degraded"


async def test_readiness_real_db(client, monkeypatch):
    """Ejercita el probe REAL de la BD (que sí está arriba en el servicio tests)."""
    monkeypatch.setattr(health, "check_redis", _up)
    monkeypatch.setattr(health, "check_ollama", _up)

    resp = await client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json()["checks"]["database"]["status"] == "up"


# --- /metrics ----------------------------------------------------------------
async def test_metrics_endpoint_format(client):
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    ctype = resp.headers["content-type"]
    assert "text/plain" in ctype and "version=0.0.4" in ctype
    assert "# TYPE http_requests_total counter" in resp.text
    assert "# TYPE http_request_duration_seconds histogram" in resp.text


async def test_metrics_uses_route_template_not_raw_path(client, auth_headers):
    """Una petición a una ruta con parámetro debe contarse con la PLANTILLA
    (/conversations/{conversation_id}), nunca con la ruta cruda — si no, cada id
    distinto crearía una serie nueva (cardinalidad sin límite). Esto además prueba
    que scope['route'] está poblado al volver del call_next en esta versión."""
    await client.get("/conversations/123456", headers=auth_headers)  # 404, da igual
    resp = await client.get("/metrics")
    assert "/conversations/{conversation_id}" in resp.text
    assert "/conversations/123456" not in resp.text


# --- Histograma (unitario, sin HTTP) -----------------------------------------
def test_histogram_is_cumulative_and_consistent():
    path = "/__unit_hist__"  # etiqueta propia para no chocar con otras series
    metrics.observe("GET", path, 200, 0.03)   # cae en le=0.05 y superiores
    metrics.observe("GET", path, 200, 7.0)    # cae en le=10 y superiores

    buckets = [
        int(line.rsplit(" ", 1)[1])
        for line in metrics.render().splitlines()
        if path in line and "_bucket" in line
    ]
    assert buckets == sorted(buckets)   # cumulativo ⇒ no decreciente
    assert buckets[-1] == 2             # el +Inf = total de observaciones
