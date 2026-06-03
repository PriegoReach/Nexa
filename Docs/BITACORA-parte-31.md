# NexaAgent — Bitácora de desarrollo (Parte 31)

> Arranque de la **Fase 3 (Interfaz)**. Tras cerrar el backend completo (Fase 2 + Drive +
> cross-lingual), el sistema dejaba de tener una cara: todo era `curl`. Esta parte construye el
> **MVP de una interfaz web** — un frontend **separado** del backend, que se comunica solo por
> la API REST. Fiel al método del proyecto ("create_task antes que send_email"), el MVP es lo
> mínimo que prueba el camino de extremo a extremo: **login + chat síncrono** funcionando contra
> la API real, antes de añadir lo rico (streaming, confirmación visual, OAuth desde la UI).

**Estado al cierre de la Parte 31:** el MVP conecta de extremo a extremo. Frontend separado (React + Vite + TypeScript) en `X:\Nexa\frontend` (hermano de `nexaagent/`, NO dentro), hablando con la API en Docker solo por HTTP/JSON. **CORS** —el prerequisito absoluto que no existía— añadido a FastAPI con orígenes explícitos (nunca `["*"]` con credenciales), y verificado: el preflight OPTIONS pasa, los headers CORS aparecen incluso en los 401. **Login** contra `POST /auth/login` (el campo es `access_token`, confirmado leyendo el schema, no asumido) con el JWT en estado de React (no localStorage). **Chat** contra `POST /chat` ({message} → {conversation_id, answer}, confirmado en el código) con indicador de carga (el LLM tarda segundos). Verificado a nivel de protocolo (curl con header `Origin`): login OK, chat respondió, cero errores de CORS. Estética **minimalist** aplicada (Geist + paleta monocroma cálida, sin el "morado IA"). La confirmación visual final queda para mañana, al abrir el navegador.

---

## 1. El reencuadre: qué eran las "skills de diseño"

La sesión arrancó con un malentendido aclarado: las "skills de diseño" mencionadas no eran un sistema de diseño propio ya implementado, sino **skills de Claude Code para *generar* frontend** (`minimalist-ui`, `industrial-brutalist-ui`, `high-end-visual-design`, `design-taste-frontend`, etc., en `X:\Nexa\.agents\skills`). Eso definió la Fase 3 como **construcción desde cero** — pero con asistencia de estas skills para que el resultado no sea genérico, no como "conectar un diseño que ya existe".

---

## 2. Las decisiones de arranque (cerradas antes de teclear)

| Decisión | Cierre | Motivo |
|---|---|---|
| Arquitectura | **Frontend separado** (SPA + API REST) | Lo profesional; defendible en entrevista. El frontend en su carpeta, comunicación solo por HTTP/JSON |
| Prerequisito | **CORS en FastAPI** (no existía) | Bloqueante absoluto: sin él, el navegador bloquea toda llamada del frontend separado |
| Stack | React + Vite + TypeScript | El estándar para SPA; TS encaja con el backend tipado (Pydantic) |
| Dónde corre (dev) | **Local** (`npm run dev`, Vite hot-reload) | Iterar UI dockerizado es engorroso; el hot-reload de Vite es parte de la agilidad frontend |
| Estética | **`minimalist-ui` + `design-taste-frontend`** | Sobrio = confianza; encaja con un agente que ejecuta acciones serias (ver §5) |
| Alcance | **MVP: login + chat síncrono** | Prueba CORS + auth + request/response; lo rico viene después |

**La elección de estética, razonada (no por capricho).** Se descartaron las alternativas con argumento: el **brutalismo** grita estilo-sobre-función y choca con algo que ejecuta acciones irreversibles (confirmar un correo no debe sentirse como una terminal hostil); el **high-end** puede leerse como producto de marketing y restar a una pieza de ingeniería seria. **Minimalista** ganó porque *es* la filosofía del proyecto hecha visual: NexaAgent actúa con cuidado (confirma lo irreversible, falla hacia el lado seguro), y una interfaz sobria y clara transmite control y confianza, no espectáculo. Para el portafolio dice lo correcto: "sé construir algo profesional sin esconder la ingeniería detrás de efectos". En una frase: **debe sentirse como una herramienta en la que confías para actuar en tu nombre.**

---

## 3. CORS — el prerequisito absoluto

El backend NO tenía CORS (solo `RequestIDMiddleware` — todo había sido `curl`, que no lo necesita). Para un frontend separado es un **bloqueante total**: en cuanto el frontend (en `localhost:5173`) llama a la API (`localhost:8000`), el navegador **bloquea la llamada por la política de mismo-origen, antes de que el request salga**. Y falla de forma confusa — parece que el frontend está roto cuando el problema es el backend.

