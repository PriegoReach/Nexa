# NexaAgent — Bitácora de desarrollo (Parte 22)

> Continuación de la Parte 21. Tres bloques encadenados que llevan la Fase 2 desde "una
> herramienta de escritura interna" hasta "un efecto externo irreversible, idempotente":
> **A** — completar el CRUD de tareas, aprendiendo a *direccionar* un registro (resolver una
> referencia difusa a un id antes de actuar). **S** — sacar los secretos `change-me-in-env`
> a un mecanismo real (Docker secrets) y **cerrar la fuga** de env vars. **B** — `call_webhook`,
> la primera integración externa, donde la idempotencia-por-clave de la P21 estrena una
> consecuencia que ya no se puede deshacer.

**Estado al cierre de la Parte 22:** los tres bloques cerrados y verificados. **A:** `list_tasks`/`update_task`/`delete_task` siguiendo el molde de `create_task`, sin migración (la columna `status` ya existía); el direccionamiento difuso ("la tarea de Rubí" → id) funcionó 2/2 con el 7B encadenando list→act de forma fiable. **S:** `JWT_SECRET` y `AUTH_PASSWORD` migrados a Docker secrets (archivo en `/run/secrets/`, leído por un `field_validator` de pydantic con fallback), las env vars **eliminadas de `.env`** y confirmadas vacías en api y worker (`env=[]` — fuera de `docker inspect`), suite 24/24 verde. **B:** `call_webhook` con tabla `webhook_events`, idempotencia **lado-emisor**, orden **registrar-primero**; el replay devolvió `DUPLICATE` y el capturador local se quedó en **1 POST** — el efecto irreversible no se repitió. Queda como deuda viva la migración de `POSTGRES_PASSWORD` (toca el contenedor `db` y la `DATABASE_URL`), que abre la P23.

---

## 1. El plan y el orden obligado

La parte cubre cuatro rutas de la Fase 2 que se habían tabulado: completar A (CRUD), Secretos (operabilidad), B (externo de bajo riesgo) y C (empresarial). Se atacaron **A + Secretos + B** en una sola sesión, dejando C para el futuro. El orden no fue libre — **B depende de Secretos**: meter una credencial externa real (la URL de un webhook) en un `.env` con secretos de juguete sería sembrar el agujero que el bloque busca cerrar. La secuencia obligada:

```
A (CRUD interno, sin credenciales)  →  S (secretos)  →  B (externo, consume el secreto)
```

A arranca sin prerequisito; Secretos es el puente; B consume lo que Secretos montó. Misma disciplina de toda la fase: cada capa verificada antes de la siguiente, aquí con dependencia dura entre bloques.

---

## 2. Bloque A — el CRUD de `tasks` y el direccionamiento

### 2.1 La capacidad nueva: direccionar un registro

`create_task` (P20/P21) era escritura "ciega" — crear, sin referirse a nada existente. El CRUD introduce lo genuinamente nuevo: **"borra la tarea de Rubí" no es una operación, son dos** — el agente **lista**, ve los ids, elige el correcto, y **actúa por id**. Esa resolución de referencia-difusa-a-id es la capacidad que B y C también necesitarán ("manda el correo sobre el proyecto X").

**Decisión de diseño clave:** las tools de mutación (`update_task`, `delete_task`) operan **por id**, nunca por texto. El direccionamiento difuso lo resuelve el **modelo** llamando `list_tasks` primero, no la tool adivinando. El motivo es de seguridad: si `delete_task` aceptara texto y adivinara, un "borra Rubí" con dos tareas de Rubí borraría la equivocada **en silencio** — el primo del bug de fecha de la P20 (acción destructiva confiadamente errada). Listando primero, el modelo desambigua con contexto o pregunta cuál.

### 2.2 Las tres tools (sin migración)

Verificación de realidad previa: el CRUD **no necesita migración** — la columna `status` (de la migración 0003 de la P20) ya sirve para el "marcar hecha". Solo tres `@tool` nuevas en `app/agent/tools/manage_tasks.py`, siguiendo el molde de `create_task` (puente `_run_async` + `worker_session()` + commit):

| Tool | Operación | Idempotencia |
|---|---|---|
| `list_tasks(include_done)` | `SELECT` sobre tabla interna mutable (≠ RAG) | lectura, n/a |
| `update_task(id, status)` | `UPDATE ... SET status=:s WHERE id=:id` | **por valor absoluto** (marcar hecha 2× = mismo estado) |
| `delete_task(id)` | `DELETE ... WHERE id=:id RETURNING content` | **no-op benigno** (borrar lo ya borrado ≠ error) |

