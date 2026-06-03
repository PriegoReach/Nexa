# NexaAgent — Bitácora de desarrollo (Parte 27)

> Cierre de la **Fase 2 (Automatización)**. Contenido propio: **enviar correo con Gmail** — el
> punto más alto de la escalera de irreversibilidad del proyecto (un correo no se deshace, va a
> un tercero, y el modelo redacta el contenido). Y, por ser el cierre de fase, un **balance del
> arco completo** (P20→P27): el camino de "saber/recordar" a *actuar*, desde una escritura
> interna reversible hasta una acción externa irreversible a una persona real, con confirmación
> humana e idempotencia. La Fase 2 cumple la promesa original de la P1.

**Estado al cierre de la Parte 27 — y de la Fase 2:** `send_email` funciona, y es la acción más peligrosa del proyecto domada con la barrera más fuerte. El hallazgo recurrente del 7B —"afirma efectos que no ocurrieron" (P24/P25/P26)— quedó **resuelto de raíz** con *propuesta-sin-modelo*: el modelo elige la tool (su trabajo bueno) pero su narración de la propuesta se descarta y se reemplaza por la pregunta literal del pending; verificado `answer == pending["question"]`. La barrera de confirmación demostró ser **real**: el correo recibido coincide carácter por carácter con lo que se confirmó (MIME decodificado, acentos UTF-8 intactos). Idempotencia lado-emisor (Gmail no ofrece id de cliente como Calendar): tabla `sent_emails`, registrar-primero, un solo correo ante doble envío. Ambos correos de prueba llegaron a la bandeja real → verificación end-to-end. La Fase 2 queda cerrada con el trípode **saber (RAG) / recordar (memoria) / actuar (escritura interna, Calendar, Gmail)** completo.

---

## 1. Gmail: por qué cruza un umbral que ninguna parte anterior cruzó

La escalera de irreversibilidad que el proyecto subió a lo largo de la Fase 2:

| Acción | Parte | Irreversibilidad |
|---|---|---|
| `create_task` | P20 | reversible — un DELETE en mi BD |
| `call_webhook` | P22 | "irreversible" pero bajo daño — una notificación |
| `delete_task` | P24 | destructivo pero interno — una fila de prueba |
| `create_calendar_event` | P26 | externo, pero **se borra** — un evento mal creado se elimina |
| **`send_email`** | **P27** | **externo + irreversible-de-verdad + a un TERCERO** |

`send_email` tiene tres cosas que ningún otro tuvo:

1. **No se puede deshacer.** Un evento se borra; un correo enviado ya llegó. No hay "unsend" real (el "deshacer" de Gmail son segundos de delay local).
2. **Va a una persona real**, no a mi propia cuenta. El daño es a un tercero: un correo mal dirigido o mal redactado afecta a alguien que no soy yo.
3. **El 7B redacta el contenido.** Hasta ahora el modelo elegía args estructurados (`task_id`, `summary`, fecha); ahora redacta **prosa** que se envía a un humano.

Por eso, aquí la confirmación **no es cosmética** (como se trató en P26): es la única barrera entre "el 7B propuso" y "un correo equivocado llegó a alguien". Y por eso convergió con el peor hallazgo del proyecto (sección 3): si el 7B afirma efectos no ocurridos, en Gmail eso es peligroso en dos direcciones — dice "ya envié" y no envió (el usuario cree que avisó y no), o redacta mal y sí se envía (llegó algo equivocado a un tercero).

---

## 2. La idempotencia: lado-emisor, porque Gmail no la ofrece

El círculo de la regla, cerrado. Se verificó: **Gmail `users.messages.send` NO acepta un id de cliente** que prevenga duplicados — enviar el mismo `raw` dos veces produce dos correos. A diferencia de Calendar (`events.insert` con id → 409, P26). Así que **lado-emisor**, como el webhook de la P22-B:

- Tabla `sent_emails` (migración a mano, `down_revision` = head): `id`, `idempotency_key` (unique), `to`, `subject`, `created_at`, `status`.
- `idempotency_key = sha256(to | subject | body normalizado)` (patrón P21/P22-B).
- **Registrar-primero** (sesgo a no-duplicar, P22-B): `INSERT ... ON CONFLICT (idempotency_key) DO NOTHING RETURNING id`. Si `row` es None → ya se envió → no reenviar, devolver "Ese correo ya se había enviado". Si insertó → enviar vía Gmail API (MIME RFC 2822, base64url), luego `UPDATE status`.

