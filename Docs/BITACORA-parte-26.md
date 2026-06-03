# NexaAgent — Bitácora de desarrollo (Parte 26)

> Segunda mitad de la integración Google: **ESCRIBIR en Calendar** (crear eventos). Es la
> parte donde **tres piezas construidas por separado convergen por primera vez** sobre un
> efecto externo irreversible-de-verdad: el OAuth de la P25, el flujo de confirmación de la
> P24 (hasta ahora probado solo sobre `delete_task` interno), y la idempotencia de la P22-B
> (hasta ahora sobre un webhook de bajo daño). Crear un evento es la primera acción externa
> que pasa por confirmación, y la primera idempotencia sobre un sistema que no controlamos.

**Estado al cierre de la Parte 26:** crear eventos en Google Calendar funciona, confirmable e idempotente. El flujo de confirmación de la P24 se **generalizó** de una sola acción (`delete_task`) a un **registro de ejecutores** (`CONFIRMABLE_ACTIONS`), de modo que `run_agent` quedó agnóstico a qué acciones existen — y P27 (gmail) será "registrar una función", no "otro `elif`". La idempotencia es **lado-receptor**: un `event_id` determinístico (sha1 → base32hex) que se pasa a Google; dos inserciones con la misma intención → Google devuelve 409 → **un solo evento**, sin tabla propia y sin el trade-off de commit distribuido de la P22-B. Verificado el borde clave (doble inserción → 1 evento) y que `delete_task` sigue intacto tras el refactor. Hallazgo recurrente, ahora por tercera vez: el 7B **afirma un efecto que no ocurrió** (dijo "he creado el evento" al solo *proponer*) — cosmético para la seguridad (el control es correcto, no se creó nada), pero un problema de confianza del usuario, anotado como deuda.

---

## 1. La convergencia: lo que ninguna parte anterior enfrentó sola

Cada pieza llegó a P26 ya construida y probada, pero **en aislamiento**:

| Pieza | Construida en | Probada solo sobre |
|---|---|---|
| OAuth Google + refresh | P25 | lectura (reversible) |
| Flujo de confirmación | P24 | `delete_task` interno (bajo daño) |
| Idempotencia por clave | P21 / P22-B | tarea interna / webhook de bajo daño |

P26 las usa **las tres a la vez, sobre un efecto externo irreversible**: crear un evento en un calendario real, que pasa por confirmación humana y no debe duplicarse. Esa convergencia es la novedad — no una pieza nueva, sino la primera vez que las tres operan juntas. Y trajo dos decisiones de diseño que el aislamiento no había forzado: cómo generalizar la confirmación a más de una acción (sección 2), y de qué lado vive la idempotencia cuando el receptor es Google (sección 3).

---

## 2. El refactor: de una acción confirmable a un registro

### El problema que P26 fuerza

En la P24, el flujo de confirmación se construyó **solo para `delete_task`**: `pending.py` ya era agnóstico (guarda un `intent` dict genérico), pero `_handle_confirmation` en el orquestador llamaba `perform_delete` **hardcodeado**. Con una segunda acción confirmable (`create_calendar_event`), la rama de confirmación necesita saber *qué* ejecutar según el `action` del pending. Dos formas:

- **(1) if/elif sobre `action`** — simple ahora, pero crece con cada acción (un `elif` por gmail, drive…), y engorda `run_agent`, el código de control más crítico.
- **(2) registro de ejecutores** `{action_name: perform_fn}` — cada tool confirmable registra su ejecutor en su módulo; `run_agent` busca por nombre y llama, agnóstico a qué acciones existen.

**Decisión: registro (2).** Razón: vienen al menos dos acciones confirmables más (gmail en P27, probablemente Drive). Generalizar **con la segunda acción** es el momento óptimo — es cuando el patrón se revela, y esperar a la quinta significa refactorizar bajo la presión de añadir gmail. Es el mismo principio que `get_tools()` ya usa (las tools se registran, el orquestador no las conoce una a una).

### La implementación

`app/agent/confirmable.py` (nuevo): `CONFIRMABLE_ACTIONS = {}` + un decorador `@confirmable_action(name)`. Contrato uniforme: `async def (args: dict) -> str` — recibe el `args` del pending, devuelve el mensaje de resultado.

- `perform_delete` pasa a aceptar `(args: dict)`, registrado como `"delete_task"`, arma su propio mensaje.
- `perform_create_event` (nuevo), registrado como `"create_calendar_event"`.
- `_handle_confirmation`, al recibir "sí", hace `CONFIRMABLE_ACTIONS[intent["action"]](intent["args"])` — **ya no menciona `perform_delete` por nombre**.

