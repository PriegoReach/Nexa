# NexaAgent — Bitácora de desarrollo (Parte 20)

> Inicio de la **Fase 2 (Automatización)**: el salto de "saber" y "recordar" a **actuar**.
> Objetivo: construir la **primera herramienta de escritura** del agente —`create_task`,
> que crea un registro en una tabla nueva— eligiendo a propósito el caso de menor riesgo
> (escritura interna reversible) para aprender el patrón completo de "actuar" antes de
> tocar lo irreversible. Y medir, en ese terreno seguro, el problema que define toda la
> fase: el choque entre el **retry de la Fase 1** (P15) y las **herramientas que escriben**.

**Estado al cierre de la Parte 20:** `create_task` funciona end-to-end (el agente decide llamarla, escribe la fila por `worker_session()`, el commit pega, se verifica con un `SELECT`). El trípode del objetivo original queda completo: el agente ya **sabe** (RAG), **recuerda** (memoria) y **actúa** (escribe). Se cerraron las decisiones de diseño de la fase (A + gradual + permisiva), y el **experimento del retry** confirmó —medido, no supuesto— que `_ainvoke_with_retry` (P15) re-ejecuta efectos de escritura: el acoplamiento no-idempotente que vuelve obligatoria la idempotencia-por-clave antes de la primera herramienta irreversible (B/C). Queda como deuda viva la fecha del 7B, que en la verificación salió confiadamente equivocada (ver §6).

---

## 1. El reencuadre de la fase: por qué la Fase 2 cambia las reglas

Hasta la Fase 1, lo peor que podía pasar ante un fallo era una respuesta incorrecta o un 503 — todo era lectura. A partir de la escritura, lo peor es que el agente haga algo **irreversible** en el mundo. Eso introduce tres problemas que no existían:

1. **Idempotencia (P15, anotada como deuda de la fase).** Reintentar `ainvoke` es seguro mientras todo es lectura. En cuanto una herramienta escribe, el retry de la P15 se vuelve peligroso: si "mandar correo" falla a mitad y reintenta, ¿mandó dos o ninguno? Cada herramienta de escritura necesita ser idempotente o quedar excluida del retry.
2. **Confirmación.** ¿El agente actúa solo o pide permiso? Decisión de producto, no técnica — define la arquitectura (un agente que confirma necesita un ida-y-vuelta que hoy no existe).
3. **Autorización real.** El JWT (P12) controla *quién habla* con el agente; falta controlar *qué puede hacer* el agente y con qué credenciales externas (deuda de los `change-me-in-env`, P12/P14).

### Las decisiones de alcance, cerradas

El alcance del primer paso lo condiciona todo. Tres opciones por perfil de riesgo:

| Opción | Qué es | Riesgo |
|---|---|---|
| **A** | Escritura **interna reversible** (la propia BD, sin credenciales externas) | Mínimo |
| B | Integración externa de lectura-escritura de bajo riesgo (webhook, servicio propio) | Medio (ya toca credenciales) |
| C | Integración empresarial completa (correo, calendario, CRM) | Máximo (irreversible + secretos + mundo real) |

**Decisión: A**, por la misma lógica de todo el proyecto — aprender el patrón completo (idempotencia + el cableado de una tool que escribe) en el caso más seguro, antes del riesgo externo. Saltar a C sería como haber empezado el RAG por el re-ranking en vez del retriever básico.

**Filosofía de la fase: autónomo o asistido → gradual.** Un agente autónomo ejecuta sin preguntar (potente, peligroso); uno asistido propone y espera confirmación (seguro, con fricción). Decisión registrada como **principio de toda la Fase 2**: autónomo en lo reversible, confirmación reservada para lo irreversible (B/C). La confirmación misma no se implementa aún —no hay acción irreversible todavía— pero la política queda fijada antes de escribir código, no después.

---

## 2. La primera herramienta: `create_task`

"Escritura interna reversible" es abstracto; la elección de *qué* construir define qué se aprende. Elegida: **gestión de tareas/notas internas** — una tabla `tasks` y una herramienta que crea un registro. Por qué esta:

- Es escritura **real** (ejercita todo el patrón) pero trivialmente **reversible** (un `DELETE` en la BD).
- **No necesita credenciales externas** (la deuda de secretos de la P12/P14 no bloquea).
- Caso de uso natural y **verificable**: "recuérdame revisar Rubí el viernes" → el agente crea la tarea → se confirma con un `SELECT`.
- **Completa el trípode**: el agente recupera (RAG) y recuerda (memoria); ahora actúa. La pieza que faltaba de *saber/recordar/actuar*.

