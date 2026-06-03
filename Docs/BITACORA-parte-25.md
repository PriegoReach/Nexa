# NexaAgent — Bitácora de desarrollo (Parte 25)

> Primera mitad de la integración externa real de la **Fase C**: **OAuth con Google + LECTURA
> de Google Calendar**. Aplicando el patrón de toda la fase (aprender en terreno seguro antes
> de lo irreversible), esta parte conecta el proveedor y *lee* — una operación reversible — y
> deja *escribir* (crear eventos) para la P26. El hito real, fijado antes de tocar la API: un
> **primer login OAuth exitoso**, con el ciclo de vida del token (refresh) probado, no asumido.

**Estado al cierre de la Parte 25:** OAuth con Google funcionando de punta a punta. El flujo de autorización de tres patas completo (login → código → tokens), con `refresh_token` capturado (vía `access_type=offline`) y guardado en una tabla nueva `oauth_accounts` en Postgres. El **refresh automático probado**: forzando `expires_at` al pasado, `get_valid_token()` renovó el access token solo. La tool `list_calendar_events` lee el calendario real (GET → 200, 10 eventos). Decisión de credencial confirmada: `client_id`/`client_secret` como Docker secrets (estáticos, patrón P23); los tokens OAuth en Postgres (dinámicos, no son secreto estático). Hallazgo para la decisión del 14B, más matizado que en la P24: el 7B acertó el *routing* de la tool (a diferencia de P24) pero narró mal los datos correctos que la tool devolvió — partiendo el problema del modelo en dos capacidades distintas (selección vs síntesis), una de las cuales resultó ser en parte un problema de *formato*, no del modelo.

---

## 1. Por qué OAuth es una clase de credencial nueva

Los secretos de las P22/P23 eran **estáticos**: un valor en un archivo (`/run/secrets/`), leído igual cada vez. Un token OAuth es **dinámico** y eso cambia todo el diseño:

- **Caduca** (~1 hora el access token de Google).
- **Se renueva** con un `refresh_token`, sin intervención del usuario.
- **Obtenerlo requiere un flujo de tres patas**: la app redirige al usuario a Google → el usuario autoriza → Google devuelve un código → la app lo cambia por tokens.

Por eso el mecanismo de la P23 **no sirve** para los tokens: un valor que se reescribe solo cada hora no es un secreto estático en un archivo. La decisión de almacenamiento (sección 2) es la consecuencia directa.

**El reparto correcto, que define la arquitectura de la parte:**

| Credencial | Naturaleza | Dónde vive |
|---|---|---|
| `client_id` / `client_secret` | **estáticos** (identifican la app ante Google) | Docker secrets (patrón P23) |
| `access_token` / `refresh_token` | **dinámicos** (cambian, caducan) | tabla `oauth_accounts` en Postgres |

---

## 2. Decisiones de diseño

### 2.1 Proveedor y alcance (Google, Calendar, leer antes de escribir)

**Google**, porque un solo sistema OAuth abre Calendar, Gmail, Drive, Docs y Sheets — y sus APIs son las más documentadas. **Calendar primero, lectura primero**, por perfil de riesgo: leer eventos es inocuo, crear un evento es reversible, y un evento mal creado se borra — frente a un correo, que va a una persona real. Es el mismo "aprende en terreno seguro" que arrancó la Fase 2 con `create_task`: aquí, **leer vía OAuth** (P25) antes de **escribir vía OAuth** (P26). Por ahora solo el scope `calendar.readonly`; el de escritura entra en P26.

### 2.2 El callback: loopback sin servidor (y el OOB muerto)

El flujo de tres patas necesita que Google devuelva el código. Para un cliente tipo **Desktop app** (el correcto para "app local que yo autorizo"), las opciones se redujeron a una por un hecho que Claude Code verificó en la documentación:

**El OOB (`urn:ietf:wg:oauth:2.0:oob`) — el copiar-pegar nativo de Google — está muerto desde el 31-ene-2023.** Ya no existe. Para Desktop apps, el único redirect soportado hoy es **loopback** (`http://localhost` / `http://127.0.0.1[:puerto]`). El flujo oficial pediría un servidor local escuchando ese puerto — pero la app vive en Docker, y exponer un puerto del contenedor al navegador del host es plomería que no aporta nada a aprender OAuth.