**Crítico — la seguridad de la P24 se preservó exacta.** El refactor cambió *qué* se ejecuta (lookup en vez de hardcode), NO *cuándo* (la heurística sí/no/ambiguo intacta, lo ambiguo nunca ejecuta y conserva el pending, `clear_pending` solo tras ejecutar o cancelar). El riesgo del refactor era romper esa heurística al generalizar; se verificó que no (casos d y f).

---

## 3. La idempotencia: lado-receptor, porque Google la garantiza

### El refinamiento sobre la P22-B

En el webhook (P22-B) la idempotencia fue **lado-emisor** (tabla propia `webhook_events`) *porque no se podía confiar en que un webhook genérico la honrara*. Pero **Google Calendar la soporta nativamente**: `events.insert` acepta un `id` de evento generado por el cliente, y si insertas dos veces con el mismo id, Google rechaza el duplicado (409) y devuelve el existente. El receptor **sí** la honra.

**Decisión: lado-receptor.** Y es la lección de la P22-B *refinada*: **lado-emisor cuando no puedes confiar en el receptor; lado-receptor cuando el receptor lo garantiza.** Delegar en Google elimina el trade-off de commit distribuido que la P22-B tuvo que aceptar (registrar-primero-vs-disparar-primero, donde siempre hay una ventana de crash que pierde o duplica). Con Google como árbitro de la unicidad, no hay tabla propia ni ventana de inconsistencia: la idempotencia vive del lado que ejecuta el efecto.

La clave sigue siendo del lado-emisor (la generamos nosotros, determinística desde la intención); la *garantía* es del lado-receptor (Google la honra). Lo mejor de ambos.

### El `event_id` (las reglas de Google)

Google exige **base32hex** para los ids de evento: solo `a-v` y `0-9`, longitud 5-1024. La codificación:

```
sha1(summary_normalizado + "|" + start + "|" + end)   # 20 bytes
  → base64.b32hexencode                                # alfabeto 0-9A-V, sin padding
  → .lower()                                           # → 0-9a-v, exacto al rango permitido
```

base32hex *es* precisamente ese alfabeto, así que la codificación cae exacta sin recortes artesanales. Determinístico: misma intención → mismo id → Google rechaza el duplicado. Es la clave de la P21/P22-B aplicada a un id de un sistema externo.

**El 409 como "ya existía", no como error** — es el equivalente exacto del `ON CONFLICT DO NOTHING` de la P22-B, del lado de Google: insertar con un id existente da 409, que se traduce a "el evento ya estaba creado", no a un fallo. Eso hace la idempotencia visible y benigna, como el "duplicate ignored" de la P22-B.

---

## 4. La acción confirmable: `create_calendar_event`

Dos mitades, el patrón propone-vs-ejecuta de la P24:

- **`create_calendar_event` @tool (PROPONE)** — parsea la petición a `{summary, start, end}` (reusando/extendiendo el resolver de fechas de `create_task`; las horas de eventos son más complejas que un `due_date`), escribe el pending con `set_pending({action: "create_calendar_event", args, description})`, y **devuelve la pregunta de confirmación**. NO crea el evento — esa es la convergencia con la P24: la acción externa pasa por confirmación igual que `delete_task`.
- **`perform_create_event(args)` (EJECUTA, registrado)** — el POST real a la Calendar API con el `event_id` determinístico. Solo lo invoca la rama de confirmación ante pending + sí explícito. Maneja el 409 como benigno.

Más: subir el scope de `calendar.readonly` (P25) a `calendar.events` (escritura acotada — más que readonly, menos que el `calendar` completo). Como el scope cambió, hubo que **re-autorizar**; el `prompt=consent` de la P25 forzó el re-consentimiento con el scope nuevo.

---

## 5. Verificación — los cinco casos y el borde de la convergencia

Tras re-autorizar (scope `calendar.events` confirmado en `oauth_accounts`), cada caso con su efecto real en Calendar:

| Caso | Resultado | Calendar |
|---|---|---|
| **(a) Proponer** | pregunta de confirmación + pending guardado | **404 — NO creado** ✓ |
| **(b) Confirmar "sí"** | "Evento creado: reunión de equipo…" (string de la tool, sin pasar por el modelo) | **200 — 1 evento**, offset −06:00 por timeZone ✓ |
| **(c) Cancelar "no"** | "Cancelado, no hice nada" + pending limpiado | **404 — NO creado** ✓ |
| **(d) Ambiguo "mmm tal vez"** | repregunta con la descripción, pending conservado | **404 — NO creado** ✓ (borde de seguridad P24 intacto) |
| **(e) Idempotencia** ⚡ | doble insert, mismos args, desacoplado del modelo: 1º "creado" → 2º "ya estaba creado (no lo dupliqué)" (rama del 409) | **1 SOLO evento** (200 por id) ✓ |
| **(f) `delete_task`** | "Tarea #14 eliminada" vía el registro | fila borrada ✓ — P24 no se rompió |

**El caso (e) es el corazón de P26 — la convergencia que ninguna parte probó sola.** Confirmar una acción externa Y que sea idempotente. Probado de forma determinista (re-invocando `perform_create_event` con los mismos args, desacoplado del modelo — el patrón de la P21/P22-B, porque a través del agente el 7B podría deduplicar semánticamente antes de llegar al efecto, la falacia de la "Prueba 1" de la P21). **La señal: un evento en el Calendar real, no dos**, con Google devolviendo 409 en el segundo intento. Es el "1 POST en el capturador" de la P22-B, ahora sobre un sistema externo que no controlamos — y la idempotencia lado-receptor lo logró sin la ventana de inconsistencia que la P22-B tuvo que aceptar.

**Routing del 7B (dato para el 14B):** eligió `create_calendar_event` correctamente en los tres proponer (a, c, d) y `delete_task` en (f). Como en la P25, el routing siguió OK — dos partes consecutivas de evidencia hacia "el routing del 7B es suficiente con tools bien descritas", lo que hace el 14B menos urgente de lo que parecía tras la P24.

**Nota operativa:** Uvicorn no corre con `--reload`, así que hizo falta `docker compose restart api worker` para que el proceso de larga vida cargara el scope nuevo y la tool (el código montado como volumen lo veían los `docker exec python` frescos al instante, pero el proceso vivo tenía el módulo viejo en memoria).

---

## 6. El borde que falló: el 7B afirma un efecto que no ocurrió (3ª vez)

**El hallazgo honesto, y un patrón ya consistente.** En los casos de *proponer* (a, c, d) el 7B respondió "Sí, he creado/programado el evento…" en vez de pasar la pregunta de confirmación que la tool devuelve.

**No es un bug de seguridad** — el flujo de control es correcto: el pending se guardó, el evento NO se creó (confirmado con 404 en los tres), y el "sí"/"no" del turno siguiente entra por la rama de confirmación sin tocar el modelo. Por eso (b) y (f) sí muestran el texto fiel: en la confirmación **no hay modelo** (el orquestador devuelve el string del ejecutor tal cual). La causa: en el paso de *propuesta* el modelo parafrasea el resultado de la tool, y tiende a convertir "¿Creo el evento X?" en "he creado el evento X".

**Pero es un problema de confianza del usuario, no de seguridad del sistema** — y por eso se anota, no se ignora. Si el agente dice "he creado el evento" cuando solo lo propuso, el usuario cree que está hecho y *no responde "sí"* → el pending caduca a los 10 min → el usuario descubre después que su reunión no existía. La seguridad está intacta (no se creó nada indebido), pero la *experiencia miente*.

**Es la tercera vez que el 7B afirma un efecto no ocurrido**, y eso lo hace un patrón, no una anécdota:
- P24: fraseó "he borrado #11" cuando solo había propuesto.
- P25: narró mal datos correctos (cumpleaños recurrente → "una celebración por día").
- P26: "he creado el evento" al solo proponer.

**Mitigaciones, por orden de costo (deuda, no se resuelve en P26):**
1. **Barata:** endurecer la descripción de la tool / system prompt para que copie la pregunta literal. Pero es pedirle al 7B algo que ya falló tres veces (respetar texto literal en vez de parafrasear) — podría ser frágil.
2. **Robusta (cambio de diseño):** que el paso de *propuesta*, igual que la confirmación, emita la pregunta **sin pasar por el modelo**. Eliminaría el problema de raíz (como (b)/(f) ya demuestran), pero es un cambio mayor en cómo se emite la respuesta de una tool que propone.
3. **El hilo del 14B:** "respetar el texto literal de una tool" es justo el seguimiento-de-instrucciones donde un modelo mayor ayuda. Otro voto (matizado) para el 14B.

Se intentará primero la mitigación barata en una verificación rápida; si el 7B no obedece de forma fiable, escala. No se resuelve dentro de P26.

