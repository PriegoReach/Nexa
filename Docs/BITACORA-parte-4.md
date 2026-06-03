# NexaAgent — Bitácora de desarrollo (Parte 4)

> Continuación de la Parte 3. Objetivo de esta sesión: **cerrar el techo de recuperación** del RAG
> implementando búsqueda híbrida — combinar la similitud vectorial (pgvector) con búsqueda léxica
> full-text (Postgres) y fusionarlas con Reciprocal Rank Fusion (RRF).

**Estado al cierre de la Parte 4:** búsqueda híbrida funcionando y validada. El chunk que en la Parte 3 quedaba fuera del top-4 (rescatado solo al subir `k=8`) ahora aparece en **posición [0]** con `k=4`. El léxico aporta la coincidencia exacta de entidades (nombres de proyecto, códigos) que la similitud vectorial pura se perdía. El RAG queda terminado de raíz, no por el tamaño pequeño del corpus.

---

## 1. Objetivo de la sesión

La Parte 3 reveló el talón de Aquiles del RAG ingenuo: la similitud vectorial matchea por *redacción*, no por *entidad*. Cuando los chunks comparten plantilla ("El código interno de autorización del proyecto es..."), el nombre que los distingue (Rubí, Ámbar) se diluye y la búsqueda confunde candidatos. La mitigación de entonces (subir `k` de 4 a 8) funcionó solo porque el corpus era pequeño y el chunk correcto estaba a un paso de la ventana — un parche, no una solución.

**La solución de fondo:** búsqueda híbrida. Añadir una segunda señal —coincidencia léxica exacta vía full-text de Postgres— donde el término "Rubí" pesa por sí mismo, y fusionar ambas señales.

### Decisión de diseño: fusión con RRF

Se eligió **Reciprocal Rank Fusion (RRF)** sobre la suma ponderada de scores. Razón: RRF fusiona por *posición* en cada ranking, no por magnitud de score, así que no hay que normalizar escalas incomparables (la distancia coseno y el `ts_rank` viven en rangos distintos) ni tunear pesos. Es el estándar de facto y se expresa en una sola query SQL.

### Decisión de diseño: configuración de texto `es_simple` (no `spanish`)

Para la indexación full-text se descartó el diccionario `'spanish'` en favor de una config propia `es_simple` (= `simple` + `unaccent`). Razón en la sección 3.

---

## 2. Setup de full-text en la base

La tabla `document_chunks` no tenía soporte de full-text (solo `id`, `document_id`, `content`, `embedding vector(768)`). Se añadió una columna `tsvector` **generada** (se rellena sola para chunks existentes y futuros, sin re-ingerir) más un índice GIN.

```sql
-- Extensión para normalizar acentos
CREATE EXTENSION IF NOT EXISTS unaccent;

-- Configuración de texto: simple (sin stemming) + unaccent
CREATE TEXT SEARCH CONFIGURATION es_simple (COPY = simple);
ALTER TEXT SEARCH CONFIGURATION es_simple
  ALTER MAPPING FOR hword, hword_part, word WITH unaccent, simple;

-- Columna generada + índice GIN
ALTER TABLE document_chunks
  ADD COLUMN content_tsv tsvector
  GENERATED ALWAYS AS (to_tsvector('es_simple', content)) STORED;
CREATE INDEX idx_chunks_tsv ON document_chunks USING GIN (content_tsv);
```

> **Nota:** una columna generada no se puede `ALTER`; para cambiar su configuración hay que `DROP COLUMN` y recrearla (el índice GIN cae con ella y se recrea también). Es instantáneo porque se regenera desde `content`.

---

## 3. El descubrimiento: `spanish` destroza los nombres propios

El primer intento usó el diccionario `'spanish'`. Dos problemas aparecieron al verificar (antes de escribir código):

### Problema 1 — Los acentos rompen el match

Buscar `websearch_to_tsquery('spanish', 'Rubi')` (sin acento) devolvía **0 filas**; con `'Rubí'` (con acento) sí encontraba. El stemmer español no quita acentos por sí solo. Como los usuarios no siempre acentúan, un RAG que falla por una tilde es frágil. → Solución: extensión `unaccent`.

### Problema 2 — El stemming destroza los nombres propios

Al inspeccionar la tokenización:

```
to_tsvector('spanish', 'El Proyecto Rubí ...')
  → 'correspond':4 'factur':10 'motor':8 'proyect':2 'rediseñ':6 'rub':3
```

"Rubí" se guardó como **`'rub'`** — el stemmer lo trató como palabra común y le cortó la terminación (como haría con "rubor"/"rubio"). El diccionario `'spanish'` **destruye los nombres propios**, que son justamente las llaves de búsqueda en este RAG.

### La decisión: `simple` + `unaccent`

| | `'spanish'` | `es_simple` (simple + unaccent) |
|---|---|---|
| Acentos | Sensible ("Rubi" ≠ "Rubí") | Normaliza ("Rubi" = "Rubí") |
| Nombres propios | Destruidos por stemming (`rub`) | Intactos (`rubi`) |
| Variantes de palabra común | "facturación" = "facturar" (`factur`) | No las une |

