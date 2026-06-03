# NexaAgent — Bitácora de desarrollo (Parte 15)

> Continuación de la Parte 14. Objetivo de esta sesión: **operabilidad** — el salto de
> "funciona en mi máquina por Swagger" a "sistema que aguanta producción". Tres frentes:
> **tests automatizados**, **observabilidad** (logging estructurado + correlación) y
> **resiliencia** (timeouts, reintentos, degradación con gracia, manejo limpio de fallos de infraestructura).

**Estado al cierre de la Parte 15:** suite de tests verde (14, sobre BD real). Logging JSON estructurado con correlación `request_id`/`conversation_id` que cruza incluso la frontera API→worker. Resiliencia completa en sus cinco eslabones: el sistema sobrevive a Ollama caído (503 limpio + reintento), a Redis caído (degradación), y a fallos de ingesta (reintento idempotente + estado `failed`). Quedan pendientes de observabilidad las métricas (`/metrics`) y el `/health` de readiness, que se planearon pero no se implementaron en esta sesión.

---

## 1. Objetivo de la sesión

El sistema venía completo de funcionalidad (RAG híbrido, memoria, streaming, JWT, CRUD) pero crudo de operabilidad: el único instrumento de diagnóstico era `docker-compose logs` + `SELECT` a mano, no había un solo test, y cualquier dependencia caída (Ollama, Redis) producía un `500 Internal Server Error` crudo. Esta sesión ataca eso en tres frentes, en orden deliberado:

1. **Tests** — para tener una red de seguridad antes de tocar código probado.
2. **Observabilidad primero, resiliencia después** — para que los timeouts, reintentos y degradaciones **nazcan ya logueados**, y no haya que volver a instrumentarlos.

La metodología de siempre se aplicó sin excepción: **verificar por capas y diagnosticar con datos antes de teorizar**. Esta vez la regla mordió varias veces (logs fósiles, tests inválidos, excepciones en clases inesperadas), y en una ocasión le tocó al asistente comerse su propia lección.

---

## 2. Tests automatizados (pytest)

La Parte 13 dejó dicho que esto "merece sesión propia donde se decida qué se testea, con qué framework, y se monte la infra". Se cerraron las decisiones primero.

### 2.1 Decisiones de diseño

| Decisión | Elección | Motivo |
|---|---|---|
| Framework | `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"`) | Ya estaban en dev deps |
| Cliente HTTP de tests | `httpx.AsyncClient` + `ASGITransport(app)` | Un solo event loop → evita el "Future attached to a different loop" de la Parte 2 que tendría `TestClient` |
| Ollama / embeddings | Mockeados / omitidos | No determinista y lento; el happy-path del LLM queda para otra sesión |
| Aislamiento entre tests | Truncado entre tests | Que un test no contamine al siguiente |
| BD de tests | `nexaagent_test` en el servicio `db`, migrada con Alembic | Esquema **real** (incl. `vector`/`unaccent` y el cascade de las FKs), no de juguete |

### 2.2 La infraestructura

- **`Dockerfile`** — `ARG INSTALL_DEV` instala `pytest` solo bajo demanda; la imagen de `api`/`worker` sigue limpia.
- **`docker-compose.yml`** — servicio efímero `tests` que apunta a `nexaagent_test`, depende solo de `db` (no de ollama/redis → arranca en segundos).
- **`tests/conftest.py`** — crea `nexaagent_test`, la migra con las **mismas** migraciones de Alembic, trunca entre tests y dispone el engine para no cruzar event loops.

### 2.3 Los 14 tests

- **DELETE `/conversations/{id}`** — 200; cascade real (mensajes + memorias se borran); el 2º DELETE devuelve **404, no 500** (la regresión exacta del `NameError` del `status` de la Parte 13); 404 en inexistente; gate de auth.
- **Auth** — login OK / 401; validación 422; rutas protegidas con y sin token.
- **Validación `/chat`** — 422 y gate, sin tocar Ollama.

Resultado: `14 passed in 1.75s`. Se corre con:

```powershell
docker compose run --rm tests
```

### 2.4 Dos hallazgos (bugs cazados por el propio harness)

1. **`str(URL)` enmascara la contraseña como `***` en SQLAlchemy 2.0.** El `conftest` construía la URL de la BD de test con `str(engine.url)`, que oculta la password → mandaba `***` literal a psycopg2 y la autenticación a Postgres fallaba. **Arreglo:** `render_as_string(hide_password=False)`. Bug del propio andamiaje de tests, atrapado por el andamiaje — la mejor señal de que la suite vale.

