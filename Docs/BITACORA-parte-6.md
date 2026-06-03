# NexaAgent — Bitácora de desarrollo (Parte 6)

> Continuación de la Parte 5. Objetivo de esta sesión: implementar **memoria de largo plazo** —
> que el agente extraiga hechos duraderos de las conversaciones, los guarde embebidos, y los
> recupere por similitud en conversaciones futuras, sin importar de cuál vinieron.

**Estado al cierre de la Parte 6:** memoria de largo plazo funcionando y verificada de punta a punta. Un dato contado en una conversación se recupera correctamente en otra distinta. Por el camino se descubrió y arregló que los mensajes nunca se persistían en Postgres (solo en Redis).

---

## 1. Objetivo y diseño

### Corto plazo vs largo plazo

El agente ya tenía memoria de **corto plazo**: historial en Redis (TTL 24h, últimos 20 mensajes), que se carga y se manda al modelo. Límites: muere a las 24h y solo conoce la conversación actual.

La memoria de **largo plazo** extrae hechos duraderos, los embebe y los recupera por similitud cuando son relevantes — sin importar de qué conversación vinieron ni cuánto pasó. Mecánicamente es el mismo RAG, aplicado a conversaciones en vez de documentos.

### Decisiones de arquitectura (tomadas al inicio)

| Decisión | Elección | Motivo |
|---|---|---|
| Qué guardar | Hechos extraídos (no resúmenes) | Más preciso para recuperar; sin ruido de párrafos |
| Cuándo | Cada 6 mensajes (3 intercambios), vía Celery | Reutiliza el worker; disparador simple por conteo |
| Cómo recuperar | Como herramienta (`search_long_term_memory`) | Coherente con el patrón del RAG; el agente decide |
| Búsqueda | Solo vectorial (v1) | Hechos cortos y autocontenidos; sin problema de plantilla |
| Duplicados | Tolerados en v1 (ventana móvil) | Simplicidad; dedup queda como mejora futura |

La combinación elegida replica casi exactamente el patrón del RAG ya probado, reutilizando el retriever NullPool, el helper `_run_async` de la herramienta y la tarea Celery.

---

## 2. Las piezas

1. **Migración Alembic 0002** — tabla `long_term_memories` (id, conversation_id FK, content, embedding vector(768), created_at).
2. **Modelo `LongTermMemory`** en models.py.
3. **`memory_retriever.py`** — `search_memories(query, k)`: clon vectorial del retriever del RAG, engine NullPool propio.
4. **`memory_extraction.py`** — `extract_and_store_memories(conversation_id)`: lee los últimos N mensajes, pide al LLM extraer hechos, los embebe y guarda. Lo único genuinamente nuevo.
5. **Herramienta `search_long_term_memory`** — hermana de `search_knowledge_base`, con `_run_async`.
6. **Disparador en `orchestrator.py`** — cuenta mensajes en Postgres; si es múltiplo de 6, encola la tarea Celery.

---

## 3. Errores y hallazgos

### Hallazgo 1 — El `--autogenerate` quiere BORRAR el full-text

Al generar la migración 0002 con `alembic revision --autogenerate`, el log incluía:

```
Detected removed index 'idx_chunks_tsv' on 'document_chunks'
Detected removed column 'document_chunks.content_tsv'
```

- **Causa:** Alembic compara los modelos de SQLAlchemy contra la base. La columna `content_tsv` y el índice GIN existen en la base pero NO están declarados en `models.py` (son SQL custom de la Parte 4). Alembic concluye "esto sobra" y genera el DROP.
- **Riesgo:** aplicar esa migración habría destruido la búsqueda híbrida de la Parte 4.
- **Solución:** NO usar autogenerate aquí. Se escribió la migración 0002 a mano (igual que la 0001), solo con el `create_table` de la tabla nueva, sin tocar `document_chunks`.
- **Lección permanente:** el autogenerate es ciego a objetos que viven fuera de los modelos (columnas generadas, índices custom, extensiones). En CADA migración futura hay que revisar el archivo generado y borrar cualquier DROP espurio del full-text, o escribir a mano lo que toque esos objetos.

### Hallazgo 2 — Las migraciones no se persistían en el host

El archivo generado por autogenerate apareció DENTRO del contenedor pero no en la carpeta local: el `docker-compose.yml` montaba `./app` pero no `./alembic`. Las migraciones eran efímeras (se perderían al reconstruir).