El "beneficio" de `'spanish'` (unir variantes de palabras comunes por raíz) es **redundante con la búsqueda vectorial**: el embedding de "facturación" y "facturar" ya es casi idéntico, el vector las une sin ayuda. El léxico está aquí precisamente para lo que el vector falla: la coincidencia *exacta* de entidades. Por eso `simple` es la elección correcta para un sistema híbrido — división limpia de labores: **vector = semántica difusa, léxico = coincidencia exacta**.

Verificación tras el cambio:

```
to_tsvector('es_simple', 'Proyecto Rubi facturacion')
  → 'facturacion':3 'proyecto':1 'rubi':2     -- todo intacto, sin acentos
```

---

## 4. El retriever híbrido (RRF)

Se reescribió `app/rag/retriever.py` manteniendo el patrón de engine `NullPool` propio de la Parte 2 (para no reintroducir el bug del event loop). La búsqueda ahora es una sola query con dos CTEs fusionados:

```python
RRF_K = 60  # constante estándar de RRF; amortigua el peso de las primeras posiciones

async def search(query: str, k: int = 4) -> list[str]:
    query_vec = get_embeddings().embed_query(query)
    pool_size = k * 5  # cada señal rankea un pool amplio; RRF recorta a k al final

    sql = text("""
        WITH vec AS (
            SELECT id, content,
                   ROW_NUMBER() OVER (ORDER BY embedding <=> :qvec) AS rank
            FROM document_chunks ORDER BY embedding <=> :qvec LIMIT :pool_size
        ),
        lex AS (
            SELECT id, content,
                   ROW_NUMBER() OVER (ORDER BY ts_rank(content_tsv, q) DESC) AS rank
            FROM document_chunks,
                 websearch_to_tsquery('es_simple', unaccent(:qtext)) AS q
            WHERE content_tsv @@ q LIMIT :pool_size
        )
        SELECT COALESCE(vec.content, lex.content) AS content,
               COALESCE(1.0 / (:rrf_k + vec.rank), 0.0)
             + COALESCE(1.0 / (:rrf_k + lex.rank), 0.0) AS score
        FROM vec FULL OUTER JOIN lex ON vec.id = lex.id
        ORDER BY score DESC LIMIT :k
    """)

    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    try:
        SearchSession = async_sessionmaker(engine, expire_on_commit=False)
        async with SearchSession() as session:
            rows = await session.execute(sql, {
                "qvec": str(query_vec), "qtext": query,
                "pool_size": pool_size, "rrf_k": RRF_K, "k": k,
            })
            return [r[0] for r in rows.fetchall()]
    finally:
        await engine.dispose()
```

### Cómo funciona

- `vec`: chunks ordenados por distancia coseno, con su posición (`ROW_NUMBER`).
- `lex`: chunks que matchean el `tsquery`, ordenados por `ts_rank`, con su posición. La query se pasa por `unaccent` igual que el contenido, para que "Rubi" sin tilde matchee "Rubí".
- `FULL OUTER JOIN` por `id`: un chunk puede estar en una lista, en otra, o en ambas.
- Score RRF: `1/(60+rank_vec) + 1/(60+rank_lex)`, con `COALESCE(..., 0)` neutralizando la señal ausente. Gana quien puntúa bien en ambas; quien está en una sola aún suma.

Sin normalizar escalas, sin tunear α. Cada señal rankea un pool de `k*5` candidatos y RRF recorta a `k`, dando margen para rescatar un chunk bien posicionado en una sola señal.

---

## 5. Validación

### Retriever aislado — el caso que fallaba en la Parte 3

Buscando "Rubi" **sin acento**, con **k=4**:

```
[0] ** RB-2023-Q7 AQUI ** 12.3 millones... fase de diseño. El código i...
[1] Negocio. El presupuesto... 5.4 millones... (Ónix, menciona a Rubí de pasada)
[2] regional. La fecha objetivo... (Ámbar)
[3] medio de emisión de facturas... (segunda mitad de Rubí)
```

El chunk con `RB-2023-Q7` pasó de estar **fuera del top-4** (Parte 3, solo entraba en [4] con k=8) a **posición [0]** con k=4. El léxico lo empujó a la cima; `unaccent` permitió el match sin tilde.

### Agente end-to-end

- **Ónix** (sin acento): "El código de autorización del Proyecto Onix es ON-2024-B5 y actualmente se encuentra en pausa, a la espera... del Proyecto Rubí." ✓ — caso que el vector confundía con vecinos en la Parte 3.
- **Rubí** (regresión): "La responsable del Proyecto Rubí es Lucía Domínguez... Su código interno de autorización es RB-2023-Q7." ✓ — la híbrida no rompió lo que ya funcionaba.

Ambas consultas con `unaccent` confirmado en el flujo real del agente, no solo en SQL.

---

## 6. Aprendizajes clave

