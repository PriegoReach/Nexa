# NexaAgent — Bitácora de desarrollo (Parte 2)

> Continuación de la Parte 1. Objetivo de esta sesión: probar y dejar funcionando
> el flujo **RAG end-to-end** — desde subir un documento hasta que el agente lo consulte y responda con su contenido.

**Estado al cierre de la Parte 2:** RAG completo verificado de punta a punta. El agente recibe una pregunta, decide llamar a la herramienta de búsqueda, recupera el chunk correcto de pgvector y responde basándose en el documento. Se cambió el modelo de chat de `llama3.1` a `qwen2.5` por su fiabilidad con herramientas.

---

## 1. Objetivo de la sesión

En la Parte 1 quedó todo el stack levantado, pero el RAG estaba implementado **sin probar**. El plan de hoy fue validarlo por etapas, no "subir un documento y rezar". Definimos cuatro checkpoints:

1. **Subida** — `/documents/upload` recibe el archivo, guarda el registro en Postgres y encola la tarea en Celery.
2. **Worker** — Celery procesa: parseo → chunking → embeddings.
3. **Persistencia** — los chunks y sus vectores quedan en pgvector.
4. **Consulta** — el agente, ante una pregunta, usa la herramienta de búsqueda y responde con info que *solo* está en ese documento.

**Metodología clave:** verificar cada etapa de forma aislada, para que si algo falla sepamos exactamente en qué punto del pipeline se rompió.

---

## 2. El documento de prueba (la "huella")

Se creó `prueba-rag.txt` con un dato **inventado y distintivo** que ningún modelo puede conocer por su entrenamiento:

```
Informe interno NexaCorp — Proyecto Zafiro

El Proyecto Zafiro es una iniciativa confidencial de NexaCorp iniciada en marzo de 2024.
El responsable del proyecto es Mariana Velázquez y su presupuesto asignado fue de 4.7 millones de pesos.
El código interno de autorización del proyecto es ZF-2024-X9.
La fecha límite de entrega es el 15 de noviembre de 2026.
```

**Por qué importa:** el código `ZF-2024-X9` es la huella. Si el agente lo recita, es prueba irrefutable de que la respuesta vino del RAG y no del conocimiento general del modelo. Un dato real (ej. "la capital de Francia") no serviría: el modelo podría acertar sin haber consultado nada.

---

## 3. Errores encontrados y soluciones

### Error 1 — Validación de Swagger: `x-api-key Required field is not provided`

- **Síntoma:** al subir el documento desde `/docs`, error de validación antes de enviar nada.
- **Causa:** el campo `x-api-key` quedó vacío. No era un bug del pipeline.
- **Solución:** usar el botón **"Authorize"** (arriba a la derecha en Swagger), pegar el `x-api-key` una sola vez, y queda aplicado a todos los endpoints. Alternativamente, escribir el valor en el campo de cada petición.

### Error 2 — `RuntimeError: got Future attached to a different loop` (Celery worker)

```
RuntimeError: Task <ingest_document() ...> got Future <...> attached to a different loop
```

- **Síntoma revelador:** el bug era **intermitente**. En la tabla `documents`, los IDs impares quedaban `ready` y los pares `pending`. Los logs mostraban tareas que `succeeded` mezcladas con tracebacks.
- **Causa:** el engine async de SQLAlchemy (`SessionLocal`) se crea una vez al importar el módulo, y su *pool* de conexiones queda atado al primer event loop que lo usa. Pero Celery en modo *prefork* ejecuta cada tarea con `asyncio.run(...)`, que crea un loop nuevo y lo cierra en cada invocación. Las conexiones de `asyncpg` están amarradas al loop que las creó; cuando una tarea posterior intenta reutilizar una conexión del pool desde su loop nuevo → "attached to a different loop". La intermitencia depende de si el proceso *fork* que recibe la tarea ya tiene el pool "envenenado".
- **Importante:** el `tasks.py` estaba bien. El problema era compartir un engine global con pool entre loops efímeros.
- **Solución:** que la ingesta cree su **propio engine sin pool** (`NullPool`) *dentro* del loop de la tarea y lo deseche al terminar. Así cada conexión siempre pertenece al loop activo.

```python
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

async def ingest_document(document_id: int, file_path: str) -> int:
    # ... parseo, chunking, embeddings ...
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    try:
        WorkerSession = async_sessionmaker(engine, expire_on_commit=False)
        async with WorkerSession() as session:
            # ... INSERT chunks + UPDATE status ...
            await session.commit()
    finally:
        await engine.dispose()
    return len(chunks)
```

