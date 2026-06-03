# NexaAgent — Bitácora de desarrollo (Parte 12)

> Continuación de la Parte 11. Objetivo de esta sesión: **seguridad con JWT** —
> reemplazar el `x-api-key` estático por autenticación JWT Bearer, dejando el sistema
> listo para escenarios reales de despliegue (token con caducidad, firma criptográfica,
> rotación de claves).

**Estado al cierre de la Parte 12:** autenticación JWT funcionando. Endpoint `/auth/login` que valida una contraseña maestra y emite un JWT HS256 con caducidad de 24h. Todos los endpoints protegidos requieren `Authorization: Bearer <token>`. Verificado: login correcto → token, login fallido → 401, endpoint con Bearer válido → respuesta normal, Bearer inválido → 401, RAG y memoria intactos bajo la nueva auth.

---

## 1. La decisión de alcance: Modelo B (cliente único)

Antes de tocar código, hubo que distinguir dos modelos muy distintos:

| Modelo | Qué implica | Trabajo |
|---|---|---|
| **A — Multi-usuario** | Tabla `users`, contraseñas hasheadas, `user_id` en claims, filtrar TODAS las queries por usuario | Varias sesiones |
| **B — Cliente único** | Una sola credencial maestra → JWT firmado con caducidad. Sin aislamiento de datos. | Una sesión |

**Elección: Modelo B.** Razones:
- Es lo que el sistema realmente necesita hoy (no hay caso de uso multi-tenant).
- El sistema asume un usuario implícito en memoria, conversaciones y RAG; cambiar eso sin un caso de uso real sería sobre-ingeniería.
- Es un peldaño hacia el Modelo A si algún día se necesita (añadir tabla users + claim user_id + filtros, sin tirar nada).
- Aporta valor real sobre el x-api-key estático: caducidad, firma criptográfica, claims auditables, revocación por rotación de clave.

### Decisiones técnicas dentro del Modelo B

| Decisión | Elección | Motivo |
|---|---|---|
| Obtención del token | Endpoint `/auth/login` con credencial | Patrón canónico; permite futuras renovaciones |
| Algoritmo | HS256 (simétrico) | Correcto para un cliente único; RS256 es para servicios separados |
| Caducidad | 24 horas | Balance entre seguridad y comodidad de desarrollo |
| Refresh tokens | NO (v1) | Si expira, re-login; sin sistema de revocación granular |
| Hash de la password | NO | En `.env`, no en BD; comparación con `secrets.compare_digest` |
| Header | `Authorization: Bearer` (estándar) | Reemplaza `x-api-key`; mejor integración con Swagger |
| Librería | PyJWT (estándar de facto) | Ligero, mantenido, sin sobre-dependencias |

---

## 2. Las dos claves del sistema

Una distinción crucial que conviene tener clara:

| Variable | Para qué | Característica |
|---|---|---|
| `AUTH_PASSWORD` | Lo que el cliente envía a `/auth/login` | Memorizable/copiable a mano |
| `JWT_SECRET` | Firma criptográfica HS256 | Larga y aleatoria (256+ bits de entropía) |

`AUTH_PASSWORD` la teclea el usuario; `JWT_SECRET` no la teclea nadie nunca — vive en `.env` y la lee el servidor. Generar `JWT_SECRET` correctamente:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Si en el futuro se sospecha filtración: cambiar `JWT_SECRET` invalida AUTOMÁTICAMENTE todos los tokens emitidos. Esa es la propiedad de revocación que el x-api-key no podía dar.

---

## 3. Implementación

### `app/core/config.py` — campos nuevos, api_key eliminado

```python
auth_password: str = "change-me-in-env"
jwt_secret: str = "change-me-in-env-with-secrets-token-urlsafe-48"
jwt_algorithm: str = "HS256"
jwt_expires_hours: int = 24
```

El viejo `api_key` se quitó por completo — tener dos sistemas de auth coexistiendo es exactamente lo que crea confusión y agujeros.

### `app/core/security.py` — reemplazo completo

Dos funciones nuevas:

- **`create_access_token(extra_claims)`**: emite JWT HS256 con claims `iss`, `iat`, `exp`, `sub`. Devuelve `(token, expires_in_seconds)`.
- **`require_jwt`**: dependencia FastAPI usando `HTTPBearer(auto_error=True)`. Extrae el Bearer del header, valida firma, caducidad y formato; devuelve los claims o levanta 401 con `WWW-Authenticate: Bearer`. Distingue `ExpiredSignatureError` ("Token expired") de `InvalidTokenError` ("Invalid token") para mejor diagnóstico.

