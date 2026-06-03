# NexaAgent — Bitácora de desarrollo (Parte 23)

> Cierre del hilo de secretos abierto en la Parte 22: migrar **`POSTGRES_PASSWORD`** a Docker
> secrets — el "segundo paso" que la P22 dejó explícitamente anotado como deuda. Más enredado
> que `JWT_SECRET`/`AUTH_PASSWORD`: la contraseña de Postgres la consumen **dos** sitios
> independientes (el contenedor `db` al arrancar, y la app dentro de `DATABASE_URL`), toca un
> contenedor de terceros, y exige descomponer la URL de conexión. Una sesión enfocada en un
> solo secreto, pero con tres sutilezas que `JWT`/`AUTH` no tenían.

**Estado al cierre de la Parte 23:** `POSTGRES_PASSWORD` migrado a Docker secrets, cerrando el último `change-me-in-env` de la infraestructura. La contraseña ya no aparece en el entorno de **ningún** proceso ni en `docker inspect`: `DATABASE_URL=[]` y `POSTGRES_PASSWORD=[]` confirmados en db, api y worker. Conectividad real verificada por las **dos** rutas que consumen la BD (login + `/conversations` 200 vía la app; `list_tasks` real vía `worker_session`), y suite 24/24 verde. La decisión clave —**reubicar el valor, no rotarlo**— mantuvo el volumen `pgdata` y el rol `nexa` intactos. Con esto, los cuatro secretos del sistema (jwt, auth, webhook_url, postgres) viven en archivos, fuera del alcance de `docker inspect`.

---

## 1. Por qué Postgres es distinto de JWT/AUTH

La P22 migró `JWT_SECRET` y `AUTH_PASSWORD` limpiamente: son secretos que **solo lee el código propio** (`security.py` vía `settings`). Un `field_validator` que prefiere `/run/secrets/` y se eliminan de `.env` — fin. `POSTGRES_PASSWORD` no se deja migrar igual por tres razones que se identificaron antes de tocar nada:

1. **Dos consumidores independientes.** La contraseña la usa el **contenedor `db`** (la imagen oficial de postgres la lee de `POSTGRES_PASSWORD` al inicializar el cluster) **y** la **app** (embebida en `DATABASE_URL = postgresql+asyncpg://nexa:<password>@db:5432/...`). Migrar uno sin el otro deja el sistema roto.

2. **Toca un contenedor de terceros.** No es código nuestro: es la imagen `pgvector/pgvector:pg16`. Hay que usar el mecanismo que *esa* imagen ofrece (`POSTGRES_PASSWORD_FILE`), no el nuestro.

3. **La contraseña está embebida en una URL, no suelta.** `DATABASE_URL` es una cadena con la contraseña dentro. No se puede "leer la contraseña de un archivo" sin **descomponer la URL** en sus partes y re-ensamblarla con la contraseña del secreto.

---

## 2. La decisión central: reubicar, no rotar

El matiz que define toda la sesión, y que es **opuesto** al de JWT:

- Con **JWT**, rotar el secreto era *deseable* — invalidaba los tokens viejos (la prueba de revocación de la P12). Cambiar el valor era parte del punto.
- Con **Postgres**, rotar el valor sería **destructivo**. El rol `nexa` y el volumen `pgdata` ya existen, con la contraseña vieja horneada en el cluster. Cambiar el *valor* rompería la conexión contra los datos existentes (exigiría un `ALTER ROLE ... PASSWORD` dentro del cluster ya inicializado, y coordinar ese cambio con el secreto nuevo).

Por eso la operación fue **reubicar el mismo valor** de env-var a archivo, **sin tocarlo**. El cluster sigue esperando la misma contraseña; solo cambió de **dónde** la leen el contenedor `db` y la app. `pgdata` intacto, rol `nexa` intacto, cero migración de datos.

> La distinción **rotar-vs-reubicar** es la lección transferible: el mismo mecanismo (secreto en archivo) se aplica con intención opuesta según si el secreto protege algo *sin estado* (un token que se puede invalidar) o algo *con estado* (un cluster que ya tiene el valor grabado). Aplicar el patrón sin entender esta diferencia habría roto la base de datos.

---

## 3. Los tres cambios

### 3.1 El contenedor `db` — `POSTGRES_PASSWORD_FILE`

