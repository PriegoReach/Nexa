# NexaAgent — Bitácora de desarrollo (Parte 16)

> Continuación de la Parte 15. Objetivo de esta sesión: **terminar el frente de observabilidad**
> que la Parte 15 dejó a medias — añadir **métricas** (`/metrics` en formato Prometheus),
> la **línea `request_end`** con latencia y status por petición, y un **`/health/ready`** de
> readiness que distingue dependencias críticas de degradables.

**Estado al cierre de la Parte 16:** observabilidad operativa completa. `/metrics` expone contador de requests, histograma de latencia y gauge de in-progress en formato de exposición Prometheus, hecho a mano (cero dependencias nuevas). Cada petición emite una línea `request_end` JSON con `duration_ms` y status, correlacionada con su `request_id`. `/health` (liveness) intacto y nuevo `/health/ready` que sondea DB/Redis/Ollama con criticidad diferenciada. Suite en 23 verdes (14 previos + 9 nuevos). La latencia que antes se leía a mano de los timestamps ahora es un dato estructurado y agregable.

---

## 1. Objetivo de la sesión

La Parte 15 cerró tests y resiliencia, y montó logging estructurado con correlación, pero dejó observabilidad coja: faltaban las **métricas**, el **`/health` de readiness** y el **middleware de latencia por request**. Esta sesión cierra ese hueco. El hilo es directo: en la Parte 15 la latencia (p. ej. el cold-load de ~39s) se capturaba **leyendo y restando timestamps a mano**; el objetivo aquí es que sea un dato estructurado, agregable y scrapeable.

Metodología de siempre: decisiones de diseño cerradas primero, y **no asumir** comportamiento de librerías — verificar con dato.

---

## 2. Decisiones de diseño (cerradas antes de teclear)

### 2.1 La decisión grande: `/metrics` a mano, sin `prometheus_client`

`prometheus_client` **no** está en `pyproject.toml`, y el proyecto fija versiones con `==` desde el Error 4 de la Parte 1. Sumando el aprendizaje #10 de la Parte 15 ("cero dependencias nuevas = sin rebuild `--no-cache`"), se decidió implementar `/metrics` **a mano**, en formato de exposición Prometheus. El precedente directo es el `JsonFormatter` de la Parte 15: si el logging JSON se hizo con la stdlib, las métricas también pueden hacerse sin dependencia. Coherencia con la filosofía del proyecto.

### 2.2 Tabla de decisiones

| Decisión | Elección | Motivo |
|---|---|---|
| Formato de métricas | Exposición Prometheus, a mano | Cero deps (sección 2.1) |
| Modelo de registro | Uno por proceso | `api` arranca con un único `uvicorn` (sin `--workers`) → un registro por proceso es justo el modelo de scrape de Prometheus; sin esto haría falta un colector multiproceso |
| Dónde medir latencia | **Dentro** del `RequestIDMiddleware` existente | Evita que la correlación dependa del orden de `add_middleware` (footgun; ver sección 4) |
| Etiqueta de ruta | Plantilla (`/conversations/{conversation_id}`), no cruda | Evita explosión de cardinalidad en Prometheus |
| Auth en sondas | `/metrics` y `/health/ready` **sin** auth | Convención de scrapers/sondas; en prod se protegen a nivel de red/ingress |
| Criticidad de readiness | DB y Ollama críticos; **Redis degradable** | Coherente con la degradación con gracia de Redis de la Parte 15 (paso 9) |

---

## 3. Las piezas

- **`app/core/metrics.py`** — registro hecho a mano: un **contador** de requests (por método/plantilla/status), un **histograma** de latencia (buckets cumulativos), un **gauge** de in-progress, y `render()` que emite el texto en formato Prometheus. Más `observe(method, route, status, duration)` para alimentarlo.
- **`RequestIDMiddleware` ampliado** — además de fijar/propagar el `request_id` (Parte 15), ahora cronometra la petición, emite la línea `request_end` y llama a `metrics.observe(...)` con la **plantilla** de ruta.
- **`memory.ping()`** — chequeo de readiness de Redis, **reusando** el pool `_redis()` ya existente en `memory.py` (no abre conexión nueva por sonda).
- **`health.py` ampliado** — `/health` (liveness) intacto; nuevo `/health/ready` que sondea DB/Redis/Ollama con criticidad.
- **`app/api/metrics.py`** — endpoint `GET /metrics`, incluido en `main.py`.