**Solución: loopback sin servidor.** El `redirect_uri` es `http://localhost`, pero **no hay servidor escuchando** (a propósito, cero puertos expuestos). El flujo:

```
1. GET /oauth/google/start → la app devuelve la auth_url
2. El usuario la abre, autoriza en Google
3. El navegador intenta ir a http://localhost/?code=4/0A...&scope=...
   → falla con "no se puede conectar" (ESPERADO: no hay servidor ahí)
4. El usuario copia el `code` de la barra de direcciones
5. POST /oauth/google/callback {"code": "..."} → la app lo cambia por tokens
```

El mismo `http://localhost` se reenvía en el intercambio de tokens (Google exige que coincida con el del paso de autorización). Es la variante de copiar-pegar que decidimos, adaptada a que el mecanismo nativo (OOB) ya no existe — captura manual del código desde la URL fallida en vez del campo OOB.

### 2.3 La tabla `oauth_accounts` (Modelo B otra vez)

Migración a mano (trampa de autogenerate de la P6 — `document_chunks` tiene columnas fuera de los modelos), `down_revision = "webhook_events"`. Columnas: `id`, `provider` (varchar, `UNIQUE`), `access_token`, `refresh_token` (nullable — Google solo lo da en la 1ª autorización), `expires_at` (timestamptz), `scope`, `created_at`, `updated_at`.

**Sin `user_id`** — es el **Modelo B** de la P12 (cliente único) aplicado a OAuth: una sola cuenta Google, `UNIQUE(provider)` para upsert sobre ella. Si algún día hay multiusuario, se añade `user_id`; hoy no hay caso de uso que lo justifique, y añadirlo sería las mismas sesiones de trabajo especulativo que el Modelo A del JWT.

---

## 3. La implementación

### 3.1 La lógica OAuth (`app/integrations/google_oauth.py`)

httpx directo, **sin la librería pesada `google-auth`** (criterio del proyecto: menos dependencia si httpx basta). Cuatro funciones:

- **`build_auth_url()`** — la URL de autorización, scope `calendar.readonly`. **Con `access_type=offline` + `prompt=consent`** — crítico: sin esto Google da un access token que muere en 1h y **ningún refresh token**, y la integración se rompería sola sin explicación. (Es el error clásico de OAuth con Google.)
- **`exchange_code(code)`** — cambia el código por tokens (POST al token endpoint con client_id/secret de settings).
- **`refresh_access_token(refresh_token)`** — renueva el access token cuando caduca.
- **`get_valid_token()`** — la función que usan las tools: lee `oauth_accounts`, y si `expires_at` ya pasó (con ~60s de margen), refresca solo, actualiza la fila, devuelve un token válido. Usa `worker_session()` (NullPool, P13) con cuidado del cross-loop (P2/P24) si se llama desde el ThreadPoolExecutor de una tool.

### 3.2 Los endpoints (`app/api/oauth.py`)

Tres, protegidos con el `require_jwt` existente (P12): `/oauth/google/start` (devuelve la auth_url), `/oauth/google/callback` (recibe el código, hace el intercambio + upsert), `/oauth/google/status` (¿conectado? ¿token válido? — sin exponer el token).

**El UPSERT con `COALESCE` — un detalle que evita un bug silencioso.** Google entrega el `refresh_token` **solo en la primera autorización**; en reautorizaciones manda solo el access token. Un upsert ingenuo sobrescribiría el `refresh_token` con `NULL` la segunda vez, y la integración perdería la capacidad de refrescar — sin error visible, hasta que el token caducara una hora después. El `COALESCE` (mantener el refresh viejo si el nuevo viene vacío) lo previene. No estaba pedido explícitamente; se añadió por buen criterio.

### 3.3 La tool de lectura (`app/agent/tools/calendar.py`)

`@tool list_calendar_events` siguiendo el molde existente (puente `_run_async`, httpx con timeout, devuelve string): llama `get_valid_token()`, hace GET a la Calendar API (eventos del calendario `primary`), formatea los próximos N legibles. El caso "sin cuenta conectada" devuelve un mensaje claro ("No hay calendario conectado; conéctalo primero"), no una excepción. Registrada en `get_tools()` + una línea en el `SYSTEM_PROMPT`.