La imagen oficial de postgres soporta de forma nativa leer la contraseña de un archivo en vez de una env var (la convención `_FILE` de esa imagen). En `docker-compose.yml`, el servicio `db` pasa de:

```yaml
    environment:
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-<POSTGRES_PASSWORD>}
```

a leer el secreto montado:

```yaml
    environment:
      POSTGRES_PASSWORD_FILE: /run/secrets/postgres_password
    secrets:
      - postgres_password
```

(El usuario y la base — `POSTGRES_USER`, `POSTGRES_DB` — **no** son secretos; siguen como antes. Solo la contraseña se reubica.)

### 3.2 `config.py` — ensamblar `DATABASE_URL` desde componentes

El cambio de fondo: en vez de una `DATABASE_URL` monolítica con la contraseña dentro, la URL se **arma** a partir de componentes no-secretos (`db_user`, `db_host`, `db_port`, `db_name`) + la contraseña leída del secreto. Con una condición crítica:

```python
# La URL solo se ensambla desde componentes SI viene vacía.
# Si DATABASE_URL llega explícita (el servicio `tests` la fija a nexaagent_test),
# se respeta tal cual — no se sobrescribe.
```

**Por qué la condición importa.** El servicio `tests` fija `DATABASE_URL` explícita apuntando a `nexaagent_test`. Si config la ensamblara *siempre* desde componentes, apuntaría los tests a la BD de **producción** — un desastre silencioso. Ensamblar **solo cuando viene vacía** (y respetar la explícita cuando se da) es la misma lógica de fallback del Bloque S de la P22: el valor explícito gana, el ensamblado es el respaldo. Por eso la suite siguió verde.

### 3.3 `worker_db.py` — el consumidor con trampa

**El verdadero examen de la P23.** Todo lo anterior era mecánico; esto era el riesgo real. `worker_db.py` —el helper `worker_session()` que usan las tools y el worker (P13)— leía la URL **directo del entorno**, no de `settings`:

```python
# ANTES (la trampa):
engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
# DESPUÉS:
engine = create_async_engine(settings.database_url, poolclass=NullPool)
```

Era un consumidor que el ensamblado de `config.py` **no habría alcanzado**: al quitar `DATABASE_URL` del `.env`, `os.environ["DATABASE_URL"]` lanzaría `KeyError`. Y el fallo sería **solo en runtime, no al arrancar** — la app conectaría bien (vía `settings.database_url`), pero el worker y cualquier tool que tocara la BD reventarían en cuanto se invocaran. Migrarlo a `settings.database_url` lo alinea con la fuente única.

Que se cazara *antes* de aplicar —y se probara con `list_tasks` real, que ejercita exactamente la ruta `worker_session`— es el método funcionando: el `grep` que mapeó "quién toca la BD" (P20, al diseñar `create_task`) es lo que dejó este consumidor visible como punto de riesgo conocido.

### 3.4 `.env` — eliminar las dos líneas

Tanto `POSTGRES_PASSWORD` como `DATABASE_URL` se eliminaron de `.env` — **ambas** llevaban la contraseña (una suelta, otra embebida). Igual que en la P22: cerrar la fuga es *eliminar* la env var, no solo preferir el archivo. El secreto `postgres_password` se montó en los **tres** servicios que lo necesitan: `db` (para inicializar/validar el cluster), `api` y `worker` (para ensamblar `DATABASE_URL`).

---

## 4. Verificación — las dos rutas y el entorno limpio

La verificación tuvo que cubrir **dos rutas de conexión distintas**, porque Postgres tiene dos consumidores (a diferencia de JWT, que solo lo lee `security.py`). Probar una sola habría dejado la otra sin confirmar:

| Qué se probó | Ruta | Resultado |
|---|---|---|
| Entorno limpio (db) | — | `DATABASE_URL=[]`, `POSTGRES_PASSWORD=[]` |
| Entorno limpio (api) | — | ambas vacías |
| Entorno limpio (worker) | — | ambas vacías |
| Conectividad **vía app** | `settings.database_url` en los endpoints | login + `/conversations` → **200** |
| Conectividad **vía worker_session** | `settings.database_url` en `worker_db` | `list_tasks` real → tareas listadas |
| Regresión | servicio `tests` con URL explícita | suite **24/24** verde |