La regla completa de la Fase 2 sobre idempotencia externa: **lado-receptor cuando el receptor lo garantiza (Calendar, P26); lado-emisor cuando no (webhook P22-B, Gmail P27).** La misma pregunta, respondida según lo que cada receptor ofrece.

---

## 3. Propuesta-sin-modelo: el cierre de raíz del patrón recurrente del 7B

### El problema, ahora con consecuencia grave

Tres veces el 7B afirmó un efecto que no ocurrió: P24 ("he borrado #11" al solo proponer), P25 (narró mal datos correctos), P26 ("he creado el evento" al solo proponer). En P24/P26 se trató como cosmético — el control era correcto, no se ejecutaba nada indebido. **En P27 deja de ser cosmético:** si el 7B parafrasea la pregunta de confirmación de un correo, podría *alterar el destinatario o el cuerpo* al narrarlo, y el usuario confirmaría algo distinto de lo que se envía. La confirmación dejaría de ser una barrera.

### La causa, y la solución quirúrgica

El flujo de una propuesta (P24/P26): el agente elige la tool → la tool escribe el pending y devuelve la pregunta → ese string vuelve al agente → **el agente redacta la respuesta final, y ahí parafrasea**. El paso de redacción final es donde el 7B reescribe.

**Solución — propuesta-sin-modelo (override, sin reestructurar el agente):** se aprovecha que el modelo **elige bien la tool** (routing OK en P25/P26) y solo **narra mal la propuesta**. Entonces: se deja que el modelo corra (elija la tool, ejecute la propuesta), pero su narración final se **descarta** y se reemplaza por el texto literal.

- La tool, al proponer, guarda en el pending un campo nuevo `question` = la pregunta de confirmación exacta y formateada.
- `run_agent` (y `run_agent_stream`), **después** de la llamada al agente, comprueba: ¿no había pending al entrar y ahora sí? Si se acaba de crear un pending, **descarta lo que el modelo redactó y devuelve `pending["question"]` tal cual.**

El resultado: el modelo elige la tool, pero el usuario ve **exactamente** lo que se va a enviar/hacer, no la versión del 7B. Verificado: `answer == pending["question"]` fue `True` en todos los casos de propuesta. Y un beneficio colateral — esto **arregla el borde cosmético de P26 para todas las acciones**: `delete_task` y `create_calendar_event` también dejaron de decir "ya lo hice" al proponer (caso f). El borde que P26 dejó como deuda opcional, P27 lo cerró para todo el sistema.

> Es la forma correcta de cerrar una deuda: no cuando molesta, sino cuando un caso la vuelve obligatoria — y el arreglo, hecho bien, sirve para todo lo demás. El patrón recurrente del 7B no se parcheó con un prompt más fuerte (pedirle que no parafrasee, que ya falló 3 veces); se eliminó la *posibilidad* de que parafraseara la propuesta.

---

## 4. La acción: `send_email`

- **`send_email` @tool (PROPONE)** — recibe `(to, subject, body)` que el modelo redacta. Valida que `to` parece un email (sin guardar pending si no). Escribe el pending con `action: "send_email"`, `args`, `description`, y `question`: la pregunta **completa y literal** mostrando los **tres** campos — destinatario, asunto, y **cuerpo completo** (no resumido). El error peligroso de un correo no es "se envía sin querer" (la confirmación lo previene) sino "mal dirigido" o "el cuerpo dice algo que no querías"; por eso se muestran los tres, completos. NO envía aquí.
- **`perform_send_email(args)`** (registrado `@confirmable_action("send_email")`) — el envío real con la idempotencia lado-emisor de la sección 2. Solo se invoca desde el registro tras un "sí" explícito.

Más: scope `gmail.send` (el **mínimo** — solo enviar, no leer; menos scope, menos riesgo) añadido al de `calendar.events`, con re-autorización (el `prompt=consent` de la P25 la fuerza). Construcción del MIME con `email.mime` de stdlib — sin la librería pesada `google-api-python-client` (criterio del proyecto: httpx directo).

---