**Alcance inicial: solo CREATE** (no update/delete aún). Una operación, bien hecha, antes de las demás — el mismo "una capa verificada antes de la siguiente" de todo el proyecto.

### Verificación de realidad: el patrón de tools actual

Fiel al método, antes de escribir se leyó cómo se declaran las 3 tools existentes, con un `grep` (sin asumir rutas de módulo):

```powershell
docker exec nexaagent-api-1 sh -c "grep -rnE 'get_tools|@tool|StructuredTool|worker_session|sessionmaker' /app/app --include=*.py"
```

Hallazgos que fijaron el molde:

- Las 3 tools (`knowledge_base`, `long_term_memory`, `web_request`) son funciones **síncronas** con el decorador `@tool` de `langchain_core`, ensambladas en `tools/__init__.py` (`get_tools() -> list[StructuredTool]`).
- **Todo lo que toca la BD pasa por `worker_session()`** (el helper NullPool de la P13), no por el `SessionLocal` global — por el problema de loops de la P2 (las tools corren en un `ThreadPoolExecutor` con loop propio).
- El molde exacto es `long_term_memory`: un `@tool` síncrono que puentea a async con `_run_async` (ThreadPoolExecutor + `asyncio.run`) y abre `worker_session()` dentro de *ese* loop.
- Dato operativo que decide la escritura: **`worker_session()` NO hace auto-commit** (lo dice su docstring). Así que `create_task` = el patrón de `long_term_memory` + INSERT + `await session.commit()`.

Conclusión: cero invención. `create_task` es la forma de `long_term_memory` montada sobre el commit de `memory_extraction.py`. Ambos patrones ya probados.

---

## 3. Las decisiones de diseño cerradas

| Decisión | Cierre | Motivo |
|---|---|---|
| Primera tool | `create_task` (solo CREATE) | Escritura real, reversible, sin credenciales externas |
| Tabla | `tasks` (id, content, due_date?, status, created_at) | Migración Alembic nueva, patrón 0001 |
| Ruta de escritura | `@tool` + `worker_session()` + commit | Mismo helper P13 que usan retriever/memoria |
| Registro | añadir a `get_tools()` | El patrón de tool-calling que ya existe |
| Política de ejecución | **Autónoma** (crear una tarea es reversible) | Decisión: autónomo en lo reversible |
| Idempotencia | **Permisiva** aquí | Reversible y de bajo riesgo; por-clave obligatoria en B/C |
| Retry | `create_task` ⚠️ acoplada al retry P15 | El choque Fase 1 ↔ Fase 2, ver §5 |

**Sobre la idempotencia permisiva (decisión consciente).** Para `create_task`, cada llamada crea una fila, duplicados incluidos. Es aceptable porque crear una tarea es reversible y de bajo riesgo, y un duplicado es molesto pero no peligroso (a veces hasta deseable). **PERO** la idempotencia **por clave** queda documentada como obligatoria para B/C: cuando lleguemos a "mandar correo", permisiva es inaceptable (dos correos), y ahí la clave de intención será forzosa. Se empieza con el cableado simple en terreno seguro, sabiendo que el patrón robusto entra cuando sube el riesgo.

---

## 4. La migración y la herramienta

### La tabla `tasks`

Migración Alembic a mano (patrón de la 0001), creada **en el host** (el código va dentro de la imagen vía `COPY`, P1 — un archivo generado dentro del contenedor se perdería al rebuild):

```python
def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
```

`due_date` como `DATE` (lo más simple correcto para "el viernes", sin líos de zona horaria; ampliable a `timestamptz` si algún día se quieren recordatorios con hora).

### `create_task.py`

Réplica de `long_term_memory` (puente `_run_async` idéntico) pero escribiendo:

```python
@tool
def create_task(content: str, due_date: Optional[str] = None) -> str:
    """Create a task or reminder in the user's task list.
    Use this when the user asks you to remember, note, or schedule something —
    e.g. "remind me to review the Ruby project on Friday".
    Args:
        content: What the task is about, in plain language.
        due_date: Optional due date in ISO format (YYYY-MM-DD). Omit if no date.
    """
    # ... parseo de fecha + _run_async(_insert_task(...)) ...
```