---

## 7. Aprendizajes clave

1. **La convergencia es un hito distinto de construir las piezas.** OAuth, confirmación e idempotencia funcionaban por separado; hacerlas operar juntas sobre un efecto externo irreversible trajo decisiones (registro de ejecutores, lado-receptor) que el aislamiento no había forzado. Integrar no es solo sumar.

2. **Generalizar con la segunda instancia, no con la quinta.** El flujo de confirmación pasó de hardcode (una acción) a registro (N acciones) al aparecer la segunda — el momento en que el patrón se revela. Esperar a tener muchas significa refactorizar bajo presión. P27 (gmail) ya es "registrar una función", no "otro elif".

3. **Lado-emisor vs lado-receptor depende de la confianza en el receptor.** La P22-B usó lado-emisor porque un webhook no garantiza idempotencia; P26 usa lado-receptor porque Google sí. Delegar en un receptor que la garantiza elimina el trade-off de commit distribuido. La regla se refina con el caso.

4. **La clave determinística viaja, la garantía vive en el receptor.** El `event_id` (sha1→base32hex) lo genera el emisor desde la intención, pero la unicidad la impone Google. La misma clave de P21/P22-B, ahora como id de un sistema externo, sin tabla propia.

5. **El 409 es el `ON CONFLICT` del receptor.** Insertar con id existente → 409 → "ya estaba creado" (benigno), no error. La idempotencia visible y benigna, el equivalente externo del "duplicate ignored".

6. **Un refactor de control debe preservar la seguridad explícitamente.** Generalizar `_handle_confirmation` cambió *qué* se ejecuta, no *cuándo*; la heurística "lo ambiguo nunca ejecuta" se mantuvo intacta y se verificó (casos d, f). El riesgo de un refactor es romper una invariante de seguridad sin querer.

7. **Probar el efecto en el sistema real, de forma determinista.** El caso (e) se verificó re-invocando el ejecutor con los mismos args (no a través del agente, que deduplicaría semánticamente), y la señal es "1 evento en el Calendar real". El efecto medido > la intención logueada, ahora sobre un sistema externo.

8. **El 7B afirma efectos que no ocurren — patrón, no anécdota (3ª vez).** P24 ("he borrado" al proponer), P25 (síntesis infiel), P26 ("he creado" al proponer). La seguridad se protege con el control determinístico (el efecto no depende de lo que el modelo diga), pero la confianza del usuario sufre. Distinguir "el sistema hace lo correcto" de "el modelo lo narra correctamente" — y reconocer cuándo un fallo se repite lo bastante para ser estructural.

---

## 8. Comandos de referencia (nuevos de esta parte)

### Re-autorizar tras cambiar el scope (readonly → events)

```powershell
# el scope cambió -> el token viejo no escribe -> re-autorizar (prompt=consent re-pide)
curl.exe http://localhost:8000/oauth/google/start -H "Authorization: Bearer <TOKEN>"
# abrir auth_url, autorizar, copiar el code, entregarlo:
curl.exe -X POST http://localhost:8000/oauth/google/callback -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" -d '{\"code\": \"4/0A...\"}'
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "SELECT provider, scope FROM oauth_accounts;"  # confirma calendar.events
```

### Recargar el proceso vivo tras cambios (Uvicorn sin --reload)

```powershell
docker compose restart api worker   # el proceso de larga vida tiene el módulo viejo en memoria
```

### Verificar el flujo de confirmación de un evento (propone → crea)

```powershell
# proponer -> pregunta, NO crea
curl.exe -X POST http://localhost:8000/chat -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" -d '{\"message\": \"agenda una reunión de equipo mañana a las 3pm\"}'
# confirmar -> crea (1 evento en Calendar)
curl.exe -X POST http://localhost:8000/chat -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" -d '{\"message\": \"sí\"}'
```

---

## 9. Estado y siguientes pasos

### Funcionando ✅ (nuevo en la Parte 26)

- **Crear eventos en Google Calendar**, confirmable (flujo P24) e idempotente (lado-receptor).
- **Registro de ejecutores** (`CONFIRMABLE_ACTIONS`): el flujo de confirmación generalizado a N acciones; `run_agent` agnóstico. `delete_task` migrado, sigue intacto.
- **Idempotencia lado-receptor**: `event_id` determinístico (sha1→base32hex) → Google 409 → 1 evento. Sin tabla propia, sin commit distribuido.
- **Scope de escritura** (`calendar.events`) + re-autorización (el `prompt=consent` de P25 pagó).
- **Convergencia probada**: OAuth + confirmación + idempotencia operando juntas sobre un efecto externo irreversible.

