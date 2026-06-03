# NexaAgent — Bitácora de desarrollo (Parte 1)

> Agente de IA autónomo para automatización empresarial: memoria, herramientas y RAG,
> sobre FastAPI + PostgreSQL (pgvector) + Redis + Celery, ejecutándose 100% local con Ollama.

**Estado al cierre de la Parte 1:** stack completo levantado y funcionando. Chat con memoria de corto plazo operativo. RAG implementado pero aún sin probar end-to-end.

---

## 1. Objetivo del proyecto

Construir un agente IA empresarial con:

- Memoria (corto y largo plazo)
- Herramientas (tools) que el agente puede invocar
- RAG (recuperación de contexto desde documentos)
- Integración con APIs externas y procesamiento de documentos
- Arquitectura escalable: FastAPI + PostgreSQL + Redis
- Despliegue con Docker

**Decisión clave:** se eligió correr todo en local con **Ollama** en lugar de OpenAI, para eliminar costos de API y no depender de claves externas durante el desarrollo.

---

## 2. Arquitectura

| Componente | Rol |
|---|---|
| **FastAPI** | Capa de API async (`/chat`, `/documents`, `/health`) |
| **Agente (LangChain)** | Orquestador tool-calling sobre modelos Ollama |
| **Memoria** | Historial de conversación de corto plazo en Redis (TTL 24h) |
| **Herramientas** | Búsqueda en base de conocimiento (RAG) + peticiones HTTP |
| **RAG** | Documentos troceados, embebidos y guardados en pgvector |
| **PostgreSQL + pgvector** | Conversaciones, mensajes, documentos y vectores |
| **Redis** | Caché, memoria de corto plazo y broker de Celery |
| **Celery worker** | Procesa la ingesta pesada de documentos fuera del request |
| **Ollama** | Inferencia local del LLM (llama3.1 + nomic-embed-text) |

**Modelos elegidos:**

- Chat: `llama3.1` (8B parámetros, ~4.7 GB)
- Embeddings: `nomic-embed-text` (768 dimensiones)

---

## 3. Configuración (.env)

```dotenv
APP_NAME=NexaAgent
ENVIRONMENT=development
API_KEY=<API_KEY>

OLLAMA_BASE_URL=http://ollama:11434
OLLAMA_MODEL=llama3.1
OLLAMA_EMBEDDING_MODEL=nomic-embed-text
EMBEDDING_DIM=768

POSTGRES_USER=nexa
POSTGRES_PASSWORD=<POSTGRES_PASSWORD>
POSTGRES_DB=nexaagent
DATABASE_URL=postgresql+asyncpg://nexa:<POSTGRES_PASSWORD>@db:5432/nexaagent

REDIS_URL=redis://redis:6379/0
CELERY_BROKER_URL=redis://redis:6379/1
```

> **Nota sobre `EMBEDDING_DIM`:** depende ÚNICAMENTE del modelo de embeddings, no del de chat.
> `nomic-embed-text` produce vectores de 768 dimensiones. Si se cambia el modelo de embeddings,
> hay que ajustar este número Y recrear la base de datos (`docker-compose down -v`).

---

## 4. Errores encontrados y soluciones

Esta es la sección más valiosa para el futuro. Cada error real que apareció al levantar el proyecto, con su causa y arreglo.

### Error 1 — `database "nexa" does not exist`

```
db-1 | FATAL: database "nexa" does not exist
```

- **Causa:** el healthcheck de Postgres usaba `pg_isready -U nexa` sin especificar la base. Sin `-d`, `pg_isready` intenta conectarse a una base con el mismo nombre del usuario (`nexa`), que no existe — la base se llama `nexaagent`.
- **Solución:** añadir `-d` al healthcheck en `docker-compose.yml`:

```yaml
test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-nexa} -d ${POSTGRES_DB:-nexaagent}"]
```

- **Nota:** la base `nexaagent` sí se creaba bien; era solo el healthcheck reintentando. No afectaba los datos.

### Error 2 — Ollama marcado como "unhealthy"

```
dependency failed to start: container nexaagent-ollama-1 is unhealthy
```

