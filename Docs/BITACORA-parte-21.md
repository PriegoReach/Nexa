# NexaAgent — Bitácora de desarrollo (Parte 21)

> Continuación de la Parte 20. Dos objetivos encadenados: **(1)** arreglar la fecha que el
> 7B resolvía confiadamente mal (la deuda viva de la P20), y **(2)** implementar la
> **idempotencia-por-clave** en `create_task` —el gate de B/C— aprendiéndola en el terreno
> seguro de A. La sesión cierra el arco que la P20 abrió: el experimento del retry mostró el
> **peligro medido**; esta parte muestra la **cura medida**, en el mismo terreno.

**Estado al cierre de la Parte 21:** la fecha quedó correcta vía un **resolver determinístico en Python puro** (cero dependencias), tras descartar `dateparser` por medición. `create_task` es ahora **idempotente por clave** (columna `idempotency_key` + índice único + `ON CONFLICT DO NOTHING`), verificado con una **inyección de blip transitorio determinística** que reprodujo el escenario para el que existe el retry (imposible a mano) y confirmó la cura: el replay computa la misma clave → una sola fila. El sistema queda limpio (instrumentación quitada, boot 200). B/C queda reducido a "este patrón exacto + credenciales externas".

---

## 1. El defecto heredado de la P20 y la palanca

La P20 cerró con la fecha mal: "el viernes" → `2023-10-06`. Año equivocado (2023, no 2026) y ni siquiera el viernes correcto — el 7B agarró un viernes plausible de su corte de entrenamiento. **La palanca correcta: darle la fecha de hoy al modelo.** Con un matiz de arquitectura que importa:

- El `SYSTEM_PROMPT` se **hornea una vez** en el singleton del agente (P8). Una fecha ahí **envejecería** en el primer rebuild.
- La fecha de hoy tiene que llegar **por turno**, donde se arman los `messages`, no en el prompt estático.

### Fix A: inyección de la fecha por turno (el primer intento)

Se inyectó un contexto de fecha dinámico (`date.today()` con día de semana en español) **dentro del turno humano**, no como segundo `SystemMessage`. La razón es metodológica, no estética: `create_agent` antepone su propio system_prompt en cada llamada; un `SystemMessage` extra dejaría al modelo viendo **dos** system messages, y no era verificable desde fuera cómo el stack los compone. Si la fecha saliera mal con ese diseño, habría **dos** sospechosos (composición de los dos system messages vs aritmética del 7B). Inyectándola en el turno humano —la posición que todo chat model lee con certeza— se elimina al primer sospechoso: **medición de una sola variable.**

**Resultado de A:** la respuesta del modelo pasó a "viernes **7 de junio de 2026**". El año se arregló (2023→2026) → la inyección **funciona**, el modelo ya razona desde el calendario correcto. **Pero la fecha siguió mal**: el 7 de junio de 2026 es **domingo**, no viernes (desde el sábado 30 de mayo, el viernes es el **5 de junio**). Con la fecha de hoy delante, el 7B no solo erró el cálculo — etiquetó mal el día de la semana.

**El diseño de una sola variable pagó:** contexto presente y correcto, fallo aislado limpiamente en la **aritmética de calendario del modelo**. Ese es el gatillo de B.

---

## 2. `dateparser` descartado por medición, no por intuición

La opción natural para B era `dateparser`. En vez de asumir que resuelve el español relativo, se **midió** contra una base fija (sábado 2026-05-30, como "hoy"):

```
'el viernes'         -> None          ✗
'este viernes'       -> None          ✗
'viernes'            -> 2026-06-05     ✓
'el martes'          -> None          ✗
'el 15 de junio'     -> None          ✗  (¡una fecha concreta!)
'el lunes que viene' -> None          ✗
```

