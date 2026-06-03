# NexaAgent — Bitácora de desarrollo (Parte 24)

> Primera pieza de la **Fase C** (integración empresarial): el **flujo de confirmación
> asistida**. La política "autónomo en lo reversible, confirmación en lo irreversible" se
> fijó en la P20 como principio de toda la Fase 2, pero **nunca se implementó** — porque hasta
> ahora nada era irreversible-de-verdad. Esta parte la construye sobre `delete_task` (destructivo
> pero de bajo daño: el terreno seguro para aprender la mecánica), y de paso responde con
> evidencia la pregunta abierta sobre subir el modelo a 14B.

**Estado al cierre de la Parte 24:** el flujo de confirmación funciona y es **seguro**. `delete_task` ya no borra directamente — propone el borrado, escribe una *intención pendiente* en Redis, y espera la respuesta del usuario en el siguiente turno. `run_agent` bifurca: si hay intención pendiente, el turno se resuelve con una **heurística determinística** (sí/no/ambiguo) **sin llamar al modelo**; lo ambiguo **nunca borra** y conserva la intención. Los cuatro casos verificados, incluido el borde peligroso (c). Hallazgo que cierra la pregunta del 14B con dato: el mecanismo de confirmación es sólido (no depende del modelo, por diseño), pero **el 7B falla al *disparar* `delete_task` desde lenguaje natural** — el cuello no es el flujo, es la decisión del modelo de qué tool llamar. Se diferió un cambio de modelo (la P23-diag mostró que un 14B Q4 no cabe en la 4070 junto al reranker); esta parte confirma el problema que lo motivaría, sin resolverlo aún.

---

## 1. La deuda de la P20 y por qué C es arquitectónicamente nuevo

Desde la P20, el proyecto carga una política sin código: *autónomo en lo reversible, confirmación en lo irreversible*. Se fijó como principio pero no se implementó, porque ninguna acción hasta la P23 era irreversible-de-verdad — `create_task` es reversible (un DELETE), `call_webhook` v1 es notificación de bajo daño. C trae lo irreversible real (mandar un correo, crear un evento ajeno), y ahí la confirmación deja de ser teoría.

**Por qué esto es un cambio de estructura, no una tool más.** Hoy `run_agent` es un solo turno: pregunta → el agente decide → respuesta. Un agente que confirma necesita **estado entre turnos**: propone ("¿borro la #5?") → termina el turno → espera → en el *siguiente* turno recuerda qué iba a hacer y lo ejecuta o cancela. Ese "recuerda qué iba a hacer" es nuevo — la primera vez en el proyecto que el agente debe recordar una **acción en suspenso**, no solo el historial de conversación. Es la razón de empezar C por aquí (el flujo) antes de OAuth: el cambio de control es la dificultad nueva, y se aprende mejor en terreno interno seguro.

**El vehículo: `delete_task`.** Destructivo (justifica confirmar de verdad, no de adorno) pero de bajo daño para el sistema (es una tarea de prueba, no un correo a un cliente). Tiene la *forma* de lo irreversible sin el *riesgo* — el mismo patrón "aprender la mecánica en terreno seguro" que arrancó la Fase 2 con `create_task` en vez de con correo, aplicado dentro de C. Las demás tools (`create_task`, `update_task`, `list_tasks`) siguen autónomas — solo el borrado confirma (la gradualidad de la P20).

---

## 2. La decisión de diseño: dónde vive la intención pendiente

El flujo necesita recordar la acción propuesta entre el turno que propone y el que confirma. Tres opciones, con el tradeoff que define la elección:

| Opción | Mecanismo | Problema |
|---|---|---|
| 1 — inferir del historial | el modelo re-lee "¿borro #5?"/"sí" y re-decide el tool-call | depende del 7B (que falla tool-calls); frágil donde no se quiere |
| **2 — intención estructurada en Redis** | clave aparte con `{"action","args"}`; ejecuta de datos | estado nuevo que gestionar, pero **robusto pese al 7B** |
| 3 — tabla en Postgres | como la 2 pero durable | sobra: una intención pendiente es efímera (TTL), no durable |

