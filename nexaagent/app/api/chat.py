import json

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.agent.orchestrator import run_agent, run_agent_stream
from app.core.security import require_jwt
from app.db.models import Conversation
from app.db.session import SessionLocal
from app.schemas.chat import ChatRequest, ChatResponse

router = APIRouter(prefix="/chat", tags=["chat"], dependencies=[Depends(require_jwt)])


async def _ensure_conversation(payload: ChatRequest) -> int:
    """Crea la conversación si no se pasó conversation_id; devuelve el id."""
    if payload.conversation_id is not None:
        return payload.conversation_id
    async with SessionLocal() as session:
        convo = Conversation(title=payload.message[:80])
        session.add(convo)
        await session.commit()
        await session.refresh(convo)
        return convo.id


@router.post("", response_model=ChatResponse)
async def chat(payload: ChatRequest) -> ChatResponse:
    conversation_id = await _ensure_conversation(payload)
    answer = await run_agent(conversation_id, payload.message)
    return ChatResponse(conversation_id=conversation_id, answer=answer)


@router.post("/stream")
async def chat_stream(payload: ChatRequest) -> StreamingResponse:
    conversation_id = await _ensure_conversation(payload)

    async def event_generator():
        # Primer evento: el conversation_id (no hay body JSON en streaming).
        yield f"data: {json.dumps({'type': 'meta', 'conversation_id': conversation_id})}\n\n"

        async for event in run_agent_stream(conversation_id, payload.message):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # desactiva buffering en proxies
        },
    )
