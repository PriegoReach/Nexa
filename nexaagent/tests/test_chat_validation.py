"""Tests de validación/gate de /chat SIN invocar al agente.

Tanto el 403 (falta token) como el 422 (body inválido) se resuelven ANTES de
ejecutar el handler, así que run_agent() —y por tanto Ollama— nunca se llama.
El camino feliz de /chat (que sí usa el LLM) queda para una parte futura con
el modelo mockeado."""


async def test_chat_requires_auth(client):
    # Sin token → 401 (gate JWT), antes de tocar el agente.
    resp = await client.post("/chat", json={"message": "hola"})
    assert resp.status_code == 401


async def test_chat_rejects_invalid_body(client, auth_headers):
    # Falta 'message' (requerido) → 422 antes de tocar el agente.
    resp = await client.post("/chat", json={}, headers=auth_headers)
    assert resp.status_code == 422
