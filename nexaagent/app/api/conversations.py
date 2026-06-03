from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from pydantic import BaseModel

from app.core.security import require_jwt
from app.db.session import SessionLocal
from app.schemas.chat import (
    ConversationDetail,
    ConversationListResponse,
    ConversationSummary,
    MessageItem,
)

router = APIRouter(
    prefix="/conversations",
    tags=["conversations"],
    dependencies=[Depends(require_jwt)],
)


@router.get("", response_model=ConversationListResponse)
async def list_conversations(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ConversationListResponse:
    async with SessionLocal() as session:
        total = (
            await session.execute(text("SELECT count(*) FROM conversations"))
        ).scalar_one()

        rows = await session.execute(
            text(
                """
                SELECT
                    c.id,
                    c.title,
                    c.created_at,
                    COALESCE(stats.msg_count, 0)         AS message_count,
                    left(last_msg.content, 120)          AS last_message,
                    last_msg.created_at                  AS last_message_at
                FROM conversations c
                LEFT JOIN LATERAL (
                    SELECT count(*) AS msg_count
                    FROM messages m WHERE m.conversation_id = c.id
                ) stats ON true
                LEFT JOIN LATERAL (
                    SELECT content, created_at
                    FROM messages m WHERE m.conversation_id = c.id
                    ORDER BY m.id DESC LIMIT 1
                ) last_msg ON true
                ORDER BY COALESCE(last_msg.created_at, c.created_at) DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {"limit": limit, "offset": offset},
        )

        items = [
            ConversationSummary(
                id=r.id,
                title=r.title,
                created_at=r.created_at,
                message_count=r.message_count,
                last_message=r.last_message,
                last_message_at=r.last_message_at,
            )
            for r in rows
        ]

    return ConversationListResponse(
        items=items, total=total, limit=limit, offset=offset
    )


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(conversation_id: int) -> ConversationDetail:
    async with SessionLocal() as session:
        convo = (
            await session.execute(
                text(
                    "SELECT id, title, created_at FROM conversations "
                    "WHERE id = :cid"
                ),
                {"cid": conversation_id},
            )
        ).first()

        if convo is None:
            raise HTTPException(status_code=404, detail="Conversation not found")

        msg_rows = await session.execute(
            text(
                "SELECT role, content, created_at FROM messages "
                "WHERE conversation_id = :cid ORDER BY id ASC"
            ),
            {"cid": conversation_id},
        )
        messages = [
            MessageItem(role=m.role, content=m.content, created_at=m.created_at)
            for m in msg_rows
        ]

    return ConversationDetail(
        id=convo.id,
        title=convo.title,
        created_at=convo.created_at,
        messages=messages,
    )

# 2) Añadir el schema de respuesta junto a los otros (ConversationSummary, etc.):
class ConversationDeleteResponse(BaseModel):
    deleted: bool
    conversation_id: int


# 3) Y el endpoint en sí, al final del router:
@router.delete(
    "/{conversation_id}",
    response_model=ConversationDeleteResponse,
    dependencies=[Depends(require_jwt)],
)
async def delete_conversation(conversation_id: int) -> ConversationDeleteResponse:
    """Borra una conversación y todo lo asociado (cascade en BD).

    Las FKs ON DELETE CASCADE de `messages` y `long_term_memories` se ocupan
    de arrastrar mensajes y memorias asociados. Devuelve 404 si la conversación
    no existe (consistente con GET /{id}).
    """
    async with SessionLocal() as session:
        # ¿Existe? — si no, 404.
        exists = await session.execute(
            text("SELECT 1 FROM conversations WHERE id = :id"),
            {"id": conversation_id},
        )
        if exists.first() is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Conversation {conversation_id} not found",
            )

        await session.execute(
            text("DELETE FROM conversations WHERE id = :id"),
            {"id": conversation_id},
        )
        await session.commit()

    return ConversationDeleteResponse(deleted=True, conversation_id=conversation_id)
