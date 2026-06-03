# NexaAgent — Bitácora de desarrollo (Parte 8)

> Continuación de la Parte 7. Objetivo de esta sesión: **streaming de respuestas (SSE)** — que el
> agente emita su respuesta token a token en vivo, además de señales de cuándo usa herramientas,
> en lugar de devolver el bloque entero al final.

**Estado al cierre de la Parte 8:** streaming funcionando de punta a punta vía un endpoint nuevo `POST /chat/stream`. Emite el `conversation_id`, señales de inicio/fin de herramienta, los tokens del LLM uno a uno, y un evento de cierre. La persistencia (Redis + Postgres + extracción de memorias) ocurre al cerrar el stream, idéntica a `/chat`.

---

## 1. Objetivo y diseño

El endpoint `/chat` devolvía la respuesta completa en un JSON al final. El streaming la emite a medida que se genera, para una UX de "ver escribir". Pero un agente no es un LLM lineal: piensa, llama herramientas, recibe resultados, y entonces responde. Eso planteó tres tensiones.

### Las tres tensiones (y cómo se resolvieron)

1. **El agente es un grafo con herramientas, no un LLM lineal.** Hay que emitir solo lo relevante (tokens de la respuesta + señales de herramienta), no basura intermedia. → Resuelto usando `astream_events` y filtrando por tipo de evento.

2. **La persistencia ocurre al final, el streaming emite durante.** `run_agent` guarda en Redis/Postgres y dispara la extracción DESPUÉS de tener la respuesta. → Resuelto acumulando los tokens en una lista mientras se emiten, y persistiendo al cerrar el stream con el texto completo.

3. **SSE tiene formato y transporte propios.** No es JSON normal. → `StreamingResponse` de FastAPI con `media_type="text/event-stream"`, cada evento como `data: {json}\n\n`.

### Decisiones

| Decisión | Elección |
|---|---|
| Nivel de streaming | Tokens del LLM (ver escribir) |
| Endpoint | Nuevo `/chat/stream` (se deja `/chat` intacto) |
| Señales de herramienta | Sí (tool_start / tool_end) — mejor UX de agente |
| API de LangGraph | `astream_events(version="v2")` |

Por qué `astream_events` y no `stream_mode="messages"`: el modo `messages` da tokens del LLM pero no avisa limpiamente del inicio/fin de herramientas. Como se quería emitir AMBOS (tokens + señales), `astream_events` —que emite eventos tipados granulares— era la API correcta.

---

## 2. Diagnóstico previo: sondear los eventos reales

Antes de escribir el endpoint, se sondeó qué eventos emite el stack real (`langgraph==1.2.1`), en vez de asumir. Una pregunta que dispara una herramienta reveló:

- `on_chat_model_stream` (name=`ChatOllama`) → un token, en `data['chunk']`.
- `on_tool_start` (name=la herramienta) → empezó, en `data['input']`.
- `on_tool_end` (name=la herramienta) → terminó, en `data['output']`.
- `version='v2'` funciona en este stack.

Con ese mapa, el generador se escribió sin adivinar nombres de eventos.

---

## 3. Implementación

### `run_agent_stream` (orchestrator.py) — generador async

```python
async def run_agent_stream(conversation_id, user_input):
    history = await memory.load_history(conversation_id)
    messages = _to_lc_messages(history) + [HumanMessage(content=user_input)]
    full_answer = []

    async for ev in _agent_singleton().astream_events({"messages": messages}, version="v2"):
        etype = ev["event"]
        if etype == "on_chat_model_stream":
            token = getattr(ev["data"]["chunk"], "content", "") or ""
            if token:
                full_answer.append(token)
                yield {"type": "token", "value": token}
        elif etype == "on_tool_start":
            yield {"type": "tool_start", "tool": ev.get("name", "")}
        elif etype == "on_tool_end":
            yield {"type": "tool_end", "tool": ev.get("name", "")}

    answer = "".join(full_answer).strip()
    # Persistencia diferida (texto completo), igual que run_agent:
    await memory.append(conversation_id, "user", user_input)
    await memory.append(conversation_id, "assistant", answer)
    await _persist_messages(conversation_id, user_input, answer)
    await _maybe_extract_memories(conversation_id)
    yield {"type": "done"}
```