- **Solución:** montar `./alembic:/app/alembic` y `./alembic.ini:/app/alembic.ini` en los servicios `api` y `worker`.

### Bug 1 — `NameError: name 'extract_and_store_memories' is not defined`

- **Causa:** `tasks.py` usaba la función pero faltaba el import.
- **Solución:** `from app.agent.memory_extraction import extract_and_store_memories`.

### Bug 2 — Los mensajes NUNCA se persistían en Postgres

El descubrimiento más importante de la sesión. Al diseñar el disparador (contar `messages` en Postgres) se vio que la tabla `messages` estaba **vacía**: el código solo escribía en Redis (`memory.append`), nunca hacía INSERT en Postgres. La tabla, modelada en la Parte 1 y migrada en la Parte 5, nunca se había usado.

- **Implicación:** sin persistencia durable, el historial se perdía al expirar Redis (24h). Y el disparador por conteo en Postgres habría dado siempre 0.
- **Solución:** persistir cada par user/assistant en `messages` dentro de `run_agent`, además de en Redis. Una corrección, tres beneficios: disparador fiable, historial durable, y datos listos para el futuro endpoint de "listar conversaciones".

```python
async def _persist_messages(conversation_id, user_input, answer):
    async with SessionLocal() as session:
        session.add_all([
            Message(conversation_id=conversation_id, role="user", content=user_input),
            Message(conversation_id=conversation_id, role="assistant", content=answer),
        ])
        await session.commit()
```

### Bug 3 — La extracción devolvía 0 hechos (prompt demasiado estrecho)

Tras arreglar el Bug 1, la extracción corría (`succeeded ... : 0`) pero guardaba 0 hechos. Diagnóstico con datos: se corrió el LLM a mano con el prompt y los mensajes reales, y respondió literalmente `'NADA'`.

- **Causa:** el prompt pedía "hechos sobre el usuario, su EMPRESA o sus PROYECTOS". qwen2.5, con lógica, descartó un dato personal (un gato y su comida) por no ser empresarial.
- **Solución:** ampliar el prompt para capturar CUALQUIER hecho memorable (datos personales, mascotas, preferencias, rutinas, además de lo empresarial), con ejemplos explícitos. Y pedir reformular en tercera persona ("El usuario...") para mejorar la recuperación posterior (el embedding matchea mejor un hecho declarativo que el texto conversacional original).
- **Resultado:** extrajo "El usuario solo alimenta a su gato con atún los jueves."

### Falso Bug — La herramienta "escupía su docstring"

En un test temprano, el agente respondió con una paráfrasis del docstring de la herramienta ("Search your long-term memory...") en vez de ejecutarla. PARECÍA un bug de tool-calling, pero el test era inválido: por el Bug 1/3, la tabla estaba vacía y no había nada que recuperar. Con un hecho real en la base, qwen2.5 llamó a la herramienta correctamente. No requirió arreglo. (Lección: no diagnosticar un bug sobre un test que no es válido.)

---

## 4. Verificación — la huella conversacional

El método: contar un dato inventado en una conversación y recuperarlo en OTRA distinta. Si cruza la frontera entre conversaciones, es memoria de largo plazo (no de corto).

**Fase 1 — enseñar (conversación 3):** 3 intercambios para cruzar el umbral de 6 mensajes. Huella: "mi gato se llama Wenceslao y solo come atún los jueves".

**Fase 2 — extracción (verificada por capas):**
- `long_term_memories` quedó con: "El usuario solo alimenta a su gato con atún los jueves." (1 hecho).

**Fase 3 — retriever aislado:** `search_memories('como come el gato')` devolvió el hecho.

**Fase 4 — la prueba real (conversación NUEVA, id 6, sin contexto de Redis):**

```
P: "Te acuerdas que le doy de comer a mi gato y cuando?"
R: "Tu gato se alimenta con atún los jueves."
```

El log mostró `conv=6 enviando 1 mensajes al modelo` — un solo mensaje, sin historial previo — y aun así respondió con el dato. Prueba de que vino de la memoria de largo plazo, no del contexto de la conversación.

---

## 5. Aprendizajes clave