```python
async def _insert_task(content, due) -> int:
    async with worker_session() as session:
        result = await session.execute(
            text("INSERT INTO tasks (content, due_date, status) "
                 "VALUES (:content, :due_date, 'pending') RETURNING id"),
            {"content": content, "due_date": due})
        task_id = result.scalar_one()
        await session.commit()                       # worker_session NO auto-commitea
        logger.info("create_task executed", extra={"task_id": task_id})  # rastro del experimento
        return task_id
```

Dos detalles que no son cosméticos: (1) el **docstring es la descripción que el LLM ve** para decidir llamar la tool (así funciona `@tool`, igual que en `long_term_memory`); de su claridad depende que el agente *decida* usarla. (2) `logger.info("create_task executed")` es el rastro que hace **medible** el experimento del retry (§5).

El registro, una línea en `__init__.py`:

```python
def get_tools() -> list[StructuredTool]:
    return [search_knowledge_base, search_long_term_memory, http_get, create_task]
```

Y una línea nueva en el `SYSTEM_PROMPT` para reforzar el tool-calling flojo del 7B: *"Cuando el usuario te pida recordar, anotar o agendar algo, USA `create_task`."*

---

## 5. El experimento del retry — el corazón de la fase, medido

El cambio delicado donde la Fase 1 y la Fase 2 se tocan: `_ainvoke_with_retry` (P15) envuelve **toda** la llamada `agent.ainvoke(...)`. Como **no hay checkpointer de LangGraph** en el setup (el historial se carga de Redis y se pasa como mensajes en cada turno, sin estado persistido), un reintento **reinicia el grafo entero desde el input**.

**El modo de fallo concreto:** el LLM decide `create_task` → la tool hace el INSERT y commit → el LLM va a redactar → Ollama tiene un hipo → `ConnectionError` → tenacity reintenta → `ainvoke` corre otra vez desde cero → `create_task` ejecuta **de nuevo** → segunda fila. El usuario pidió una tarea, le quedan dos. Es exactamente el peligro anotado en la P15.

**El regalo de empezar por A:** en vez de dejar el acoplamiento en teoría, se **mide** en terreno seguro (donde un duplicado es un `DELETE` inofensivo). La verificación honesta —la misma familia que la "Nota sobre lo observable" de la P15 §4.5—:

- **Lo que el `stop ollama` manual SÍ muestra:** con Ollama totalmente caído, el intento 1 escribe la fila y falla en la redacción; el intento 2 muere en su **primera** llamada al modelo (antes de re-alcanzar `create_task`) → reintentos agotados → 503. Resultado: **una sola fila**. El retry no re-ejecuta la tool *en este modo de fallo*.
- **La rama peligrosa, razonada (no medible a mano):** un hipo que se **recupera dentro del backoff de ~0.5s** haría que el intento 2 sí llegue a `create_task` → segunda fila. Cronometrar una recuperación sub-segundo a mano no es limpio (es el mismo muro de la P15); la rama queda razonada y se mide determinísticamente en la P21.

**Veredicto de la P20:** el acoplamiento es **no-idempotente por diseño**. Seguro en el modo "Ollama caído del todo", peligroso en el modo "blip transitorio". Eso convierte "la idempotencia por clave es obligatoria en B/C" de afirmación a **hecho fundado**, y justifica el gate antes de la primera tool irreversible. La instrumentación usada (un `sleep` gated por env tras la escritura) se quitó al cerrar, dejando el código limpio.

---

## 6. La verificación, y el defecto que destapó

La prueba real: pedir una tarea y mirar el efecto en la BD.

```powershell
curl.exe -X POST http://localhost:8000/chat -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" -d '{\"message\": \"Recuerdame revisar el Proyecto Rubi el viernes\"}'
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "SELECT * FROM tasks ORDER BY id DESC LIMIT 5;"
```

**El hito (✓):** el agente decidió llamar `create_task`, la tool escribió, el commit pegó, fila en `tasks`. El `content` quedó limpio ("Revisar el Proyecto Rubi", sin el "recuérdame"). Ese `id=1` es el `ZF-2024-X9` de la Fase 2.

**El defecto (la fecha):** `due_date = 2023-10-06`. Mal por dos lados: es **2023**, no 2026; y ni siquiera es "el viernes" correcto. El 6 de octubre de 2023 *fue* viernes, así que el 7B agarró un viernes plausible de su distribución de entrenamiento y lo narró con total seguridad — no vacío, sino **confiadamente equivocado**, que es peor (un dato errado guardado en silencio).