### `app/api/auth.py` — nuevo endpoint

```python
@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest):
    if not secrets.compare_digest(payload.password, settings.auth_password):
        raise HTTPException(401, "Invalid credentials")
    token, expires_in = create_access_token()
    return TokenResponse(access_token=token, expires_in=expires_in)
```

`secrets.compare_digest` para evitar timing attacks. La comparación tarda lo mismo sea cual sea el prefijo coincidente — sin esto, un atacante podría inferir bytes midiendo tiempos.

### Cambios en los routers existentes

`chat.py`, `documents.py`, `conversations.py`: una sola sustitución en cada uno:

```python
# antes:
from app.core.security import require_api_key
dependencies=[Depends(require_api_key)]
# ahora:
from app.core.security import require_jwt
dependencies=[Depends(require_jwt)]
```

Y `main.py` registra el router de auth: `app.include_router(auth.router)`.

### Swagger queda compatible

`HTTPBearer` declara el esquema correctamente en OpenAPI, así que el botón **Authorize** de `/docs` pide un Bearer token; pegas el access_token (sin la palabra "Bearer") y queda aplicado a todos los endpoints protegidos.

---

## 4. Errores encontrados

### Error 1 — `ModuleNotFoundError: No module named 'jwt'`

- **Síntoma:** la API no arrancaba tras pegar el código; el log mostró el ImportError de `jwt` en `security.py`.
- **Causa:** la línea `pyjwt==2.10.1` estaba en `pyproject.toml` pero el rebuild reusó la capa cacheada de `pip install`. Es el patrón ya conocido (Parte 5 con `psycopg2-binary`).
- **Solución:** `docker-compose build --no-cache api` + `up -d`.
- **Lección (reiterada):** al cambiar dependencias declaradas, SIEMPRE `--no-cache`. Y el `up -d` por sí solo nunca reconstruye.

### Confusión metodológica al probar (no es bug, es UX)

Al pegar las instrucciones de los tests, dejé `eyJhbGciOi...` como placeholder con puntos suspensivos. El usuario los copió literalmente al curl, recibiendo correctamente `{"detail":"Invalid token"}` — el sistema rechazó el token truncado, lo cual es comportamiento correcto. Lección para escribir instrucciones: usar marcadores explícitos tipo `<PEGA-TU-TOKEN-AQUI>` en vez de elipsis que parecen continuación.

---

## 5. Verificación

Cuatro tests cubren el ciclo completo:

| Test | Acción | Resultado |
|---|---|---|
| 1 | `POST /auth/login` con password correcta | 200 + token JWT firmado |
| 2 | `POST /auth/login` con password falsa | 401 `Invalid credentials` |
| 3 | `GET /conversations` con Bearer válido (token completo) | 200 + lista de 26 conversaciones |
| 4 | `GET /conversations` con Bearer inválido o truncado | 401 `Invalid token` |

Y la prueba de regresión: `POST /chat` con Bearer válido y la pregunta de Rubí → "Lucía Domínguez, de la dirección de Finanzas" ✓. El RAG, las herramientas, la memoria — todo sigue funcionando bajo la nueva autenticación.

Bonus observado en la lista de conversaciones: el historial muestra la evolución del proyecto a lo largo de las partes (conv 8-14 con respuestas incorrectas de Rubí, conv 16+ correctas tras el re-chunking). La base de datos se ha vuelto un registro arqueológico del proyecto, y la nueva auth lo expone limpiamente.

---

## 6. Aprendizajes clave

1. **"JWT" sin más es ambiguo: define el modelo primero.** Modelo A (multi-usuario, BD) vs Modelo B (cliente único, .env) son escalas de trabajo radicalmente distintas. Elegir mal aquí o lleva a sobre-ingeniar o a dejar agujeros. La pregunta clarificadora: "¿hay realmente usuarios distintos, o un solo cliente?".

2. **Dos secretos, dos propósitos.** `AUTH_PASSWORD` (humano-friendly, lo teclea el cliente) y `JWT_SECRET` (criptográfica, generada con CSPRNG, vive solo en `.env`). Confundirlas, p.ej. usando la misma para ambas, debilita la seguridad de los tokens.

