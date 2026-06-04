"""Endpoint TTS: proxy autenticado al servicio XTTS-v2 (contenedor `tts`).

El servicio tts no se expone fuera de la red interna de compose; este router es
la única puerta y exige JWT (igual que el resto). Recibe {text, voice}, valida la
voz, llama a tts:8000/synthesize y devuelve audio/wav. Si el servicio no responde
a tiempo, degrada con un error claro (no cuelga): la lectura por voz es opcional y
su caída no debe romper el chat.
"""
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel

from app.core.config import settings
from app.core.security import require_jwt

logger = logging.getLogger("nexa.api.tts")

router = APIRouter(prefix="/tts", tags=["tts"], dependencies=[Depends(require_jwt)])

_VOICES = {"ana", "alma"}
_TIMEOUT = 60.0  # XTTS: ~1-2s frases cortas, ~7s párrafos; margen amplio sin colgar


class SpeakRequest(BaseModel):
    text: str
    voice: str = "ana"


@router.post("")
async def speak(payload: SpeakRequest) -> Response:
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="texto vacío")
    voice = (payload.voice or "").strip().lower()
    if voice not in _VOICES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"voz inválida: {payload.voice!r} (usa 'ana' o 'alma')",
        )

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.tts_base_url}/synthesize",
                json={"text": text, "voice": voice},
            )
            resp.raise_for_status()
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        # Servicio TTS caído o lento -> degradación fail-safe: error claro, no cuelga.
        logger.warning("tts unavailable", extra={"event": "tts_degraded", "exc_type": type(exc).__name__})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="El servicio de voz no está disponible ahora mismo.",
        ) from exc
    except httpx.HTTPStatusError as exc:
        # El servicio respondió un error (p.ej. 413 texto demasiado largo).
        logger.warning("tts returned error", extra={"event": "tts_error", "status": exc.response.status_code})
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="El servicio de voz devolvió un error.",
        ) from exc

    return Response(content=resp.content, media_type="audio/wav")