El filtro `if token:` descarta de forma natural los tokens "vacíos" que el modelo produce al decidir una llamada a herramienta (esos van en `tool_calls`, no en `content`).

### El endpoint (chat.py)

```python
@router.post("/stream")
async def chat_stream(payload: ChatRequest) -> StreamingResponse:
    conversation_id = await _ensure_conversation(payload)

    async def event_generator():
        # Primer evento: el conversation_id (no hay body JSON en streaming).
        yield f"data: {json.dumps({'type':'meta','conversation_id':conversation_id})}\n\n"
        async for event in run_agent_stream(conversation_id, payload.message):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
```

Detalles: `ensure_ascii=False` preserva los acentos del español (código, autorización). El `conversation_id` viaja como PRIMER evento (`meta`) porque en streaming no hay body donde devolverlo (Tensión 3). Se refactorizó la creación de conversación a `_ensure_conversation`, compartida con `/chat`.

---

## 4. Error encontrado: `SyntaxError: 'return' with value in async generator`

Al pegar las dos funciones (`run_agent` y `run_agent_stream`), el `return answer` de la primera se coló al final de la segunda por un copy-paste cruzado. Resultado:
- `run_agent_stream` (un generador, tiene `yield`) terminaba en `return answer` → ilegal en Python, un generador no puede `return` con valor. La app no arrancaba.
- `run_agent` quedó SIN su `return answer` → habría devuelto `None` (bug silencioso en `/chat`).

- **Diagnóstico:** el log de arranque señaló la línea exacta: `SyntaxError: 'return' with value in async generator`.
- **Solución:** mover el `return answer` — quitarlo del final de `run_agent_stream` (que cierra con `yield {"type":"done"}`), y restaurarlo al final de `run_agent`.
- **Lección:** una función con `yield` es un generador y nunca lleva `return <valor>`; emite todo por `yield`. Al fusionar funciones por copy-paste, verificar que cada `return`/`yield` quedó en la función correcta.

---

## 5. Verificación

### El streaming funciona

`POST /chat/stream` con la pregunta de Rubí emitió, en orden y progresivamente:

```
data: {"type": "meta", "conversation_id": 8}
data: {"type": "tool_start", "tool": "search_knowledge_base"}
data: {"type": "tool_end", "tool": "search_knowledge_base"}
data: {"type": "token", "value": "El"}
data: {"type": "token", "value": " código"}
... (tokens uno a uno, partidos como los emite el modelo: "Rub"+"í", "E"+"SM"+"-"+"2"...)
data: {"type": "done"}
```

Orden correcto: el agente busca (tool_start/end) ANTES de generar la respuesta. Los tokens fluyen de a poco (verificado con `curl -N`, que desactiva el buffering). Las tres tensiones resueltas.

### La persistencia diferida funciona

Tras un stream, los mensajes quedaron en la tabla `messages` — el streaming guarda igual que `/chat`. La memoria y el historial intactos.

### Hallazgo colateral (NO es del streaming): extracción incorrecta de Rubí

El streaming devolvió `ESM-2025-K3` como código de Rubí — INCORRECTO (ese es el de Esmeralda; el de Rubí es RB-2023-Q7).

- **No es un fallo del streaming:** el endpoint `/chat` normal (con `ainvoke`) respondió IGUAL de mal. El transporte es transparente al contenido; emite lo que el modelo produzca.
- **Causa:** el chunk recuperado contiene AMBOS proyectos (Esmeralda + Rubí cayeron juntos en un chunk desde la Parte 3). qwen2.5 7B, ante dos códigos en el mismo texto, a veces toma el equivocado. Al pedirle explícitamente "el de Rubí, no el de Esmeralda", acertó (RB-2023-Q7).
- **No es una regresión nueva:** en la Parte 4 acertó, pero un 7B a temp 0 no es perfectamente determinista entre arranques de contenedor. La fragilidad del chunk multi-proyecto siempre estuvo ahí.
- **La cura de fondo** es el re-chunking consciente de estructura (pendiente desde la Parte 3): trocear por sección para que cada proyecto tenga su propio chunk con un solo código. Merece su propia sesión.

