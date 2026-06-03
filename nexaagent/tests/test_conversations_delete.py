"""Tests de DELETE /conversations/{id} — el endpoint estrella de la Parte 13.

Cubren las dos ramas (existe → 200, no existe → 404), el cascade real de las
FKs, y sobre todo la REGRESIÓN del NameError de `status`: el segundo DELETE del
mismo id debe devolver 404, no 500. Ese bug solo aparecía en la rama "no existe",
que la primera prueba (con una conversación existente) nunca ejecutaba.
"""
from sqlalchemy import text

from app.db.session import engine

# Vector cero de 768 dims (= settings.embedding_dim) para poder insertar memorias
# sin calcular un embedding real (no llamamos a Ollama en los tests).
_ZERO_VEC = "[" + ",".join(["0"] * 768) + "]"


async def _seed_conversation(*, messages: int = 2, memories: int = 1) -> int:
    """Inserta una conversación con N mensajes y M memorias de largo plazo.
    Devuelve el id. Usa el engine global (ya apuntando a la BD de test)."""
    async with engine.begin() as conn:
        cid = (
            await conn.execute(
                text(
                    "INSERT INTO conversations (title, created_at) "
                    "VALUES ('test', now()) RETURNING id"
                )
            )
        ).scalar_one()
        for i in range(messages):
            await conn.execute(
                text(
                    "INSERT INTO messages (conversation_id, role, content, created_at) "
                    "VALUES (:cid, 'user', :content, now())"
                ),
                {"cid": cid, "content": f"mensaje {i}"},
            )
        for i in range(memories):
            await conn.execute(
                text(
                    "INSERT INTO long_term_memories "
                    "(conversation_id, content, embedding, created_at) "
                    "VALUES (:cid, :content, CAST(:emb AS vector), now())"
                ),
                {"cid": cid, "content": f"memoria {i}", "emb": _ZERO_VEC},
            )
    return cid


async def _count(table: str, cid: int) -> int:
    async with engine.begin() as conn:
        return (
            await conn.execute(
                text(f"SELECT count(*) FROM {table} WHERE conversation_id = :cid"),
                {"cid": cid},
            )
        ).scalar_one()


async def test_delete_existing_returns_200(client, auth_headers):
    cid = await _seed_conversation()
    resp = await client.delete(f"/conversations/{cid}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"deleted": True, "conversation_id": cid}


async def test_delete_cascades_messages_and_memories(client, auth_headers):
    cid = await _seed_conversation(messages=3, memories=2)
    assert await _count("messages", cid) == 3
    assert await _count("long_term_memories", cid) == 2

    resp = await client.delete(f"/conversations/{cid}", headers=auth_headers)
    assert resp.status_code == 200

    # El ON DELETE CASCADE de las FKs arrastró mensajes y memorias.
    assert await _count("messages", cid) == 0
    assert await _count("long_term_memories", cid) == 0


async def test_delete_is_idempotent_second_call_is_404(client, auth_headers):
    """Regresión del NameError de `status`: el 2º DELETE del mismo id debe ser
    404, no 500. Esta es la rama que reventaba en la Parte 13."""
    cid = await _seed_conversation(memories=0)

    first = await client.delete(f"/conversations/{cid}", headers=auth_headers)
    assert first.status_code == 200

    second = await client.delete(f"/conversations/{cid}", headers=auth_headers)
    assert second.status_code == 404
    assert second.json()["detail"] == f"Conversation {cid} not found"


async def test_delete_nonexistent_returns_404(client, auth_headers):
    resp = await client.delete("/conversations/999999", headers=auth_headers)
    assert resp.status_code == 404


async def test_delete_requires_auth(client):
    resp = await client.delete("/conversations/1")
    assert resp.status_code == 401