- **Causa:** el healthcheck usaba `curl`, pero la imagen oficial `ollama/ollama` **no trae curl instalado**. El comando fallaba siempre y bloqueaba a todos los servicios dependientes.
- **Solución:** usar el CLI propio de Ollama, que siempre está disponible:

```yaml
healthcheck:
  test: ["CMD", "ollama", "list"]
  interval: 10s
  timeout: 5s
  retries: 5
  start_period: 10s
```

### Error 3 — `init-ollama` colgado en "Waiting for Ollama to start..."

- **Causa doble:**
  1. El script `init-ollama.sh` también usaba `curl` para esperar.
  2. El CLI `ollama` dentro del contenedor `init-ollama` apuntaba a `localhost` (su propio contenedor, sin servidor) en vez de al contenedor `ollama`.
- **Solución:** en `init-ollama.sh`, esperar con `ollama list` y exportar `OLLAMA_HOST` explícitamente al inicio del script:

```bash
#!/bin/bash
export OLLAMA_HOST=http://ollama:11434

echo "Waiting for Ollama to start..."
until ollama list >/dev/null 2>&1; do
  sleep 2
done
ollama pull ${OLLAMA_MODEL:-llama3.2}
ollama pull ${OLLAMA_EMBEDDING_MODEL:-nomic-embed-text}
```

### Error 4 — `ImportError: cannot import name 'AgentExecutor'`

```
ImportError: cannot import name 'AgentExecutor' from 'langchain.agents'
```

- **Causa:** se instaló **LangChain 1.3.1**, que reorganizó toda la API. `AgentExecutor` y `create_tool_calling_agent` ya no existen; ahora se usa `create_agent` (corre sobre LangGraph internamente).
- **Solución:** reescribir `orchestrator.py` usando la API nueva:

```python
from langchain.agents import create_agent

def _build_agent():
    llm = ChatOllama(model=settings.ollama_model, base_url=settings.ollama_base_url, temperature=0)
    return create_agent(model=llm, tools=get_tools(), system_prompt=SYSTEM_PROMPT)
```

### Error 5 — `create_agent() got an unexpected keyword argument 'prompt'`

- **Causa:** en LangChain 1.3 el parámetro se llama `system_prompt`, no `prompt`.
- **Solución:** cambiar `prompt=SYSTEM_PROMPT` por `system_prompt=SYSTEM_PROMPT`.

### Error 6 — "Internal Server Error" en `/chat`

- **Causa:** consecuencia del Error 5; el agente fallaba al construirse.
- **Solución:** el detalle real siempre está en los logs del contenedor, no en la respuesta de curl. Diagnosticar con `docker-compose logs --tail 50 api`.

### "Falso" Error 7 — El agente "no recuerda" el historial

- **Síntoma:** el agente respondía "no tengo acceso a la historia de preguntas anteriores".
- **Diagnóstico:** se verificó Redis directamente y los mensajes SÍ estaban guardados. Un log temporal confirmó que los 5 mensajes SÍ llegaban al modelo.
- **Conclusión:** no era un bug. Era llama3.1 (modelo pequeño) soltando una respuesta genérica de asistente sin mirar el contexto. Con una pregunta más directa ("repite exactamente mi primer mensaje") el modelo sí usó la memoria correctamente.
- **Lección:** los modelos pequeños son inconsistentes; antes de tocar código, verificar el dato objetivo (qué hay en Redis, qué se envía al modelo).

---

## 5. Aprendizajes clave

1. **El código va dentro de la imagen, no montado.** El `Dockerfile` usa `COPY . .`, así que editar un archivo en el PC NO actualiza el contenedor. `docker-compose restart` reinicia con la imagen vieja. Para aplicar cambios de código hay que **reconstruir**: `docker-compose up -d --build api`. (Alternativa para desarrollo: montar `./app:/app/app` como volumen.)

2. **LangChain cambia de API entre versiones mayores.** Fijar versiones con `>=` trae la última, que puede romper. La 1.x reorganizó agentes por completo. En producción conviene **fijar versiones exactas**.

