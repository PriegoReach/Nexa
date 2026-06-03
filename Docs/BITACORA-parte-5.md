# NexaAgent — Bitácora de desarrollo (Parte 5)

> Continuación de la Parte 4. Objetivo de esta sesión: hacer el proyecto **reproducible y durable**
> — fijar versiones exactas de las dependencias y poner el esquema bajo control de Alembic,
> de modo que un `docker-compose down -v` ya no destruya el setup de full-text de la Parte 4.

**Estado al cierre de la Parte 5:** versiones congeladas en `pyproject.toml`. Esquema completo (tablas + extensiones + full-text) versionado en una migración de Alembic. Prueba de fuego superada: tras `down -v`, un solo `alembic upgrade head` reconstruye todo desde cero, incluido el full-text que antes se perdía.

---

## 1. Objetivo de la sesión

Dos deudas técnicas heredadas, ambas con la misma raíz (falta de reproducibilidad):

1. **Versiones con `>=`** — el `pyproject.toml` tenía todas las dependencias con rangos abiertos. Esa fue la causa del Error 4 de la Parte 1: `langchain>=0.2` trajo la 1.x y reorganizó la API.
2. **Setup de full-text efímero** — la extensión `unaccent`, la config `es_simple`, la columna `content_tsv` y el índice GIN (Parte 4) se crearon a mano por SQL. No estaban en ningún sitio versionado; un `down -v` los borraba y dejaba el retriever híbrido roto.

**Orden de trabajo:** versiones primero, Alembic después. Alembic genera y aplica migraciones apoyándose en SQLAlchemy y el resto del stack; si las versiones bailan, las migraciones no son reproducibles. Se congela el terreno y luego se construye encima.

---

## 2. Congelar versiones

### Diagnóstico: ver qué corre de verdad

```powershell
docker exec nexaagent-api-1 pip freeze
```

Esto da las versiones EXACTAS instaladas (lo que funciona hoy), distinto de lo que declare el manifiesto. De ahí salieron los números a fijar.

### El archivo equivocado (paso en falso documentado)

Primer intento: editar `requirements.txt`. **No funcionó** — el `Dockerfile` no usa `requirements.txt`, instala desde `pyproject.toml`:

```dockerfile
COPY pyproject.toml ./
RUN pip install --no-cache-dir . 
```

Lección: verificar CÓMO instala el Dockerfile antes de editar manifiestos. Tener dos archivos de dependencias (requirements.txt + pyproject.toml) es justo lo que causa esta confusión; se eliminó el requirements.txt para dejar una sola fuente de verdad.

### Estrategia de fijado

Fijar con `==` las dependencias **directas y críticas**, dejando que pip resuelva las transitivas. Se subieron a directas (y se fijaron) `langchain-core` y `langgraph`, que antes entraban como transitivas — son las que reorganizan API entre versiones mayores, así que dejarlas sin fijar reabría la puerta al Error 4.

```toml
dependencies = [
    "fastapi==0.136.3",
    "langchain==1.3.1",
    "langchain-core==1.4.0",
    "langgraph==1.2.1",
    "sqlalchemy[asyncio]==2.0.49",
    "asyncpg==0.31.0",
    "psycopg2-binary==2.9.10",   # NUEVO: driver sync para Alembic
    "alembic==1.18.4",
    # ... (resto fijado con == desde pip freeze)
]
```

### Verificación

```powershell
docker-compose build --no-cache api    # --no-cache: forzar relectura del manifiesto
docker-compose up -d
docker exec nexaagent-api-1 pip show langchain | findstr Version   # -> 1.3.1
```

> **Nota:** `--no-cache` es necesario. Un build normal puede reusar la capa cacheada de `pip install` y no notar el cambio en el manifiesto. Y `docker-compose up -d` por sí solo NO reconstruye: hay que correr `build` antes.

---

## 3. Alembic con baseline (base existente)

### La decisión: baseline, no empezar de cero

La base ya tenía esquema y datos. En vez de borrarla, se generó una migración inicial que describe el estado actual y se marcó como "ya aplicada" con `alembic stamp` — sin ejecutarla sobre la base viva.