---

## 4. El hallazgo de diseño: por qué latencia y métricas van DENTRO del middleware de request_id

Un `BaseHTTPMiddleware` **resetea su contextvar al salir** del `dispatch` que lo fijó. Si la latencia y las métricas vivieran en un middleware **aparte**, la correlación del `request_end` con el `request_id` dependería del **orden de registro** de los middlewares (si el de latencia corre "por fuera" del de request_id, el contextvar ya se reseteó y el log saldría con `request_id: "-"`). Eso es un footgun silencioso: funcionaría o no según el orden, sin error visible.

**Solución:** plegar el cronometraje + el `request_end` + el `metrics.observe(...)` **dentro del mismo `RequestIDMiddleware`**, donde el contextvar está garantizadamente fijado. Así la correlación no depende de ningún orden. Es la misma familia de razonamiento que el muro de `BaseHTTPMiddleware` del paso (7) de la Parte 15 — entender cómo starlette maneja el ciclo del middleware evita un bug que no da traza.

---

## 5. La incógnita que un test convirtió en hecho: plantilla de ruta vs path crudo

Para etiquetar las métricas por ruta sin reventar la cardinalidad de Prometheus, hace falta la **plantilla** (`/conversations/{conversation_id}`), no el path crudo (`/conversations/37`, `/conversations/41`, ...). Cada path crudo sería una serie distinta → explosión de series → scrape inviable.

La plantilla vive en `scope["route"]`. **La incógnita:** ¿está `scope["route"]` poblado al volver de `call_next` en esta versión de Starlette? No se asumió. Se cerró con un test verde, **`test_metrics_uses_route_template_not_raw_path`**, usando `GET /conversations/{conversation_id}` (ruta parametrizada y protegida, el caso ideal para distinguir plantilla de cruda). El test pasó → `scope["route"]` SÍ está poblado tras `call_next` en esta versión → la plantilla funciona.

> **Para el yo del futuro:** este test es un canario. "`scope['route']` poblado tras `call_next`" es comportamiento de *esta* versión de Starlette; si algún día se sube Starlette, este test es lo que avisa si ese contrato cambió. No es cosmético — protege contra una explosión de cardinalidad silenciosa.

---

## 6. Readiness con criticidad diferenciada

`/health/ready` sondea las tres dependencias, pero **no todas tumban el readiness por igual** — y esa asimetría es deliberada, heredada de la resiliencia de la Parte 15:

| Dependencia | Criticidad | Si está caída |
|---|---|---|
| **PostgreSQL** | Crítica | `not ready` (503) — sin BD no hay nada |
| **Ollama** | Crítica | `not ready` (503) — sin inferencia el agente no responde |
| **Redis** | **Degradable** | `degraded` (200) — NO tumba el readiness |

Redis es degradable porque en la Parte 15 (paso 9) se construyó degradación con gracia explícita para su caída: la memoria de corto plazo degrada a historial vacío y el encolado no tumba la respuesta. Sería incoherente que el readiness reportara "not ready" por algo que el sistema está diseñado para sobrevivir. Por eso Redis caído ⇒ `degraded/200`, no `not ready/503`. DB y Ollama, en cambio, son condición necesaria para servir.

El probe de DB se ejercita **real** en los tests (la BD está garantizada en el servicio `tests`): `test_readiness_real_db` confirmó que `_ping_db` devuelve `up`. Los probes de Redis/Ollama se **mockean** en los tests, porque el servicio `tests` solo declara `depends_on: db` (no redis/ollama) — mockearlos es lo que los hace deterministas.

---

## 7. Verificación

### 7.1 Nota operativa (un tropiezo de entorno)

El primer sondeo se lanzó desde `x:\Nexa` y `docker compose ps` salió vacío con exit 1 — porque el `docker-compose.yml` vive en `x:\Nexa\nexaagent`. **Conclusión:** todos los comandos de compose necesitan `-f x:\Nexa\nexaagent\docker-compose.yml` (o lanzarse desde esa carpeta). No es un bug del sistema, es ubicación del manifiesto.

### 7.2 La suite (23 verdes)

```powershell
docker compose -f x:\Nexa\nexaagent\docker-compose.yml run --rm tests
```