`CORSMiddleware` añadido en `main.py`, con tres decisiones de cuidado:

1. **Orígenes explícitos** (`http://localhost:5173` + `http://127.0.0.1:5173`), en una constante de config (`cors_origins`), **nunca `["*"]`** — porque va con `allow_credentials=True`, y esa combinación es inválida por spec (y un riesgo de seguridad).
2. **CORS después de `RequestIDMiddleware`**, a propósito: en Starlette el último middleware añadido queda como el más externo, así CORS atiende el preflight OPTIONS **antes** de tocar nada y pone los headers en toda respuesta — **incluidos los errores** (verificado: un 401 también los lleva, lo que importa para que el frontend distinga "credencial mala" de "CORS roto").
3. **Headers reflejados** (`authorization`, `content-type`) para que el JWT pase en el preflight.

Verificación con curl simulando el navegador: `OPTIONS /chat` (preflight con `Origin` + `Access-Control-Request-Headers: authorization`) → 200 con `access-control-allow-origin: http://localhost:5173` y `allow-headers` reflejados. Un `POST /auth/login` con `Origin` en caso 401 → el header CORS sigue presente.

---

## 4. El frontend separado y el MVP

### La separación, concreta

`X:\Nexa\frontend` (hermano de `nexaagent/`, **no dentro**). El código Python y el del frontend no se mezclan; se comunican **solo por la API REST**. Estructura mínima y ordenada (sin sobre-ingeniería para un MVP):

```
X:\Nexa\frontend\
  .env                  VITE_API_URL=http://localhost:8000
  vite.config.ts        puerto 5173 fijo (strictPort)
  src\
    config.ts           única fuente de la URL base
    api.ts              login() + sendChat() + ApiError (con status)
    App.tsx             JWT en estado de React (NO localStorage)
    components\Login.tsx
    components\Chat.tsx
    styles\global.css
```

**`strictPort: true` en Vite** — decisión fail-safe: si 5173 estuviera ocupado y Vite saltara a 5174, el CORS configurado para 5173 fallaría *en silencio*. Mejor que Vite falle ruidoso. Es el principio del proyecto (fallar visible, no romper callado) aplicado al frontend.

### Login y chat — los shapes confirmados, no asumidos

Se leyeron los schemas del backend antes de codear el cliente (el "confirmado, no supuesto" del proyecto, cruzado al frontend):

- `POST /auth/login` → `{password}` → `{access_token, token_type, expires_in}` — el campo es **`access_token`** (no `token`; el error fácil, evitado por leer).
- `POST /chat` → `{message, conversation_id?}` + `Authorization: Bearer` → `{conversation_id, answer}` — confirmado en el código.

Decisiones del MVP:
- **JWT en estado de React, no localStorage** — más simple para el MVP, y evita el riesgo de seguridad de tokens en localStorage (XSS). Se pierde al recargar, a propósito; la persistencia de sesión (forma correcta: httpOnly cookies) es trabajo futuro si se quiere.
- **401 en `/chat` → vuelve al login** con aviso "sesión expirada" (`api.ts` marca el status, `App.tsx` enruta).
- **Indicador de carga** que no congela la UI — el backend tarda segundos (la generación del LLM); un indicador de "pensando" animado es la diferencia entre "lento pero funciona" y "parece colgado".
- **TypeScript estricto**, `tsc -b` limpio (con el layout oficial de 3 tsconfig de Vite para que `composite` no choque con `noEmit`).

### La verificación, honesta sobre su alcance

Probado a **nivel de protocolo** (curl / Invoke-WebRequest con header `Origin` — lo que el navegador hace a nivel HTTP), no clicando en un navegador (no hay GUI headless aquí):

```
LOGIN OK  -> token_type=bearer  expires_in=86400s  token_len=172
=== POST /chat (Origin + Bearer JWT) ===
CHAT HTTP 200
Access-Control-Allow-Origin: http://localhost:5173
conversation_id = 126
answer = ¡Hola! ¿En qué puedo ayudarte hoy?
```

Esa verificación **es** el test crítico que importaba: el preflight pasa, el JWT viaja en `Authorization` y se acepta, el ciclo mensaje→respuesta funciona, cero errores de CORS. Lo que queda para mañana (al abrir el navegador) es la confirmación **visual** — que la estética se vea como se diseñó. Distinguir "probé el protocolo" de "vi la pantalla" es honestidad de método (los acentos salieron como `Â¡Hola!` en PowerShell por encoding del terminal; el JSON es UTF-8 correcto y `fetch().json()` lo decodifica bien en el navegador).

---

## 5. La estética aplicada (minimalist + design-taste)