Dos detalles deliberados, ambos ecos de partes anteriores:

- **`update_task` fija valor absoluto, no un toggle.** "Marca como hecha" → `status='done'`, no "invierte el estado". Por eso es idempotente *por construcción* — sin clave de por medio: aplicar el mismo valor dos veces deja el mismo estado final. (Un toggle sería el bug: dos llamadas volverían al estado original.)
- **`delete_task` usa `RETURNING content` para distinguir "borré algo" de "no había nada", y el no-op es benigno.** Borrar una tarea ya borrada devuelve "La tarea #N ya no existe (quizá ya la borraste)", **no** un error. Es exactamente el **404-vs-500 del `DELETE /conversations` de la P13**, ahora trasladado a la capa de tool del agente: la operación es idempotente porque el estado final (la tarea no existe) es el mismo se haya borrado antes o no.

### 2.3 Verificación: el direccionamiento, 2/2

La línea nueva en el `SYSTEM_PROMPT` enseña el patrón (crítico para el 7B): *"Si el usuario se refiere a una tarea por descripción ('la tarea de Rubí'), PRIMERO llama `list_tasks` para ver los id, identifica la correcta, y luego actúa con su id. Si hay varias que coinciden, pregunta cuál."*

Los cinco bordes, verdes:

| Caso | Resultado |
|---|---|
| `delete` de #4 ya borrada | "ya no existe (quizá ya la borraste)" — benigno, no error |
| `update` de #999 inexistente | "No existe la tarea #999." |
| `update` estado inválido | "Estado inválido 'archivada'. Usa 'done' o 'pending'." |
| `list` solo pending | excluye las `done` |
| `list` con `include_done` | muestra ○ pending y ✓ done |

Y la prueba central — **direccionamiento difuso 2/2**: "marca como hecha la de Rubí" → #9, "borra la del proveedor" → #4, ambos resueltos **listando primero y actuando por id**. El 7B encadenó list→act de forma fiable (sin refuerzo extra de prompt). Ojo: el direccionamiento exige *dos* tool-calls encadenados, más frágil que uno solo dado el ~25% de fallo del 7B (P18) — en esta corrida salió limpio.

> **Nota de observabilidad (espíritu P15):** `list_tasks` se le añadió un `logger.info("list_tasks", extra={"include_done", "n"})` *después* de la primera verificación, porque sin él la prueba del encadenamiento era *estructural* (ids correctos imposibles sin listar + el bracketing de 3 llamadas al modelo), no un testigo directo. Como B también direcciona, se hizo el list→act **directamente observable** en el log — la misma diferencia "confirmado por inferencia" vs "confirmado por testigo" que el WARNING del blip de la P21.

---

## 3. Bloque S — secretos a Docker secrets y el cierre de la fuga

### 3.1 La decisión: Docker Compose secrets (el "Modelo B" de esta parte)

El requisito —"single-host, con anticipación de despliegue, seguridad en mi máquina"— decide limpio: **Docker Compose secrets**, por el mismo razonamiento que el Modelo B del JWT (P12):

- **Nativo del stack** — cero infra nueva, cero dependencia.
- **Archivo, no env var** — monta cada secreto en `/run/secrets/<n>` en lugar de inyectarlo como variable de entorno. Las env vars se filtran en `docker inspect`, en `/proc/<pid>/environ`, en logs de proceso; **un archivo con permisos restringidos no**. Esa es toda la diferencia de seguridad.
- **Peldaño hacia un vault** — la forma "leer secreto desde archivo" es la que un vault (HashiCorp, AWS Secrets Manager) también usa. Si algún día se despliega multi-host, cambia de *dónde* viene el archivo, no el código que lo consume. Un vault hoy sería sobre-ingeniería para un solo host.

### 3.2 El helper integrado en pydantic (no paralelo)

Hallazgo de la verificación previa: `config.py` lee de `.env` vía `pydantic-settings` (`env_file=".env"`). El helper de secretos **debe vivir dentro del flujo de pydantic**, no ser un `os.getenv` paralelo — o habría dos caminos de configuración compitiendo (la trampa de los "dos manifiestos" de la P5). La solución: un `field_validator(mode="after")` por secreto que prefiere el archivo y cae al valor de `.env`/default:

```python
def _read_secret_file(env_value: str, secret_name: str) -> str:
    path = f"/run/secrets/{secret_name}"
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()        # .strip() absorbe un \n accidental
    return env_value                        # fallback: desarrollo y tests sin secrets montados
```

**`security.py` no cambió ni una línea** — sigue leyendo `settings.jwt_secret`/`settings.auth_password` por atributo; solo cambió de dónde se rellenan. Esa invisibilidad de la fuente para el consumidor *es* la prueba de que el diseño es limpio.

Los archivos se crearon en el host con `-NoNewline -Encoding ascii` — crítico: un `\n` o un BOM de PowerShell se leería como parte del secreto (el `.strip()` es red de seguridad, pero mejor no meterlo).

### 3.3 El paso que cierra el bloque de verdad: quitar las env vars de `.env`

El mecanismo de archivo es el *cómo*; el **objetivo** de S era cerrar la fuga. Pero mientras `JWT_SECRET`/`AUTH_PASSWORD` siguieran en `.env` con `env_file: .env`, seguían seteados como env vars en el contenedor → seguían en `docker inspect`. **El validator prefiriendo el archivo no es lo mismo que la env var no existir.** Dejar las líneas en `.env` habría sido construir la caja fuerte y dejar el secreto pegado en la puerta con cinta — la misma trampa de "una sola fuente de verdad" de la P5 y de los "logs fósiles" de la P15.

Por eso se **eliminaron las dos líneas de `.env`**. Seguro porque la rotación ya estaba probada (el archivo es, demostrablemente, la fuente viva; quitar el fallback no puede romper la auth). El costo que se anotó —"correr fuera de Docker cae al default roto"— no es un flujo real (app, tests y Alembic corren todos en Docker), y si lo fuera, caer a `change-me-in-env` falla **ruidoso**, que es deseable.

### 3.4 Verificación: la rotación + el artefacto de la fuga cerrada

La prueba de S es la **revocación de la P12**, ahora con el secreto en archivo:

1. Login con la `AUTH_PASSWORD` del archivo → token. ✓
2. Token en endpoint protegido → 200 (el `JWT_SECRET` del *archivo* firma/valida). ✓
3. Rotar el `JWT_SECRET` del archivo + recrear → el token viejo da **401** (el secreto nuevo invalida los firmados con el viejo — propiedad de la P12 intacta). ✓

Y **el artefacto que prueba que S triunfó** —el equivalente al WARNING del blip de la P21, el testigo directo del éxito—:

```powershell
docker exec nexaagent-api-1 sh -c 'echo "JWT_SECRET=[$JWT_SECRET]"'
# -> JWT_SECRET=[]     (VACÍO: no es env var, no está en docker inspect)
docker exec nexaagent-api-1 cat /run/secrets/jwt_secret
# -> el valor real      (leído del archivo)
```

`env=[]` en api **y** worker, con el valor presente solo en el archivo = la fuga cerrada, medida. Suite **24/24** verde tras tocar `.env` (los tests usan su env var explícita; el archivo no existe en el servicio `tests` → fallback, sin cambios).

De paso, al tocar el compose se arregló el **bug fósil del servicio `worker`**: una línea `./uploads` duplicada y comentarios `← AÑADIR` que ya estaban aplicados (ruido que la P15 enseñó a no dejar pasar).

---

## 4. Bloque B — `call_webhook` y la idempotencia con consecuencia irreversible

### 4.1 La decisión central: dónde vive la idempotencia

En `create_task` (P21), la clave protegía un duplicado **recuperable** (un `DELETE`). En `call_webhook`, **un POST que sale ya salió** — no hay vuelta atrás. Eso reabre la pregunta de *dónde* vive la protección, con dos opciones:

- **Lado receptor:** mandar un header `Idempotency-Key` y confiar en que el servidor lo honre. Frágil — un webhook genérico no garantiza honrarlo; depende de un sistema que no se controla.
- **Lado emisor (elegido):** registrar en *nuestra* BD qué eventos ya disparamos, **antes** de disparar. Un replay ve el registro y no redispara. Es el patrón de la P21 (clave + constraint `ON CONFLICT`) aplicado a un efecto externo, sin delegar en la buena voluntad del receptor.

### 4.2 La lección central de B: el trade-off registro-vs-efecto