## 5. Verificación — los seis casos, todos a la propia cuenta

Regla de seguridad de las pruebas: **todos los correos a mi propia dirección**, nunca a un tercero — el daño de un bug aquí es a una persona real, y durante las pruebas esa persona debo ser yo.

| Caso | Resultado |
|---|---|
| **(a) Proponer** | respuesta = texto **literal** de la pregunta (Para/Asunto/Cuerpo completo); `answer == pending["question"]` ✓; `sent_emails` vacío → no enviado |
| **(b) "lo mostrado = lo enviado"** ⚡ | "sí" → enviado, `status='sent'`; **MIME decodificado: To/Asunto/Cuerpo coinciden carácter por carácter** con lo mostrado (acentos UTF-8 intactos) |
| **(c) Cancelar** | "no" → "Cancelado, no hice nada", pending limpio, 0 envíos |
| **(d) Ambiguo** | "tal vez luego" → repregunta, pending conservado, 0 envíos (seguridad P24 intacta en la acción más peligrosa) |
| **(e) Idempotencia** | doble envío mismos args (desacoplado del modelo): 1º "enviado", 2º "ya se había enviado"; **un solo correo** en la bandeja |
| **(f) delete_task + create_event** | ambos muestran ahora la pregunta **literal** (ya no "ya lo hice"); evento 404→200 tras "sí"; tarea borrada — P24/P26 intactos, borde cosmético curado |

**El caso (b) es la prueba que define P27** — la convergencia del override (sección 3) con el efecto real: la confirmación muestra el cuerpo guardado en el pending, y `perform_send_email` envía *ese mismo* cuerpo, así que **lo confirmado es literalmente lo enviado**. Decodificar el MIME del correo recibido y verlo coincidir carácter por carácter es lo que hace la barrera real, no teatro. **Confirmación end-to-end:** los dos correos de prueba (b y e) llegaron a la bandeja real.

**Routing del 7B (cierre del hilo del 14B, sección 7):** eligió `send_email` correctamente en todas las propuestas de correo, y `create_calendar_event`/`delete_task` en (f). Routing sólido — tercera parte consecutiva (P25/P26/P27).

**Bordes que aparecieron (honesto):**
- **Gmail API deshabilitada** en el proyecto Cloud → 403 SERVICE_DISABLED en el primer (b). No era bug: el código construyó token+MIME+POST bien y registró `status='failed'`. Habilitada (es una API aparte de Calendar), (b) pasó.
- **El api cayó con exit 137 (OOM)** entre el stop y la verificación. Reiniciado, OAuth intacto. **Señal de memoria** (sección 7): el sistema ya está al borde con el 7B.
- **El trade-off registrar-primero, tensionado por un caso nuevo:** un envío que falla **definitivamente antes de salir** (403/401/400 — configuración/petición) deja la `idempotency_key` grabada, bloqueando el reintento legítimo. Hubo que borrar la fila `failed` a mano para reintentar (b). P22-B es correcto para fallos *ambiguos* (¿salió o no?), pero excesivo para un fallo inequívoco pre-envío. Deuda con forma clara (sección 7).

---

## 6. Balance del arco — la Fase 2 (Automatización), cerrada

El camino de "saber/recordar" a *actuar*, parte por parte:

| Parte | Hito | La capacidad / lección que añadió |
|---|---|---|
| **P20** | `create_task` | La primera escritura. El acoplamiento retry↔escritura medido (el retry re-ejecuta efectos). Escritura interna reversible: el terreno seguro. |
| **P21** | idempotencia + fix de fecha | La clave por contenido + `ON CONFLICT`. La fecha resuelta en código, no por el 7B (el efecto se ejecuta de datos, no de la interpretación del modelo). |
| **P22** | CRUD + secretos + webhook | Direccionar un registro (listar→actuar). Docker secrets (la fuga cerrada: `env=[]`). Idempotencia lado-emisor y el trade-off registrar-vs-disparar. |
| **P23** | Postgres a secrets | Reubicar (no rotar) un secreto con estado. El consumidor con trampa (`worker_db` fuera de la fuente única). |
| **P24** | confirmación asistida | El flujo propone→confirma→ejecuta. Estado entre turnos (pending en Redis). "Lo ambiguo nunca ejecuta". |
| **P25** | OAuth + lectura Calendar | La credencial dinámica (token que caduca, refresh). El ciclo de vida probado, no solo el login. |
| **P26** | crear eventos | La convergencia (OAuth+confirmación+idempotencia). Registro de ejecutores. Idempotencia lado-receptor (Google la garantiza). |
| **P27** | enviar correo | La cima de la irreversibilidad. Propuesta-sin-modelo (cierra el patrón del 7B). Lo confirmado = lo enviado. |