Se leyeron ambas skills y se resolvió su tensión: para una UI de software seria mandan las reglas de `design-taste-frontend` (serif prohibida en dashboards), así que **Geist** (una sans con carácter, vía Google Fonts, fallback a stack de sistema) + **Geist Mono** para metadatos.

- **Paleta monocroma cálida:** lienzo hueso `#F7F6F3`, superficies blancas, texto carbón `#2F3437` (nunca negro puro), bordes `#E7E6E1`. Un solo acento: tinta casi-negra `#1D1D1B` para la acción primaria. **Sin gradientes, sin sombras pesadas, sin el "morado IA".**
- **Login:** tarjeta estrecha centrada, wordmark "Nexa" + eyebrow mono `AGENTE · v0.1`, botón sólido oscuro, error inline en rojo pastel.
- **Chat:** header con punto de estado "en línea" que respira, hilo a `max-w 720px`, mensajes de usuario en burbuja a la derecha, respuestas del agente como documento con etiqueta `NEXA` mono a la izquierda. Composer con textarea que crece, hint con teclas `<kbd>`. Estado vacío compuesto, no en blanco.
- **Motion casi invisible:** fade-up de mensajes, dots de "pensando" en vez de spinner genérico, `scale(0.98)` al pulsar, `prefers-reduced-motion` respetado.
- **Sin emojis, sin librerías de iconos:** SVG primitivos inline con trazo uniforme.

Es la sensación que se buscaba: sobrio, serio, confiable — una herramienta, no una demo llamativa.

---

## 6. Aprendizajes clave

1. **Frontend separado = CORS es el primer prerequisito, no un detalle.** Sin CORS, el navegador bloquea toda llamada antes de que salga, y falla de forma confusa (parece el frontend roto, es el backend). Resolverlo *antes* de la primera línea de UI evita pasar la sesión depurando el síntoma equivocado.

2. **El orden de los middlewares decide qué atiende el preflight.** CORS debe quedar como el más externo (último añadido en Starlette) para atender el OPTIONS antes de nada y poner headers incluso en errores (un 401 con CORS deja al frontend distinguir "credencial mala" de "CORS roto").

3. **"Confirmado, no supuesto" cruza al frontend.** Leer los schemas del backend (`access_token`, no `token`) antes de codear el cliente evita el error más fácil de un frontend que consume una API. La disciplina del backend aplica igual al consumir.

4. **`strictPort` es fail-safe.** Si Vite saltara de puerto, el CORS configurado fallaría en silencio. Fallar ruidoso (Vite no arranca) > romper callado (CORS bloquea sin avisar). El principio del proyecto, en el frontend.

5. **El MVP prueba el camino, no la apariencia.** Login + chat síncrono prueba las tres cosas que pueden fallar en un frontend separado (CORS, auth, request/response). Construir todo de una vez mezclaría un fallo de CORS con uno de streaming. La apariencia se valida después.

6. **La estética debe servir a qué es el producto.** Un agente que ejecuta acciones irreversibles necesita transmitir confianza y calma — minimalista, no brutalista ni high-end. La elección de diseño es una decisión de carácter, razonada desde qué hace el sistema, no un gusto arbitrario.

7. **Verificación a nivel de protocolo vs visual — distinguirlas.** Probar el ciclo HTTP (preflight, JWT, respuesta) con curl es el test crítico de que el frontend *puede* hablar con el backend; ver la pantalla es un test distinto (la apariencia). Ser honesto sobre cuál se hizo.

---

## 7. Comandos de referencia (nuevos de esta parte)

### Arrancar el frontend (desarrollo)

```powershell
cd X:\Nexa\frontend
npm run dev
# abre http://localhost:5173 (puerto fijo con strictPort)
```

### Verificar CORS desde la línea de comandos (simular el navegador)

```powershell
# preflight OPTIONS — debe devolver los headers CORS
curl.exe -X OPTIONS http://localhost:8000/chat -H "Origin: http://localhost:5173" -H "Access-Control-Request-Method: POST" -H "Access-Control-Request-Headers: authorization,content-type" -i
# debe verse: access-control-allow-origin: http://localhost:5173
```

### Recargar el backend tras añadir CORS

```powershell
docker compose restart api   # el proceso vivo tiene el main.py viejo en memoria (Uvicorn sin --reload)
```

---

## 8. Estado y siguientes pasos

### Funcionando ✅ (nuevo en la Parte 31)