**Veredicto:** las formas con artículo ("el viernes", "el martes") devuelven `None`, y hasta "el 15 de junio" —una fecha explícita— falla. Solo las formas desnudas funcionan. Como dependencia pesada que además había que parchear (`--break-system-packages`), no es la opción correcta para este proyecto. La medición evitó adoptar una dependencia que habría fallado justo en el caso de uso ("recuérdame el viernes").

Pero el resultado apuntó a algo mejor: el 7B **ya identifica bien** que es "viernes" (lo dijo en su respuesta) — lo único que no sabe es que ese viernes es el 5 de junio. **El fallo es aritmética pura, no extracción.** Así que solo la aritmética se mueve al código.

---

## 3. Fix B: el resolver determinístico en Python puro

Un resolver de stdlib (`re`, `unicodedata`, `datetime` — **cero dependencias**, así que sin `--no-cache`, regla P15). Medido contra la misma base, clava **todo** lo que `dateparser` no pudo:

```
HOY: sábado 2026-05-30
'el viernes'         -> 2026-06-05  (viernes)   ✓
'este viernes'       -> 2026-06-05  (viernes)   ✓
'el martes'          -> 2026-06-02  (martes)    ✓
'pasado mañana'      -> 2026-06-01  (lunes)     ✓
'el 15 de junio'     -> 2026-06-15  (lunes)     ✓
'el lunes que viene' -> 2026-06-01  (lunes)     ✓
'2026-12-25'         -> 2026-12-25  (viernes)   ✓
```

### El cambio de contrato que hace que B funcione

B solo arregla si el modelo pasa la **frase** ("el viernes"), no una fecha calculada. Tres partes que van juntas:

1. **El resolver en el código** (la aritmética determinística).
2. **Quitar la inyección de A** — sin la fecha de hoy delante, el modelo no tiene base para computar y se ve forzado a entregar la frase a la tool. (El andamiaje de A se revierte: era un experimento que no alcanzó.)
3. **Instruir al modelo** —en el `SYSTEM_PROMPT` y en el docstring— a pasar la fecha *tal como la dijo el usuario*, sin convertirla ni calcular el día.

Más una **red de seguridad** contra el bug original de la P20: **rechazar fechas en el pasado**. Un ISO de 2023 venido del calendario viejo del modelo cae aquí en vez de guardarse en silencio — es justo lo que habría cazado el `2023-10-06`.

```python
def _resolve_due(phrase: str, today: date) -> Optional[date]:
    """Resuelve una fecha en lenguaje natural (es) o ISO a un date real.
    Determinístico: la aritmética la hace el código, no el LLM (el 7B falla).
    Devuelve None si no se entiende O si la fecha ya pasó (calendario viejo).
    """
    # ... ISO directo (rechaza pasado) ...
    # ... hoy / mañana / pasado mañana ...
    for name, idx in _WEEKDAY_IDX.items():            # día de la semana
        if re.search(rf"\b{name}\b", s):
            ahead = (idx - today.weekday()) % 7
            return today + timedelta(days=ahead or 7)  # 'el viernes' nunca es hoy
    # ... 'D de Mes' (este año o el siguiente si ya pasó) ...
```

La tool además **formatea** la respuesta con el día de semana correcto desde el `date` resuelto, así que la frase "viernes 5 de junio" que el usuario ve sale del **código**, no de la cabeza del modelo — el 7B ya no puede equivocar el día ni en la respuesta.

**Resultado de B (✓):** `due_date = 2026-06-05`, respuesta "viernes 5 de junio de 2026". Probado también con "pasado mañana" → `2026-06-01` (lunes), correcto. Y el `due_date` que loguea `create_task executed` confirma qué le pasó el modelo a la tool (la frase, no un ISO calculado) — el contrato funcionó.

---

## 4. La idempotencia-por-clave: qué es "la misma intención"

Con la fecha resuelta, el gate de B/C: hacer `create_task` idempotente. Una clave de idempotencia solo sirve si dos llamadas que *deberían* ser la misma producen la *misma* clave — y eso choca con el contrato de B.