---

## 6. Aprendizajes clave

1. **Sondear los eventos del stack antes de escribir el handler.** `astream_events` cambia entre versiones; volcar los eventos reales (nombres, estructura de `data`) evitó adivinar y escribir código frágil.

2. **`astream_events` para emitir tokens Y señales de herramienta.** `stream_mode="messages"` da solo tokens; los eventos tipados de `astream_events` permiten distinguir token / tool_start / tool_end.

3. **Acumular durante, persistir al cerrar.** El patrón para reconciliar streaming con efectos secundarios (guardar, disparar tareas): acumular el texto emitido en una lista y ejecutar la persistencia tras el `async for`, con la respuesta completa.

4. **Un generador (`yield`) nunca lleva `return <valor>`.** Mezclar `yield` y `return con valor` es un SyntaxError. Cuidado al fusionar funciones por copy-paste.

5. **El `conversation_id` va como primer evento en streaming.** No hay body JSON donde devolverlo; se emite como evento `meta` antes de los tokens.

6. **`curl -N` para ver el streaming en vivo.** Sin `-N`, curl bufferiza y muestra todo al final, ocultando el efecto.

7. **Aislar transporte de contenido al diagnosticar.** El código equivocado de Rubí parecía culpa del streaming; probar `/chat` (no streaming) demostró que el modelo fallaba igual. El streaming estaba perfecto.

---

## 7. Comandos de referencia (nuevos de esta parte)

### Sondear los eventos de astream_events (diagnóstico)

```powershell
docker exec nexaagent-api-1 python -c "import asyncio; from app.agent.orchestrator import _agent_singleton; from langchain_core.messages import HumanMessage; ...(volcar ev['event'] de astream_events version='v2')..."
```

### Probar el streaming (con -N para ver los tokens fluir)

```powershell
curl.exe -N -X POST http://localhost:8000/chat/stream -H "x-api-key: <API_KEY>" -H "Content-Type: application/json" -d '{\"message\": \"Cual es el codigo de autorizacion del Proyecto Rubi?\"}'
```

### Confirmar persistencia diferida tras un stream

```powershell
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "SELECT conversation_id, role, left(content,40) FROM messages ORDER BY id DESC LIMIT 2;"
```

---

## 8. Estado y siguientes pasos

### Funcionando ✅ (nuevo en la Parte 8)

- **Streaming SSE end-to-end** (`POST /chat/stream`): meta → tokens + señales de herramienta → done.
- Persistencia diferida idéntica a `/chat` (Redis + Postgres + extracción de memorias).
- `/chat` original intacto; creación de conversación compartida vía `_ensure_conversation`.

### Pendiente de probar / construir 🔜 (heredado y nuevo)

- **Re-chunking consciente de estructura (PRIORIDAD ELEVADA):** ahora con evidencia fresca — el chunk compartido Esmeralda+Rubí hace que el modelo entregue el código equivocado de forma intermitente. Trocear por sección/proyecto lo resolvería de raíz. Validar con el test de los cuatro proyectos como regresión.
- **Deduplicación de memorias** (deuda de la Parte 6).
- **`DELETE /conversations/{id}`** (opcional; limpiar conversaciones de prueba).
- **Seguridad de producción:** OAuth2/JWT en vez de x-api-key.
- **Bug menor:** `OLLAMA_HOST=hhttp://` en docker-compose.yml.
- **Refactor opcional:** unificar el patrón "engine NullPool propio" (4 sitios).
- **Recordatorio Alembic:** cada `--autogenerate` futuro intentará borrar el full-text; revisar el archivo generado.

### Nota sobre señales de herramienta en la UI

Los eventos `tool_start`/`tool_end` ahora mismo solo llevan el nombre de la herramienta. Para una UI se podrían enriquecer (ej. mensaje amigable "Buscando en documentos..." mapeado por nombre de herramienta). Queda como mejora de presentación.

---

*Cierre de la Parte 8.*
