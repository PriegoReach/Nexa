"""Genera corpus sintético insertándolo DIRECTO en la BD (Opción B, P19).

Un proyecto sintético = UN chunk: la frase es corta, no hace falta partir, y así
N proyectos = N chunks de plantilla idéntica compitiendo entre sí. Ese es el peor
caso de falsos amigos léxicos, a propósito.

Dos VARIANTES de identificación (mismo corpus de fondo, distinta forma de pedir):

- "numero"  (control): el bloque dice "Proyecto Sintético {i}" y la pregunta cita
  ese número. El ordinal es un FARO: aparece igual en content y en pregunta, así
  que el full-text empareja por el número, no por semántica. Dio Recall@1=1.0.

- "atributo" (el experimento honesto): el número se BORRA del content. El bloque
  se identifica solo por sector + responsable + huella, y la pregunta pide por
  sector+responsable, sin número. Para que la huella sea inequívoca hay 50
  responsables ÚNICOS; para que el retriever sufra, el sector se REPITE (5 por
  sector) y los responsables comparten vocabulario (mismo nombre de pila dentro
  de un sector, distinto apellido). El cross-encoder tiene que desempatar de
  verdad. Si Recall@1 sigue ~1.0 -> el re-ranking escala por semántica; si baja
  -> el 1.0 del control lo inflaba el faro numérico. Ambas salidas son hallazgo.

Tres invariantes verificados contra el código real, no asumidos:

1. content_tsv (tsvector de la búsqueda híbrida) es GENERATED ALWAYS AS
   to_tsvector('es_simple', content) STORED (alembic 0001). Insertar `content` la
   puebla sola -> el lado LÉXICO encuentra los chunks sin trabajo extra.
2. embedding: get_embeddings().embed_documents(), LA MISMA función que la ingesta
   real y que embed_query del retriever -> mismo espacio, distancias comparables.
   El tipo Vector de pgvector acepta la lista de floats directa (sin str()).
3. documents.created_at es NOT NULL sin server default -> insertar vía el ORM
   (Document(...)) para que aplique el default=_now de Python.

NOTA sobre el faro: la huella (SYN-007-X7) lleva dígitos y vive en el content,
pero NUNCA aparece en la pregunta -> no puede emparejar por número. El faro que
borra la variante "atributo" es el ORDINAL DEL NOMBRE, que sí estaba en ambos lados.
"""
import json
import sys
from pathlib import Path

# Las deps de BD/embeddings se importan DENTRO de las funciones async: así
# generar() y la generación de datasets JSON (sin arg) corren con solo stdlib,
# sin Ollama ni la BD (la propiedad "cero deps" de la P17).

SIZES = [10, 25, 50, 100]
MARKER = "synthetic_eval_"          # prefijo de filename → limpieza por LIKE

SECTORES = ["logística", "energía", "salud", "finanzas", "agroindustria",
            "telecomunicaciones", "manufactura", "turismo", "educación", "minería"]

# Control: 10 responsables reciclados con % 10 (el número distingue, no el nombre).
NOMBRES = ["Ana Ruiz", "Luis Sáez", "Marta Gil", "Pedro Lim", "Sara Vega",
           "Tomás Ortiz", "Elena Cruz", "Iván Soto", "Nora Paz", "Hugo Mena"]

# Variante atributo: grid 10 pilas × 5 apellidos = 50 nombres ÚNICOS con vocabulario
# muy solapado. Dentro de un sector los 5 hermanos comparten nombre de pila y solo
# difieren en apellido -> máximo solape léxico justo en el conjunto confundible.
PILAS = ["Ana", "Luis", "Marta", "Pedro", "Sara",
         "Tomás", "Elena", "Iván", "Nora", "Hugo"]
APELLIDOS = ["Ruiz", "Sáez", "Gil", "Lima", "Vega"]
MAX_ATRIBUTO = len(PILAS) * len(APELLIDOS)          # 50 proyectos únicos


def _responsable(i: int, variante: str) -> str:
    """Responsable del proyecto i. Único e inequívoco en la variante 'atributo'."""
    if variante == "atributo":
        # i en 1..50 -> (pila, apellido) del grid, todas las combinaciones únicas.
        return f"{PILAS[(i - 1) % len(PILAS)]} {APELLIDOS[(i - 1) // len(PILAS)]}"
    return NOMBRES[i % len(NOMBRES)]