**La sutileza:** el LLM ahora pasa la **frase cruda** ("el viernes"), y el código la resuelve. Si la clave se derivara de la frase, "el viernes" y "este viernes" —misma intención, fecha idéntica tras resolver— darían claves distintas y duplicarían. Así que la clave se deriva de los **valores ya normalizados**: `content` + el `due_date` **resuelto** (el `date`, no el texto):

```python
def _idempotency_key(content: str, due: Optional[date]) -> str:
    basis = f"{content.strip().lower()}|{due.isoformat() if due else ''}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()
```

### El alcance honesto de esta clave

Lo que **sí** deduplica: un **replay del mismo turno** (el caso del experimento del retry — idéntico `content`, idéntico `due` resuelto, dentro de la misma ejecución). Es **a prueba de replay**, que es exactamente lo que el retry no-idempotente de la P15 amenaza.

Lo que **NO** pretende: deduplicar "el usuario pidió lo mismo el lunes y otra vez el jueves" — son dos intenciones legítimamente distintas en el tiempo, y meter una ventana temporal para distinguirlas es la complejidad que NO se quiere en A. Y un caso de borde explícito: dos tareas **sin fecha** con texto idéntico colapsan a una (`content|""`) — es el trade-off intencional de la protección-por-replay, no un bug.

Se dice claro porque "idempotencia" suena a más garantía de la que esto da. El valor real es enseñar el patrón `clave → constraint → ON CONFLICT` en terreno seguro, no resolver la deduplicación semántica universal.

### El mecanismo: la BD como árbitro (lección de la P15)

Sobre el hallazgo de la P15 §4.4 ("idempotencia PRIMERO, vía constraint de BD, no vía lógica de app"): la BD arbitra, no un `SELECT`-antes-de-`INSERT` (que tiene su propia carrera).

- **Migración a mano** (¡trampa de la P6!): `tasks` se creó a mano, y `document_chunks` tiene `content_tsv` + índice GIN que **no viven en los modelos**. Un `--autogenerate` los creería sobrantes y emitiría `DROP` espurios, destruyendo la búsqueda híbrida (Hallazgo 1 de la P6). Migración a mano, como la 0001/0002.
- **`idempotency_key` nullable** + índice único. En Postgres un índice único **no cuenta los NULL como colisión**, así las filas existentes (sin clave) conviven con el constraint sin rellenarlas.
- **`INSERT ... ON CONFLICT (idempotency_key) DO NOTHING RETURNING id`**: si la clave existe, no inserta ni devuelve fila → la tool detecta el no-op (recupera el id existente con un `SELECT`) y responde "ya tenías esa tarea".

---

## 5. La falacia de la "Prueba 1" y los dos niveles de deduplicación

Primer intento de verificación: pedir la misma tarea dos veces. El resultado **engañó** — y el engaño es instructivo.

**Lo observado:** dos peticiones de "Rubí viernes", y la tabla con **tres** filas (ids 3, 6, 7), no una. La idempotencia parecía no funcionar.

**El diagnóstico, confirmado por `SELECT`:** la fila 7 tenía un hash real; las filas 6 y 3 tenían `idempotency_key = NULL` (creadas **antes** de la migración). NULL nunca colisiona con NULL → el `ON CONFLICT` no tenía contra qué chocar. El constraint estaba bien; las filas viejas eran **invisibles** para él. Borradas (`DELETE 3`), slate limpio.

**Pero el hallazgo de fondo es otro, y mayor:** la segunda respuesta del modelo fue *"**Tienes** una tarea programada... ¿necesitas cambiar algo?"* — un agente que **decidió no crear** y te informó de algo que ya existe. El modelo **no llamó a `create_task` la segunda vez**: consultó memoria, vio que la tarea existía, y respondió sin actuar (las dos peticiones fueron en conversaciones distintas, 55 y 56). **Eso es buen comportamiento del agente** — pero significa que la Prueba 1 **nunca ejercita el `ON CONFLICT`**.