---

## 4. Verificación — el hito del login y el ciclo de vida del token

Los cuatro pasos, en orden de "el login es el entregable, antes que la API":

| Paso | Qué probó | Resultado |
|---|---|---|
| **(a) Login** 🎯 | el flujo de tres patas completo | `connected:true`, `refresh_token_received:true`; fila en BD con access+refresh+expires futuro |
| **(b) Status** | la cuenta quedó consultable | `connected:true`, `token_valid:true`, `has_refresh_token:true` |
| **(c) Refresh** | el ciclo de vida, no solo el login | forcé `expires_at` al pasado → `get_valid_token()` refrescó solo: token cambió (`...IMgJ→...IMSz`), `expires_at` al futuro, `updated_at` actualizado |
| **(d) Lectura** | la tool, vía el agente | el agente llamó `list_calendar_events` (logs: GET events → 200, n=10), devolvió eventos reales |

**El paso (c) es el que de verdad importa.** El login que funciona una vez (a) prueba poco — una integración OAuth se rompe en una hora si no refresca. Forzar la caducidad y ver el access token cambiar solo prueba que la integración **sobrevive** al ciclo de vida del token, que es la propiedad de producción. Es la diferencia entre "funcionó" y "sigue funcionando" — el mismo rigor que distinguir el mecanismo del escalado en la P18/P19.

**Una nota de proceso:** el paso (a) requirió intervención humana — la autorización la hace el usuario en su navegador, no Claude Code. El flujo automático se detuvo, devolvió la `auth_url`, el usuario autorizó y devolvió el código. (Y de paso surgió el `Error 403: access_denied` — el correo no estaba en la lista de *test users* del consent screen; se resolvió añadiéndolo en la consola de Google. La app en modo Testing solo admite test users listados, que es lo correcto para desarrollo: evita la verificación de Google.)

---

## 5. El hallazgo del 7B — más matizado que en la P24

El dato que más afina la decisión del 14B, y es **distinto** al de la P24:

- **En la P24:** el 7B falló el *routing* — llamó `update_task` en vez de `delete_task`.
- **En la P25:** el 7B acertó el routing (llamó `list_calendar_events`), pero la **fidelidad del resumen fue pobre** — interpretó un cumpleaños anual recurrente como "celebraciones, una por cada día del fin de semana". La tool entregó datos correctos; el modelo los **narró mal**.

Esto parte el problema del 7B en **dos capacidades independientes**:

1. **Selección de tool** (¿qué herramienta llamar) — falló en P24, acertó en P25.
2. **Fidelidad de síntesis** (¿narra bien lo que la tool devolvió) — falló en P25.

Y la implicación para el 14B es la clave: un modelo más grande probablemente ayuda con (1), la selección. Pero (2), la síntesis, **es más turbia** — y en parte resultó ser un problema de **formato, no de modelo**. Los eventos de todo el día recurrentes anuales aparecían **sin año** → 10 líneas que parecían duplicadas; parte de la "narración mala" eran datos mal presentados, no incompetencia del modelo. Se arregló el formato (añadir el año), y eso es más barato que cambiar el modelo.

> Es el patrón recurrente del proyecto: **antes de culpar al modelo, mirar si el problema es la presentación de los datos.** Como en la P21 (la fecha errada no era incompetencia del 7B, sino falta de contexto temporal) o la P19 (el "fallo de tool-call" de p-7 era en realidad el retriever no trayendo el chunk). El hallazgo de P25 NO es un voto limpio a favor del 14B: el 7B tiene dos debilidades distintas, una de las cuales (la síntesis) se ataca en parte mejorando cómo la tool formatea los datos.

**Estado de la decisión del 14B tras P25:** ayuda probable al routing, incierto en síntesis, y parte de la síntesis es formato (más barato). La pregunta sigue abierta, con más matiz — y se vuelve aguda en P26/P27, donde el modelo elige entre *varias* tools externas (calendar vs gmail) y el routing importa más.

---