3. **Las imágenes mínimas no traen herramientas comunes.** `ollama/ollama` no tiene `curl`. Para healthchecks, usar herramientas que la propia imagen garantice.

4. **Healthchecks bloquean dependencias.** Un healthcheck mal escrito no solo da un falso negativo: impide que arranquen todos los servicios que dependen de él con `condition: service_healthy`.

5. **Diagnosticar con datos, no con suposiciones.** Ante "no recuerda", revisar Redis y los logs reveló que el código estaba bien. Habríamos perdido tiempo "arreglando" algo que no estaba roto.

6. **PowerShell ≠ bash.** En Windows: usar `curl.exe` (no `curl`), `findstr` (no `grep`), y escapar las comillas internas del JSON con `\`.

7. **La primera respuesta de Ollama es lenta.** En CPU, cargar llama3.1 por primera vez tarda de 30s a varios minutos. Las siguientes son rápidas porque el modelo queda en memoria.

---

## 6. Comandos de referencia

### Levantar y reconstruir

```powershell
docker-compose up --build          # primera vez (descarga modelos, ~5 min)
docker-compose up -d --build api   # reconstruir solo el api tras cambiar código
docker-compose down                # bajar (conserva datos)
docker-compose down -v             # bajar y BORRAR volúmenes (pierde datos)
docker-compose restart api         # reiniciar (NO recoge cambios de código)
```

### Logs y diagnóstico

```powershell
docker-compose logs -f api             # seguir logs del api en vivo
docker-compose logs --tail 50 api      # últimas 50 líneas
docker exec nexaagent-worker-1 pip show langchain   # ver versión de un paquete
```

### Ollama

```powershell
docker exec -it nexaagent-ollama-1 ollama list             # modelos descargados
docker exec -it nexaagent-ollama-1 ollama run llama3.1     # chat directo con el modelo
docker exec -it nexaagent-ollama-1 ollama pull mistral     # descargar otro modelo
```

### Redis (verificar memoria)

```powershell
docker exec nexaagent-redis-1 redis-cli LRANGE "nexa:memory:2" 0 -1
```

### Probar la API

```powershell
# Chat (nueva conversación)
curl.exe -X POST http://localhost:8000/chat -H "x-api-key: <API_KEY>" -H "Content-Type: application/json" -d '{\"message\": \"Hola, que puedes hacer?\"}'

# Chat (continuar conversación existente)
curl.exe -X POST http://localhost:8000/chat -H "x-api-key: <API_KEY>" -H "Content-Type: application/json" -d '{\"message\": \"Cual fue mi pregunta anterior?\", \"conversation_id\": 2}'
```

> Alternativa cómoda: abrir **http://localhost:8000/docs** (Swagger UI). Pulsar "Authorize",
> pegar el `x-api-key`, y probar los endpoints desde formularios sin pelear con el escapado de comillas.

---

## 7. Estado y siguientes pasos

### Funcionando ✅

- API FastAPI levantada y respondiendo
- Agente LangChain (create_agent sobre llama3.1)
- Memoria de corto plazo en Redis
- Persistencia de conversaciones y mensajes en PostgreSQL
- Worker de Celery conectado y listo
- Dos herramientas registradas: búsqueda en base de conocimiento y HTTP GET

### Pendiente de probar / construir 🔜

- **Flujo RAG end-to-end:** subir un documento a `/documents/upload`, verificar que el worker lo procese (parseo → chunking → embeddings → pgvector) y que el agente lo consulte.
- **Memoria de largo plazo:** resúmenes recuperables por similitud vectorial.
- **Endpoint para listar conversaciones.**
- **Streaming de respuestas (SSE).**
- **Seguridad de producción:** reemplazar el `x-api-key` simple por OAuth2/JWT.
- **Fijar versiones exactas** de dependencias (sobre todo LangChain).
- **Migraciones con Alembic** en lugar de auto-crear tablas al arrancar.

### Posible mejora de calidad

llama3.1 es inconsistente decidiendo cuándo usar herramientas y a veces ignora el contexto. Opciones: reforzar el system prompt, o subir a un modelo más capaz si los recursos lo permiten.

---

*Cierre de la Parte 1.*