**Decisión: Opción 2.** Y la razón es la lección que atraviesa todo el proyecto: **el efecto debe ejecutarse desde datos estructurados, no desde la re-interpretación del modelo.** Es lo mismo que en la P21 (la fecha la resuelve el código, no el 7B) y en la P22-B (idempotencia lado-emisor, no "confía en que el modelo no repita"). Una confirmación que dependiera de que el 7B relea el contexto y re-emita el tool-call correcto (Opción 1) reintroduciría la fragilidad que el proyecto lleva 23 partes diseñando para evitar. La Opción 2 hace la confirmación **determinística**: el "sí" ejecuta el `{"action","args"}` guardado, palabra por palabra. (Esta elección resultó *crucial* — ver §5: el 7B sí falla un paso antes, y la Opción 2 es justo lo que protege el momento del borrado.)

**Por qué Redis y no Postgres:** una intención pendiente es efímera por naturaleza — si no confirmas en un rato, caduca. Redis con TTL hace eso bien; Postgres sería sobre-ingeniería, contra el patrón "Redis para lo efímero, Postgres para lo durable" ya establecido (P6).

---

## 3. La implementación

### 3.1 `app/agent/pending.py` — la intención pendiente

`set_pending`/`get_pending`/`clear_pending` sobre la clave `nexa:pending:<conv_id>` (aparte del historial `nexa:memory:<id>`), TTL 600s. La intención se guarda estructurada:

```json
{"action": "delete_task", "args": {"task_id": 5}, "description": "Revisar Rubí"}
```

Misma política de degradación que el resto de `memory.py`: si Redis cae, loguear warning y degradar con gracia — **fail-safe**: Redis caído → no hay pendiente → no se borra nada. (La degradación aquí es hacia el lado seguro: la ausencia de pending nunca causa un borrado, solo impide una confirmación en curso.)

### 3.2 `manage_tasks.py` — `delete_task` deja de borrar, pasa a proponer

El cambio central de la pieza. La lógica de borrado real se **extrae** del `@tool` a una función separada:

- **`perform_delete(task_id) -> str | None`** — el `DELETE ... RETURNING content` + commit de siempre. **NO es un `@tool`** — la llama *solo* la rama de confirmación del orquestador, nunca el modelo.
- **`@tool delete_task`** — ya no borra. Verifica que la tarea existe (un SELECT del content), escribe la intención con `set_pending`, y **devuelve la pregunta**: "¿Seguro que quieres borrar la tarea #5 'Revisar Rubí'? Responde sí para confirmar o no para cancelar." Si la tarea no existe, no escribe intención y devuelve el no-op benigno de siempre ("No existe la tarea #N").

La propiedad clave: **el efecto destructivo no ocurre cuando el modelo llama la tool.** La tool propone; el borrado vive en un solo sitio nuevo (la rama de confirmación), invocable únicamente ante pending + sí explícito.

### 3.3 `orchestrator.py` — la bifurcación

En `run_agent` (y simétricamente en `run_agent_stream`), justo tras `load_history` y antes de llamar al agente:

```
get_pending(conv_id)?
├── HAY pendiente → este turno es respuesta a la propuesta. NO se llama al modelo.
│   Heurística determinística sobre user_input (normalizado: lower/strip/sin acentos):
│     · afirmativo (sí, si, confirmo, dale, ok, hazlo, adelante…)
│         → perform_delete(args["task_id"]) + clear_pending + "Tarea #N eliminada"
│           (si perform_delete devuelve None: "ya no existía")
│     · negativo (no, mejor no, cancela, déjalo, olvídalo…)
│         → clear_pending + "Cancelado, no borré nada"
│     · AMBIGUO (cualquier otra cosa)
│         → NO ejecutar, NO borrar la intención, "No entendí. ¿Confirmas? sí o no"
│   [REGLA DE SEGURIDAD: lo ambiguo SIEMPRE falla hacia no-ejecutar]
│   [el intercambio se persiste en Redis/Postgres igual que un turno normal]
└── NO hay pendiente → flujo normal, sin cambios. El agente decide.
```

