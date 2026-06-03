# NexaAgent — Bitácora de desarrollo (Parte 14)

> Pausa en el hilo de operabilidad (Parte 13 → 15) para un cambio de infraestructura:
> **mover la inferencia de Ollama de CPU a la GPU (RTX 4070)**. El servicio `ollama` en Docker
> corría 100% en CPU pese a haber una GPU NVIDIA disponible en la máquina; faltaba pasarle la GPU al contenedor.

**Estado al cierre de la Parte 14:** Ollama hace toda la inferencia (chat y embeddings) en la RTX 4070. Verificado en vivo: `ollama ps` reporta `100% GPU`, los logs pasan de `library=cpu` / `total_vram="0 B"` a `library=CUDA` / `total_vram="12.0 GiB"`, y la VRAM usada sube a ~5.5 GiB durante la inferencia. Los modelos no se perdieron (volumen `ollama_data`). El cambio es permanente entre reinicios.

---

## 1. El problema: inferencia en CPU sin saberlo

El escritorio y el monitor ya usaban la RTX 4070, así que era fácil suponer que Nexa también. Pero Nexa no hace inferencia "en la máquina": la hace el servicio `ollama` **dentro de Docker**, y ese contenedor no tenía acceso a la GPU. Resultado: el LLM corría en CPU pura, cargando el modelo (4.1 GiB) en RAM. Esto explica de raíz la lentitud de la primera respuesta que ya habíamos anotado en la Parte 1 (aprendizaje #7): en CPU, cargar el modelo tarda de 30 s a varios minutos.

---

## 2. Diagnóstico (con datos, antes de tocar nada)

### El entorno ya estaba listo

- Docker 29.5.2.
- Runtime `nvidia` **ya instalado** (visible en `docker info` → runtimes incluye `nvidia: nvidia-container-runtime`).
- `nvidia-smi` en el host: RTX 4070, 12 GB (12282 MiB), driver 591.44.
- Contenedores arriba: `api`, `worker`, `redis`, `db` (healthy), `ollama` (healthy).

La pieza clave: el runtime ya estaba; faltaba que el contenedor de Ollama lo **pidiera**.

### La firma de "CPU puro" en los logs

Antes de cambiar nada, se confirmó el estado real:

- `docker exec nexaagent-ollama-1 nvidia-smi -L` → `executable file not found`. El contenedor ni siquiera tenía las herramientas NVIDIA → señal de que no había acceso a la GPU.
- Logs de Ollama:
  - `discovering available GPUs...` → solo encontró `library=cpu`.
  - `vram-based default context ... total_vram="0 B"`.
  - El modelo cargó con `device=CPU size="4.1 GiB"` y `GPULayers:[]` (cero capas en GPU).

Esa terna (`library=cpu`, `total_vram="0 B"`, `GPULayers:[]`) es la huella inequívoca de inferencia en CPU. Igual que el resto del proyecto: el diagnóstico salió del log, no de la suposición.

---

## 3. El cambio: pasar la GPU al contenedor

Como el runtime `nvidia` ya estaba instalado, solo faltó **declarar la reserva de GPU** en el servicio `ollama` de `docker-compose.yml` (forma canónica de Compose moderno):

```yaml
  ollama:
    # ... resto del servicio (image, volumes, healthcheck, etc.) ...
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
```

Aplicación, sin perder modelos:

```bash
docker compose up -d ollama
```

Solo recrea el contenedor `ollama`. Los modelos viven en el volumen `ollama_data`, así que **no se re-descarga nada** ni se pierde el estado.

---

## 4. Verificación en vivo (el testigo autoritativo)

### El contenedor ahora ve la GPU

- `docker exec nexaagent-ollama-1 nvidia-smi -L` → `GPU 0: NVIDIA GeForce RTX 4070 (UUID: GPU-4043ae03-...)`.
- Logs de Ollama tras recrear:
  - `inference compute ... library=CUDA compute=8.9 name=CUDA0 description="NVIDIA GeForce RTX 4070" libdirs=ollama,cuda_v13 driver=13.1 ... total="12.0 GiB" available="11.1 GiB"`.
  - `vram-based default context ... total_vram="12.0 GiB"` (antes `0 B`).

### La prueba definitiva: `ollama ps`

```
NAME              SIZE      PROCESSOR    CONTEXT
qwen2.5:latest    4.9 GB    100% GPU     4096
```

`PROCESSOR: 100% GPU` — el modelo corre **completamente** en la 4070. En paralelo, `nvidia-smi` en el host mostró la VRAM usada saltar a **5534 MiB** durante la generación. Confirmación cruzada.

### Antes / ahora

| | Antes | Ahora |
|---|---|---|
| Backend de inferencia | `library=cpu` | `library=CUDA` (compute 8.9) |
| VRAM vista por Ollama | `0 B` | `12.0 GiB` |
| Carga del modelo | RAM / CPU, `GPULayers:[]` | 100% en VRAM de la RTX 4070 |
| `ollama ps` → PROCESSOR | (CPU) | `100% GPU` |

---

## 5. Errores y rarezas (todas cosméticas, ninguna bloqueó)

1. **`nvidia-smi: not found` dentro del contenedor (antes del cambio).** No era un error: era el síntoma de la falta de acceso a GPU. Tras añadir la reserva, `nvidia-smi -L` ya funciona dentro del contenedor (el passthrough trae las herramientas).
2. **`cat /tmp/out.txt` falló con ruta `C:/Users/KAKAWOL/AppData/Local/Temp/out.txt`.** Traducción de rutas de Git Bash (MSYS): reinterpretó la ruta del contenedor como una de Windows. Misma familia que el `<` de PowerShell (Parte 3) y `curl.exe` vs `curl` (Parte 1): el shell de Windows reinterpreta rutas y operadores.
3. **`curl: not found` dentro del contenedor `ollama`.** Es **el mismo hecho del Error 2 de la Parte 1**: la imagen `ollama/ollama` no trae curl. Por eso la verificación se hizo con `ollama ps` y el CLI propio, no con curl.
4. **`nvidia-smi --query-compute-apps` mostró `[Insufficient Permissions]` y procesos de Windows.** Desde el host en Windows/Docker Desktop no se puede inspeccionar limpiamente el proceso de cómputo del contenedor. La fuente autoritativa es `ollama ps`, no la lista de procesos del host.

---

## 6. Aprendizajes clave

1. **Runtime NVIDIA instalado ≠ contenedores usando la GPU.** Tener el runtime es condición necesaria, no suficiente: cada servicio debe pedir explícitamente la reserva de GPU en `docker-compose.yml`. Eco de las lecciones de healthchecks/dependencias de la Parte 1: la configuración explícita manda.
2. **`ollama ps` (columna PROCESSOR) es la prueba autoritativa de CPU vs GPU**, no la lista de procesos de `nvidia-smi` del host (que en Windows da `[Insufficient Permissions]`). Diagnosticar con la herramienta correcta, no con la que tengo más a mano.
3. **La firma de "CPU puro" vive en los logs:** `discovering available GPUs... library=cpu`, `total_vram="0 B"`, `GPULayers:[]`. Una vez más, el diagnóstico salió del dato objetivo, no de la suposición.
4. **Cambiar CPU→GPU no toca modelos ni datos.** El volumen `ollama_data` persiste; recrear el contenedor no re-descarga nada. Mismo principio que "cambiar el modelo de chat ≠ recrear la base" (Parte 2): saber qué invalida —y qué no— un cambio antes de temer perder estado.
5. **`curl` ausente en la imagen `ollama`, otra vez** (Parte 1): las imágenes mínimas no traen herramientas comunes. Usar el CLI propio (`ollama ps` / `ollama list`) o la API.
6. **Solo la 4070 entra en juego.** Ollama soporta CUDA/ROCm/Vulkan, no la iGPU Intel UHD 770; toda la carga de inferencia va a la NVIDIA.

---

## 7. Comandos de referencia (nuevos de esta parte)

### Comprobar el entorno Docker/NVIDIA en el host

```powershell
docker info --format '{{json .Runtimes}}'           # ¿está el runtime nvidia?
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv
```

### ¿El contenedor ve la GPU?

```powershell
docker exec nexaagent-ollama-1 nvidia-smi -L
```

### La firma CPU vs GPU en los logs de Ollama

```powershell
# En PowerShell: Select-String (o findstr). En bash/Linux: grep -iE
docker logs nexaagent-ollama-1 2>&1 | Select-String -Pattern "discovering|inference compute|library|vram|gpu"
```

### La prueba autoritativa: dónde corre el modelo

```powershell
docker exec nexaagent-ollama-1 ollama ps    # columna PROCESSOR: 100% GPU / CPU
```

### Recrear solo Ollama tras editar el compose (modelos intactos)

```powershell
docker compose up -d ollama
```

---

## 8. Estado y siguientes pasos

### Funcionando ✅ (nuevo en la Parte 14)

- Inferencia de Ollama (chat `qwen2.5` + embeddings `nomic-embed-text`) 100% en la RTX 4070.
- Reserva de GPU declarada en `docker-compose.yml`; cambio permanente entre `down`/`up`.
- Verificado en vivo: `ollama ps` → `100% GPU`; logs `library=CUDA`, `total_vram=12.0 GiB`; VRAM ~5.5 GiB en inferencia.

### Decisión abierta (de esta sesión)

- **Context length.** Ahora en 4096 (default calculado por VRAM cuando era 0 B). Con 12 GB hay margen de sobra para subirlo (p. ej. `OLLAMA_CONTEXT_LENGTH=8192` en `.env`) si se quieren conversaciones/documentos más largos. Pendiente de decidir.

### Puente con la operabilidad (Parte 15)

- El cambio a GPU **acelera el cold-start**, lo que afina la decisión de `timeout` de `ChatOllama` que quedó pendiente para resiliencia: ya no hace falta un timeout tan holgado como en CPU (Parte 1, #7), pero el modelo **sigue** cargándose a VRAM la primera vez.
- Los logs muestran `OLLAMA_KEEP_ALIVE:5m0s`: tras 5 min de inactividad el modelo se descarga de la VRAM y la siguiente petición paga una recarga (ahora más barata). Conecta directo con la palanca `keep_alive` que discutimos para resiliencia.

### Pendiente / deudas (hilo de operabilidad → Parte 15)

- **Tests:** ya implementados (suite con BD real, 14 verdes); pendientes de documentar en la Parte 15 junto al resto de operabilidad.
- **Resiliencia:** `timeout` + `keep_alive` en `ChatOllama`; retry nativo del framework en no-stream + evento de error en stream; degradación si Redis cae; idempotencia + `autoretry` + estado `failed` en la ingesta Celery; handler 503.
- **Observabilidad:** logging estructurado (sustituir el `print(flush=True)` de `orchestrator.py`), métricas, exception handler.
- **Secretos:** sacar los `change-me-in-env` del `.env` a un gestor real.
- **Backups:** estrategia de copia del volumen `pgdata`.
- **Cabos sueltos previos:** `path_separator` en `alembic.ini`; drift de docs (`.env.example` / README aún hablan de `x-api-key`); comentario 401/403 desactualizado en `security.py`.

---

*Cierre de la Parte 14.*