**La distinción que el experimento destapó — dos capas de deduplicación, independientes:**

| Capa | Quién deduplica | Qué protege |
|---|---|---|
| **Semántica** | El agente razona "ya existe, no creo" | Repeticiones del usuario que el modelo nota |
| **A prueba de replay** | La BD rechaza la clave repetida | El replay mecánico del retry (P15) |

La clave que construimos es la **segunda**. La Prueba 1 confundió las dos: intentó ejercitar la clave por la vía del usuario-repite, pero esa vía la **intercepta el agente** antes de llegar a la BD. Es la misma trampa de "el experimento no prueba lo que dice probar", una capa más abajo.

**Conclusión:** la única verificación que ejercita la clave es el **replay del retry** — la misma invocación reintentada, donde el modelo *no* tiene un turno nuevo para razonar "ya existe".

---

## 6. La cura medida: la inyección determinística del blip

### El muro del `stop ollama` manual (P15 §4.5, otra vez)

La receta intuitiva —parar Ollama tras la escritura— **no ejercita el `ON CONFLICT`**, por la razón que la P15 ya dejó escrita: con Ollama caído, el intento 2 muere en su primera llamada al modelo, **antes** de re-alcanzar `create_task`. Para que el reintento llegue a la tool, Ollama tiene que estar **arriba durante el intento 2** — lo que exige recuperación dentro del backoff de ~0.5s. A mano es imposible (arrancar Ollama + recargar el modelo son segundos). En el flujo manual el request muere en 503 y nunca hay segunda ejecución. Es el mismo muro de la P20 §5.

### Nota honesta sobre lo observable (gemela de la P15 §4.5)

El "hipo que se recupera" que la P15 dijo que no se puede hacer a mano **sí se puede hacer determinísticamente**: un fallo de una sola vez inyectado en `_ainvoke_with_retry`, gated por env, que falla la 1ª invocación **después** de que ya corrió (y escribió) y deja pasar el reintento con Ollama sano todo el tiempo:

```python
_FAIL_FIRST = os.getenv("FAIL_FIRST_INVOKE") == "1"
_invoke_attempts = {"n": 0}

async def _ainvoke_with_retry(agent, messages):
    result = await agent.ainvoke({"messages": messages})   # corre create_task -> escribe fila
    if _FAIL_FIRST and _invoke_attempts["n"] == 0:
        _invoke_attempts["n"] += 1
        raise ConnectionError("P21 simulated transient blip")  # en el tuple del @retry -> reintenta
    return result
```

- **Intento 1:** el agente llama `create_task` (escribe) → se lanza el `ConnectionError` → tenacity reintenta.
- **Intento 2** (Ollama nunca cayó): `create_task` corre otra vez → `ON CONFLICT DO NOTHING` → **una sola fila**.

Reproduce fielmente el escenario para el que existe el retry, **sin coordinación de tiempos**, sin sleep, sin parar Ollama. Y no toca la política de retry — solo una guarda env temporal.

**La salvedad crítica del contador (estado de módulo, no por-request):** `_invoke_attempts` vive en el proceso, no se resetea entre requests. Eso es lo correcto para que el reintento *dentro de un request* no vuelva a fallar — **pero el experimento solo es válido en el primer request tras recrear el contenedor** (contador en 0). Una segunda corrida sin rebuild vería `n==1`, no inyectaría el blip, y daría una sola fila **por la razón equivocada** (nunca hubo reintento) — un falso "cura confirmada" idéntico en la tabla al verdadero. **La distinción es invisible en el conteo de filas; solo el log la revela.** Regla operativa: **una corrida por rebuild, y se valida por el WARNING del blip en el log, no por el conteo.**

### Las tres señales de la cura (la verificación buena)