Resultado: **23 passed, 1 warning in 2.67s** (14 previos + 9 nuevos). El único warning es el `path_separator` de Alembic, cabo suelto ya anotado en la Parte 15, ajeno a esto. Arranca solo `db` (healthy); no toca ollama/redis.

Los 9 nuevos cubren: readiness up/down/degraded (con probes mockeados + el de DB real), formato del texto Prometheus, y el de cardinalidad (`test_metrics_uses_route_template_not_raw_path`).

### 7.3 Render real del texto Prometheus + rutas cableadas

```powershell
docker compose -f x:\Nexa\nexaagent\docker-compose.yml run --rm --no-deps tests python -c "import app.main; from app.core import metrics; metrics.observe('POST','/chat',200,0.0314); metrics.observe('POST','/chat',503,39.2); metrics.observe('GET','/conversations/{conversation_id}',404,0.012); print('ROUTES_NEW:', [p for p in sorted(getattr(r,'path','') for r in app.main.app.routes) if p in ('/metrics','/health','/health/ready')]); print('---METRICS---'); print(metrics.render())"
```

Resultado: `ROUTES_NEW: ['/health', '/health/ready', '/metrics']` y el texto Prometheus completo, bien formado. **Buckets cumulativos correctos:** la observación de 39.2s cae en `le="60"` y `+Inf` (`sum=39.2314`, `count=2`), y la plantilla de ruta aparece en las etiquetas. Las tres rutas nuevas cableadas.

> **Decisión de buckets:** se extendieron los buckets más allá del tope de 10s por defecto. Motivo concreto: el cold-load del modelo a VRAM se midió en ~39s en la Parte 15. Con un tope en 10s, esa observación caería solo en `+Inf` y se perdería de vista justo en el rango que interesa vigilar (¿se está recargando el modelo seguido?). Que 39.2s caiga limpio en `le="60"` significa que los buckets reflejan la latencia real de **este** sistema, no un default genérico.

### 7.4 El artefacto estrella: la línea `request_end` real

Provocada con una petición sin token (401 — no toca DB/Ollama/Redis, así que mide el overhead del middleware puro sin ruido de dependencias) a través del stack ASGI:

```powershell
docker compose -f x:\Nexa\nexaagent\docker-compose.yml run --rm --no-deps tests python -c "import app.main; from starlette.testclient import TestClient; c=TestClient(app.main.app); r=c.post('/chat', json={'message':'hola'}, headers={'X-Request-ID':'demo-parte16'}); print('STATUS', r.status_code); print('RESP_HDR_REQID', r.headers.get('x-request-id'))"
```

Salida (la línea que cierra la verificación):

```json
{"ts":"…","level":"INFO","logger":"nexa.access","msg":"request end","request_id":"demo-parte16",
 "event":"request_end","method":"POST","path":"/chat","status":401,"duration_ms":3.36}
```

Correlacionada con el `X-Request-ID: demo-parte16` enviado — prueba de que el contextvar sigue fijado cuando se loguea (el motivo de plegarlo en el mismo middleware, sección 4). El header de respuesta también devuelve el id. (El `StarletteDeprecationWarning` es de la sonda con `TestClient`, no del código ni de la suite, que usa `ASGITransport`.)

---

## 8. Lo que NO queda perfecto (honestidad)

1. **Streaming `/chat`: la latencia medida es ≈ time-to-first-event, no la duración total del stream.** Por diseño de `BaseHTTPMiddleware`, el `request_end`/histograma cierran cuando el middleware ve la respuesta, que en streaming es el primer evento. El cold-start igual se ve (el primer token espera la carga del modelo). El `/chat` **no-stream** (el que se medía a mano en la P15) sí se captura completo. Es una limitación conocida, no un bug; medir la duración total del stream requeriría instrumentar dentro del generador.
2. **`/metrics` y `/health/ready` sin auth** — convención de sondas/scrapers; en prod se protegen a nivel de red/ingress.
3. **El gauge `in_progress` es por método, no por ruta** — la plantilla de ruta no se conoce *antes* del routing (solo al volver de `call_next`), y el gauge se incrementa al entrar. Por método es lo correcto dado ese orden.

---

## 9. Aprendizajes clave

1. **El modelo de despliegue determina el de métricas.** `api` con un único `uvicorn` (sin `--workers`) = un registro por proceso, que encaja directo con el scrape de Prometheus. Con múltiples workers habría hecho falta un colector multiproceso. Conocer cómo arranca tu propio servicio define la solución.