**La cadena honesta:** la tool está **bien** (la respuesta del modelo y la fila coinciden — `create_task` guardó fielmente lo que el modelo decidió; el fallo es aguas arriba, en el razonamiento del LLM). La causa raíz: qwen2.5 no tiene noción de "hoy" — ancla su "ahora" a su corte de entrenamiento (~oct 2023) y a eso resuelve lo relativo. **Queda como deuda viva para la P21**, donde se arregla con la palanca correcta (darle la fecha de hoy al modelo).

---

## 7. Aprendizajes clave

1. **La Fase 2 cambia la naturaleza del peor caso.** En lectura, un fallo da una respuesta mala. En escritura, da un efecto irreversible. El diseño entero (idempotencia, confirmación, autorización) se reorganiza alrededor de eso — y conviene fijarlo *antes* de escribir código.

2. **Empezar por el caso de menor riesgo es la misma metodología de siempre.** Escritura interna reversible (A) ejercita todo el patrón de "actuar" donde el peor error es un `DELETE`. B y C son "el mismo patrón + credenciales externas". Aprender la mecánica en terreno seguro vale más que el realismo prematuro.

3. **`worker_session()` es la respuesta a "¿cómo escribe una tool?".** Todo acceso a BD ya pasaba por ese helper (P13) por el problema de loops (P2). Una tool de escritura es el patrón de `long_term_memory` + commit. Leer el código antes de escribir reveló el molde exacto, incluido que el helper **no** auto-commitea.

4. **El docstring de un `@tool` es la interfaz con el LLM.** No es documentación: es lo que el modelo lee para decidir si llamar la herramienta. Su claridad determina la conducta del agente, tanto como el código.

5. **Medir el acoplamiento en terreno seguro > razonarlo en abstracto.** El retry re-ejecuta efectos de escritura (no hay checkpointer; el reintento reinicia el grafo). En vez de creerlo, se midió donde el costo es un `DELETE`. "Confirmado, no supuesto", el método del proyecto.

6. **Un fallo de la herramienta vs un fallo del modelo son cosas distintas.** La fecha errada NO es un bug de `create_task` (que guardó fielmente lo decidido); es el razonamiento del 7B sin ancla temporal. Distinguir dónde está el fallo evita "arreglar" código sano (eco del "falso Error 7" de la P1).

7. **Un dato confiadamente equivocado es peor que un error visible.** La fecha 2023 se guardó en silencio, narrada con seguridad. Validar formato (`fromisoformat`) no es validar sentido. La lección apunta directo al diseño de la P21.

---

## 8. Comandos de referencia (nuevos de esta parte)

### Ver el patrón de tools actual (antes de añadir una nueva)

```powershell
docker exec nexaagent-api-1 sh -c "grep -rnE 'get_tools|@tool|StructuredTool|worker_session' /app/app --include=*.py"
```

### Aplicar la migración de una tabla nueva (sin deps nuevas → sin --no-cache, regla P15)

```powershell
docker compose up -d --build api
docker exec nexaagent-api-1 alembic upgrade head
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "\d tasks"
```

### La prueba de una tool de escritura (el efecto en la BD es la prueba)

```powershell
curl.exe -X POST http://localhost:8000/chat -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" -d '{\"message\": \"Recuerdame revisar el Proyecto Rubi el viernes\"}'
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "SELECT * FROM tasks ORDER BY id DESC LIMIT 5;"
```

### Limpiar una fila de prueba (la reversibilidad de A en acción)

```powershell
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "DELETE FROM tasks WHERE id = 1;"
```

---

## 9. Estado y siguientes pasos

### Funcionando ✅ (nuevo en la Parte 20)

- **`create_task` end-to-end:** el agente decide, la tool escribe por `worker_session()`, commit, fila verificada con `SELECT`. El trípode *saber/recordar/actuar* completo.
- **Tabla `tasks`** migrada (Alembic a mano, patrón 0001).
- **Decisiones de la Fase 2 cerradas:** A (escritura interna reversible) + gradual (autónomo en reversible, confirmación reservada para irreversible) + permisiva (con idempotencia-por-clave documentada como obligatoria en B/C).
- **Acoplamiento retry ↔ escritura medido:** `_ainvoke_with_retry` re-ejecuta efectos (no hay checkpointer). Seguro con Ollama caído del todo, peligroso en blip transitorio.

### Deudas / pendientes 🔜