### Lo que Alembic NO autogenera (el detalle clave del caso)

El autogenerador mira los modelos de SQLAlchemy, pero el setup de full-text de la Parte 4 NO sale de los modelos: la extensión `unaccent`, la config de texto `es_simple`, la columna generada `content_tsv` y el índice GIN. Todo eso se añadió A MANO a la migración con `op.execute(...)`. Por eso no se usó `--autogenerate`: se escribió la migración inicial completa y curada directamente.

### Orden correcto dentro de la migración

CRÍTICO: las extensiones y la config de texto van ANTES de las tablas, porque `document_chunks.embedding` usa el tipo `Vector` y `content_tsv` depende de que `es_simple` exista:

1. `CREATE EXTENSION vector` + `unaccent`
2. Config `es_simple` (con guarda `DO $$ IF NOT EXISTS ... $$` porque no hay `CREATE ... IF NOT EXISTS` para configs de texto)
3. `create_table` de las 4 tablas
4. Columna generada `content_tsv` + índice GIN (`op.execute`)

### env.py: Alembic síncrono sobre una app async

La app usa `postgresql+asyncpg://`. Alembic corre operaciones puntuales de esquema, no necesita async. El `env.py` toma `settings.database_url` y convierte `+asyncpg` -> `+psycopg2` en memoria. Por eso se añadió `psycopg2-binary` (wheel precompilado, no necesita compilar). La app sigue intacta con asyncpg.

```python
def _sync_url() -> str:
    return settings.database_url.replace("+asyncpg", "+psycopg2")
```

### Error encontrado: `ModuleNotFoundError: No module named 'psycopg2'`

- **Síntoma:** `alembic stamp` falló al crear el engine.
- **Causa:** psycopg2-binary estaba en el manifiesto pero no en la imagen (el build aún no lo había recogido — y al principio estaba en el archivo equivocado, requirements.txt).
- **Diagnóstico:** el traceback mostró que `env.py` funcionó hasta crear el engine (encontró la app, convirtió la URL) — solo faltaba el driver. `pip show psycopg2-binary` confirmó la ausencia.
- **Solución:** corregir el `pyproject.toml` (no el requirements.txt) y `docker-compose build --no-cache api`.

### Aplicación

```powershell
docker exec nexaagent-api-1 alembic stamp 0001_initial   # marca como aplicada SIN ejecutar
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "SELECT * FROM alembic_version;"   # -> 0001_initial
```

### Desactivar el create_all del arranque

El `lifespan` de `main.py` llamaba a `init_db()` (con `create_all`) en development. Con Alembic al mando, eso compite. Se dejó el `lifespan` sin el llamado; `init_db` queda en `session.py` solo como helper manual.

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # El esquema lo gestiona Alembic. init_db() ya no corre al arrancar.
    yield