2. **Plegar la latencia en el middleware del request_id, no en uno aparte.** Un `BaseHTTPMiddleware` resetea su contextvar al salir; un middleware de latencia separado haría que la correlación dependiera del orden de registro — footgun silencioso. Dentro del mismo middleware, la correlación está garantizada.

3. **Plantilla de ruta, no path crudo, en las etiquetas de métricas.** El path crudo (`/conversations/37`, `/conversations/41`...) explota la cardinalidad de Prometheus. La plantilla (`/conversations/{conversation_id}`) la mantiene acotada. Que `scope["route"]` esté poblado tras `call_next` se **verificó con un test**, no se asumió — y ese test es el canario ante futuros upgrades de Starlette.

4. **Buckets de histograma calibrados a la latencia real del sistema.** El default de 10s habría enterrado el cold-load de ~39s en `+Inf`. Extender los buckets (hasta 60s+) es lo que hace que la métrica refleje la realidad medida en la P15, no un rango genérico.

5. **Readiness con criticidad diferenciada, coherente con la resiliencia previa.** Redis es degradable (caído ⇒ `degraded/200`) porque la P15 construyó degradación con gracia para él; DB y Ollama son críticos (`not ready/503`). El readiness debe reflejar qué está diseñado para sobrevivir el sistema, no marcar "not ready" por todo.

6. **Cero dependencias nuevas, de nuevo.** `/metrics` a mano (precedente: el `JsonFormatter` de la P15) evitó `prometheus_client` y el rebuild `--no-cache`. La filosofía del proyecto se sostiene una parte más.

7. **Mockear lo que el entorno de test no garantiza; ejercitar lo que sí.** El servicio `tests` solo trae `db`, así que los probes de Redis/Ollama se mockean (deterministas) y el de DB se ejercita real. Saber qué garantiza tu harness define qué se mockea.

---

## 10. Comandos de referencia (nuevos de esta parte)

### Ubicación del compose (lección operativa)

```powershell
# Todos los comandos de compose necesitan -f con la ruta, o lanzarse desde x:\Nexa\nexaagent
docker compose -f x:\Nexa\nexaagent\docker-compose.yml ps
```

### Correr la suite (23 tests)

```powershell
docker compose -f x:\Nexa\nexaagent\docker-compose.yml run --rm tests
```

### Render del texto Prometheus sin levantar dependencias

```powershell
docker compose -f x:\Nexa\nexaagent\docker-compose.yml run --rm --no-deps tests python -c "from app.core import metrics; metrics.observe('POST','/chat',200,0.03); print(metrics.render())"
```

### Endpoints nuevos (contra el stack en marcha)

```powershell
# Métricas (sin auth, para el scraper)
curl.exe http://localhost:8000/metrics

# Liveness (intacto desde antes)
curl.exe http://localhost:8000/health

# Readiness: DB/Ollama críticos, Redis degradable
curl.exe http://localhost:8000/health/ready
# 200 ready (todo up) · 200 degraded (Redis caído) · 503 not ready (DB u Ollama caídos)
```

### Ver la latencia por request en los logs

```powershell
# Cada petición emite una línea request_end con duration_ms y status, correlacionada
docker compose -f x:\Nexa\nexaagent\docker-compose.yml logs api | Select-String "request_end"
```

---

## 11. Estado y siguientes pasos

### Funcionando ✅ (nuevo en la Parte 16)

- **`/metrics`** en formato Prometheus, hecho a mano (contador de requests + histograma de latencia + gauge in-progress), un registro por proceso.
- **Línea `request_end`** por petición (JSON, con `method`/`path`/`status`/`duration_ms`), correlacionada con el `request_id`. La latencia dejó de leerse a mano.
- **`/health/ready`** con criticidad diferenciada (DB/Ollama críticos, Redis degradable); `/health` (liveness) intacto.
- **Etiquetas por plantilla de ruta** (cardinalidad acotada), verificado con test.
- **23 tests verdes** (14 + 9 nuevos de observabilidad).

### Deudas / pendientes 🔜