def generar(n: int, variante: str = "numero") -> tuple[list[str], list[dict]]:
    """Devuelve (bloques, dataset) para n proyectos en la variante dada."""
    if variante not in ("numero", "atributo"):
        raise ValueError(f"variante desconocida: {variante!r}")
    if variante == "atributo" and n > MAX_ATRIBUTO:
        raise ValueError(
            f"variante 'atributo' soporta hasta {MAX_ATRIBUTO} proyectos únicos; "
            f"pediste {n}. Amplía PILAS/APELLIDOS para escalar."
        )

    bloques, dataset = [], []
    for i in range(1, n + 1):
        huella = f"SYN-{i:03d}-X{i % 10}"
        sector = SECTORES[i % len(SECTORES)]
        resp = _responsable(i, variante)

        if variante == "atributo":
            # SIN ordinal en el content: solo sector + responsable + huella.
            bloque = (f"Proyecto de cartera interna. Pertenece al sector {sector} "
                      f"y su responsable es {resp}. El código interno de "
                      f"autorización es {huella}.")
            # La pregunta identifica por atributos, nunca por número.
            dataset.append({
                "pregunta": f"¿cuál es el código del proyecto del sector {sector} "
                            f"cuyo responsable es {resp}?",
                "huella": huella, "proyecto": f"{sector}/{resp}"})
            dataset.append({
                "pregunta": f"codigo de autorizacion del proyecto del sector "
                            f"{sector} a cargo de {resp}",
                "huella": huella, "proyecto": f"{sector}/{resp}"})
        else:
            # Control: el ordinal vive en content y en pregunta = faro numérico.
            bloque = (f"El Proyecto Sintético {i} tiene el código interno de "
                      f"autorización {huella}. Este proyecto pertenece al sector "
                      f"{sector} y su responsable es {resp}.")
            dataset.append({"pregunta": f"¿cuál es el código del Proyecto Sintético {i}?",
                            "huella": huella, "proyecto": f"Sintético {i}"})
            dataset.append({"pregunta": f"codigo del proyecto sintetico {i}",
                            "huella": huella, "proyecto": f"Sintético {i}"})
        bloques.append(bloque)
    return bloques, dataset


def _suffix(variante: str) -> str:
    return "" if variante == "numero" else f"_{variante}"


def dataset_path(n: int, variante: str = "numero") -> Path:
    return Path(f"app/eval/datasets/{MARKER}{n}{_suffix(variante)}.json")


async def limpiar() -> int:
    """Borra TODO lo sintético por el marcador. El cascade limpia los chunks.

    No toca los proyectos reales (sus filenames no empiezan por el marcador).
    Cubre ambas variantes: synthetic_eval_50 y synthetic_eval_50_atributo.
    """
    from sqlalchemy import text

    from app.db.worker_db import worker_session

    async with worker_session() as session:
        res = await session.execute(
            text("DELETE FROM documents WHERE filename LIKE :p"),
            {"p": f"{MARKER}%"},
        )
        await session.commit()
        return res.rowcount or 0


async def generar_e_insertar(
    n: int, variante: str = "numero"
) -> tuple[int, int, list[dict]]:
    """Genera n proyectos, los embebe con la función real e inserta 1 chunk c/u.

    Devuelve (doc_id, n_chunks, dataset). Escribe también el dataset JSON para
    que el harness (app.eval.retrieval) pueda leerlo por ruta.
    """
    from app.db.models import Document, DocumentChunk
    from app.db.worker_db import worker_session
    from app.rag.embeddings import get_embeddings

    bloques, dataset = generar(n, variante)
    # MISMA función de embedding que la ingesta real -> mismo espacio vectorial.
    vectors = get_embeddings().embed_documents(bloques)

    async with worker_session() as session:
        # ORM para el documento: aplica el default=_now de created_at (NOT NULL).
        doc = Document(filename=f"{MARKER}{n}{_suffix(variante)}", status="ready")
        session.add(doc)
        await session.flush()                       # asigna doc.id sin cerrar la tx
        session.add_all([
            DocumentChunk(document_id=doc.id, content=b, embedding=v)
            for b, v in zip(bloques, vectors, strict=True)
        ])
        await session.commit()
        doc_id = doc.id

    dataset_path(n, variante).write_text(
        json.dumps(dataset, ensure_ascii=False, indent=2), encoding="utf-8")
    return doc_id, len(bloques), dataset


if __name__ == "__main__":
    if len(sys.argv) > 1:
        import asyncio

        n = int(sys.argv[1])
        variante = sys.argv[2] if len(sys.argv) > 2 else "numero"
        doc_id, n_chunks, _ = asyncio.run(generar_e_insertar(n, variante))
        print(f"n={n} ({variante}): doc_id={doc_id}, {n_chunks} chunks insertados "
              f"-> dataset {dataset_path(n, variante)}")
    else:
        # Sin arg: materializa los datasets JSON, sin tocar la BD (offline, stdlib).
        for n in SIZES:
            _, ds = generar(n, "numero")
            dataset_path(n).write_text(
                json.dumps(ds, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"n={n}: {len(ds)} preguntas -> {dataset_path(n)} (control)")
        # Y la variante atributo del experimento (N=50).
        _, ds = generar(50, "atributo")
        dataset_path(50, "atributo").write_text(
            json.dumps(ds, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"n=50: {len(ds)} preguntas -> {dataset_path(50, 'atributo')} (atributo)")