## 6. Aprendizajes clave

1. **Una credencial dinámica no es un secreto estático.** Un token OAuth caduca y se renueva; tratarlo como un valor de archivo (P23) es un error de categoría. Los identificadores estáticos de la app (client_id/secret) van a secrets; los tokens que cambian van a Postgres.

2. **`access_type=offline` + `prompt=consent` o no hay refresh token.** Sin esos parámetros, Google da un access token de 1h y nada más, y la integración muere sola sin error visible. El parámetro que parece opcional es el que decide si la integración sobrevive.

3. **El OOB murió; loopback sin servidor es la adaptación a Docker.** El copiar-pegar nativo de Google ya no existe (2023). Para una app en contenedor, dejar que el redirect a `localhost` falle y capturar el código de la URL evita exponer puertos — la decisión de diseño (copiar-pegar) sobrevive aunque su mecanismo nativo no.

4. **El UPSERT debe preservar el refresh token (`COALESCE`).** Google solo da el refresh en la primera autorización; un upsert ingenuo lo borraría en la segunda, rompiendo el refresh una hora después en silencio. Conocer la semántica del proveedor evita el bug que no se ve hasta que es tarde.

5. **Probar el ciclo de vida, no solo el login.** Un login que funciona una vez no prueba que la integración perdure; forzar la caducidad y ver el refresh automático prueba la propiedad de producción. "Sigue funcionando" > "funcionó".

6. **El problema del modelo se parte en routing vs síntesis.** El 7B puede elegir bien la tool y aun así narrar mal sus datos (P25), o elegir mal la tool (P24). Son capacidades distintas; un modelo más grande ayuda a una con más claridad que a la otra.

7. **Antes de culpar al modelo, revisar el formato de los datos.** Parte de la "síntesis pobre" del 7B eran eventos sin año que parecían duplicados — un problema de presentación de la tool, no del modelo. Como la fecha de la P21 o el retriever de la P19: la capa de datos a menudo explica lo que parece incompetencia del modelo.

8. **El Modelo B (cliente único) se extiende a OAuth.** Sin `user_id`, `UNIQUE(provider)`, upsert sobre una sola cuenta — la misma decisión de alcance del JWT (P12). No construir multiusuario sin caso de uso.

---

## 7. Comandos de referencia (nuevos de esta parte)

### Iniciar el flujo OAuth (la parte humana)

```powershell
# 1. obtener la auth_url (requiere token JWT)
curl.exe http://localhost:8000/oauth/google/start -H "Authorization: Bearer <TOKEN>"
# 2. abrir esa URL en el navegador, autorizar, copiar el `code` de la URL fallida (localhost)
# 3. entregar el código:
curl.exe -X POST http://localhost:8000/oauth/google/callback -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" -d '{\"code\": \"4/0A...\"}'
```

### Verificar estado y forzar el refresh (probar el ciclo de vida)

```powershell
curl.exe http://localhost:8000/oauth/google/status -H "Authorization: Bearer <TOKEN>"
# forzar caducidad para probar el refresh automático:
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "UPDATE oauth_accounts SET expires_at = now() - interval '1 hour' WHERE provider='google';"
# luego una lectura -> get_valid_token refresca solo:
curl.exe -X POST http://localhost:8000/chat -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" -d '{\"message\": \"¿qué reuniones tengo?\"}'
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "SELECT provider, expires_at, updated_at FROM oauth_accounts;"
```

---

## 8. Estado y siguientes pasos

### Funcionando ✅ (nuevo en la Parte 25)

- **OAuth con Google** de punta a punta: login de tres patas, refresh token capturado, ciclo de vida del token probado (refresh automático medido).
- **Tabla `oauth_accounts`** en Postgres (Modelo B, `UNIQUE(provider)`, refresh nullable).
- **`client_id`/`secret` en Docker secrets** (patrón P23); tokens en Postgres (dinámicos).
- **Redirect loopback sin servidor** — adaptación a Docker del OOB muerto, cero puertos expuestos.
- **UPSERT con COALESCE** — preserva el refresh token en reautorizaciones.
- **`list_calendar_events`** lee el calendario real; el agente la invoca correctamente.