```

---

## 4. La prueba de fuego

El escenario que antes de hoy rompía el sistema: destruir la base entera y reconstruirla solo con Alembic.

```powershell
docker-compose down -v          # borra TODOS los volúmenes (base + modelos Ollama)
docker-compose up -d            # primer arranque lento: re-descarga modelos (~6 min)
docker exec nexaagent-api-1 alembic upgrade head
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "\d document_chunks"
```

Resultado: la tabla `document_chunks` renació con la columna `content_tsv` (generada con `es_simple`) y el índice `idx_chunks_tsv` GIN — sin un solo comando SQL manual. Las extensiones `vector` y `unaccent` también presentes. Antes de hoy, ese mismo `down -v` dejaba el sistema con tablas pero sin full-text.

---

## 5. Aprendizajes clave

1. **Verificar CÓMO instala el Dockerfile antes de editar manifiestos.** Se perdió tiempo editando `requirements.txt` cuando el proyecto instala desde `pyproject.toml`. Tener dos manifiestos es una trampa; mantener uno solo.

2. **Rangos `>=` en dependencias = bomba de tiempo.** Es la causa raíz del Error 4 de la Parte 1. Fijar con `==` las directas críticas (sobre todo el stack que reorganiza API entre mayores, como LangChain) hace los builds reproducibles.

3. **`--no-cache` al cambiar dependencias, y `build` antes de `up`.** `up -d` no reconstruye; un `build` sin `--no-cache` puede reusar la capa de `pip install` cacheada e ignorar el manifiesto nuevo.

4. **Alembic no autogenera todo.** Extensiones, configs de texto y columnas generadas no salen de los modelos de SQLAlchemy; hay que añadirlas a mano con `op.execute`. Para una base con SQL custom, escribir la migración inicial a mano es más fiable que `--autogenerate`.

5. **El orden importa en la migración.** Extensiones y tipos custom (Vector) antes de las tablas que los usan; columna generada después de que exista su config de texto.

6. **`stamp` integra una base existente sin tocarla.** Para adoptar Alembic en un proyecto con datos, `stamp` marca el estado actual como aplicado sin ejecutar la migración. El `upgrade` real solo se ejecuta sobre bases vacías.

7. **Alembic síncrono basta para una app async.** Convertir la URL a psycopg2 en el `env.py` evita la complejidad del modo async para operaciones de esquema puntuales. La app no cambia.

---

## 6. Comandos de referencia (nuevos de esta parte)

### Ver versiones instaladas (la verdad, no el manifiesto)

```powershell
docker exec nexaagent-api-1 pip freeze
docker exec nexaagent-api-1 pip show langchain | findstr Version
```

### Reconstruir tras cambiar dependencias

```powershell
docker-compose build --no-cache api    # forzar relectura del pyproject.toml
docker-compose up -d
```

### Alembic

```powershell
# Integrar base existente (marca como aplicada, NO ejecuta)
docker exec nexaagent-api-1 alembic stamp 0001_initial

# Aplicar migraciones sobre base vacía
docker exec nexaagent-api-1 alembic upgrade head

# Ver versión actual de la base
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "SELECT * FROM alembic_version;"

# (futuro) generar una nueva migración tras cambiar modelos
docker exec nexaagent-api-1 alembic revision --autogenerate -m "descripcion"
```

### Verificar reproducibilidad total (prueba de fuego)

```powershell
docker-compose down -v
docker-compose up -d
docker exec nexaagent-api-1 alembic upgrade head
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "\d document_chunks"
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "SELECT extname FROM pg_extension WHERE extname IN ('vector','unaccent');"
```

---

## 7. Estado y siguientes pasos

### Funcionando ✅ (nuevo en la Parte 5)

- **Versiones fijas** en `pyproject.toml`; builds reproducibles. Stack LangChain clavado (causa del Error 4 neutralizada).
- **Esquema completo bajo Alembic:** tablas + extensiones + full-text en una migración versionada.
- **Reproducibilidad total verificada:** `down -v` + `alembic upgrade head` reconstruye el sistema entero, full-text incluido, sin SQL manual.
- `create_all` del arranque desactivado; Alembic es la única autoridad del esquema.

### Pendiente de probar / construir 🔜 (heredado y nuevo)

- **Futuras migraciones:** ahora que el baseline está, los cambios de modelo se hacen con `alembic revision --autogenerate` + revisar el archivo generado. Recordar que el full-text custom no se autogenera.
- **Re-chunking consciente de estructura** (no partir entidades entre chunks).
- **Memoria de largo plazo:** resúmenes recuperables por similitud vectorial.
- **Endpoint para listar conversaciones.**
- **Streaming de respuestas (SSE).**
- **Seguridad de producción:** reemplazar el `x-api-key` simple por OAuth2/JWT.
- **Bug menor pendiente:** en `docker-compose.yml`, `init-ollama` tiene `OLLAMA_HOST=hhttp://...` (una "h" de más). Funciona solo porque el script exporta su propio OLLAMA_HOST; conviene corregirlo.
- **Refactor opcional:** unificar el patrón "engine NullPool propio" en un helper compartido (`ingest.py` + `retriever.py`).

---

*Cierre de la Parte 5.*