**Lo que la Fase 2 logró:** el trípode del objetivo original (P1) está completo. El agente **sabe** (RAG, Fase 1), **recuerda** (memoria de largo plazo) y **actúa** — y "actuar" llegó hasta su forma más exigente: una acción externa, irreversible, a un tercero, mediada por confirmación humana e idempotencia. Cada peldaño se subió en orden de riesgo creciente, aprendiendo el patrón en terreno seguro antes de añadir consecuencia — exactamente la metodología que arrancó la fase ("A antes que C").

**Los hilos conductores de la fase, en retrospectiva:**
- **La idempotencia** fue el "hilo conductor" anunciado en P20, y lo fue: reapareció en cada acción de escritura, con su forma adaptándose al riesgo (permisiva en lo reversible → por clave → lado-emisor/lado-receptor según el receptor).
- **"El efecto se ejecuta de datos, no de la interpretación del modelo"** atravesó P21 (fecha), P24 (confirmación determinística), P26 (event_id), P27 (propuesta-sin-modelo). Es la defensa estructural contra la falibilidad del 7B.
- **El cross-loop de la P2** (`Future attached to a different loop`) reapareció en P24 (Redis) — un fantasma de la Fase 0 que el patrón aprendido convirtió en trivial.
- **El 7B** mostró un perfil consistente: routing sólido (P25/P26/P27), pero síntesis/narración infiel (P24/P25/P26) — resuelto no mejorando el modelo sino quitándolo del camino donde fallaba.

---

## 7. Aprendizajes clave

1. **La confirmación es una barrera real solo si lo confirmado es lo ejecutado.** Mostrar el cuerpo del pending y enviar ese mismo cuerpo (no una narración del modelo) hace que "lo que ves es lo que se envía". Verificarlo carácter por carácter (MIME decodificado) distingue una salvaguarda de una ilusión de salvaguarda.

2. **Propuesta-sin-modelo: quitar al modelo del paso donde falla.** El 7B elige bien la tool pero narra mal la propuesta; descartar su narración y mostrar el texto literal del pending elimina el fallo de raíz, sin reestructurar el agente. Mejor que pedirle que no falle (ya falló 3 veces).

3. **Cerrar una deuda cuando un caso la vuelve obligatoria — y que sirva para todo.** El borde cosmético de P26 se volvió peligroso en P27 (Gmail); el arreglo (propuesta-sin-modelo) lo cerró para *todas* las acciones confirmables, no solo email. El momento y el alcance correctos.

4. **Lado-emisor vs lado-receptor: la regla completa.** Lado-receptor cuando el receptor garantiza idempotencia (Calendar, id→409); lado-emisor cuando no (Gmail no da id de cliente, webhook genérico tampoco). Verificar qué ofrece el receptor antes de elegir.

5. **El scope mínimo es seguridad.** `gmail.send` (solo enviar) en vez de Gmail completo: menos permiso, menos superficie de daño si algo sale mal. Pedir solo lo que la función necesita.

6. **El trade-off registrar-primero necesita clasificar el fallo.** Bloquear el reintento es correcto para un fallo ambiguo (¿salió?), excesivo para uno inequívoco pre-envío (403/401/400, no salió). La idempotencia conservadora puede estorbar cuando el fallo es definitivamente "no ocurrió". (Deuda con forma clara.)

7. **OOM como dato contra el 14B.** El exit 137 muestra que el sistema ya está al borde de memoria con el 7B. La decisión del 14B no es solo "¿mejora el routing?" (que en P25/P26/P27 fue sólido sin él) sino "¿el host aguanta?" — y el margen es estrecho. Dos señales convergentes (no cabe en VRAM, P23-diag; OOM con el 7B, P27) inclinan a diferirlo.