La heurística es deliberadamente **simple y conservadora** — listas de afirmativo/negativo en español, generosas pero no temerarias, y todo lo demás cae en *ambiguo → no ejecutar*. El sí/no se interpretó con código, no con el modelo, por la misma razón que la Opción 2: una confirmación malinterpretada es peligrosa, y una heurística determinística que trata lo dudoso como "no entendí" falla hacia el lado seguro.

### 3.4 El bug durante la implementación: el cross-loop, otra vez

En el espíritu de las notas honestas: el primer test dio **500 — `RuntimeError: Future attached to a different loop`**. El cliente Redis singleton se ataba al loop principal, pero `delete_task` escribe el pending desde el loop de un `ThreadPoolExecutor` (el puente `_run_async` de las tools). **Es el mismo cross-loop de la P2** que `worker_db.py` ya resolvía — ahora aplicado a Redis en vez de a Postgres. Cura: cliente Redis **efímero por llamada** (mismo patrón NullPool que `worker_session`). Tras eso, todo verde.

Vale notar lo que esto dice del proyecto acumulando: el `Future attached to a different loop` costó una sesión entera en la P2; aquí se reconoció y curó en minutos, porque el patrón ya estaba aprendido. El mismo error, tres niveles de experiencia después, es trivial.

---

## 4. Verificación: los cuatro casos y el borde peligroso