### Deudas / pendientes 🔜

- **El 7B afirma efectos no ocurridos (patrón, 3ª vez):** "he creado" al proponer. Cosmético para seguridad, problema de confianza del usuario. Mitigación barata (prompt) a intentar; si falla, cambio de diseño (propuesta sin modelo) o el 14B. Anotado, no resuelto en P26.
- **CRUD de eventos incompleto:** solo crear. `delete_calendar_event`/`update_calendar_event` pendientes si se quiere completitud de Calendar (el equivalente del CRUD de tareas de P22-A; borrar evento sería confirmable como `delete_task`).
- **Limpieza:** 2 eventos de prueba quedaron en el Calendar real ("reunión de equipo" mar 2 jun 15:00, "Demo idempotencia P26" mié 10 jun 11:00). Se borran a mano en Calendar (no hay tool de borrado de eventos aún).
- **Tokens en claro en `oauth_accounts`** (P25): deuda de seguridad anotada; cifrado a nivel de app en producción.
- **Refresh token de app no verificada caduca ~7 días** (Testing de Google): re-autorizar para uso personal.
- **Heredadas:** URL de webhook de producción; `request_id: "-"` en la ingesta; `retry_backoff` de Celery; `path_separator` en `alembic.ini`; drift de docs; `SecurityWarning` de Celery como root.

### Lo que sigue

- **P27 — Gmail (mandar correo):** lo irreversible-de-verdad (un correo va a una persona real, y a diferencia de un evento, no se "borra"). Gracias al registro de ejecutores, `send_email` es "registrar un ejecutor confirmable", no tocar `run_agent`. Aquí el routing entre varias tools externas (calendar vs gmail) hace la decisión del 14B más aguda — y el patrón "el 7B afirma efectos no ocurridos" es más peligroso (un "ya envié el correo" falso).
- **Drive (post-P27):** "busca el documento X → Drive → PDF → RAG".
- **Fase 3 — Interfaz:** frontend sobre la API, con OAuth y confirmación como patrones a mostrar.

---

## 10. Temario de estudio (Parte 26)

> Conceptos transferibles que esta parte enseña. Para repaso.

### A. Integración de capacidades

- **Convergencia vs construcción**: hacer operar juntas piezas probadas en aislamiento (OAuth + confirmación + idempotencia) trae decisiones que ninguna pieza sola forzó. La integración es una fase con su propio diseño.
- **Generalizar en el momento óptimo**: pasar de hardcode (una instancia) a registro (N) al aparecer la segunda instancia, no la quinta — cuando el patrón se revela, antes de la presión de muchas.
- **El registro como desacoplamiento**: `{action: perform_fn}` deja al código de control agnóstico a qué acciones existen; cada módulo registra lo suyo. El mismo principio que un registro de tools.

### B. Idempotencia contra sistemas externos (continuación de P22-B)

- **Lado-emisor vs lado-receptor según confianza**: lado-emisor cuando el receptor no garantiza idempotencia (webhook); lado-receptor cuando sí (Google `events.insert` con id de cliente). Delegar elimina el trade-off de commit distribuido.
- **Clave determinística como id externo**: generar el id desde la intención (hash) y dejar que el receptor imponga la unicidad. Las reglas de formato del receptor importan (base32hex de Google).
- **El código de conflicto del receptor como no-op benigno**: 409 = "ya existía", no error — el `ON CONFLICT` del lado externo.

### C. La brecha entre el sistema y la narración del modelo

- **Seguridad de control vs confianza de narración**: el flujo puede ser correcto (efecto no ocurre) y el modelo narrarlo mal ("he creado") — un problema de confianza del usuario, no de seguridad. Distinguir lo que el sistema *hace* de lo que el modelo *dice*.
- **Por qué la confirmación es fiel y la propuesta no**: la confirmación no pasa por el modelo (string del ejecutor tal cual); la propuesta sí (el modelo parafrasea). Quitar el modelo de un paso elimina su infidelidad en ese paso.
- **Un fallo recurrente es estructural**: el mismo tipo de error en P24/P25/P26 (afirmar efectos no ocurridos) es un patrón del modelo, no mala suerte — y orienta dónde un modelo mayor ayuda (seguimiento de instrucciones literales).

---

*Cierre de la Parte 26. Integración Google de escritura completa para Calendar; falta Gmail (P27).*