- **Latencia total del streaming:** el `request_end` mide time-to-first-event en `/chat/stream`, no la duración completa. Instrumentar dentro del generador si se necesita el dato total.
- **`/metrics` y `/health/ready` sin auth:** proteger a nivel de red/ingress en producción.
- **`request_id: "-"` en la ingesta:** la correlación de la tarea de ingesta sigue sin propagarse (deuda viva de la P15). La observabilidad del request está completa, pero la de la ingesta de fondo no — no darla por "completa" mientras este hueco persista.
- **`retry_backoff` de Celery** (`Retry in 0s`, P15) — deuda anotada.
- **Falso positivo de dedup** (Paco ≈ Bruno, P15) — recalibrar umbral o post-procesar.
- **Evaluación sistemática** — un harness de pares pregunta→respuesta-esperada con puntuación, para medir objetivamente cambios de prompt/chunking/modelo. El candidato de mayor valor de fondo; ahora más al alcance con la infra de tests + las métricas ya montadas.
- **Cabos sueltos menores:** `path_separator = os` en `alembic.ini`; comentario 401/403 en `security.py`; drift de docs (`.env.example`/README); `SecurityWarning` de Celery como root.
- **Saltos mayores:** re-ranking con cross-encoder (P10); herramientas con escritura + integraciones reales (el salto hacia "automatización empresarial" del objetivo original).

---

## 12. Temario de estudio (Parte 16)

> Conceptos transferibles que esta parte enseña. Para repaso.

### A. Métricas y formato Prometheus

- **Modelo de exposición Prometheus (pull/scrape)**: por qué "un registro por proceso" encaja con un `uvicorn` de un solo worker, y cuándo haría falta un colector multiproceso.
- **Los tres tipos básicos**: contador (monótono), histograma (con **buckets cumulativos**: `le="..."` y `+Inf`, `sum`, `count`) y gauge (sube y baja, p. ej. in-progress).
- **Calibrar buckets a la latencia real**: por qué el default genérico (tope ~10s) puede enterrar tu cola de interés (cold-load de ~39s) en `+Inf`.
- **Cardinalidad y plantilla de ruta**: por qué etiquetar con el path crudo (IDs incrustados) explota las series y cómo `scope["route"]` da la plantilla; el riesgo de cardinalidad alta en Prometheus.
- **Implementar el formato a mano**: viable sin `prometheus_client` (precedente: un formatter de logs JSON a mano), coherente con "cero deps".

### B. Observabilidad de requests

- **Latencia por petición como dato estructurado** (`request_end` con `duration_ms`/status) en vez de restar timestamps a mano.
- **Por qué cronometrar dentro del middleware que fija el contextvar**: el reset del contextvar al salir de un `BaseHTTPMiddleware` haría que un middleware de latencia separado dependiera del orden de registro (footgun).
- **Elegir una sonda sin efectos colaterales** (un 401 que no toca DB/Ollama/Redis) para medir overhead puro.

### C. Health checks

- **Liveness vs readiness**: `/health` (¿el proceso vive?) vs `/health/ready` (¿puede servir tráfico?).
- **Criticidad diferenciada de dependencias**: crítica (caída ⇒ `not ready`/503) vs degradable (caída ⇒ `degraded`/200), y por qué debe ser coherente con la degradación con gracia ya implementada (Redis degradable, DB/Ollama críticos).
- **Reusar conexiones existentes para las sondas** (`ping()` sobre el pool ya creado) en vez de abrir una nueva por chequeo.
- **Convención de auth en sondas**: `/metrics` y `/health/ready` sin auth, protegidos por red/ingress.

### D. Testing de observabilidad

- **Convertir una incógnita de librería en un hecho verificado**: el test `scope["route"]` poblado tras `call_next` como contrato fijado (y canario ante upgrades).
- **Mockear lo que el harness no garantiza, ejercitar lo que sí**: probes de Redis/Ollama mockeados (deterministas) vs probe de DB real.

### E. Metodología (transversal)

- **Ubicación del manifiesto de compose**: `-f <ruta>` o lanzar desde la carpeta correcta; un exit 1 con salida vacía suele ser eso.
- **No asumir comportamiento de librerías**: cerrar las incógnitas con tests verdes, no con fe (continuad de la P15).
- **Honestidad sobre los límites de la solución**: documentar que el streaming mide time-to-first-event, que el gauge es por método, que las sondas no llevan auth — para que el "yo del futuro" no los confunda con bugs.

---

*Cierre de la Parte 16.*