1. **Para búsqueda de entidades, NO uses un diccionario con stemming.** `'spanish'` reduce "Rubí" a `rub`, destruyendo el nombre propio que es la llave de búsqueda. La config `simple` (sin stemming) preserva los términos intactos. En un RAG híbrido, el trabajo semántico ya lo hace el vector; el léxico debe ser exacto.

2. **`unaccent` es casi obligatorio para texto en español.** Los usuarios no acentúan de forma consistente. Hay que aplicar `unaccent` en AMBOS lados: al indexar (en la config de texto) y al consultar (envolviendo la query). Si solo se aplica en uno, no matchea.

3. **RRF fusiona sin necesidad de normalizar ni tunear.** Combinar señales por posición de ranking (no por score) evita el problema de escalas incomparables entre distancia coseno y `ts_rank`. La constante `k=60` es el estándar y no requiere ajuste.

4. **La búsqueda híbrida resuelve de raíz lo que subir `k` solo parcheaba.** El chunk correcto pasó de [4] (con k=8) a [0] (con k=4). En un corpus grande, subir `k` no habría bastado; la señal léxica sí, porque ataca la causa (el término exacto no pesaba).

5. **Las columnas generadas se rellenan solas.** Añadir `content_tsv` como `GENERATED ALWAYS ... STORED` indexó todos los chunks existentes sin re-ingerir ni tocar `ingest.py`. Para cambiar su config, `DROP` + recrear (instantáneo).

6. **Verificar la tokenización ANTES de construir encima.** Inspeccionar `to_tsvector(...)` directamente reveló el problema del stemming (`rub`) antes de escribir una línea de Python. Diagnosticar con datos, una vez más, ahorró construir sobre una base rota.

---

## 7. Comandos de referencia (nuevos de esta parte)

### Inspeccionar cómo se tokeniza un texto (clave para diagnosticar)

```powershell
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -t -c "SELECT to_tsvector('es_simple', 'Proyecto Rubi facturacion');"
```

### Probar la búsqueda léxica aislada (sin el retriever)

```powershell
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "SELECT document_id, left(content,55) FROM document_chunks WHERE content_tsv @@ websearch_to_tsquery('es_simple', unaccent('Rubi')) ORDER BY ts_rank(content_tsv, websearch_to_tsquery('es_simple', unaccent('Rubi'))) DESC;"
```

### Probar el retriever híbrido aislado (marca el chunk con la huella)

```powershell
docker exec nexaagent-api-1 python -c "import asyncio; from app.rag.retriever import search; r = asyncio.run(search('codigo de autorizacion del Proyecto Rubi', k=4)); print(chr(10).join(f'[{i}] ' + ('** RB AQUI ** ' if 'RB-2023-Q7' in c else '') + c[:80] for i,c in enumerate(r)))"
```

### Ver el esquema de la tabla (confirmar columna + índice)

```powershell
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "\d document_chunks"
```

---

## 8. Estado y siguientes pasos

### Funcionando ✅ (nuevo en la Parte 4)

- **Búsqueda híbrida (vectorial + léxica, fusión RRF)** funcionando y validada end-to-end.
- Full-text en español con `es_simple` (simple + unaccent): nombres propios intactos, insensible a acentos.
- Columna `content_tsv` generada + índice GIN sobre `document_chunks`.
- El techo de recuperación de la Parte 3 cerrado de raíz: el chunk correcto sube del fondo a la cima.

### Pendiente de probar / construir 🔜 (heredado y nuevo)

- **Persistir el setup de full-text en la inicialización del esquema.** Ahora mismo la extensión `unaccent`, la config `es_simple`, la columna `content_tsv` y el índice GIN se crearon a mano por SQL. Si se hace `docker-compose down -v` (que recrea la base), se pierden. Hay que moverlos a la creación de tablas o, mejor, a una migración de Alembic (ver más abajo).
- **Re-chunking consciente de estructura:** trocear por sección/proyecto para no partir el dato de una entidad entre dos chunks.
- **Memoria de largo plazo:** resúmenes recuperables por similitud vectorial.
- **Endpoint para listar conversaciones.**
- **Streaming de respuestas (SSE).**
- **Seguridad de producción:** reemplazar el `x-api-key` simple por OAuth2/JWT.
- **Fijar versiones exactas** de dependencias (sobre todo LangChain).
- **Migraciones con Alembic** en lugar de auto-crear tablas al arrancar — ahora más urgente, porque el setup de full-text necesita versionarse.
- **Refactor opcional:** unificar el patrón "engine NullPool propio" en un helper compartido (`ingest.py` + `retriever.py`).

### Nota importante sobre durabilidad

El setup de full-text de hoy vive solo en la base actual. **No sobrevive a `docker-compose down -v`.** Esto eleva la prioridad de Alembic: es el lugar natural para versionar tanto las tablas como la extensión `unaccent`, la config `es_simple`, la columna generada y el índice GIN. Mientras no se haga, anotar estos comandos como paso manual de setup.

---

*Cierre de la Parte 4.*