2. **El gate JWT devuelve `401`, no `403`.** Un comentario en `security.py` afirmaba que `HTTPBearer(auto_error=True)` da 403 si falta el header. Pero FastAPI 0.136 devuelve **401** — que además es lo semánticamente correcto (401 = sin credenciales; 403 = autenticado pero sin permiso). El comentario quedó desactualizado; los tests fijan el **contrato real**.

### 2.5 Cabos sueltos identificados (pendientes)

- **`path_separator = os`** en `alembic.ini`: silencia el warning `No path_separator found in configuration` y previene una rotura futura entre versiones mayores de Alembic (misma filosofía que el Error 4 de la Parte 1: no depender de un default que se mueve). Recomendado.
- **Mock de Ollama** para el happy-path de `/chat`: merece su mini-sesión (fabricar los eventos del modelo).
- **Comentario 401/403 en `security.py`**: corregir, ya que los tests fijaron el contrato real.
- **Drift de docs**: `.env.example` y README aún hablan de `API_KEY`/`x-api-key`, anterior a la migración a JWT de la Parte 12.

---

## 3. Observabilidad — logging estructurado y correlación

### 3.1 Decisiones de diseño

| Área | Elección | Nota |
|---|---|---|
| Logging | stdlib `logging` + formatter JSON hecho a mano | **Cero dependencias nuevas** → sin el baile de `--no-cache` (Partes 5 y 12) |
| Correlación | middleware + `contextvar` + filtro | Inyecta `request_id` y `conversation_id` en **cada** línea |
| Formato unificado | `dictConfig` que vacía los handlers de uvicorn y los hace propagar | Un solo JSON para todo, no logs planos mezclados |
| Niveles | INFO normal · WARNING degradación · ERROR fallo · DEBUG (off) | El token-a-token del streaming iría a DEBUG |

### 3.2 Las piezas

- **`app/core/log_context.py`** — dos `ContextVar` (`request_id`, `conversation_id`) y un `ContextFilter` que los inyecta en cada `LogRecord`.
- **`app/core/logging_config.py`** — un `JsonFormatter` (con `ensure_ascii=False` para preservar acentos, misma lección que la Parte 8) y `setup_logging()` con `dictConfig`. Los loggers de uvicorn se vacían y propagan al root para salir en el mismo JSON.
- **`app/api/request_context.py`** — `RequestIDMiddleware` (un `BaseHTTPMiddleware`) que asigna o propaga un `X-Request-ID` por petición y lo fija en el contextvar.
- **Integración**: `setup_logging()` **antes** de crear la app (para capturar logs de arranque); el `print(..., flush=True)` de `orchestrator.py` y los `[memory dedup]` de `memory_extraction.py` pasaron a `logger` con campos (`extra={...}`).

### 3.3 El cruce de proceso (API → worker Celery)

El detalle clave, y un eco directo de la Parte 2 ("otro proceso, otro loop"): **el contextvar NO viaja solo a Celery.** Vive en el proceso de FastAPI; el worker es otro proceso. Hay que:

1. Pasar el `request_id` **explícitamente como argumento** de la tarea: `extract_memories.delay(conversation_id, request_id=request_id_var.get())`.
2. Re-fijarlo dentro de la tarea: `request_id_var.set(request_id)` al entrar.
3. Conectar `setup_logging()` a la señal `setup_logging` de Celery, para que el worker emita el mismo JSON.

### 3.4 Verificación (las huellas)

Mismo método que las huellas `ZF-2024-X9` (Parte 2) y Wenceslao (Parte 6): un marcador distintivo y rastrearlo.

- **En proceso** (`X-Request-ID: prueba-abc-123`): las 4 líneas de un `/chat` salieron correlacionadas, **incluidas las de `httpx`** (la librería de terceros que llama a Ollama). El contextvar se inyecta en todo lo que loguea durante la petición, no solo en código propio.
- **Cruce de proceso** (`worker-test-777`): el mismo id apareció en los logs del **worker** (`nexa.worker`, `nexa.memory`, `httpx`). La correlación cruza la frontera. Fase de correlación verificada de punta a punta.

**Matiz observado:** las líneas que Celery emite en su propio framework (`Task ... received` / `succeeded`) salen con `request_id: "-"` en el campo, pero el id viaja en el `payload` (`kwargs`). Solo las líneas del código propio lo llevan en el campo. Es esperado: el framework loguea el "received" **antes** de que el código entre y haga `set()`. Nada que arreglar.

### 3.5 Hallazgo: el falso positivo de dedup (Paco ≈ Bruno)

La observabilidad pagó su primer dividendo de inmediato. Al probar el cruce de proceso con un dato nuevo (un loro llamado Paco), el log mostró:

```
dedup skip (distance: 0.079) fact: "El usuario tiene un loro llamado Paco."
                                matched: "El usuario tiene un perro llamado Bruno."
```

El sistema descartó "loro Paco" como duplicado de "perro Bruno" a distancia **0.079** (umbral 0.15) → `stored=2 skipped=1`, perdiendo el hecho de la existencia del loro. **Es un falso positivo**, y confirma **en vivo** la limitación estructural de `nomic-embed-text` que se predijo en la Parte 11: el embedding pondera la estructura de la frase ("El usuario tiene un [animal] llamado [nombre]") por encima del contenido concreto. Antes este fallo habría sido invisible (la tabla simplemente no tendría a Paco, sin explicación); ahora está ahí, con su distancia exacta y los dos hechos que colisionaron. **Deuda** (recalibrar umbral o post-procesar hechos atómicos), no se tocó esta sesión.

### 3.6 Lo que quedó pendiente de observabilidad

Se planearon cinco pasos; se completaron logging + correlación (incl. cruce de proceso). **No** se implementaron:

- **Middleware de latencia/status por request** (una línea `request_end` con ms y status). La latencia hoy se lee **a mano** de los timestamps de los logs (así se capturó el cold-start de ~39s).
- **`/metrics`** (Prometheus client).
- **`/health` de readiness** (DB/Redis/Ollama alcanzables).

Quedan para una próxima sesión de observabilidad.

---

## 4. Resiliencia — cinco eslabones

Orden de implementación: cada eslabón se apoyó en el anterior, y la observabilidad de la sección 3 hizo que todos nacieran logueados.

### 4.1 (6) `timeout` + `keep_alive` en `ChatOllama`

**Verificación previa (no asumir el nombre del parámetro):** `inspect.signature(ChatOllama)` reveló que **NO acepta `timeout=` directo**, pero sí `client_kwargs` y `async_client_kwargs`. El timeout va por ahí (al cliente httpx subyacente). Como el agente usa rutas **async** (`ainvoke`, `astream_events`), `async_client_kwargs` es el que importa; se pusieron ambos por simetría.

```python
llm = ChatOllama(
    model=settings.ollama_model,
    base_url=settings.ollama_base_url,
    temperature=0,
    keep_alive="30m",                       # mantiene el modelo residente en VRAM
    client_kwargs={"timeout": 120},
    async_client_kwargs={"timeout": 120},   # ruta async (la que usa el agente)
)
```

**Sutilezas:**
- `keep_alive` aquí (por-request) **sobreescribe** el `OLLAMA_KEEP_ALIVE:5m0s` del servidor (visto en los logs de la Parte 14). No hay que tocar el `.env` de Ollama.
- El `timeout` es la red de seguridad (Ollama colgado no cuelga el request para siempre); `keep_alive` es lo que de verdad evita el cold-load recurrente. Con GPU (Parte 14), el cold-load bajó pero **sigue existiendo** la primera vez que el modelo carga a VRAM.

**Verificación:** tras una llamada, `ollama ps` mostró `qwen2.5  100% GPU  CONTEXT 8192  UNTIL 29 minutes from now`. Tres ajustes de las últimas sesiones confirmados de un vistazo: GPU activa, contexto 8192 (Parte 14) y keep_alive 30m. Latencias observadas: ~39s en frío (cold-load a VRAM), ~5s con el modelo residente.

### 4.2 (7) Handler 503 y el muro de `BaseHTTPMiddleware`

**El hallazgo más jugoso de la sesión.** El primer intento —registrar `app.add_exception_handler(httpx.ConnectError, ...)` y `httpx.TimeoutException`— **falló**: con Ollama parado, `/chat` devolvía `500 Internal Server Error` crudo, sin la línea ERROR del handler.

**Diagnóstico con datos, en dos pasos:**
1. `print(list(app.exception_handlers.keys()))` → los handlers **SÍ** estaban registrados (`ConnectError: True`). No era falta de registro.
2. El traceback completo mostró la excepción subiendo por `request_context.py:14, in dispatch / await call_next(request)` y `starlette/_utils.py, collapse_excgroups`. **Causa:** un `BaseHTTPMiddleware` (el `RequestIDMiddleware`) **esquiva los exception handlers de FastAPI** — limitación conocida de starlette. La excepción se recaptura en el `call_next` del middleware y nunca llega a los handlers a nivel de app.

**Solución:** traducir la excepción **dentro del propio código**, antes de que cruce el middleware. Una excepción de dominio propia:

```python
# app/core/exceptions.py
class UpstreamUnavailable(Exception):
    """El servicio de inferencia (Ollama) no respondió. Se traduce a 503."""
```