8. **Probar acciones irreversibles contra uno mismo.** Todos los correos de prueba a la propia cuenta: el daño de un bug es a una persona real, y durante la verificación esa persona debe ser el desarrollador. Nunca un tercero hasta confiar en el sistema.

---

## 8. Comandos de referencia (nuevos de esta parte)

### Re-autorizar con el scope de Gmail (añadido a Calendar)

```powershell
curl.exe http://localhost:8000/oauth/google/start -H "Authorization: Bearer <TOKEN>"
# autorizar, copiar code, entregarlo a /oauth/google/callback
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "SELECT provider, scope FROM oauth_accounts;"  # gmail.send + calendar.events
```

### Verificar el flujo de envío (propone → muestra literal → envía)

```powershell
# proponer -> la respuesta es la pregunta LITERAL (Para/Asunto/Cuerpo), NO se envía
curl.exe -X POST http://localhost:8000/chat -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" -d '{\"message\": \"envia un correo a MI_EMAIL diciendo que la reunion es a las 4\"}'
# confirmar -> se envía (verificar en la propia bandeja)
curl.exe -X POST http://localhost:8000/chat -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" -d '{\"message\": \"sí\"}'
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "SELECT to_addr, subject, status FROM sent_emails ORDER BY id DESC LIMIT 3;"
```

### Desbloquear un reintento tras un fallo definitivo pre-envío (workaround de la deuda)

```powershell
# un envío 4xx (config/petición) deja la key y bloquea el reintento; borrarla para reintentar:
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "DELETE FROM sent_emails WHERE status='failed';"
```

---

## 9. Estado y siguientes pasos

### La Fase 2 (Automatización): CERRADA ✅

- ✅ **P20** — `create_task`: la primera escritura; acoplamiento retry↔escritura medido.
- ✅ **P21** — idempotencia por clave + fecha resuelta en código.
- ✅ **P22** — CRUD + direccionamiento; Docker secrets (fuga cerrada); webhook idempotente.
- ✅ **P23** — Postgres a secrets (reubicar, no rotar; el consumidor con trampa).
- ✅ **P24** — confirmación asistida (propone→confirma; lo ambiguo nunca ejecuta).
- ✅ **P25** — OAuth Google + lectura Calendar (ciclo de vida del token probado).
- ✅ **P26** — crear eventos (convergencia; registro de ejecutores; idempotencia lado-receptor).
- ✅ **P27** — enviar correo (cima de irreversibilidad; propuesta-sin-modelo; lo confirmado = lo enviado).

El trípode **saber / recordar / actuar** completo. La promesa de la P1, cumplida.

### Deudas / pendientes 🔜

- **Clasificar el fallo en la idempotencia de envío** (deuda con forma clara): distinguir fallo-ambiguo (timeout/5xx → mantener la key, P22-B) de fallo-definitivo-pre-envío (4xx config/petición → borrar la key, permitir reintento). El workaround hoy es borrar la fila `failed` a mano.
- **El 14B, ahora con dos señales en contra:** no cabe en VRAM junto al reranker (P23-diag) Y el sistema ya hace OOM con el 7B (P27). A favor: nada urgente — el routing del 7B fue sólido en P25/P26/P27, y la narración infiel se resolvió quitando al modelo del camino (propuesta-sin-modelo), no agrandándolo. **Inclinación: diferir, salvo que aparezca un caso donde el routing del 7B falle de forma costosa.**
- **CRUD de eventos incompleto:** solo crear. `delete_calendar_event`/`update_calendar_event` (borrar evento sería confirmable como `delete_task`) si se quiere completitud.
- **Tokens en claro en `oauth_accounts`** (P25): cifrado a nivel de app en producción.
- **Refresh token de app no verificada caduca ~7 días** (Testing de Google): re-autorizar para uso personal.
- **Artefactos de prueba:** 3 eventos en el Calendar real (reunión de equipo 2 jun, Demo idempotencia P26 10 jun, Llamada con Soporte 4 jun) + 2 correos en la bandeja — limpiar a mano (no hay tool de borrado de eventos).
- **Heredadas:** URL de webhook de producción; `request_id: "-"` en la ingesta; `retry_backoff` de Celery; `path_separator` en `alembic.ini`; drift de docs; `SecurityWarning` de Celery como root; margen de memoria del host (el OOM de P27).

