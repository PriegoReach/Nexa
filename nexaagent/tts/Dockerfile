FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 COQUI_TOS_AGREED=1

WORKDIR /app

# libsndfile para soundfile (escribir el WAV).
RUN apt-get update && apt-get install -y --no-install-recommends libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

# Orden CRÍTICO (aprendido en la prueba aislada _tts_probe/):
# 1) torch CUDA desde el índice cu121 PRIMERO. Si no, coqui-tts arrastra el torch de CPU.
RUN pip install --no-cache-dir torch==2.5.1 torchaudio==2.5.1 \
    --index-url https://download.pytorch.org/whl/cu121
# 2) coqui-tts (XTTS-v2). Pulla transformers 5.x, que ROMPE XTTS (quita isin_mps_friendly).
RUN pip install --no-cache-dir coqui-tts==0.27.5
# 3) Fijar transformers a la última 4.x: >=4.57 (lo exige coqui) y <5 (aún tiene isin_mps_friendly).
RUN pip install --no-cache-dir "transformers>=4.57,<5"
# 4) Servidor web.
RUN pip install --no-cache-dir fastapi "uvicorn[standard]"

COPY server.py ./

EXPOSE 8000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