```python
# en run_agent (no-stream): captura y RELANZA como tipo propio
try:
    result = await _ainvoke_with_retry(_agent_singleton(), messages)
except (httpx.ConnectError, httpx.TimeoutException, ConnectionError) as exc:
    logger.error("ollama unreachable after retries",
                 extra={"event": "upstream_error", "exc_type": type(exc).__name__},
                 exc_info=exc)
    raise UpstreamUnavailable() from exc
```

El handler ahora escucha `UpstreamUnavailable` (que se relanza desde dentro, no atraviesa el muro de la misma forma).

**Beneficio colateral:** como la captura ocurre **dentro del request** (contextvar activo), el error ERROR queda **correlacionado con su `request_id`** — antes el 500 crudo lo emitía uvicorn fuera de contexto y salía con `request_id: "-"`. El fix arregló el status **y** la trazabilidad.

**Asimetría stream vs no-stream (clave):**
- `run_agent` (no-stream): relanza `UpstreamUnavailable` → 503.
- `run_agent_stream` (stream): **no puede** relanzar — el `200 OK` y el `meta` ya salieron. En su lugar **emite un evento** `{"type": "error", "message": "..."}` y cierra con `return`.

```python
# en run_agent_stream:
except (httpx.ConnectError, httpx.TimeoutException, ConnectionError) as exc:
    logger.error("ollama unreachable mid-stream", extra={...}, exc_info=exc)
    yield {"type": "error", "message": "El servicio de inferencia no está disponible."}
    return  # NO relanza; el 200 ya se envió
```

**Verificación:** no-stream → `503` limpio con log correlacionado; stream → `data: {"type":"meta"...}` seguido de `data: {"type":"error"...}` y cierre ordenado; **el 404 del DELETE intacto** (`Conversation 999999 not found`), prueba de que el handler discrimina infraestructura de errores deliberados.

**Decisión consciente:** se ven **dos** líneas ERROR por fallo (`nexa.agent "ollama unreachable"` + `nexa.errors "upstream unavailable"`). No es duplicación accidental: es la cadena de traducción (capturar+loguear en el orchestrator → relanzar tipo limpio → handler responde 503). En un fallo real interesan ambos puntos de vista; se dejan los dos.

### 4.3 (9) Degradación ante la caída de Redis

Redis tiene **dos roles** (desde la Parte 1) con tolerancias opuestas: memoria de corto plazo **y** broker de Celery. Por eso la degradación tiene **dos puntos**:

- **Punto A — historial (`memory.py`):** `load_history` degrada a **historial vacío**; `append` degrada a **omitir** la caché (los mensajes igual van a Postgres desde la Parte 6, así que no se pierde el registro durable). Captura `RedisError` (la clase **base**, que cubre todos los modos de fallo).
- **Punto B — encolado (`orchestrator._maybe_extract_memories`):** el `.delay()` envuelto en `try/except OperationalError` (kombu), para que un fallo al encolar **no tumbe la respuesta ya generada**.

**Verificación:** con Redis parado, `/chat` respondió **200**. El log de un único request mostró los tres puntos disparándose sin propagar: un `WARNING redis_degraded op=load_history` + dos `op=append` (user + assistant), `exc_type: ConnectionError` (subclase de `RedisError`, capturada correctamente). El Punto B **no se ejercitó** (la prueba fue de 1 mensaje, no cruzó el umbral de 6); queda implementado pero sin verificación en vivo.

**Comportamiento esperado:** con Redis caído, cada turno arranca sin memoria reciente. Dos preguntas seguidas → el agente no recuerda la primera. Es la degradación funcionando, no un bug.

### 4.4 (10) Idempotencia + autoretry + estado `failed`

**El orden interno es ley: idempotencia PRIMERO, autoretry DESPUÉS.** Al revés se corrompen datos.

**Parte 1 — Idempotencia.** La tarea hace parseo → chunking → embeddings → INSERT. Si fallaba a mitad de los embeddings y reintentaba, **reinsertaba** los chunks ya guardados → duplicados → RAG ensuciado. Cura: **borrar** los chunks del `document_id` **antes** de insertar, en una transacción (con el `worker_session()` de la Parte 13).

```python
async with worker_session() as session:
    await session.execute(text("DELETE FROM document_chunks WHERE document_id = :doc_id"),
                          {"doc_id": document_id})
    # ... INSERT de los chunks nuevos + UPDATE status='ready' ...
    await session.commit()   # borrado + inserción + estado: atómico
```

*Verificación:* conteo del doc 8 = 7 → re-ingerir → **7** (no se duplicó). Tarea repetible. Solo con esto verificado se pasó al autoretry.

