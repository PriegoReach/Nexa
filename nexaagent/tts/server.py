"""Servicio TTS de NexaAgent — XTTS-v2 como contenedor propio (GPU).

Aislado del backend principal (igual que Ollama/Postgres/Redis): el backend le
habla por HTTP en la red interna de compose. Carga XTTS-v2 una vez al arrancar y
expone POST /synthesize {text, voice} -> wav. Dos voces femeninas en español:
'ana' (Ana Florence) y 'alma' (Alma María).
"""
import io
import logging
import os
import threading
from contextlib import asynccontextmanager

os.environ.setdefault("COQUI_TOS_AGREED", "1")  # licencia CPML del modelo (no interactivo)

import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from TTS.api import TTS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nexa.tts")

_MODEL = "tts_models/multilingual/multi-dataset/xtts_v2"
_LANGUAGE = "es"
_MAX_CHARS = 2000  # cota defensiva: textos enormes tardan/saturan; el agente responde conciso

# voice key -> nombre exacto del speaker integrado de XTTS-v2 (validados en la prueba aislada)
VOICES = {
    "ana": "Ana Florence",
    "alma": "Alma María",
}
VOICES.update({v: v for v in list(VOICES.values())})  # acepta también el nombre exacto

# XTTS no es seguro para llamadas concurrentes sobre el mismo modelo en GPU -> serializa.
_lock = threading.Lock()
_state = {"tts": None, "sr": 24000, "device": "cpu"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    device = "cuda" if torch.cuda.is_available() else "cpu"
    logger.info("cargando XTTS-v2 en %s ...", device)
    tts = TTS(_MODEL).to(device)
    _state["tts"] = tts
    _state["sr"] = tts.synthesizer.output_sample_rate
    _state["device"] = device
    logger.info("XTTS-v2 listo (device=%s, sr=%d)", device, _state["sr"])
    yield


app = FastAPI(title="NexaAgent TTS", version="0.1.0", lifespan=lifespan)


class SynthRequest(BaseModel):
    text: str
    voice: str = "ana"


@app.get("/health")
def health():
    return {
        "status": "ok" if _state["tts"] is not None else "loading",
        "device": _state["device"],
        "voices": sorted({k for k in VOICES if k in ("ana", "alma")}),
    }


@app.post("/synthesize")
def synthesize(req: SynthRequest):
    if _state["tts"] is None:
        raise HTTPException(status_code=503, detail="modelo aún cargando")
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="texto vacío")
    if len(text) > _MAX_CHARS:
        raise HTTPException(status_code=413, detail=f"texto demasiado largo (>{_MAX_CHARS} chars)")
    speaker = VOICES.get(req.voice) or VOICES.get(req.voice.strip())
    if speaker is None:
        raise HTTPException(status_code=400, detail=f"voz desconocida: {req.voice!r}")

    with _lock:
        wav = _state["tts"].tts(text=text, speaker=speaker, language=_LANGUAGE)

    buf = io.BytesIO()
    sf.write(buf, np.asarray(wav, dtype="float32"), _state["sr"], format="WAV")
    return Response(content=buf.getvalue(), media_type="audio/wav")