### Deudas / pendientes 🔜

- **Tokens en claro en `oauth_accounts`** (deuda de seguridad anotada): credenciales de terceros sin cifrar. Aceptable single-host de desarrollo (la BD está tras el secreto de la P23); en producción querrían cifrado a nivel de app antes del INSERT. No bloqueante; anotado.
- **Refresh token de app no verificada caduca a ~7 días** (modo Testing de Google): para uso personal se re-autoriza; para uso continuo real habría que publicar la app (posible verificación de Google con scopes sensibles). Fase futura.
- **Síntesis del 7B / la decisión del 14B:** routing OK en P25, pero síntesis pobre (en parte formato, ya arreglado). Se revisita en P26/P27 con múltiples tools externas.
- **`secrets/` al `.gitignore`** ya puesto; versionado del proyecto diferido.
- **Heredadas:** URL de webhook de producción (P22); `request_id: "-"` en la ingesta; `retry_backoff` de Celery; `path_separator` en `alembic.ini`; drift de docs; `SecurityWarning` de Celery como root.

### Lo que sigue

- **P26 — escribir en Calendar (crear eventos):** la segunda mitad de la integración. "El mismo OAuth (ya resuelto) + el scope de escritura + el flujo de confirmación de la P24 + idempotencia". Crear un evento es la primera acción externa que pasa por el flujo de confirmación construido en P24 — y la idempotencia (P22-B) protege algo que de verdad cuesta deshacer.
- **P27 — Gmail** (mandar correo): lo irreversible-de-verdad. Aquí el routing entre varias tools externas (calendar vs gmail) hace la decisión del 14B más aguda.
- **Drive (post-P27):** "busca el documento X → Drive → PDF → RAG" — el agente trayendo documentos que luego indexa. Extiende el trípode a "buscar fuera".
- **Fase 3 — Interfaz:** frontend sobre la API, ahora con OAuth y confirmación como patrones que una UI mostraría bien.

---

## 9. Temario de estudio (Parte 25)

> Conceptos transferibles que esta parte enseña. Para repaso.

### A. OAuth 2.0 en la práctica

- **El flujo de tres patas (authorization code)**: app redirige → usuario autoriza → código → intercambio por tokens. Por qué la autorización es necesariamente humana (no la puede hacer un agente).
- **`access_type=offline` y el refresh token**: la diferencia entre una sesión de 1h y una integración duradera. El parámetro que decide si hay refresh.
- **Redirect para apps de escritorio (loopback)**: por qué el OOB murió, y cómo capturar el código de un redirect loopback sin servidor cuando la app está en contenedor.
- **El ciclo de vida del token**: access token caduca, refresh token renueva, refresco preventivo con margen. Probar la caducidad, no solo el login.

### B. Almacenamiento de credenciales dinámicas

- **Estático vs dinámico**: client_id/secret (estáticos → secrets) vs tokens (dinámicos → BD). Por qué el mecanismo de secretos de archivo no sirve para lo que cambia.
- **El UPSERT que preserva**: COALESCE para no borrar un refresh token que el proveedor solo entrega una vez. Conocer la semántica del proveedor para evitar bugs silenciosos.
- **Credenciales en reposo**: tokens de terceros en una tabla son credenciales; en producción querrían cifrado a nivel de app. La deuda de seguridad que se anota sin sobre-construir.

### C. Diagnóstico de capacidades del modelo

- **Routing vs síntesis como capacidades distintas**: elegir la tool correcta y narrar bien sus datos son habilidades separadas; un fallo en una no implica fallo en la otra, y un modelo más grande las ayuda de forma desigual.
- **Formato de datos vs incompetencia del modelo**: parte de una "síntesis pobre" puede ser datos mal presentados por la tool (eventos sin año). Revisar la capa de datos antes de atribuir el fallo al modelo — patrón recurrente (P19, P21).
- **Una decisión de infraestructura cara se afina con cada dato**: la pregunta del 14B no se cierra de golpe; cada parte (P24 routing, P25 síntesis) añade matiz sobre dónde un modelo mayor ayuda y dónde no.

---

*Cierre de la Parte 25. Primera mitad de la integración Google; falta escribir (P26).*