**Parte 2 — Autoretry (la trampa de la clase de excepción).** El primer intento de autoretry **no reintentó**: un solo `attempt=0` y directo a `failed`, en ~3s. El traceback reveló por qué:

```
ollama/_client.py:145: raise ConnectionError(CONNECTION_ERROR_MESSAGE) from None
ConnectionError: Failed to connect to Ollama...
```

Los **embeddings** (`OllamaEmbeddings`) usan el cliente oficial `ollama`, que atrapa el error de httpx y lo **re-lanza como `ConnectionError` built-in de Python** (no `httpx.ConnectError`). El `autoretry_for=(httpx.ConnectError, httpx.TimeoutException)` no incluía esa clase → Celery no lo trató como reintentable.

**La asimetría clave:** el **chat** (`ChatOllama`, paso 7) lanza `httpx.ConnectError`; los **embeddings** (`OllamaEmbeddings`) lanzan `ConnectionError` built-in. **Mismo Ollama caído, dos clases de excepción según la ruta de cliente.** Imposible de adivinar sin el traceback.

**Arreglo (una línea):** añadir `ConnectionError` built-in a la tupla. Como `httpx.ConnectError` **hereda** de `ConnectionError`, capturar `ConnectionError` cubre ambos casos.

```python
@celery_app.task(
    name="ingest_document", bind=True,
    autoretry_for=(httpx.ConnectError, httpx.TimeoutException, ConnectionError),  # + built-in
    retry_backoff=True, retry_backoff_max=60, max_retries=3,
)
```

**Parte 3 — Estado `failed`.** Tras agotar reintentos, el hook `on_failure` de la tarea marca el documento `failed` (antes un fallo permanente lo dejaba `pending`, invisible — hueco propio señalado). Nota: `on_failure` corre en contexto **síncrono**, no async; no puede usar el `worker_session()` async directo.

**Verificación final:** con Ollama caído y subiendo por Swagger (que encola la **tarea real**, no la corutina pelada — el `python -c` se salta el decorador de autoretry), el log mostró `attempt=0 → retry → attempt=1 → retry → attempt=2 → ingest failed permanently`, y `status='failed'`. Los **tres** intentos numerados.

**Deuda anotada:** los reintentos dicen `Retry in 0s`, no el backoff exponencial (1s, 2s, 4s) de `retry_backoff=True`. En Celery 5.6.3 el `retry_backoff` del decorador no surte efecto con `autoretry_for` tal cual está. El objetivo de resiliencia se cumple (reintenta + degrada), así que se **anota y no se investiga** esta sesión; si en producción el reintento-inmediato martillea un Ollama recuperándose, se afina con `self.retry(countdown=...)` explícito.

### 4.5 (8) Retry de la inferencia (no-stream)

Retry de la **inferencia** (no de la ingesta). Decisión: **Opción A — tenacity** (explícito, controlado por nosotros, reutiliza `UpstreamUnavailable`), **2 intentos totales (1 retry), backoff corto** — porque hay un humano esperando (tensión interactivo-vs-background: la ingesta usa 3 reintentos en background; el chat usa 2).

```python
@retry(
    retry=retry_if_exception_type((httpx.ConnectError, httpx.TimeoutException, ConnectionError)),
    stop=stop_after_attempt(2),                       # 2 intentos = 1 reintento
    wait=wait_exponential(multiplier=0.5, max=2),     # ~0.5s, corto a propósito
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,                                     # ← CLAVE
)
async def _ainvoke_with_retry(agent, messages):
    return await agent.ainvoke({"messages": messages})
```

**Por qué `reraise=True` es la pieza crítica:** sin él, tenacity envuelve el fallo final en su propio `RetryError`, que el `try/except` externo (paso 7) **no** capturaría → volvería al 500 crudo. Con `reraise=True`, tras agotar los 2 intentos relanza la **excepción original** (`httpx.ConnectError`), que el `try/except` externo sí traduce a `UpstreamUnavailable` → 503. tenacity reintenta; el `try/except` del paso 7 traduce. Cada uno su trabajo.

**El streaming NO se tocó** — ya resuelto en el paso 7 (emite evento de error, no reintenta a media emisión).

**Verificación:** con Ollama caído, el log mostró `WARNING Retrying _ainvoke_with_retry in 0.5 seconds as it raised ConnectError` entre los dos intentos, luego `ERROR ollama unreachable after retries` → `ERROR upstream unavailable` → **503**. El camino feliz (Ollama arriba) respondió `¡Hola!` normal, sin reintentos.