La señal de éxito del bloque: las env vars vacías en los tres servicios (`echo $DATABASE_URL` → `[]`) **con** la conectividad funcionando por ambas rutas. Vacío + conecta = la contraseña vive solo en el archivo, fuera de `docker inspect`, y los dos consumidores la obtienen del sitio correcto. El `list_tasks` real fue el testigo específico de que la ruta `worker_session` —la del consumidor con trampa— quedó bien migrada.

---

## 5. Aprendizajes clave

1. **Reubicar un secreto ≠ rotarlo, y la diferencia depende del estado.** Un secreto sin estado (un token JWT) se puede rotar libremente — invalidar lo viejo es deseable. Un secreto con estado grabado (la contraseña de un cluster ya inicializado) hay que **reubicarlo sin tocar el valor**, o se rompe la conexión contra los datos existentes. El mismo mecanismo, intención opuesta.

2. **Un secreto embebido en una URL exige descomponer la URL.** No se puede leer "la contraseña" de un archivo si vive dentro de `postgresql://user:pass@host/db`. Hay que separar la URL en componentes no-secretos y re-ensamblarla con la contraseña del secreto.

3. **Un contenedor de terceros usa su propio mecanismo de secretos.** La imagen oficial de postgres ofrece `POSTGRES_PASSWORD_FILE`; hay que usar *ese*, no el `field_validator` propio. Migrar un secreto puede tocar mecanismos distintos para consumidores distintos.

4. **El consumidor con trampa es el que no pasa por la fuente única.** `worker_db` leía `os.environ["DATABASE_URL"]` directo, no `settings` — invisible al ensamblado de config, habría dado `KeyError` solo en runtime. Mapear *todos* los consumidores antes de tocar la fuente (el `grep` de la P20) es lo que lo dejó visible.

5. **El fallo en runtime es más peligroso que el fallo al arrancar.** La app habría conectado bien; solo el worker y las tools habrían reventado, y solo al invocarse. Un error que no aparece al arrancar se descubre en producción. Por eso la verificación probó la ruta `worker_session` explícitamente, no solo la de la app.

6. **El ensamblado condicional respeta el override de los tests.** Armar `DATABASE_URL` desde componentes *solo si viene vacía* deja que el servicio `tests` la fije explícita (a `nexaagent_test`) sin ser sobrescrito. Sobrescribir siempre habría apuntado los tests a producción. Misma lógica de fallback del Bloque S.

7. **Dos consumidores = dos verificaciones.** Postgres se consume por la app y por `worker_session`; confirmar una no confirma la otra. La cobertura de la verificación debe igualar el número de rutas, no asumir que una representa a todas.

---

## 6. Comandos de referencia (nuevos de esta parte)

### Crear el secreto de Postgres (reubicar el valor EXISTENTE, sin cambiarlo)

```powershell
# OJO: el valor debe ser el MISMO que ya tiene el cluster (reubicar, no rotar).
"<POSTGRES_PASSWORD>" | Out-File -NoNewline -Encoding ascii secrets\postgres_password.txt
```

### Verificar el entorno limpio en los tres servicios

```powershell
docker exec nexaagent-db-1   sh -c 'echo "PWD=[$POSTGRES_PASSWORD] URL=[$DATABASE_URL]"'
docker exec nexaagent-api-1  sh -c 'echo "PWD=[$POSTGRES_PASSWORD] URL=[$DATABASE_URL]"'
docker exec nexaagent-worker-1 sh -c 'echo "PWD=[$POSTGRES_PASSWORD] URL=[$DATABASE_URL]"'
# -> todas vacías: [] [] — la contraseña vive solo en el archivo
```

### Confirmar la contraseña montada como archivo

```powershell
docker exec nexaagent-db-1 cat /run/secrets/postgres_password
```

### Verificar conectividad por las DOS rutas

```powershell
# ruta app:
curl.exe http://localhost:8000/conversations -H "Authorization: Bearer <TOKEN>"   # -> 200
# ruta worker_session (vía una tool que tocan la BD):
curl.exe -X POST http://localhost:8000/chat -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" -d '{\"message\": \"Que tareas tengo?\"}'
```

---

## 7. Estado y siguientes pasos

### Funcionando ✅ (nuevo en la Parte 23)