| Caso | Acción | Resultado | Fila (antes→después) |
|---|---|---|---|
| **(a) Confirmar** | pending {#11} + "sí" | "Tarea #11 eliminada", pending limpio, **0 llamadas al modelo** | borrada ✓ |
| **(b) Cancelar** | pending {#12} + "no" | "Cancelado, no borré nada" | sigue ✓ |
| **(c) Ambiguo ⚠️** | pending {#13} + "mmm no sé" / "¿qué tareas tengo?" | ambos → "No entendí. ¿Confirmas? sí o no", pending **sobrevive** | sigue ✓ |
| **(c) cierre** | luego "sí" | "Tarea #13 eliminada" (el pending sobrevivió a lo ambiguo) | borrada ✓ |
| **(d) Sin pendiente** | conv nueva + "sí" | mensaje normal, 1 llamada al modelo (no interceptado) | nada borrado |

**El borde peligroso (c) pasa limpio: lo ambiguo NUNCA borra y conserva la intención.** Este es el test que más importaba — es el equivalente en C del "confiadamente equivocado" de la P20. Una confirmación malinterpretada (un "mejor no" leído como "sí") sería el peor bug de la fase: borrar lo que no se debía, irreversiblemente. La regla "ambiguo → no-ejecutar" lo cierra, y la verificación lo confirma sembrando un pending y respondiendo cosas ambiguas: la fila sobrevive, el pending también, y un "sí" posterior sí ejecuta (la intención no se perdió por lo ambiguo). El caso (a) con **0 llamadas al modelo** prueba que la rama de confirmación es puramente determinística — el modelo no participa en el momento del borrado.

> **Nota de método:** el flujo se verificó **sembrando el pending de forma determinista**, desacoplado del modelo. La razón es el Hallazgo 1 (§5): el 7B no dispara `delete_task` de forma fiable desde NL, así que probar el flujo *a través* del modelo mezclaría dos cosas (¿falló el flujo o el tool-calling?). Sembrar el pending aísla el flujo — la misma "medición de una sola variable" de la P21.

---

## 5. Los dos hallazgos honestos

### 5.1 El 7B falla al *disparar* `delete_task` — y eso cierra la pregunta del 14B

El hallazgo que ata el hilo del modelo. El **mecanismo de confirmación es sólido**, pero el **7B falla al decidir llamar `delete_task` desde lenguaje natural**:

- "borra la tarea de café" → el modelo llamó **`update_task`** ("he reabierto…") en vez de `delete_task`.
- En otro turno propuso correctamente pero **fraseó mal la respuesta** ("Sí, he borrado #11" cuando solo había *propuesto*, no borrado).

**La seguridad NO se compromete por esto** — `perform_delete` solo lo invoca el orquestador ante pending + sí explícito; ningún desvío del modelo borró nada. El fraseo equivocado ("he borrado" cuando solo propuso) es un problema de *presentación*, no de efecto: la fila seguía ahí. La Opción 2 es exactamente lo que blinda esto — el borrado vive en código determinístico, no en lo que el 7B decida o diga.

Pero el hallazgo **confirma con evidencia concreta lo que la P23-diag dejó en teoría**: el problema del 7B es real y es "elegir la tool correcta desde NL diverso". El encadenamiento NL → `delete_task` → confirmación es frágil en el 7B. Esto es el dato que faltaba para la decisión del 14B:

- **A favor del 14B:** el cuello (decisión de tool desde NL) es real y medido, no anecdótico.
- **En contra (de la P23-diag):** un 14B Q4 **no cabe** en la 4070 junto al reranker (~9.0 GB libres vs ~10.5 GB) — exigiría mover el reranker a CPU (deshacer la P18) y/o bajar el contexto a 4096. Degradar capacidades construidas para arreglar esto.

**Queda planteado, no resuelto** — es una decisión de producto genuina (¿pagar el costo del 14B por una mejora en la decisión de tool?), y se revisitará en OAuth, donde el modelo elige tools externas y la fragilidad podría doler más. La pieza de confirmación, por diseño, no la necesita: funciona con el 7B porque no depende de él en el momento crítico.

### 5.2 El pending "captura" la conversación (UX, intencional)

Mientras hay un pending activo, **todo turno se interpreta como respuesta a la confirmación** — incluso "¿qué tareas tengo?" recibe "No entendí, ¿confirmas?". El pending captura la conversación hasta que se resuelve (sí/no) o caduca (TTL 600s). Es **intencional y seguro** (lo ambiguo no borra), pero significa que el usuario no puede hacer otra cosa hasta resolver o esperar.

Se anota como **deuda de UX, no se toca ahora**: permitir que un cambio claro de tema cancele la propuesta implícitamente sería más amable, pero introduce un juicio nuevo ("¿es cambio de tema o respuesta ambigua?") que reabre justo el riesgo que el diseño conservador cerró. "Feo pero seguro" gana a "elegante pero ambiguo" en algo que borra datos. Mejora futura, con cuidado.

---

## 6. Aprendizajes clave

1. **Confirmar es un cambio de estructura de control, no una tool más.** Requiere estado entre turnos (la acción en suspenso) y una bifurcación en `run_agent` (¿hay pendiente? → rama confirmación / rama normal). La primera vez que el agente recuerda una acción pendiente, no solo el historial.

2. **El efecto se ejecuta de datos estructurados, no de la re-interpretación del modelo.** La Opción 2 (intención `{"action","args"}` en Redis) hace la confirmación determinística: el "sí" ejecuta lo guardado, palabra por palabra. Es la misma lección de la fecha-en-código (P21) y la idempotencia-lado-emisor (P22-B), y resultó crucial: el 7B falla un paso antes, y esto blinda el momento del borrado.

3. **Lo ambiguo SIEMPRE falla hacia no-ejecutar.** En una acción irreversible, una confirmación malinterpretada es el peor bug posible. La heurística conservadora trata todo lo dudoso como "no entendí, ¿confirmas?" y nunca borra ante la duda. El borde (c) es el test que lo prueba.

4. **Interpretar el sí/no con heurística, no con el modelo.** Una heurística determinística es predecible y falla hacia el lado seguro; pasar la confirmación al modelo reintroduciría la fragilidad que el flujo busca evitar. Coherente con no depender del 7B en el momento crítico.

5. **La degradación de un mecanismo de seguridad debe ser fail-safe.** Redis caído → no hay pending → no se borra. La ausencia del estado nunca causa el efecto peligroso; solo impide una confirmación en curso. Degradar hacia el lado seguro, no hacia el efecto.

6. **El cross-loop reaparece en cada frontera async nueva.** `Future attached to a different loop` (P2) volvió porque el cliente Redis singleton se ataba al loop principal y la tool escribe desde el loop del ThreadPoolExecutor — igual que `worker_db` con Postgres. Cura idéntica (cliente efímero por llamada). El patrón aprendido convierte un bug de una-sesión en uno de minutos.

7. **Separar el efecto del `@tool` que lo dispara.** `perform_delete` (el borrado real) extraído del `@tool delete_task` (que ahora propone) deja el efecto irreversible en un solo sitio, invocable solo por la rama de confirmación — nunca por el modelo. La superficie de lo que puede borrar se reduce a un punto controlado.

8. **Un mecanismo seguro puede tener un eslabón frágil un paso antes.** El flujo de confirmación es sólido, pero el 7B falla al *iniciarlo* (elegir `delete_task` desde NL). Distinguir "el mecanismo" de "lo que lo dispara" evita atribuir al diseño un fallo del modelo — y aísla dónde un modelo mejor ayudaría (la decisión de tool) y dónde no (el flujo, que ya es robusto).

---

## 7. Comandos de referencia (nuevos de esta parte)

### Inspeccionar / limpiar una intención pendiente en Redis

```powershell
docker exec nexaagent-redis-1 redis-cli GET "nexa:pending:<conv_id>"
docker exec nexaagent-redis-1 redis-cli TTL "nexa:pending:<conv_id>"   # segundos restantes
docker exec nexaagent-redis-1 redis-cli DEL "nexa:pending:<conv_id>"   # forzar limpieza
```

### Verificar el flujo de confirmación (sembrando el pending de forma determinista)

```powershell
# 1. crear una tarea de prueba (vía /chat) y anotar su id con un SELECT
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "SELECT id, content FROM tasks ORDER BY id DESC LIMIT 3;"

# 2. proponer el borrado -> debe responder la PREGUNTA y NO borrar
curl.exe -X POST http://localhost:8000/chat -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" -d '{\"message\": \"borra la tarea #N\"}'
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "SELECT id FROM tasks WHERE id = N;"   # sigue ahí

# 3a. confirmar -> borra
curl.exe -X POST http://localhost:8000/chat -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" -d '{\"message\": \"sí\"}'
# 3b. o ambiguo -> NO borra (el borde crítico)
curl.exe -X POST http://localhost:8000/chat -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" -d '{\"message\": \"mmm no sé\"}'
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "SELECT id FROM tasks WHERE id = N;"
```

---

## 8. Estado y siguientes pasos

### Funcionando ✅ (nuevo en la Parte 24)

- **Flujo de confirmación asistida** sobre `delete_task`: propone → espera → ejecuta/cancela. Política gradual de la P20 por fin implementada (solo el borrado confirma; create/update/list autónomas).
- **Intención pendiente en Redis** (`pending.py`): estructurada, TTL 600s, degradación fail-safe.
- **`delete_task` ya no borra directamente**: `perform_delete` extraído, invocable solo por la rama de confirmación. El efecto irreversible en un punto controlado.
- **Heurística sí/no determinística** en `run_agent`/`run_agent_stream`: lo ambiguo nunca borra.
- **Cuatro casos verificados**, incluido el borde peligroso (c): ambiguo → no-ejecutar, confirmado.
- **Bug cross-loop de Redis** cazado y curado (cliente efímero, patrón P2/worker_db).

### Deudas / pendientes 🔜

- **Fiabilidad del 7B para iniciar el borrado por NL (el cuello real):** el modelo falla al elegir `delete_task` desde lenguaje natural diverso. La seguridad no se compromete (la Opción 2 lo blinda), pero el encadenamiento NL→tool es frágil. **Reabre la pregunta del 14B** — diferida porque no cabe en la 4070 junto al reranker (P23-diag); se revisitará en OAuth.
- **UX: el pending captura la conversación** hasta resolver/caducar. Intencional y seguro; permitir cancelación por cambio-de-tema es mejora futura (con cuidado: reabre el juicio ambiguo).
- **`secrets/` al `.gitignore`** ya está puesto; el versionado del proyecto sigue diferido (la bifurcación de raíz `nexaagent/` vs `x:\Nexa` para incluir `Docs/`).
- **Heredadas:** URL de webhook de producción (P22); `request_id: "-"` en la ingesta; `retry_backoff` de Celery; `path_separator` en `alembic.ini`; drift de docs; `SecurityWarning` de Celery como root.

### Lo que sigue

- **C — OAuth + proveedor real (correo/calendario):** la pieza final de la Fase 2. El flujo de confirmación ya está; falta la credencial OAuth (tokens que caducan, refresh — otra clase que los secretos estáticos de la P22/P23) y conectar un proveedor. **Aquí se revisita el 14B**, porque el modelo elegirá tools externas y la fragilidad de "qué tool desde NL" podría doler más que con tareas internas.
- **Fase 3 — Interfaz:** frontend sobre la API. Las señales de herramienta del streaming siguen sin UI, y ahora el flujo de confirmación añade un patrón nuevo (propuesta/respuesta) que una UI mostraría bien. Mejor después de que el contrato de la API se estabilice tras C.

---

## 9. Temario de estudio (Parte 24)

> Conceptos transferibles que esta parte enseña. Para repaso.

### A. Flujos de confirmación en agentes

- **Estado entre turnos (la acción en suspenso)**: un agente que confirma necesita recordar la acción propuesta entre el turno que propone y el que ejecuta — distinto del historial de conversación. La bifurcación ¿hay-pendiente? cambia la estructura de control.
- **Ejecutar de datos estructurados, no de re-interpretación**: guardar `{"action","args"}` y ejecutarlo determinísticamente, en vez de que el modelo relea el contexto y re-decida. Robustez frente a un modelo poco fiable.
- **Separar el efecto del disparador**: extraer la operación irreversible (`perform_delete`) del `@tool` que la propone, de modo que solo un punto controlado (la rama de confirmación) pueda ejecutarla — nunca el modelo directamente.
- **Estado efímero con TTL**: una intención pendiente caduca sola; Redis con TTL encaja, Postgres sería sobre-ingeniería. El patrón "efímero→Redis, durable→Postgres".

### B. Seguridad en acciones irreversibles

- **Fail-towards-safe ante la ambigüedad**: en algo que borra datos, lo dudoso nunca ejecuta — se re-pregunta. Una confirmación malinterpretada es el peor caso; el diseño sesga hacia no-actuar.
- **Heurística determinística sobre interpretación-por-modelo**: para sí/no, una heurística predecible que falla hacia el lado seguro supera a pasar la decisión a un modelo que puede confundirla.
- **Degradación fail-safe**: si el mecanismo de estado (Redis) cae, la ausencia de estado nunca causa el efecto peligroso, solo impide una confirmación en curso.
- **Reducir la superficie del efecto a un punto**: el borrado real invocable desde un solo sitio, no desde cualquier ruta que el modelo pueda tomar.

### C. Diagnóstico de fallos del modelo vs del sistema

- **Distinguir el mecanismo del eslabón que lo dispara**: un flujo puede ser sólido y aun así fallar porque el modelo no lo *inicia* bien (elegir la tool desde NL). No atribuir al diseño un fallo del modelo.
- **Verificar aislando la variable**: sembrar el estado (el pending) de forma determinista, desacoplado del modelo, para probar el flujo sin que el tool-calling frágil contamine la medición.
- **Fallo de efecto vs fallo de presentación**: el 7B fraseó "he borrado" cuando solo propuso — un problema de presentación, no de efecto (la fila seguía). Separar lo que el sistema *hace* de lo que el modelo *dice*.
- **Una decisión de infraestructura se cierra con datos de ambos lados**: el 14B se justifica (el cuello es real) y se difiere (no cabe sin degradar el reranker) — la decisión pesa evidencia medida, no intuición.

---

*Cierre de la Parte 24. Primera pieza de la Fase C; falta OAuth + proveedor real.*