**Nota honesta sobre lo observable:** con Ollama **totalmente** caído, los 2 intentos fallan igual de rápido → no se "siente" el retry (backoff 0.5s). El retry brilla en un **hipo transitorio** (medio segundo de indisponibilidad y vuelve), que no se puede simular a mano limpiamente. La verificación honesta posible: ver el WARNING de reintento + el 503 final. En el hipo real, el 2º intento tendría éxito y el usuario nunca vería el error.

---

## 5. Aprendizajes clave

1. **Diagnosticar con datos, una y otra vez.** Esta sesión lo exigió constantemente: `inspect.signature` reveló que `ChatOllama` no acepta `timeout=` directo; el traceback reveló que el fallo de embeddings es `ConnectionError` built-in, no `httpx.ConnectError`; `list(app.exception_handlers)` confirmó que el handler 503 estaba registrado (descartando esa hipótesis). Nunca se adivinó un nombre de clase ni un parámetro.

2. **La misma excepción aparece en clases distintas según el cliente.** Ollama caído sube como `httpx.ConnectError` por la ruta de chat (`ChatOllama`) y como `ConnectionError` built-in por la de embeddings (`OllamaEmbeddings`). Capturar la clase base que las une (`ConnectionError`, de la que `httpx.ConnectError` hereda) cubre ambas.

3. **`BaseHTTPMiddleware` esquiva los exception handlers de FastAPI.** Limitación conocida de starlette: una excepción que sube a través de un `BaseHTTPMiddleware` no llega a los `add_exception_handler` a nivel de app. La solución robusta es traducir a una excepción de dominio **dentro del propio código**, antes de cruzar el middleware.

4. **Streaming y no-streaming manejan errores de forma asimétrica.** En no-stream se puede devolver un 503 (el status no se ha enviado). En stream, el `200` ya salió; solo queda emitir un evento de error y cerrar. Esta asimetría aparece en el paso 7 y se respeta en el 8.

5. **Idempotencia precede al retry.** Activar reintentos sobre una tarea no idempotente duplica datos. El patrón "DELETE-antes-de-INSERT en una transacción" vuelve la tarea repetible; solo entonces el `autoretry` es seguro. El test que importa no es una foto de la tabla, sino re-ingerir y comparar el conteo (7 → 7).

6. **Un test inválido no prueba nada — y los timestamps lo delatan.** `ollama ps` vacío no desmiente `keep_alive` (no había modelo cargado); los logs de las 06:39 reaparecían en cada `Select-String` sin `--tail` (logs fósiles); un placeholder (`abc123`) copiado literal dio `FileNotFoundError`. Reglas reconfirmadas: `--tail N` siempre, cargar el modelo justo antes de `ollama ps`, y subir por Swagger (no `python -c`) para ejercitar el decorador de Celery. *(Esta vez al asistente también le tocó: propuso `abc123` como ejemplo y se copió literal.)*

7. **La observabilidad paga dividendos inmediatos.** El falso positivo de dedup (Paco ≈ Bruno, 0.079) era completamente invisible antes; con logging estructurado quedó expuesto con su distancia y los hechos que colisionaron, confirmando en vivo la limitación de `nomic-embed-text` predicha en la Parte 11.

8. **`reraise=True` en tenacity es lo que conecta el retry con el manejo de errores existente.** Sin él, el fallo final se envuelve en `RetryError` y rompe la cadena de traducción a 503.

9. **El contexto no cruza solo entre procesos.** El `request_id` (contextvar) viaja de la API al worker Celery solo si se pasa explícitamente como argumento de la tarea y se re-fija en el worker. Mismo muro "otro proceso" de la Parte 2, ahora aplicado a la correlación de logs.

10. **Cero dependencias nuevas = sin `--no-cache`.** El logging JSON se hizo con la stdlib y tenacity ya estaba declarada, así que esta sesión evitó por completo el patrón "cambié `pyproject.toml`, rebuild `--no-cache`" de las Partes 5 y 12.

---

## 6. Comandos de referencia (nuevos de esta parte)

### Tests

```powershell
docker compose run --rm tests          # corre la suite en el servicio efímero
```

### Observabilidad: rastrear una petición por su request_id

```powershell
# Lanzar con un X-Request-ID propio y seguirlo (en proceso)
curl.exe -X POST http://localhost:8000/chat -H "Authorization: Bearer <TOKEN>" -H "X-Request-ID: mi-traza" -H "Content-Type: application/json" -d '{\"message\": \"...\"}'
docker compose logs api | Select-String "mi-traza"

# Cruce de proceso (API -> worker): llegar a 6 mensajes para disparar la extracción
docker compose logs worker | Select-String "mi-traza"
```

### Verificar parámetros reales de una clase (no asumir)

```powershell
docker exec nexaagent-api-1 python -c "from langchain_ollama import ChatOllama; import inspect; print(list(inspect.signature(ChatOllama).parameters))"
```