Una corrida, tras recrear el contenedor, con `FAIL_FIRST_INVOKE=1`. El log mostró las tres señales juntas:

```
create_task executed  (task_id=9, idempotency_key=4f8a…)
WARNING  P21: blip transitorio inyectado tras la 1a invocacion
Retrying _ainvoke_with_retry in 0.5 seconds ...
create_task duplicate ignored  (task_id=9, idempotency_key=4f8a…)   ← misma clave
```

→ `executed` → `WARNING blip` → `Retrying 0.5s` → `duplicate ignored`, **misma `idempotency_key`**, **una sola fila** (id 9). Validado por el WARNING en el log, no por el conteo (la regla, respetada).

**El arco cerrado:** la P20 mostró el peligro medido ("el retry re-ejecuta efectos"); la P21 muestra la cura medida ("la clave lo vuelve no-op"). **Mismo terreno seguro, confirmado-no-supuesto.** Sin clave, ese mismo flujo daría 2 filas — el peligro de la P20.

La instrumentación (guarda + `import os` + env) se quitó al cerrar; un `grep` confirmó cero rastros de P21 en el árbol; el api se recreó limpio y dio **health 200**.

---

## 7. Aprendizajes clave

1. **Medir una dependencia antes de adoptarla.** `dateparser` parecía la opción obvia; medirla contra el caso de uso real reveló que falla justo en "el viernes" y hasta en "el 15 de junio". La medición evitó una dependencia pesada que no servía — y reveló que el problema era aritmética, no extracción.

2. **Mover al código solo lo que el modelo hace mal.** El 7B identifica bien "viernes" pero no sabe qué fecha es. La aritmética (determinística) va al código; la extracción (lo que el modelo sí hace) se le deja. Dividir el trabajo por dónde está la flaqueza, no mover todo.

3. **Un diseño de una sola variable aísla la causa.** Inyectar la fecha en el turno humano (no como segundo system message) dejó un solo sospechoso. Cuando la fecha siguió mal, fue limpiamente la aritmética del 7B → gatillo de B sin ambigüedad.

4. **La clave de idempotencia se deriva de valores normalizados, no de la entrada cruda.** "El viernes" y "este viernes" deben colapsar; eso exige hashear el `due` *resuelto*, no la frase. La normalización es lo que hace que la clave signifique "misma intención".

5. **La BD es el árbitro de la idempotencia, no la lógica de app.** `ON CONFLICT` sobre un índice único no tiene carrera; un `SELECT`-antes-de-`INSERT` sí. Lección directa de la P15 §4.4.

6. **NULL no colisiona con NULL — bendición y trampa.** Permite que filas viejas convivan con un constraint nuevo (bendición), pero las vuelve invisibles a la deduplicación (trampa que disfrazó la "Prueba 1"). Conocer la semántica de NULL en índices únicos es lo que explicó el no-disparo.

7. **Hay dos capas de deduplicación, y un experimento puede confundirlas.** La semántica (el agente razona) intercepta al usuario-repite antes de la BD; solo el replay mecánico ejercita la clave. Diseñar la verificación exige saber *cuál* capa se está probando.

8. **El experimento, como se diseña, puede no probar lo que dice probar — dos veces.** La Prueba 1 (usuario-repite) la interceptó el agente; el `stop ollama` manual lo frenó el muro del backoff. La inyección determinística fue la única que tocó el `ON CONFLICT`. Eco de la P19 y la P20.

9. **Validar por la señal correcta, no por el resultado aparente.** Con el contador de módulo, una fila puede significar "cura" o "el experimento no corrió". Solo el WARNING del log distingue. El conteo de filas era un falso testigo.

10. **Reconstruir entre corridas > añadir lógica de reset.** El contador de módulo es correcto para una corrida; en vez de complicarlo con reset por-request, recrear el contenedor (estado en 0) es más simple y más limpio. Misma economía que el resto del proyecto.