1. **El autogenerate de Alembic es ciego a SQL custom.** Columnas generadas, índices y extensiones fuera de los modelos aparecen como "a borrar". Revisar SIEMPRE el archivo generado; escribir a mano lo que toque esos objetos. Para tablas limpias el autogenerate sirve; cuando hay objetos custom en la misma tabla, no.

2. **Montar las carpetas de migración en el host.** Si `./alembic` no está montado, las migraciones generadas en el contenedor son efímeras. Deben vivir en el host (y en git).

3. **Verificar suposiciones de persistencia.** Se asumió que los mensajes se guardaban en Postgres; no era así (solo Redis). Un `SELECT count(*)` lo reveló antes de construir el disparador sobre una base falsa.

4. **Un prompt de extracción sesgado pierde hechos.** Enfocar el prompt en "empresa/proyectos" hizo que el modelo descartara datos personales válidos. Para un asistente con memoria, el prompt debe capturar hechos amplios. Reformular en tercera persona mejora la recuperación.

5. **No diagnosticar bugs sobre tests inválidos.** El "escupir docstring" parecía un bug de tool-calling, pero ocurría porque no había datos que recuperar. Con datos reales, desapareció. Validar primero que el test tiene sentido.

6. **La memoria de largo plazo es RAG sobre conversaciones.** Reutilizó casi toda la maquinaria existente (embeddings, pgvector, engine NullPool, patrón de herramienta, tarea Celery). Lo único nuevo de verdad: la extracción de hechos y el disparador.

---

## 6. Comandos de referencia (nuevos de esta parte)

### Generar y aplicar una migración (con el cuidado del autogenerate)

```powershell
docker exec nexaagent-api-1 alembic revision --autogenerate -m "descripcion"
# REVISAR el archivo: borrar DROPs espurios de content_tsv / idx_chunks_tsv
docker exec nexaagent-api-1 alembic upgrade head
```

### Disparar la extracción de memorias a mano (debug)

```powershell
docker exec nexaagent-api-1 python -c "import asyncio; from app.agent.memory_extraction import extract_and_store_memories; print('Hechos:', asyncio.run(extract_and_store_memories(3)))"
```

### Ver los hechos guardados

```powershell
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "SELECT id, conversation_id, content FROM long_term_memories;"
```

### Probar el retriever de memorias aislado

```powershell
docker exec nexaagent-api-1 python -c "import asyncio; from app.rag.memory_retriever import search_memories; print(asyncio.run(search_memories('como se llama el gato del usuario')))"
```

### Ver la respuesta cruda del LLM en la extracción (diagnóstico de prompt)

```powershell
# Re-ejecuta el LLM con el prompt y los mensajes reales de una conversación
# (ver script completo en el historial; imprime mensajes + respuesta cruda)
```

---

## 7. Estado y siguientes pasos

### Funcionando ✅ (nuevo en la Parte 6)

- **Memoria de largo plazo end-to-end:** extracción de hechos (Celery) → embeddings → pgvector → recuperación como herramienta → uso por el agente en otra conversación.
- **Mensajes persistidos en Postgres** (antes solo en Redis): historial durable + base para el disparador y el futuro endpoint de conversaciones.
- Disparador automático cada 6 mensajes.
- Migraciones persistidas en el host (`./alembic` montado).

### Pendiente de probar / construir 🔜 (heredado y nuevo)

- **Deduplicación de memorias:** la ventana móvil puede guardar hechos repetidos. Estrategias: comprobar similitud antes de insertar, o deduplicar periódicamente.
- **Calidad de extracción:** un modelo 7B a veces resume de más (fusionó "Wenceslao" + "atún" perdiendo el nombre). Evaluable con prompts mejores o un modelo mayor.
- **Endpoint para listar conversaciones:** ahora trivial, porque los mensajes ya se persisten.
- **Streaming de respuestas (SSE).**
- **Seguridad de producción:** OAuth2/JWT en vez de x-api-key.
- **Bug menor:** `OLLAMA_HOST=hhttp://` en docker-compose.yml.
- **Refactor opcional:** unificar el patrón "engine NullPool propio" (aparece ya en 4 sitios: ingest, retriever, memory_retriever, memory_extraction).

### Nota sobre el patrón repetido

El "engine NullPool propio" ya aparece en cuatro archivos. Es el candidato perfecto para un helper compartido (`app/db/worker_engine.py` con una función `async with worker_session() as s: ...`). No urgente, pero cada vez más justificado.

---

*Cierre de la Parte 6.*