3. **`secrets.compare_digest` evita timing attacks.** Comparación bit a bit en tiempo constante. Cuesta cero esfuerzo más que `==` y es la práctica correcta para credenciales.

4. **Higiene de credenciales: no compartirlas en chats / commits / capturas.** En el desarrollo del proyecto se compartió accidentalmente un token y una contraseña. Ambos siguen siendo válidos hasta su caducidad / rotación de la clave; rotar `JWT_SECRET` es la forma estándar de revocar TODOS los tokens emitidos.

5. **Rotación de `JWT_SECRET` = revocación instantánea de todos los tokens.** Ventaja real sobre el x-api-key, donde rotar la clave requería reconfigurar a todos los clientes a la vez. Con JWT, cambias la clave y el siguiente login devuelve un token válido bajo la clave nueva.

6. **Reutilizar lecciones técnicas previas.** El `ModuleNotFoundError` de PyJWT fue el mismo patrón que el psycopg2 de la Parte 5. La regla "al cambiar `pyproject.toml`, rebuild `--no-cache`" se está volviendo memoria muscular.

---

## 7. Comandos de referencia (nuevos de esta parte)

### Generar un JWT_SECRET fuerte

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

### Login (obtener token)

```powershell
curl.exe -X POST http://localhost:8000/auth/login -H "Content-Type: application/json" -d '{\"password\": \"TU_AUTH_PASSWORD\"}'
```

### Llamada a endpoint protegido con Bearer

```powershell
curl.exe http://localhost:8000/conversations -H "Authorization: Bearer TU_TOKEN_COMPLETO"
```

### Rotar JWT_SECRET (revoca todos los tokens)

```powershell
# 1. Generar nueva clave y reemplazar JWT_SECRET en .env
# 2. Reconstruir la api (sin necesidad de --no-cache, no cambia dependencias)
docker-compose up -d --build api
# 3. Cualquier token previo dará 401 "Invalid token" hasta nuevo login
```

---

## 8. Estado y siguientes pasos

### Funcionando ✅ (nuevo en la Parte 12)

- **Autenticación JWT Bearer end-to-end** (Modelo B, cliente único).
- `POST /auth/login` emite tokens HS256 firmados con caducidad de 24h.
- Todos los endpoints protegidos (`/chat`, `/chat/stream`, `/documents/*`, `/conversations/*`) requieren Bearer válido.
- Compatible con Swagger (botón Authorize, scheme bearer).
- Rechazo correcto de tokens expirados, mal firmados y mal formateados, con headers `WWW-Authenticate: Bearer` estándar.
- RAG, memoria, herramientas y streaming intactos bajo la nueva auth.

### Pendiente / deudas conocidas 🔜

- **Multi-usuario (Modelo A)** si en algún momento se necesita: tabla `users`, registro/login con contraseña hasheada (bcrypt), `user_id` en claims JWT, y refactor de todas las queries para filtrar por usuario.
- **Refresh tokens** si la UX requiere sesiones largas sin re-login (no urgente).
- **Lista de revocación** si se necesita revocar tokens individuales antes de su expiración natural (Redis con TTL=exp, comprobar en `require_jwt`).
- **Rate limiting** sobre `/auth/login` para mitigar ataques de fuerza bruta a la contraseña (FastAPI + `slowapi`).
- **`DELETE /conversations/{id}`** (opcional, limpiar pruebas).
- **Bug menor:** `OLLAMA_HOST=hhttp://` en `docker-compose.yml`.
- **Refactor opcional:** unificar el patrón "engine NullPool propio" (5 sitios).
- **Granularidad de extracción de memorias** (Parte 11): qwen2.5 a veces combina hechos.
- **Recordatorio Alembic:** cada `--autogenerate` futuro intentará borrar el full-text.

### Nota sobre el `x-api-key` removido

Si tienes scripts antiguos o documentación que usen `-H "x-api-key: ..."`, dejarán de funcionar. La migración: primero `POST /auth/login`, luego `-H "Authorization: Bearer <token>"`. Las bitácoras de partes anteriores conservan los comandos con x-api-key como registro histórico; al ejecutarlos hoy hay que sustituir por el flujo nuevo.

---

*Cierre de la Parte 12.*
