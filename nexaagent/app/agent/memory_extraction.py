"""Extracción y deduplicación de memorias de largo plazo."""
import logging

from langchain_ollama import ChatOllama
from sqlalchemy import text

from app.core.config import settings
from app.db.worker_db import worker_session
from app.rag.embeddings import get_embeddings

logger = logging.getLogger("nexa.memory")

# --- Prompt de extracción (Parte 13: hechos atómicos) ----------------------
# Antes (Parte 6) el prompt aceptaba hechos compuestos. Resultado en Parte 11:
# qwen2.5 combinó "atún los jueves" y "Wenceslao" en un único hecho, perdiendo
# "Wenceslao" cuando dedup omitió el conjunto. La regla ahora es: UN dato por
# línea. Los ejemplos guían al modelo más que cualquier "DEBES".
_EXTRACTION_PROMPT = """Tu tarea es extraer hechos duraderos y memorables de la \
siguiente conversación, para recordarlos en el futuro.

Extrae CUALQUIER hecho memorable sobre el usuario:
- Datos personales (nombres de personas, mascotas, familiares, fechas, gustos).
- Preferencias y rutinas (cómo le gustan las cosas, horarios, hábitos).
- Información de su empresa, proyectos, códigos, roles, responsables.
- Decisiones, planes o cualquier dato concreto que mencione.

REGLAS CRÍTICAS DE FORMATO:
- UN solo dato por línea. Cada línea = un hecho atómico independiente.
- NO combines varios datos en una misma frase aunque vengan juntos en la conversación.
- Reformula en tercera persona empezando por "El usuario..." cuando aplique.
- Cada hecho debe entenderse por sí solo, sin necesitar las otras líneas.
- IGNORA saludos, agradecimientos y frases sin información.
- Si de verdad NO hay nada memorable, responde exactamente: NADA

Ejemplos:

Conversación: "Mi gato se llama Wenceslao y come atún los jueves."
INCORRECTO (compuesto):
- El gato del usuario, llamado Wenceslao, come atún los jueves.
CORRECTO (atómico, dos líneas):
- El usuario tiene un gato llamado Wenceslao.
- El gato del usuario come atún los jueves.

Conversación: "Trabajo en NexaCorp, soy responsable del Proyecto Zafiro."
INCORRECTO (compuesto):
- El usuario trabaja en NexaCorp y es responsable del Proyecto Zafiro.
CORRECTO (atómico, dos líneas):
- El usuario trabaja en NexaCorp.
- El usuario es responsable del Proyecto Zafiro.

Ahora extrae los hechos atómicos de esta conversación:

{conversation}

Hechos atómicos (uno por línea):"""

# Umbral calibrado con sonda en Parte 11.
DEDUP_DISTANCE_THRESHOLD = 0.15


async def extract_and_store_memories(conversation_id: int, window: int = 6) -> int:
    """Lee los últimos `window` mensajes, extrae hechos atómicos con el LLM,
    los embebe y los guarda en long_term_memories aplicando dedup semántica.

    Devuelve cuántos hechos NUEVOS guardó (no cuenta duplicados omitidos).
    """
    # 1. Leer los últimos `window` mensajes.
    async with worker_session() as session:
        rows = await session.execute(
            text(
                "SELECT role, content FROM messages "
                "WHERE conversation_id = :cid "
                "ORDER BY id DESC LIMIT :w"
            ),
            {"cid": conversation_id, "w": window},
        )
        recent = list(reversed(rows.fetchall()))

    if not recent:
        return 0

    convo_text = "\n".join(f"{r[0]}: {r[1]}" for r in recent)

    # 2. LLM extrae hechos atómicos.
    llm = ChatOllama(
        model=settings.ollama_model,
        base_url=settings.ollama_base_url,
        temperature=0,
    )
    resp = await llm.ainvoke(_EXTRACTION_PROMPT.format(conversation=convo_text))
    raw = resp.content.strip()

    if raw.upper().startswith("NADA") or not raw:
        return 0

    facts = [
        line.lstrip("-•* ").strip()
        for line in raw.splitlines()
        if line.strip() and not line.strip().upper().startswith("NADA")
    ]
    if not facts:
        return 0

    # 3. Embeber candidatos.
    vectors = get_embeddings().embed_documents(facts)

    # 4. Dedup + insert.
    stored = 0
    skipped = 0
    async with worker_session() as session:
        for fact, vec in zip(facts, vectors, strict=True):
            vec_str = str(vec)
            nearest = await session.execute(
                text(
                    "SELECT content, embedding <=> :v AS dist "
                    "FROM long_term_memories "
                    "ORDER BY embedding <=> :v LIMIT 1"
                ),
                {"v": vec_str},
            )
            row = nearest.first()
            if row is not None and row.dist < DEDUP_DISTANCE_THRESHOLD:
                logger.info(
                    "dedup skip",
                    extra={
                        "event": "dedup_skip",
                        "distance": round(row.dist, 3),
                        "fact": fact,
                        "matched": row.content,
                    },
                )
                skipped += 1
                continue

            await session.execute(
                text(
                    "INSERT INTO long_term_memories "
                    "(conversation_id, content, embedding, created_at) "
                    "VALUES (:cid, :content, :embedding, now())"
                ),
                {"cid": conversation_id, "content": fact, "embedding": vec_str},
            )
            stored += 1
        await session.commit()

    if stored or skipped:
        logger.info(
            "extraction done",
            extra={"event": "dedup_summary", "stored": stored, "skipped": skipped},
        )
    return stored