La idempotencia lado-emisor tiene una sutileza que `create_task` no tenía, y **es la razón de que B sea el peldaño correcto antes de C**: el orden registro-vs-efecto es un problema **sin solución perfecta**, solo un trade-off que hay que elegir conscientemente. Son dos sistemas (nuestra BD y el receptor externo) sin transacción compartida — el problema de commit distribuido:

| Orden | Si el proceso muere en medio | Sesgo |
|---|---|---|
| **Registrar primero, disparar después** | evento marcado "enviado" pero **nunca salió** → mensaje perdido en silencio | **no-enviar-de-más** (a lo sumo se pierde uno) |
| **Disparar primero, registrar después** | mensaje salió pero no quedó marcado → replay lo **reenvía** → duplicado | **no-perder** (a lo sumo se duplica) |

No se pueden tener los dos. La pregunta de diseño: **¿qué duele menos, perder un mensaje o duplicarlo?** Para una *notificación* de bajo daño, duplicar es molesto pero perder es peor. Para una *acción con efecto* (cobrar, crear), duplicar es peor. Como `call_webhook` v1 es notificación, se eligió **registrar-primero** (sesgo a no-duplicar), documentado en el comentario de la tool y la migración como **lo que C debe revisitar según el efecto del webhook**.

Esto es genuinamente más profundo que la idempotencia de A: aquí "idempotente" contra un sistema externo es un *trade-off de modo-de-fallo*, no una garantía limpia.

### 4.3 La implementación

Tabla `webhook_events` (migración 0005, `down_revision = "tasks_idempotency_key"`, **a mano** por la trampa de autogenerate de la P6): `id`, `idempotency_key` (unique), `message`, `status` (`sent`/`failed`), `created_at`.

`call_webhook.py` en tres pasos, con la URL **desde `/run/secrets/`** (el primer consumo del mecanismo de S como credencial externa — cierra el círculo entre los bloques):

```python
# PASO 1: registrar ANTES de disparar (sesgo a no-duplicar).
#   INSERT ... ON CONFLICT (idempotency_key) DO NOTHING RETURNING id
#   row None -> la clave ya existía -> replay -> return "DUPLICATE" (antes del POST)
# PASO 2: httpx.post(url, ...) con timeout=15 (lección P15); fuera de la sesión.
# PASO 3: UPDATE status = 'sent' / 'failed' con el resultado real del POST.
```

El `DUPLICATE` se retorna **antes del PASO 2** — esa es la garantía de que el efecto irreversible no se repite: si la clave ya existe, nunca se llega al `httpx.post`.

### 4.4 La verificación: el capturador local (no webhook.site)

Primera elección de receptor: webhook.site. **Descartada por su propia evidencia** — capturaba `GET` desde la IP pública del host (el navegador abriendo la UI, sondas), no el `POST` de la tool. Introducía ambigüedad justo donde se necesita certeza: imposible distinguir el efecto medido del ruido de internet. Para contar "¿salió uno o dos POSTs?", fatal.

**El capturador local** —un `HTTPServer` mínimo en la red de compose que loguea cada POST con `print("WEBHOOK_RECEIVED ...", flush=True)`, contable con `docker logs`— elimina la ambigüedad. Es **exactamente el patrón del blip determinístico de la P21**: cambiar "mirar una UI de terceros que mezcla mi tráfico con sondas" por "un observador mío, sin internet, determinístico". Cierre conceptual: B no delega la *idempotencia* en un receptor externo (lado-emisor); usar un receptor externo para *verificarla* contradiría ese principio — controlamos emisor **y** observador.

| Señal | Happy path | Replay (mismo mensaje) |
|---|---|---|
| Retorno de la tool | "Notificación enviada" | "Esa notificación ya se había enviado (no se reenvió)" |
| POSTs en el capturador | **1** | **sigue en 1** (no salió un 2º POST) |
| Tabla `webhook_events` | fila #1 `status='sent'` | sigue 1 fila, key `b5070a…` |

**La diferencia A-vs-B, medida:** el replay devolvió `DUPLICATE` —que el código solo retorna cuando `ON CONFLICT` deja `row=None`, **antes** del `httpx.post`—, por eso el capturador se quedó en **1 POST**. En A la clave protegía un `DELETE` recuperable; aquí protegió un POST que ya no se puede deshacer. Esa es la consecuencia que B estrena.