### Las direcciones que siguen (ya no la promesa central — extensiones y mejoras)

- **Google Drive** (extensión natural): "busca el documento X → Drive → trae el PDF → RAG". El agente trayendo documentos que luego indexa — extiende el trípode a "buscar fuera". La razón de valor más alta tras Gmail.
- **Harness de medición + decisión del 14B:** un baseline poblacional de routing (N consultas diversas, incluido el encadenado) daría la vara para decidir el 14B con datos, si alguna vez el routing falla costosamente. Hoy diferido.
- **CRUD de Calendar / más proveedores:** completitud de las integraciones existentes.
- **Fase 3 — Interfaz:** frontend sobre la API ya estable. Las señales de herramienta del streaming siguen sin UI, y ahora hay patrones ricos que mostrar (confirmación propone/responde, OAuth). El contrato de la API se estabilizó con el cierre de la Fase 2 — el momento para una UI es ahora más propicio.

---

## 10. Temario de estudio (Parte 27)

> Conceptos transferibles que esta parte enseña. Para repaso.

### A. Acciones irreversibles a terceros

- **La escalera de irreversibilidad**: reversible (DELETE propio) → bajo daño (notificación) → externo-borrable (evento) → irreversible-a-tercero (correo). El diseño de la salvaguarda escala con el peldaño.
- **El error peligroso no es "sin querer" sino "mal"**: para un correo, la confirmación previene el envío accidental, pero el riesgo real es destinatario o cuerpo equivocados. La confirmación debe mostrar todo lo que puede estar mal, completo.
- **Lo confirmado = lo ejecutado**: la barrera es real solo si lo que el usuario aprueba es literalmente lo que ocurre. Verificarlo sobre el efecto real (MIME del correo recibido), no sobre la intención.
- **Probar contra uno mismo**: el daño de un bug en una acción irreversible a terceros se contiene dirigiéndolo al propio desarrollador durante la verificación.

### B. Defensa estructural contra la falibilidad del modelo

- **Quitar al modelo del paso donde falla**: si el modelo elige bien pero narra mal, descartar su narración y usar datos estructurados (propuesta-sin-modelo) elimina el fallo sin agrandar el modelo.
- **Override quirúrgico vs reestructurar**: detectar "se creó un pending este turno" y reemplazar la salida es mínimo; no toca el loop interno del agente. Aprovecha lo que el modelo hace bien (routing) y suprime lo que hace mal (narrar la propuesta).
- **El efecto se ejecuta de datos, no de interpretación**: el hilo de toda la Fase 2 (fecha en código P21, confirmación determinística P24, event_id P26, propuesta-sin-modelo P27). La defensa consistente contra un modelo poco fiable.
- **Un fallo recurrente se elimina, no se parchea**: pedirle al 7B "no parafrasees" (un prompt más fuerte) ya falló 3 veces; quitar la posibilidad de parafrasear lo cierra.

### C. Idempotencia externa, la regla completa

- **Lado-receptor cuando el receptor lo garantiza, lado-emisor cuando no**: Calendar da id de cliente (→409, lado-receptor); Gmail no (lado-emisor con tabla propia). Verificar qué ofrece el receptor.
- **Clasificar el fallo para no sobre-bloquear**: registrar-primero bloquea reintentos; correcto para fallo ambiguo (¿salió?), excesivo para fallo definitivo pre-envío (4xx). La idempotencia conservadora tiene un costo cuando el fallo es inequívoco.

### D. Cierre de fase (método)

- **El balance del arco**: al cerrar una fase, sintetizar qué añadió cada parte y qué hilos la atravesaron — más valioso que listar hitos sueltos. (Como P19 cerró la Fase 1.)
- **Las señales convergentes para una decisión diferida**: el 14B se difiere por dos razones independientes que apuntan igual (no cabe en VRAM + OOM con el 7B), no por una sola. Una decisión cara se cierra cuando la evidencia converge.
- **Subir en orden de riesgo**: cada acción de la fase se construyó en terreno más seguro antes de añadir consecuencia (create_task → … → send_email). La metodología "A antes que C" que arrancó la fase, validada por su recorrido completo.

---

*Cierre de la Parte 27. Fin de la Fase 2 (Automatización) — el trípode saber/recordar/actuar, completo.*