- **MVP de interfaz end-to-end:** frontend separado (React+Vite+TS) en `X:\Nexa\frontend`, hablando con la API en Docker por HTTP/JSON.
- **CORS en FastAPI:** orígenes explícitos, después de RequestIDMiddleware, headers en errores incluidos. Verificado por preflight.
- **Login** (`/auth/login`, `access_token` confirmado) con JWT en estado de React.
- **Chat síncrono** (`/chat`, shapes confirmados) con indicador de carga y manejo de 401→login.
- **Estética minimalist** (Geist + monocromo cálido) aplicada.

### Deudas / pendientes 🔜

- **Confirmación visual de la UI** (mañana): abrir el navegador y verificar que la estética se ve como se diseñó (la verificación de hoy fue a nivel de protocolo, no visual).
- **Persistencia de sesión:** el JWT se pierde al recargar (estado de React). Si se quiere persistir, la forma correcta (httpOnly cookies) es trabajo futuro — NO localStorage.
- **Heredadas del backend:** harness `dual_validation.py` apunta a `FETCH_N=20` (P30); doc #23 con chunking anómalo; el 14B (tres señales en contra); clasificador de fallos de envío; OOM del host; tokens en claro; etc.

### Lo que sigue (la Fase 3, iterando sobre la base que conecta)

- **P32 — streaming visible** (candidato fuerte): `POST /chat/stream` ya emite `tool_start`/`tool_end` que NUNCA tuvieron UI (anotado desde P27/P28). Mostrar "el agente está usando X herramienta" es justo lo que hace que se *vea* como un agente, no un chatbot. La iteración de mayor valor visual.
- **Confirmación visual de acciones:** el flujo propone→sí/no (P24/P27) podría ser una tarjeta con botones Sí/No en vez de teclear "sí" — más seguro (clic explícito) y más claro. Refleja el patrón más rico del backend.
- **Historial de conversaciones:** `GET /conversations` + `/{id}` + `DELETE` — navegar y retomar conversaciones.
- **OAuth desde la UI:** el flujo `/oauth/google/{start,callback,status}` con una pantalla en vez de curl.
- **Subida de documentos:** `POST /documents/upload` desde la UI.
- **Dockerizar el frontend** (producción): cuando la UI esté madura, un servicio en compose para desplegarla.

---

## 9. Temario de estudio (Parte 31)

> Conceptos transferibles que esta parte enseña. Para repaso.

### A. Frontend separado de backend

- **La separación como arquitectura**: el frontend en su propio proyecto, comunicación solo por API REST. El frontend no sabe del backend más allá del contrato HTTP/JSON. Defendible y mantenible.
- **CORS como prerequisito, no detalle**: la política de mismo-origen bloquea un frontend separado sin CORS; resolverlo primero evita depurar el síntoma equivocado (parece el frontend, es el backend).
- **El orden de los middlewares**: CORS como el más externo atiende el preflight antes de nada y pone headers en errores; importa para que el frontend distinga tipos de fallo.
- **Orígenes explícitos vs comodín**: `["*"]` con credenciales es inválido por spec; listar el origen es lo correcto y seguro.

### B. Consumir una API con disciplina

- **Leer los schemas, no asumir**: confirmar los nombres de campos (`access_token` vs `token`) en el código del backend antes de codear el cliente. El "confirmado, no supuesto" aplicado al frontend.
- **JWT en estado vs localStorage**: estado de memoria es más simple y evita XSS para un MVP; la persistencia (httpOnly cookies) es una decisión aparte, no localStorage.
- **Manejar la latencia del backend**: un indicador de carga cuando el servidor tarda (generación LLM) — la diferencia entre "lento pero funciona" y "parece colgado".

### C. MVP y método

- **El MVP prueba el camino, no la apariencia**: lo mínimo que ejercita los puntos de fallo (CORS, auth, ciclo request/response) end-to-end, antes de añadir features ricas. "create_task antes que send_email" en el frontend.
- **Fail-safe en el frontend también** (`strictPort`): fallar ruidoso (Vite no arranca) en vez de romper callado (CORS bloquea sin avisar). El principio del backend, transferido.
- **Verificación a nivel de protocolo vs visual**: probar el ciclo HTTP es el test crítico de conectividad; ver la pantalla es otro test (apariencia). Distinguir cuál se hizo.

### D. Diseño con intención

- **La estética sirve a qué es el producto**: un agente que ejecuta acciones irreversibles transmite confianza con minimalismo, no con brutalismo o pulido excesivo. La decisión de diseño se razona desde la función, no el gusto.
- **Resolver la tensión entre guías de diseño**: cuando dos skills chocan (serif vs no-serif en dashboard), la regla del contexto (software serio) decide. Aplicar las guías con criterio, no al pie de la letra.

---

*Cierre de la Parte 31. Arranque de la Fase 3: el MVP de interfaz conecta de extremo a extremo; la cara del sistema, empezada.*