- **Verificación:** tras reconstruir el worker, los documentos 8, 9 y 10 quedaron los tres `ready` consistentemente, sin tracebacks. (Los 1–7, subidos antes del arreglo, conservaron el patrón intermitente impar/par.)

### Error 3 — llama3.1 no llama a la herramienta (confabulación)

- **Síntoma:** el agente respondía "no tengo acceso a los documentos internos" o "realicé una búsqueda exhaustiva y no encontré nada" — pero **nunca buscó**.
- **Diagnóstico con datos:** en los logs de la API no aparecía la línea `POST http://ollama:11434/api/embed` que delata al retriever embebiendo la query. Sin esa línea = la herramienta nunca se invocó. El modelo confabuló una búsqueda inexistente.
- **Causa:** llama3.1 (8B) es débil para *tool-calling*. Decide responder de su propio conocimiento en vez de invocar la herramienta.
- **Intento fallido (Palanca 1):** reforzar el `SYSTEM_PROMPT` con instrucciones imperativas en español ("DEBES llamar a la herramienta", "PROHIBIDO responder 'no tengo acceso'"). Cambió la *redacción* de la excusa pero **no la conducta**. El modelo seguía sin buscar.
- **Solución (Palanca 3):** cambiar el modelo de chat a `qwen2.5`, entrenado con énfasis en tool-calling.

```powershell
docker exec -it nexaagent-ollama-1 ollama pull qwen2.5
```

```dotenv
# .env — cambiar SOLO el modelo de chat
OLLAMA_MODEL=qwen2.5
```

- **Nota crítica:** NO se tocó `OLLAMA_EMBEDDING_MODEL` ni `EMBEDDING_DIM`. Los embeddings siguen con `nomic-embed-text` (768 dims) y los vectores ya guardados en pgvector siguen siendo válidos. Cambiar el modelo de chat **no** requiere recrear la base (ver nota de la Parte 1: `EMBEDDING_DIM` depende solo del modelo de embeddings).

### Error 4 — `Internal Server Error` al usar la herramienta (el bug del loop, otra vez)

```
RuntimeError: Task <search() running at /app/app/rag/retriever.py:11> got Future <...> attached to a different loop
During task with name 'tools'
```

- **Buena noticia disfrazada:** el `During task with name 'tools'` confirmó que **qwen2.5 SÍ llamó a la herramienta** (a diferencia de llama3.1). El cambio de modelo funcionó. El 500 era el siguiente eslabón.
- **Causa:** mismo patrón que el Error 2. La herramienta `search_knowledge_base` usa un `ThreadPoolExecutor` para correr `search()` en un hilo con loop nuevo (para evitar el "asyncio.run() cannot be called from a running event loop" dentro de FastAPI). Pero `search()` usaba el `SessionLocal` global, atado al loop original de FastAPI → choque de loops.
- **Solución:** darle al retriever su **propio engine sin pool**, gemelo exacto del arreglo de la ingesta.

```python
async def search(query: str, k: int = 4) -> list[str]:
    query_vec = get_embeddings().embed_query(query)
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    try:
        SearchSession = async_sessionmaker(engine, expire_on_commit=False)
        async with SearchSession() as session:
            rows = await session.execute(
                text("SELECT content FROM document_chunks "
                     "ORDER BY embedding <=> :qvec LIMIT :k"),
                {"qvec": str(query_vec), "k": k},
            )
            return [r[0] for r in rows.fetchall()]
    finally:
        await engine.dispose()
```

- **La herramienta** (`knowledge_base.py`) quedó con un helper que ejecuta la corrutina de forma segura haya o no loop activo:

```python
from concurrent.futures import ThreadPoolExecutor

def _run_async(coro):
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()
```

- **Resultado final:** respuesta limpia → `"el código de autorización del Proyecto Zafiro es ZF-2024-X9 y su responsable es Mariana Velázquez."`

---

## 4. Aprendizajes clave

1. **Un engine async de SQLAlchemy con pool global solo es seguro dentro del loop que lo creó.** Este es EL patrón de la sesión, apareció tres veces. Cualquier sitio que ejecute corrutinas en otro loop —Celery con `asyncio.run`, una tool en un thread aparte— necesita su **propio engine sin pool** (`NullPool`) creado y desechado dentro de ese loop. El `SessionLocal` global está bien para el flujo normal de la API (loop único y persistente); solo rompe cuando alguien lo usa desde otro loop.

2. **Los bugs intermitentes con event loops tienen una firma reconocible.** El patrón impar/par en `documents`, o un endpoint que falla "a veces", apunta casi siempre a estado compartido entre loops, no a aleatoriedad real.