- **La fecha del 7B (deuda viva → P21):** sin ancla temporal, resuelve fechas relativas contra su corte de entrenamiento (~2023), confiadamente equivocado. Palanca: inyectar la fecha de hoy.
- **Idempotencia-por-clave (gate de B/C → P21):** el experimento la volvió obligatoria antes de la primera tool irreversible.
- **Confirmación (asistido) sin implementar:** la política gradual está fijada; el ida-y-vuelta se construye cuando llegue lo irreversible (B/C).
- **Secretos `change-me-in-env`** (P12/P14): bloqueante en cuanto B/C toquen credenciales externas.
- **Tool-call intermitente del 7B** (~25%, P18): la verificación de una tool puede necesitar 2-3 intentos; variabilidad del modelo, no bug.
- **Heredadas:** `request_id: "-"` en la ingesta; `retry_backoff` de Celery; `path_separator` en `alembic.ini`; drift de docs; `SecurityWarning` de Celery como root.

### Lo que sigue

- **P21:** arreglar la fecha (fix de la P20) + implementar la idempotencia-por-clave en `create_task` (el gate de B/C, aprendido en terreno seguro).
- **B/C (futuro):** "este mismo patrón + idempotencia por clave + credenciales externas". El salto a lo irreversible, ya con el patrón seguro aprendido.

---

## 10. Temario de estudio (Parte 20)

> Conceptos transferibles que esta parte enseña. Para repaso.

### A. Diseño de agentes que actúan

- **El salto lectura → escritura cambia el modelo de riesgo**: de "respuesta incorrecta" a "efecto irreversible". Todo el diseño (idempotencia, confirmación, autorización) se reorganiza alrededor de la irreversibilidad.
- **Empezar por el caso reversible**: ejercitar el patrón completo de "actuar" donde el peor error es un `DELETE`, antes de añadir credenciales externas y efectos en el mundo real.
- **Autónomo vs asistido como gradación, no dicotomía**: autónomo en lo reversible (potente), confirmación en lo irreversible (seguro). Fijar la política antes de escribir código.
- **El trípode saber/recordar/actuar**: RAG (recuperar) + memoria (recordar) + herramientas de escritura (actuar) como las tres capacidades de un agente empresarial.

### B. Anatomía de una herramienta de escritura

- **El decorador `@tool` y el docstring como interfaz con el LLM**: la descripción no es documentación, es lo que el modelo lee para decidir si invocar la herramienta.
- **El puente sync→async (`_run_async`)**: una tool síncrona corre su corutina en un `ThreadPoolExecutor` con loop propio, donde el engine NullPool (`worker_session`) vive sin chocar con el loop de FastAPI (P2).
- **El commit explícito**: `worker_session()` no auto-commitea; una tool de escritura debe `await session.commit()`, a diferencia de una de lectura.
- **El efecto en la BD como prueba de la tool**: verificar con un `SELECT` que la fila existe es la señal de éxito, igual que la huella `ZF-2024-X9` lo fue para el RAG.

### C. El acoplamiento retry ↔ escritura

- **Por qué un retry de inferencia re-ejecuta tools**: sin checkpointer, el reintento reinicia el grafo desde el input, repitiendo cualquier tool ya ejecutada (incluida la escritura).
- **Idempotencia por herramienta**: cada tool de escritura debe ser idempotente o quedar excluida del retry; el peligro escala con la irreversibilidad del efecto.
- **Permisiva en lo reversible, por-clave en lo irreversible**: un duplicado de tarea es tolerable; un correo duplicado no. El patrón robusto se introduce cuando el riesgo lo justifica.
- **Medir el modo de fallo, no asumirlo**: "Ollama caído del todo" (intento 2 muere antes de la tool → 1 fila) ≠ "blip transitorio" (intento 2 re-ejecuta → 2 filas). Distinguir los modos exige instrumentación, no intuición.

### D. El LLM y el tiempo

- **Un modelo no sabe qué día es hoy**: ancla su "ahora" a su corte de entrenamiento. Resolver fechas relativas ("el viernes") sin darle la fecha actual produce errores plausibles.
- **Confiadamente equivocado > visiblemente roto**: una fecha plausible pero falsa, guardada en silencio, es más peligrosa que un error que se ve. El diseño debe sospechar de la confianza del modelo.
- **Validar formato ≠ validar sentido**: `fromisoformat` atrapa "2026-13-40" pero no "2023-10-06" (ISO válido, año equivocado). La validación de cordura es otra capa.

---

*Cierre de la Parte 20. Inicio de la Fase 2.*