> **Nota honesta de observabilidad (espíritu P15/P21):** la línea `call_webhook duplicate ignored` no aparece en los logs del *servidor* porque el replay se corrió en un `docker exec` aparte (proceso sin el `dictConfig` de logging del servidor). La prueba del no-op es el **retorno `DUPLICATE` + el conteo del capturador (1, no 2)** — que es de hecho **más fuerte** que un log: el log diría "decidí no enviar" (intención), el capturador en 1 dice "no salió" (efecto). En operación real (el agente llamando la tool dentro del proceso api) esa línea sí saldría en el stream del servidor. Se eligió el camino determinístico (re-invocar con el mensaje exacto leído de la BD) sobre el del agente, que podría deduplicar semánticamente — la misma falacia de la "Prueba 1" de la P21.

### 4.5 El receptor, formalizado (sin fósiles)

El capturador se levantó primero como contenedor ad-hoc (`docker run`, fuera de compose) — un **fósil** (P15): estado fuera del manifiesto, que no sobrevive a `compose down` y crea una dependencia invisible (si se mata, `call_webhook` falla sin explicación). Se **formalizó como servicio `webhook-receiver` en `docker-compose.yml`** (reutilizando la imagen `api` ya construida, sin `pull`). Dos cosas que da: reproducibilidad (`compose up` lo levanta, documentado en el manifiesto) y honestidad sobre qué es (infra de *prueba*, no producción — `webhook_url.txt` apuntando al servicio interno deja explícito que la **URL de producción es un pendiente real**: cambiar el archivo del secreto, no el código).

---

## 5. Aprendizajes clave

1. **Direccionar un registro es leer-para-actuar, en dos pasos.** "Borra la de Rubí" = listar → elegir id → actuar por id. Las tools de mutación operan por id (preciso); el modelo resuelve la referencia difusa llamando `list` primero. Adivinar el id en la tool sería borrar la equivocada en silencio (primo del bug de fecha de la P20).

2. **Un update idempotente fija valor absoluto, no invierte.** "Marca hecha" → `status='done'`, no toggle. Aplicar el mismo valor dos veces deja el mismo estado; un toggle sería el bug. La idempotencia a veces es gratis si se diseña la operación bien.

3. **El delete benigno es el 404-vs-500 en la capa de tool.** Borrar lo ya borrado no es un error: el estado final (no existe) es el mismo. `RETURNING content` distingue "borré" de "no había" sin un `SELECT` previo. Lección de la P13, un nivel más arriba.

4. **Seguridad de secretos = la env var NO existe, no que el archivo gane.** El validator prefiriendo `/run/secrets/` no cierra la fuga si la línea sigue en `.env` (sigue en `docker inspect`). El objetivo se cumple al *eliminar* la env var. `env=[]` es el testigo del éxito.

5. **El helper de secretos vive dentro de pydantic, no en paralelo.** Un `field_validator` con fallback, no un `os.getenv` compitiendo — una sola fuente de configuración (lección "dos manifiestos" de la P5). El consumidor (`security.py`) no cambia: la fuente del secreto le es invisible.

6. **La idempotencia contra un sistema externo es un trade-off, no una garantía.** Registrar-primero (sesga a no-duplicar) vs disparar-primero (sesga a no-perder): el problema de commit distribuido, sin solución perfecta. La elección depende de qué duele menos para el efecto concreto. Esto es lo que separa B (irreversible) de A (recuperable).

7. **El `DUPLICATE` antes del efecto es lo que hace la idempotencia real.** El no-op se retorna *antes* del `httpx.post`; si se llegara al POST y luego se detectara el duplicado, ya sería tarde. El orden de las comprobaciones es la garantía, no su mera presencia.

8. **El observador de una verificación debe controlarse, igual que el emisor.** webhook.site mezclaba el POST medido con GETs de navegador y sondas — ambigüedad fatal para contar. El capturador local (determinístico, sin internet) es el blip de la P21 aplicado a un efecto externo. Y es coherente: B no delega la idempotencia en el receptor, así que tampoco delega su verificación.

9. **El efecto medido > la intención logueada.** El capturador en "1 POST" prueba que el segundo no salió (efecto); un log "duplicate ignored" solo diría que se decidió no enviar (intención). Cuando se puede medir el efecto, es la evidencia más fuerte — mismo principio que "fila en la BD" > "el modelo dijo que creó la tarea".

10. **Un contenedor ad-hoc es un fósil; formalizarlo cuesta diez líneas.** `docker run` suelto = estado fuera del manifiesto, dependencia invisible que no sobrevive a `down`. Moverlo al compose da reproducibilidad y deja explícito qué es prueba y qué es producción.