3. **Verificar por capas ahorra horas.** Probar el retriever de forma aislada (`asyncio.run(search(...))` directo en el contenedor) ANTES de probar el agente nos dijo con certeza que la recuperación funcionaba. Así, cuando el agente falló, supimos que el problema era del modelo (decisión de usar la herramienta), no del pipeline.

4. **"No buscó" se diagnostica con la ausencia de un log, no con suposiciones.** La línea `POST .../api/embed` es el testigo de que el retriever corrió. Si no está, la herramienta nunca se invocó — sin importar lo que diga la respuesta del modelo.

5. **El prompt no arregla las limitaciones de capacidad de un modelo.** Un system prompt imperativo cambió cómo llama3.1 *redactaba* su negativa, pero no logró que invocara la herramienta. Para tool-calling fiable en modelos pequeños, la elección del modelo pesa más que el prompt. `qwen2.5` >> `llama3.1` en este aspecto.

6. **Usar un dato inventado como huella da certeza absoluta.** `ZF-2024-X9` no existe en ningún entrenamiento; verlo en la respuesta prueba que vino del RAG. Un dato real dejaría la duda de si el modelo lo sabía de antemano.

7. **Cambiar el modelo de chat ≠ recrear la base.** Mientras no cambies el modelo de *embeddings*, los vectores en pgvector siguen válidos. Solo se toca `OLLAMA_MODEL` en `.env`.

---

## 5. Comandos de referencia (nuevos de esta parte)

### Verificar el pipeline RAG por capas

```powershell
# Estado de los documentos y su procesamiento
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "SELECT id, filename, status FROM documents;"

# Chunks persistidos (con un preview del contenido)
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "SELECT document_id, left(content, 50) FROM document_chunks;"

# Probar el retriever AISLADO, sin el agente de por medio
docker exec nexaagent-api-1 python -c "import asyncio; from app.rag.retriever import search; print(asyncio.run(search('¿cuál es el código de autorización del Proyecto Zafiro?')))"
```

### Limpieza de documentos de prueba duplicados

```powershell
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "DELETE FROM document_chunks WHERE document_id != 10;"
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "DELETE FROM documents WHERE id != 10;"
```

### Cambiar de modelo de chat

```powershell
docker exec -it nexaagent-ollama-1 ollama pull qwen2.5   # descargar
# editar OLLAMA_MODEL=qwen2.5 en .env
docker-compose up -d --build api                          # recrear el api
docker exec nexaagent-ollama-1 ollama list                # confirmar modelos disponibles
```

### Prueba trampa del agente (end-to-end)

```powershell
curl.exe -X POST http://localhost:8000/chat -H "x-api-key: <API_KEY>" -H "Content-Type: application/json" -d '{\"message\": \"Segun los documentos internos, cual es el codigo de autorizacion del Proyecto Zafiro y quien es el responsable?\"}'
```

> **Señal de éxito en los logs:** la línea `POST http://ollama:11434/api/embed` (el retriever embebiendo la query) **sin** ningún traceback de "different loop", seguida de la respuesta con la huella `ZF-2024-X9`.

---

## 6. Estado y siguientes pasos

### Funcionando ✅ (nuevo en la Parte 2)

- **RAG end-to-end completo y verificado:** subida → worker → embeddings → pgvector → recuperación → respuesta del agente.
- Worker de Celery procesando ingestas sin errores de loop.
- Retriever robusto, usable desde cualquier loop (API directa o thread de la herramienta).
- Agente que **decide** usar la herramienta de búsqueda y responde con datos del documento (qwen2.5).

### Pendiente de probar / construir 🔜 (heredado y nuevo)

- **Memoria de largo plazo:** resúmenes recuperables por similitud vectorial.
- **Endpoint para listar conversaciones.**
- **Streaming de respuestas (SSE).**
- **Probar RAG con un documento grande/PDF** que genere múltiples chunks (la prueba de hoy fue 1 solo chunk; falta validar el chunking real y que la búsqueda discrimine entre varios).
- **Seguridad de producción:** reemplazar el `x-api-key` simple por OAuth2/JWT.
- **Fijar versiones exactas** de dependencias (sobre todo LangChain).
- **Migraciones con Alembic** en lugar de auto-crear tablas al arrancar.
- **Refactor opcional:** unificar el patrón "engine NullPool propio" en un helper compartido, ya que se repite en `ingest.py` y `retriever.py`.

### Nota sobre el modelo

`qwen2.5` resolvió el tool-calling de forma fiable donde llama3.1 fallaba. Conviene mantenerlo como modelo de chat por defecto. Si en el futuro se quiere volver a evaluar llama3.1 u otro, la prueba trampa del Proyecto Zafiro es el test de regresión ideal.

---

*Cierre de la Parte 2.*
