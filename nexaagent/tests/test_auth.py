"""Tests del gate de autenticación JWT (Parte 12).

Cubre el flujo /auth/login y la dependencia require_jwt sobre rutas protegidas.
No tocan Ollama ni Redis (login solo compara contraseña y firma un JWT; el GET
/conversations falla en el gate antes de tocar la BD)."""


async def test_login_success(client):
    resp = await client.post("/auth/login", json={"password": "test-password"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["expires_in"] > 0


async def test_login_wrong_password(client):
    resp = await client.post("/auth/login", json={"password": "incorrecta"})
    assert resp.status_code == 401


async def test_login_missing_field(client):
    # Falta 'password' (requerido) → 422 de validación.
    resp = await client.post("/auth/login", json={})
    assert resp.status_code == 422


async def test_protected_route_without_token_is_401(client):
    # Sin header Authorization → 401. (El comentario de security.py dice 403,
    # pero FastAPI 0.136 devuelve 401, que además es el código correcto:
    # 401 = faltan credenciales; 403 = autenticado pero sin permiso.)
    resp = await client.get("/conversations")
    assert resp.status_code == 401


async def test_protected_route_with_bad_token_is_401(client):
    # Bearer presente pero JWT inválido → require_jwt levanta 401.
    resp = await client.get(
        "/conversations", headers={"Authorization": "Bearer no-es-un-jwt"}
    )
    assert resp.status_code == 401


async def test_protected_route_with_valid_token_passes_gate(client, auth_headers):
    # GET /conversations es de solo lectura y no toca Ollama/Redis: con token
    # válido debe responder 200 (lista vacía tras el truncate).
    resp = await client.get("/conversations", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["items"] == []