---

## 8. Comandos de referencia (nuevos de esta parte)

### Medir un resolver de fechas contra una base fija (antes de adoptar dependencia)

```powershell
docker exec nexaagent-api-1 python -c "import dateparser; from datetime import datetime; base=datetime(2026,5,30); print(dateparser.parse('el viernes', languages=['es'], settings={'RELATIVE_BASE': base}))"
```

### Aplicar la migración de idempotencia (sin deps nuevas → sin --no-cache)

```powershell
docker compose up -d --build api
docker exec nexaagent-api-1 alembic upgrade head
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "\d tasks"   # ver idempotency_key + uq_tasks_idempotency_key UNIQUE
```

### Confirmar por qué una clave no colisiona (la verificación del SELECT)

```powershell
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "SELECT id, content, due_date, idempotency_key FROM tasks WHERE content ILIKE '%Rubi%' ORDER BY id DESC;"
# filas con idempotency_key vacío = creadas pre-migración, invisibles al constraint
```

### El experimento de la cura (inyección determinística del blip)

```powershell
# 1. Añadir temporalmente la guarda FAIL_FIRST_INVOKE en orchestrator.py + import os
# 2. Recrear el contenedor (contador de módulo en 0 — UNA corrida válida por rebuild):
docker compose up -d --build api
# 3. Lanzar UNA petición con la env activa, y leer el LOG (no el conteo):
docker compose logs api | Select-String "create_task executed|blip|Retrying|duplicate ignored"
# Señal de cura: executed -> WARNING blip -> Retrying 0.5s -> duplicate ignored, misma idempotency_key, 1 fila.
# 4. Quitar la instrumentación, rebuild limpio, confirmar grep sin rastros + health 200.
```

---

## 9. Estado y siguientes pasos

### Funcionando ✅ (nuevo en la Parte 21)

- **Fecha correcta** vía resolver determinístico en Python puro (cero deps): "el viernes" → `2026-06-05`, "pasado mañana" → `2026-06-01`, etc. `dateparser` descartado por medición.
- **Red de seguridad de fecha-en-el-pasado:** un ISO viejo (2023) se rechaza en vez de guardarse en silencio (habría cazado el bug de la P20).
- **`create_task` idempotente por clave:** `idempotency_key` + `uq_tasks_idempotency_key UNIQUE` + `ON CONFLICT DO NOTHING`. A prueba de replay (no de repeticiones legítimas en el tiempo).
- **La cura medida:** inyección determinística del blip → replay con misma clave → una sola fila (id 9), validado por el WARNING del log. Arco P20→P21 cerrado: peligro medido → cura medida, mismo terreno.
- **Sistema limpio:** instrumentación quitada, grep sin rastros, health 200.

### Deudas / pendientes 🔜

- **Idempotencia semántica vs por-clave** (anotado, no perseguido): la clave protege replay, no "usuario pide lo mismo en días distintos"; el agente deduplica eso a nivel de razonamiento. Dos capas independientes, ambas funcionando, ninguna universal.
- **CRUD de `tasks` incompleto:** solo CREATE. `list`/`update`/`delete` (y el problema de *direccionar* un registro: "borra la tarea de Rubí") quedan pendientes si se quiere completar la herramienta.
- **Secretos `change-me-in-env`** (P12/P14): el único bloqueante real restante para B/C.
- **Tool-call intermitente del 7B** (~25%, P18): la verificación puede necesitar 2-3 intentos.
- **Heredadas:** `request_id: "-"` en la ingesta; `retry_backoff` de Celery; `path_separator` en `alembic.ini`; drift de docs; `SecurityWarning` de Celery como root.

### Las opciones que siguen (del plan acordado)