### Confirmar handlers de excepción registrados

```powershell
docker exec nexaagent-api-1 python -c "from app.main import app; import httpx; print(list(app.exception_handlers.keys()))"
```

### Resiliencia: simular caídas

```powershell
# Ollama caído -> 503 (no-stream) / evento error (stream)
docker compose stop ollama
curl.exe -X POST http://localhost:8000/chat -H "Authorization: Bearer <TOKEN>" -H "X-Request-ID: t-503" -H "Content-Type: application/json" -d '{\"message\": \"hola\"}'
docker compose logs api | Select-String "t-503"     # ver WARNING de retry + ERROR + 503
docker compose start ollama

# Redis caído -> /chat responde 200 degradando
docker compose stop redis
curl.exe -X POST http://localhost:8000/chat -H "Authorization: Bearer <TOKEN>" -H "X-Request-ID: t-redis" -H "Content-Type: application/json" -d '{\"message\": \"hola\"}'
docker compose logs api | Select-String "t-redis"   # ver WARNING redis_degraded
docker compose start redis
```

### Resiliencia: verificar reintentos de ingesta (vía Swagger, NO python -c)

```powershell
docker compose stop ollama
# -> subir un PDF por http://localhost:8000/docs (POST /documents/upload): encola la tarea REAL
docker compose logs --tail 40 worker | Select-String "ingest_start|ingest_failed|attempt|retry"
# Esperado: attempt=0,1,2 con sus 'retry', luego 'ingest failed permanently'
docker compose start ollama
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "SELECT id, status FROM documents ORDER BY id DESC LIMIT 1;"   # -> failed
```

### Verificar keep_alive (cargar el modelo justo antes)

```powershell
curl.exe -X POST http://localhost:8000/chat -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" -d '{\"message\": \"hola\"}'
docker exec nexaagent-ollama-1 ollama ps    # columna UNTIL -> ~30 minutes from now
```

### Limpieza de artefactos de prueba

```powershell
# Esta sesión dejó docs 9 y 10 en 'failed' (pruebas de autoretry/failed)
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "SELECT id, filename, status FROM documents ORDER BY id;"
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "DELETE FROM documents WHERE id IN (9, 10);"
```

---

## 7. Estado y siguientes pasos

### Funcionando ✅ (nuevo en la Parte 15)

- **Tests automatizados:** 14 verdes sobre BD real (`nexaagent_test`), migrada con Alembic; servicio `tests` efímero; `INSTALL_DEV` mantiene la imagen de prod limpia.
- **Logging JSON estructurado** unificado (API + worker + uvicorn), con correlación `request_id`/`conversation_id` que cruza la frontera API→worker.
- **Resiliencia completa (5 eslabones):**
  - (6) `timeout` (vía `client_kwargs`) + `keep_alive="30m"` en `ChatOllama`.
  - (7) Handler 503 limpio vía `UpstreamUnavailable` (sorteando el muro de `BaseHTTPMiddleware`); evento de error en streaming.
  - (9) Degradación ante caída de Redis (historial y encolado).
  - (10) Ingesta idempotente + `autoretry` (con `ConnectionError` built-in) + estado `failed`.
  - (8) Retry de inferencia no-stream con tenacity (2 intentos, `reraise=True`).

### Deudas / pendientes 🔜

- **Observabilidad incompleta:** falta `/metrics` (Prometheus), `/health` de readiness, y el middleware de latencia/status por request (hoy la latencia se lee a mano de los timestamps). Planeados, no implementados.
- **`retry_backoff` no surte efecto** en la ingesta (`Retry in 0s` en Celery 5.6.3). Resiliencia cumple igual; afinar solo si hace falta.
- **`request_id: "-"` en la ingesta:** el upload no propaga el `request_id` al `.delay()` como sí hace `extract_memories`. Cerrar la simetría de correlación.
- **Falso positivo de dedup** (Paco ≈ Bruno, 0.079): recalibrar umbral o post-procesar hechos atómicos (deuda viva desde la Parte 11).
- **Mock de Ollama** para el happy-path de `/chat` (cobertura de tests del cableado, sobre todo `run_agent_stream`).
- **Cabos sueltos menores:** `path_separator = os` en `alembic.ini`; comentario 401/403 en `security.py`; drift de docs (`.env.example`/README con `x-api-key`); `SecurityWarning` de Celery corriendo como root (`--uid` para producción).
- **Recordatorio Alembic** (heredado): cada `--autogenerate` futuro intentará borrar el full-text; revisar el archivo generado.

---

## 8. Temario de estudio (Parte 15)

> Conceptos transferibles que esta parte enseña, agrupados por frente. Para repaso.