- **`POSTGRES_PASSWORD` en Docker secrets:** reubicado (no rotado), `pgdata` y rol `nexa` intactos. `db` vía `POSTGRES_PASSWORD_FILE`; app y worker vía `DATABASE_URL` ensamblada desde componentes + secreto.
- **`worker_db` alineado** a `settings.database_url` (el consumidor con trampa, antes leía `os.environ` directo).
- **Entorno limpio confirmado:** `DATABASE_URL=[]` y `POSTGRES_PASSWORD=[]` en db, api, worker. Fuera de `docker inspect`.
- **Conectividad por las dos rutas** (app + `worker_session`) y suite 24/24.
- **Los cuatro secretos** (jwt, auth, webhook_url, postgres) ahora en archivos. La deuda `change-me-in-env` de la P12/P14 **saldada por completo**.

### Deudas / pendientes 🔜

- **`secrets/` al `.gitignore`** (ahora más urgente): la carpeta contiene **cuatro** secretos reales. El repo no es git hoy; si se inicializa, `secrets/` debe ignorarse **antes** del primer commit.
- **URL de webhook de producción:** `webhook_url.txt` apunta al capturador interno; un webhook real = cambiar el archivo, no el código (heredada de la P22).
- **Confirmación (asistido) sin implementar:** la política gradual (P20) sigue fijada sin código; se construye cuando C toque lo irreversible.
- **Tool-call intermitente del 7B** (~25%, P18): el direccionamiento encadena dos calls; punto de fragilidad conocido (monitoreo, no trabajo).
- **Heredadas:** `request_id: "-"` en la ingesta; `retry_backoff` de Celery; `path_separator` en `alembic.ini`; drift de docs (`.env.example`/README con `x-api-key`); `SecurityWarning` de Celery como root.

### Lo que sigue

- **C — integración empresarial (correo/calendario/CRM):** el objetivo final de la Fase 2. Con secretos e idempotencia ya resueltos como prerequisitos, C es "este patrón + un proveedor con su propia auth (OAuth) + el **flujo de confirmación (asistido)** que sigue fijado pero sin construir desde la P20". El primer trabajo nuevo de C probablemente sea ese ida-y-vuelta de confirmación, porque es lo irreversible-de-verdad.
- **Fase 3 — Interfaz:** frontend sobre la API ya estable (las señales de herramienta del streaming siguen sin UI).

---

## 8. Temario de estudio (Parte 23)

> Conceptos transferibles que esta parte enseña. Para repaso.

### A. Secretos con estado

- **Rotar vs reubicar**: un secreto sin estado (token) se rota para invalidar lo viejo; un secreto con estado grabado (contraseña de un cluster inicializado) se reubica sin tocar el valor, o se rompe el acceso a los datos existentes.
- **La convención `_FILE` de imágenes de terceros**: cómo la imagen oficial de postgres (y muchas otras) lee secretos de archivo de forma nativa (`POSTGRES_PASSWORD_FILE`), un mecanismo distinto del que usa el código propio.
- **Secreto embebido en una URL de conexión**: por qué hay que descomponer la URL en componentes y re-ensamblarla con el secreto, en vez de tratar la URL entera como una sola cadena.

### B. Configuración y consumidores

- **El consumidor que no pasa por la fuente única**: un módulo que lee `os.environ` directo en vez del objeto de settings es invisible a los cambios de config y falla en runtime. Mapear todos los consumidores antes de tocar la fuente.
- **Fallo en arranque vs fallo en runtime**: un error que no aparece al levantar el sistema (porque solo una ruta lo dispara) se descubre en producción. La verificación debe ejercitar cada ruta, no asumir que una representa a todas.
- **Ensamblado condicional con respeto al override**: construir un valor derivado solo cuando no viene explícito, para que un entorno (tests) pueda fijarlo y no ser sobrescrito. La misma lógica de fallback que un secreto archivo-sobre-env.

### C. Verificación proporcional

- **Tantas verificaciones como rutas de consumo**: si un recurso se usa por N caminos distintos (app + worker), confirmar uno no confirma los demás. La cobertura iguala el número de rutas.
- **El testigo específico de la ruta de riesgo**: probar `list_tasks` real (que ejercita `worker_session`) confirma específicamente el consumidor que se migró, no solo la conectividad genérica.
- **Vacío + conecta = secreto reubicado bien**: `echo $SECRET` vacío *junto con* la funcionalidad operando prueba que el valor vive solo en el archivo y que los consumidores lo obtienen del sitio correcto.

---

*Cierre de la Parte 23. Fin del hilo de secretos abierto en la Parte 22.*