- **Completar A** (`list`/`update`/`delete` de tareas): seguro, da lectura-tras-escritura y el problema de direccionar un registro; no toca el gate de idempotencia (ya resuelto).
- **B — integración externa de bajo riesgo:** "este patrón exacto + credenciales externas". Fuerza pagar la deuda de secretos. El patrón seguro (idempotencia + escritura) ya está aprendido y medido.
- **C — integración empresarial (correo/calendario/CRM):** el objetivo final; idempotencia-por-clave ya obligatoria y ya construida, secretos como prerequisito.
- **Fase 3 — Interfaz:** frontend sobre la API estable (las señales de herramienta del streaming siguen sin UI).

---

## 10. Temario de estudio (Parte 21)

> Conceptos transferibles que esta parte enseña. Para repaso.

### A. LLMs y resolución de fechas

- **Dividir el trabajo por la flaqueza del modelo**: el LLM extrae bien ("viernes") pero calcula mal (qué fecha es ese viernes). La extracción se le deja; la aritmética determinística va al código.
- **El contrato de "pasar la frase, no la fecha"**: para que un resolver determinístico funcione, el modelo debe entregar la expresión cruda; eso exige *quitar* el contexto de fecha que lo tentaría a calcular, e instruirlo explícitamente.
- **Validación de cordura como capa aparte**: rechazar fechas en el pasado atrapa el error que la validación de formato no ve (un ISO bien formado pero con año equivocado).
- **Medir una dependencia contra el caso de uso real antes de adoptarla**: `dateparser` falla en las formas con artículo; un resolver de stdlib de 30 líneas es más simple, más correcto y sin dependencia.

### B. Idempotencia por clave en herramientas de escritura

- **La clave deriva de valores normalizados, no de la entrada cruda**: hashear el resultado resuelto (`due` como `date`) hace que entradas equivalentes ("el viernes"/"este viernes") colapsen a la misma clave.
- **La BD como árbitro (`ON CONFLICT`)**: un constraint único sin carrera, frente a un `SELECT`-antes-de-`INSERT` que sí la tiene. Lección de la P15 §4.4 aplicada a una tool.
- **NULL no colisiona en índices únicos**: permite migrar un constraint sobre filas existentes sin rellenarlas (bendición), pero las vuelve invisibles a la deduplicación (trampa).
- **El alcance honesto de "idempotente"**: a-prueba-de-replay ≠ deduplicación semántica universal. Nombrar la garantía con precisión evita prometer de más.

### C. Diseño de experimentos de verificación

- **Dos capas de deduplicación, independientes**: la semántica (el agente razona) intercepta antes de la BD; solo el replay mecánico ejercita la clave. La verificación debe saber qué capa prueba.
- **El muro de lo observable a mano**: un retry con backoff sub-segundo no se puede ejercitar parando un servicio manualmente; la inyección de fallo determinística (gated por env) reproduce el escenario sin coordinar tiempos.
- **Estado de módulo en un experimento**: un contador que no se resetea entre requests obliga a "una corrida por rebuild"; reconstruir es más simple que añadir lógica de reset.
- **Validar por la señal correcta, no por el resultado aparente**: el conteo de filas no distingue "cura" de "experimento no corrió"; solo el log (el WARNING del blip) lo hace. Un falso testigo se desenmascara eligiendo bien qué se observa.

### D. El método, recurrente

- **El experimento que no prueba lo que dice probar**: la Prueba 1 (interceptada por el agente) y el `stop ollama` manual (frenado por el backoff) son dos espejismos en una sesión; reconocerlos es la parte instructiva (eco de P19/P20).
- **Confirmado-no-supuesto, hasta el final**: el arco P20→P21 (peligro medido → cura medida, mismo terreno) cierra como cierra todo el proyecto — no con "debería funcionar", sino con la señal en el log.
- **Limpiar la instrumentación al cerrar**: guarda env + imports temporales quitados, `grep` sin rastros, health 200. El sistema vuelve a un estado limpio, como tras el experimento de la P20.

---

*Cierre de la Parte 21.*