### A. Tests

- **`pytest` + `pytest-asyncio` con `asyncio_mode="auto"`**: por qué un solo event loop importa y cómo `httpx.AsyncClient` + `ASGITransport(app)` lo respeta (vs el riesgo de loops cruzados de `TestClient`).
- **BD de test real, migrada con Alembic** (no de juguete): qué se gana probando contra el esquema verdadero (FKs `ON DELETE CASCADE`, extensiones).
- **Aislamiento entre tests** (truncado) y por qué un test no debe contaminar al siguiente.
- **Probar ambas ramas de un endpoint** (la rama "existe" y la "no existe"): el bug del `NameError` solo vivía en la rama 404 no ejecutada en la 1ª prueba.
- **Semántica HTTP 401 vs 403**: 401 = sin credenciales, 403 = autenticado pero sin permiso. Los tests fijan el contrato real.
- **`render_as_string(hide_password=False)`**: SQLAlchemy 2.0 enmascara la contraseña en `str(url)`; cuándo eso importa.

### B. Observabilidad

- **Logging estructurado JSON**: por qué supera al `print`, cómo un `JsonFormatter` + `dictConfig` unifica el formato de tu código y de uvicorn.
- **`contextvars` para correlación**: `ContextVar` + un `logging.Filter` que inyecta `request_id`/`conversation_id` en cada `LogRecord`, sin pasarlos a mano.
- **Middleware que asigna/propaga un ID por petición** (`X-Request-ID`) y lo fija en el contextvar.
- **Propagación de contexto entre procesos**: por qué el contextvar NO cruza solo a Celery, y cómo se pasa explícito como argumento de la tarea + re-`set()` en el worker. (Eco del muro "otro proceso/otro loop" de la Parte 2.)
- **Niveles de log con intención**: INFO/WARNING (degradación)/ERROR/DEBUG, y por qué los reintentos y degradaciones se loguean.
- **`ensure_ascii=False`** para preservar acentos en el JSON de logs.

### C. Resiliencia

- **Timeout y `keep_alive` en un cliente LLM local**: diferencia entre el `keep_alive` por-request (cliente) y el `OLLAMA_KEEP_ALIVE` del servidor; el cold-load a VRAM y por qué el timeout debe superarlo pero ser finito.
- **`client_kwargs` / `async_client_kwargs`**: cómo se configura el cliente httpx subyacente cuando la clase no expone `timeout=` directo; importancia de la ruta async.
- **La misma excepción en clases distintas según el cliente**: `httpx.ConnectError` (chat) vs `ConnectionError` built-in (embeddings); y la jerarquía (`httpx.ConnectError` hereda de `ConnectionError`).
- **`BaseHTTPMiddleware` esquiva los exception handlers de FastAPI**: la limitación de starlette y el patrón "excepción de dominio propia (`UpstreamUnavailable`) traducida dentro del código" para sortearla.
- **Asimetría de manejo de errores stream vs no-stream**: por qué un stream con el `200` ya enviado solo puede emitir un evento de error y cerrar, no devolver 503.
- **Degradación con gracia (graceful degradation)**: cuándo degradar (memoria de corto plazo: seguir sin historial) vs cuándo no hay nada que degradar (broker: no tumbar la respuesta ya generada); capturar la clase base de excepción (`RedisError`).
- **Idempotencia en tareas y por qué precede al retry**: el patrón "DELETE-antes-de-INSERT en transacción"; el test correcto (re-ejecutar y comparar el conteo, no una foto).
- **`autoretry` de Celery**: `autoretry_for`, `max_retries`, `retry_backoff`, y el hook `on_failure` para estado terminal (`failed`); que `on_failure` corre síncrono.
- **tenacity**: `retry_if_exception_type`, `stop_after_attempt`, `wait_exponential`, `before_sleep_log`, y por qué **`reraise=True`** es indispensable para no romper la cadena de traducción a 503.
- **Tensión interactivo vs background en políticas de retry**: menos reintentos / backoff corto cuando hay un humano esperando; más en tareas de fondo.

### D. Metodología (transversal)

- **Diagnosticar con datos antes de teorizar**: `inspect.signature` para parámetros, el traceback completo para la clase de excepción, `list(app.exception_handlers)` para descartar hipótesis.
- **Reconocer un test inválido**: `ollama ps` sin modelo cargado, logs fósiles sin `--tail`, `python -c` que se salta el decorador de Celery, placeholders copiados literales. El timestamp como testigo de "fresco vs fósil".
- **Verificar por capas**: aislar transporte de contenido, una pieza verificada antes de la siguiente.

---

*Cierre de la Parte 15.*