---

## 6. Comandos de referencia (nuevos de esta parte)

### Verificar el patrón de tools y el esquema antes de extender

```powershell
docker exec nexaagent-api-1 sh -c "grep -rnE 'get_tools|@tool|worker_session' /app/app --include=*.py"
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "\d tasks"
```

### Crear un secreto (sin BOM ni newline — crítico)

```powershell
mkdir secrets
python -c "import secrets; print(secrets.token_urlsafe(48))" | Out-File -NoNewline -Encoding ascii secrets\jwt_secret.txt
"mi-password" | Out-File -NoNewline -Encoding ascii secrets\auth_password.txt
```

### El artefacto de la fuga cerrada (el testigo del éxito de S)

```powershell
docker exec nexaagent-api-1 sh -c 'echo "JWT_SECRET=[$JWT_SECRET]"'   # -> [] vacío = no es env var
docker exec nexaagent-api-1 cat /run/secrets/jwt_secret               # -> el valor (del archivo)
```

### Rotación / revocación de secreto (la prueba de la P12, ahora en archivo)

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))" | Out-File -NoNewline -Encoding ascii secrets\jwt_secret.txt
docker compose up -d api worker
curl.exe http://localhost:8000/conversations -H "Authorization: Bearer <TOKEN_VIEJO>"   # -> 401
```

### Verificar un webhook con capturador local (contable, sin internet)

```powershell
docker compose up -d webhook-receiver api
curl.exe -X POST http://localhost:8000/chat -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" -d '{\"message\": \"Notifica que el informe esta listo\"}'
docker compose logs webhook-receiver | Select-String "WEBHOOK_RECEIVED"   # contar los POSTs
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "SELECT id, message, status FROM webhook_events ORDER BY id DESC;"
```

---

## 7. Estado y siguientes pasos

### Funcionando ✅ (nuevo en la Parte 22)

- **CRUD de tareas completo:** `list_tasks`/`update_task`/`delete_task` + `create_task`. Direccionamiento difuso 2/2 (el 7B encadena list→act). Update idempotente por valor absoluto; delete no-op benigno (P13).
- **Secretos en Docker secrets:** `JWT_SECRET` + `AUTH_PASSWORD` en `/run/secrets/`, leídos por `field_validator` con fallback. Env vars **eliminadas de `.env`** → `env=[]` en api y worker (fuera de `docker inspect`). Rotación/revocación de la P12 intacta. Suite 24/24.
- **`call_webhook` idempotente:** tabla `webhook_events`, lado-emisor, registrar-primero. Replay → `DUPLICATE` antes del POST → capturador en 1 (efecto irreversible no repetido). URL desde `/run/secrets/` (primer secreto externo).
- **`webhook-receiver` formalizado** en compose (sin fósil ad-hoc).
- **Bug del compose `worker` arreglado** (`./uploads` duplicado + comentarios fósiles).

### Deudas / pendientes 🔜

- **`POSTGRES_PASSWORD` a secrets (abre la P23):** el "segundo paso" de S. Más enredado que JWT/AUTH — lo consumen **dos** sitios (el contenedor `db` vía `POSTGRES_PASSWORD`, y la app vía la contraseña embebida en `DATABASE_URL`). Requiere `POSTGRES_PASSWORD_FILE` (nativo de la imagen postgres) **y** descomponer `DATABASE_URL` para inyectar la contraseña por separado. Otra naturaleza que JWT (secreto que solo lee el código propio); merece su propio cuidado.
- **URL de webhook de producción:** `webhook_url.txt` apunta al capturador interno; un webhook real (Slack, endpoint propio) = cambiar el archivo del secreto, no el código.
- **`secrets/` al `.gitignore`:** el repo no es git hoy; si se inicializa, `secrets/` debe ignorarse **antes** del primer commit (contiene valores reales).
- **Confirmación (asistido) sin implementar:** la política gradual (P20) sigue fijada pero sin código; el ida-y-vuelta se construye cuando C toque lo irreversible-de-verdad.
- **Tool-call intermitente del 7B** (~25%, P18): el direccionamiento encadena *dos* calls, más frágil; en esta corrida salió limpio, pero es un punto de fragilidad conocido.
- **Heredadas:** `request_id: "-"` en la ingesta; `retry_backoff` de Celery; `path_separator` en `alembic.ini`; drift de docs (`.env.example`/README con `x-api-key`); `SecurityWarning` de Celery como root.

### Lo que sigue

- **P23 — Postgres a secrets:** cerrar el último `change-me-in-env`, con el cuidado que merece (contenedor `db` + descomposición de `DATABASE_URL`).
- **C — integración empresarial (correo/calendario/CRM):** "este patrón + un proveedor con su auth (OAuth) + el flujo de confirmación (asistido)". El objetivo final de la Fase 2; idempotencia y secretos ya resueltos como prerequisitos.
- **Fase 3 — Interfaz:** frontend sobre la API estable (las señales de herramienta del streaming siguen sin UI).

---

## 8. Temario de estudio (Parte 22)

> Conceptos transferibles que esta parte enseña. Para repaso.

### A. Agentes que mutan estado

- **Direccionar un registro (leer-para-actuar)**: resolver una referencia difusa ("la tarea de X") a un id concreto en dos pasos (listar → elegir → actuar), con el modelo desambiguando, no la tool adivinando.
- **Idempotencia por construcción vs por clave**: un update de valor absoluto es idempotente sin clave; un insert necesita clave + constraint. Diseñar la operación para que sea idempotente sola, cuando se puede.
- **El no-op benigno (404-vs-500)**: una operación destructiva sobre algo ausente devuelve éxito-benigno, no error, porque el estado final es el mismo. `RETURNING` distingue los casos sin un `SELECT` previo.
- **Lectura sobre tabla interna mutable ≠ RAG**: un `SELECT` simple sobre `tasks`, no búsqueda vectorial sobre documentos. Dos clases de "lectura" en el mismo agente.

### B. Gestión de secretos

- **Docker Compose secrets (archivo vs env var)**: por qué un secreto montado en `/run/secrets/` no se filtra en `docker inspect`/`/proc/environ`, a diferencia de una env var. El mecanismo nativo single-host, escalable a vault.
- **El helper integrado en el framework de config**: un `field_validator` de pydantic con fallback, no un lector paralelo — una sola fuente de verdad. El consumidor no cambia: la procedencia del secreto le es invisible.
- **Cerrar la fuga = eliminar la env var**: que el validator prefiera el archivo no basta; la línea debe salir de `.env`. La verificación es `echo $SECRET` → vacío.
- **Rotación como prueba de revocación**: cambiar el secreto invalida los tokens firmados con el viejo (401). La propiedad de seguridad se mide rotando, no asumiendo.
- **El fallback que falla ruidoso**: caer a un default obviamente roto (`change-me-in-env`) es deseable — falla visible en vez de arrancar en silencio con un secreto débil.

### C. Idempotencia contra sistemas externos

- **Lado-emisor vs lado-receptor**: registrar localmente qué se disparó (robusto, no delega) vs confiar en un header `Idempotency-Key` que el receptor podría no honrar (frágil).
- **El trade-off registro-vs-efecto (commit distribuido)**: registrar-primero sesga a no-duplicar (puede perder); disparar-primero sesga a no-perder (puede duplicar). Sin solución perfecta; la elección depende del costo del efecto.
- **El orden de la comprobación es la garantía**: retornar el no-op *antes* del efecto irreversible. Detectar el duplicado después del POST ya es tarde.
- **Recuperable vs irreversible**: la misma clave (P21) protege un `DELETE` recuperable en A y un POST irreversible en B; la diferencia no es el mecanismo sino la consecuencia de fallar.

### D. Verificación, recurrente

- **Controlar el observador, no solo el emisor**: un receptor local determinístico (sin internet, contable con `docker logs`) elimina la ambigüedad de una UI externa que mezcla el efecto medido con ruido. El blip determinístico de la P21 aplicado a un efecto externo.
- **El efecto medido > la intención logueada**: "1 POST en el capturador" (efecto) es evidencia más fuerte que "duplicate ignored" en el log (intención). Medir el efecto cuando se puede.
- **El camino determinístico sobre el del agente**: re-invocar con el input exacto garantiza llegar al código bajo prueba; dejar que el agente repita arriesga que deduplique semánticamente antes (falacia de la "Prueba 1" de la P21).
- **No dejar fósiles**: contenedores ad-hoc, líneas duplicadas, comentarios `← AÑADIR` aplicados — estado fuera del manifiesto que crea dependencias invisibles. Formalizar en el compose es barato.

---

*Cierre de la Parte 22.*
