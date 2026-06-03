"""Smoke test del endpoint de salud (migrado al cliente async compartido)."""


async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
