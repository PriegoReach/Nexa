# NexaAgent — Bitácora de desarrollo (Parte 18)

> Continuación de la Parte 17. Objetivo: **re-ranking con cross-encoder** para subir la precisión
> del top-1/top-3 del retriever. El Recall@1 = 0.556 medido en la P17 justificaba esta parte con
> número; aquí se ataca ese techo con un cross-encoder que reordena los candidatos del retriever.

**Estado al cierre de la Parte 18:** re-ranking funcionando en GPU, medido contra el baseline de la P17. Resultado sobre el corpus de 4 proyectos: **Recall@1: 0.556 → 1.000, MRR: 0.744 → 1.000**, con Recall@8 manteniéndose en 1.000 (la línea roja respetada). Costo: ~1.3s una query RAG completa (dominado por las dos inferencias del LLM, no por el re-ranking); Ollama y el re-ranker conviven en la 4070 con 3.3GB de VRAM libre. El éxito es rotundo **sobre el corpus actual** — 1.0 sobre 4 entidades distintas prueba que el mecanismo funciona, no que escala; eso queda para la P19. Esta parte rompió deliberadamente la racha de "cero dependencias nuevas" (torch + sentence-transformers). Se clasificó además una deuda: el 7B emite tool-call como texto ~25% de las veces para ciertas queries (variabilidad del modelo, no bug del código).

---

## 1. Objetivo y justificación cuantitativa

La P17 dejó un baseline medido: Recall@1 = 0.556, Recall@3 = 0.889, Recall@5/8 = 1.000, MRR = 0.744 (corpus single-copy, 4 proyectos). El patrón "Recall@8 = 1.0 pero Recall@1 = 0.556" es la firma clásica de **el retriever encuentra pero no ordena**: las cuatro frases "código de autorización de X" compiten como falsos amigos léxicos, y el bi-encoder (`nomic-embed-text`) a veces rankea la entidad vecina (plantilla similar) por encima de la correcta.

Esta es exactamente la condición que el re-ranking ataca: un cross-encoder que mira pregunta y chunk **juntos** desempata por relevancia fina, donde el bi-encoder —que colapsa cada texto en un vector independiente— pierde el matiz. Por primera vez había un baseline contra el que medir si funciona (lo que no existía cuando se anotó este pendiente en la P10).

---

## 2. Decisiones de diseño

| Decisión | Elección | Motivo |
|---|---|---|
| Modelo | `BAAI/bge-reranker-v2-m3` | Consenso para reranking multilingüe + GPU; Apache 2.0; probado en español |
| Librería | `sentence-transformers` (`CrossEncoder`) | Interfaz estándar; bge-m3 integrado |
| Dónde corre | En el contenedor `api`, en la **GPU** (Opción A) | Simple, sin servicios nuevos; la 4070 tiene VRAM para convivir con Ollama |
| Precisión | **fp16** (`model.model.half()`) | La mitad de VRAM por pérdida despreciable en reranking; clave para convivir con Ollama |
| Pipeline | retriever trae **FETCH_N=20** → re-ranker reordena → top-k=8 al agente | El "cambio doble": el re-ranker solo mejora lo que el retriever trajo |
| No bloquear el loop | `asyncio.to_thread` para el `predict` del cross-encoder | El re-ranking es CPU/GPU-bound síncrono; aislarlo del event loop |
| Aislamiento de torch | `import torch` **lazy** dentro del singleton | El `worker` no arrastra torch ni necesita GPU; el `tests` importa limpio sin rebuild |
| Medición | El harness de la P17, antes/después | El baseline (Recall@1=0.556) es la vara exacta |

### 2.1 La decisión que rompió la racha

`bge-reranker-v2-m3` necesita un modelo y la librería para correrlo — **no hay forma de hacerlo a mano** como `/metrics` (P16) o el `JsonFormatter` (P15). Así que esta parte añadió `sentence-transformers==3.3.1` + `torch==2.5.1` a `pyproject.toml` (con `==`, fiel al Error 4 de la P1), con el costo asumido: rebuild `--no-cache`, imagen de cientos de MB → **9.78GB** (torch + CUDA), y primer build lento. Era inevitable y se aceptó conscientemente: el cross-encoder es la palanca para el techo de ranking, y no existe versión "sin dependencia".

### 2.2 El cambio doble (no solo "añadir re-ranker")

La sutileza clave: un re-ranker solo puede reordenar lo que el retriever ya trajo. Si `search()` devolvía 8 y el chunk correcto de "responsable de Rubí" estaba en posición 5 (P17), el re-ranker lo sube a 1 — bien. Pero si un chunk correcto cayera en posición 12, el re-ranker nunca lo vería. Por eso el cambio es **doble**: ampliar la ventana de recuperación a `FETCH_N=20` (la lógica híbrida de siempre, pero a 20) **y** reordenar esos 20 a los 8 mejores con el cross-encoder. Sin lo primero, lo segundo rinde poco.

---

## 3. Las piezas

- **`docker-compose.yml`**: bloque GPU `nvidia` en `api` (igual que `ollama`) + volumen `hf_cache` montado en `/root/.cache/huggingface` + `HF_HOME` → el modelo se descarga una vez (~2.3GB) y persiste entre recreaciones del contenedor.
- **`pyproject.toml`**: `sentence-transformers==3.3.1` + `torch==2.5.1` (CUDA bundled en la wheel de linux).
- **`app/rag/reranker.py`**: singleton del `CrossEncoder`, `device="cuda"`, fp16, `import torch` lazy dentro del singleton (para que el worker no lo arrastre). Expone `rerank(query, chunks, top_k)`.
- **`app/rag/retriever.py`**: el cambio doble — `_retrieve(k=FETCH_N=20)` (la búsqueda híbrida actual) + `search(query, k=8)` que reordena con el cross-encoder vía `asyncio.to_thread`.

---

## 4. Verificaciones de realidad (antes de teclear)

Tres comprobaciones, ninguna asumida:

1. **¿`search()` tiene `k` parametrizable?** Sí — `k` es parámetro, así que el cambio doble es limpio (no hay que deshardcodear nada).
2. **¿VRAM libre con Ollama cargado?** Con `qwen2.5` residente: **5.3GB libres**. bge-m3 en fp16 (~1-2GB) cabe con holgura → Opción A viable.
3. **¿El contenedor `api` ve la GPU?** **No** (exit 127 al invocar `nvidia-smi`) → faltaba el bloque GPU en `api`. Corregido (mismo bloque que `ollama`).

La #3 era el bloqueante real: sin el bloque GPU en `api`, el re-ranker habría caído a CPU. Verificar antes de construir evitó descubrir eso tras un build de 9.78GB.

---

## 5. Verificación post-build

### 5.1 El gate crítico: GPU disponible

```powershell
docker exec nexaagent-api-1 python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
# → True NVIDIA GeForce RTX 4070   (torch 2.5.1+cu124)
```

`cuda.is_available() == True` con la 4070 detectada. El re-ranker corre en GPU, no en CPU — el modo de fallo sigiloso (caer a CPU en silencio, calidad igual pero latencia desastrosa) quedó descartado **antes** de mirar calidad.

### 5.2 El juez: el harness contra el baseline

```powershell
docker compose -f x:\Nexa\nexaagent\docker-compose.yml exec api python -m app.eval.retrieval
```

| Métrica | Baseline P17 | Con re-ranker | Salto |
|---|---|---|---|
| Recall@1 | 0.556 | **1.000** | +0.444 |
| Recall@3 | 0.889 | **1.000** | +0.111 |
| Recall@5 | 1.000 | 1.000 | = |
| Recall@8 | 1.000 | **1.000** | línea roja ✓ |
| MRR | 0.744 | **1.000** | +0.256 |

**MRR = 1.000 = todos los chunks correctos en posición 1.** El desglose lo confirmó: las nueve filas en `[1]`, incluida "quién es responsable de Rubí" — la misma que cuenta la historia de toda la Fase 1 en tres estados:

```
"responsable de Rubí":  [-] (P17 triplicado)  →  [5] (P17 limpio)  →  [1] (P18 re-ranker)
```

El cross-encoder resolvió los falsos amigos léxicos que el bi-encoder no distinguía. La línea roja (Recall@8 ≥ 1.0) se respetó: el FETCH_N=20 dio margen para reordenar sin expulsar chunks correctos del top-8.

### 5.3 VRAM: convivencia confirmada

```powershell
docker exec nexaagent-ollama-1 nvidia-smi --query-gpu=memory.used,memory.total,memory.free --format=csv
# → 8671 MiB usados, 12282 total, 3340 libres
```

Ollama (~5-6GB) + bge-m3 fp16 (~2-3GB) = 8.6GB, sin OOM, **3.3GB de margen**. La Opción A funcionó. *Nota para vigilar:* 3.3GB no es mucho — si sube el contexto de Ollama, se carga un modelo mayor, o el corpus de la P19 hace batches grandes, ese margen puede evaporarse. Convivencia OK hoy; número a vigilar.

### 5.4 Latencia: el costo real

La primera medición (`rerank-warm`, query "codigo del proyecto ambar") **no sirvió**: la traza mostró un solo `llm_call`, una sola llamada a Ollama, y un `answer` con un tool-call crudo (`CallCheckKnowledgeBase(...)`) — la herramienta no se ejecutó, así que el retriever+re-ranker no corrió. 599ms que no medían lo que queríamos. (Ese fallo resultó ser la deuda de la sección 6.)

La medición válida (`rerank-warm2`, query de Rubí, que SÍ ejecutó el RAG completo):

```
agent_start → llm_call → HTTP Ollama (55.764) → HTTP Ollama (56.194) → request_end
duration_ms: 1288.9
```

**Dos** llamadas a Ollama (decidir herramienta → redactar respuesta) confirman el RAG completo. Desglose inferido de la traza: 1ª inferencia ~624ms, 2ª ~430ms, y el resto (~235ms) es retriever(20) + re-ranker + overhead. **El re-ranking en sí NO es el cuello de botella** — las dos inferencias del LLM dominan; el cross-encoder en fp16 sobre la 4070 cuesta centenas de ms o menos, como se esperaba.

> **Honestidad sobre la medición:** no hay un "antes" controlado para comparar (los logs viejos eran de queries variadas, no la misma query sin re-ranker). Así que 1289ms es el costo **absoluto** con re-ranking, no el *delta* que el re-ranking añadió. Dado que la calidad saltó a 1.0 y el total está bajo 1.5s, no se midió el delta limpio (requeriría desactivar el re-ranker) — se registra como "costo total ~1.3s, dominado por el LLM". Aquí cerró un círculo: el `request_end` de la P16 midió el costo de la mejora de la P18, justo para lo que se construyó esa observabilidad.

---

## 6. Deuda clasificada: tool-call intermitente del 7B

Durante la medición de latencia apareció un comportamiento raro: la query "codigo del proyecto ambar" devolvió `CallCheckKnowledgeBase({"query": "..."})` como **texto** en vez de ejecutar la herramienta. No se enterró bajo el éxito — se reprodujo para clasificarlo.

**El experimento:** la misma query exacta, cuatro veces:

| Intento | Resultado |
|---|---|
| 1 | Tool-call crudo (`CallCheckKnowledgeBase(...)`) |
| 2 | Respuesta perfecta (AM-2026-T1 + contexto) |
| 3 | Respuesta perfecta |
| 4 | Respuesta perfecta |

**Veredicto: variabilidad del 7B, no bug del código.** Mismo input → salidas distintas (falla ~1 de 4). Si fuera bug del orchestrator, fallaría siempre; si fuera la forma de la query, fallaría consistentemente. Falla intermitente con input idéntico = no-determinismo del muestreo del modelo, la "variabilidad intrínseca" documentada en P4/P8/P9, ahora manifestándose en el tool-calling (una flaqueza conocida de los modelos pequeños).

**Qué pasa por dentro:** `qwen2.5` 7B, ~25% de las veces para esta query, emite el tool-call en un formato que el orchestrator no reconoce como ejecutable, y lo pasa como texto. El modelo "quiso" llamar a la herramienta (se ve la intención) pero el formato no fue el esperado.

**Por qué no se persigue ahora:** (a) no es código propio — el orchestrator recibe un formato que el modelo emitió mal; (b) no contamina la P18 — el re-ranking vive dentro del retriever, que solo corre si la herramienta se ejecuta; cuando el modelo sí ejecuta (3 de 4), el RAG+re-ranking funciona perfecto, como midió el harness.

**Mitigaciones anotadas (ninguna de esta sesión):**
1. **Parser tolerante** (preferida): detectar un tool-call emitido como texto (`CallXxx({...})`) y ejecutarlo en vez de pasarlo al usuario. Recupera ese ~25% sin cambiar de modelo, la más barata.
2. **Modelo mayor** (qwen2.5 14B+): tool-calling más consistente, pero cuesta VRAM (ya en 3.3GB libres) y latencia.
3. **Reintento ante tool-call malformado**: simple, pero añade latencia y no garantiza éxito.

**Conexión con la evaluación futura:** el harness de recuperación NO ve este problema, y es correcto que no lo vea — mide `search()` directo (retriever puro), no el agente. Por eso da 1.0 limpio: la recuperación es perfecta; el fallo está en la capa del agente (decidir/ejecutar la herramienta). Es una razón más para que la **evaluación de generación** (la fase pospuesta) exista en el futuro: ahí sí se pasa por el agente completo y un fallo como este aparecería en las métricas.

---

## 7. Aprendizajes clave

1. **El re-ranking solo mejora lo que el retriever trajo → el cambio es doble.** Ampliar la ventana de recuperación (FETCH_N=20) y reordenar a 8, no solo enchufar el cross-encoder. Sin la ventana ampliada, el re-ranker no puede rescatar un chunk que el retriever dejó en posición 12.

2. **Bi-encoder vs cross-encoder, demostrado con número.** El bi-encoder colapsa cada texto en un vector y pierde el matiz entre frases de plantilla similar (Recall@1=0.556); el cross-encoder ve pregunta+chunk juntos y desempata (Recall@1=1.000). La diferencia teórica se volvió un salto medido de +0.444.

3. **Verificar el gate de GPU antes de mirar calidad.** Si el cross-encoder cae a CPU en silencio, los números de calidad serían idénticos pero la latencia, desastrosa. Confirmar `cuda.is_available()` primero descarta el modo de fallo sigiloso antes de interpretar nada.

4. **La línea roja por encima de la mejora.** Recall@8 ≥ 1.0 importa más que cualquier subida de Recall@1: un re-ranker que reordena mal podría expulsar un chunk correcto del top-8 y *degradar* la cobertura. Una victoria en precisión que cuesta cobertura no es victoria.

5. **fp16 + lazy import: dos decisiones que hacen viable la convivencia.** fp16 reduce a la mitad la VRAM del re-ranker (cabe junto a Ollama); el import lazy de torch evita que el worker arrastre 9GB de dependencias que no usa. El aislamiento es lo que separa "funciona" de "funciona limpio".

6. **Un MRR perfecto sobre 4 entidades prueba el mecanismo, no la escala.** 1.0 sobre 4 proyectos muy distintos es relativamente fácil para un cross-encoder. La pregunta real —¿1.0 con 50 entidades de plantilla idéntica?— no se puede responder con este corpus. Éxito rotundo *sobre el corpus actual*, no "problema resuelto"; esa distinción es la razón de la P19.

7. **No enterrar un comportamiento raro bajo el éxito.** El tool-call crudo se reprodujo (4×) para clasificarlo en vez de ignorarlo. Resultó variabilidad del modelo, no bug — pero solo se supo por medir, y queda como deuda *entendida y cuantificada*, no como cabo suelto.

8. **El costo de una mejora se mide con la observabilidad ya construida.** El `request_end`/`duration_ms` de la P16 dio el costo del re-ranking sin instrumentar nada nuevo. Calidad (harness P17) + costo (observabilidad P16) = veredicto completo.

9. **Romper una racha conscientemente es una decisión, no un fracaso.** "Cero dependencias nuevas" se sostuvo muchas partes, pero el cross-encoder no tiene versión sin dependencia. Aceptar torch + los 9.78GB a cambio de Recall@1=1.0 fue un trade-off explícito y documentado, no un descuido.

---

## 8. Comandos de referencia (nuevos de esta parte)

### Verificar que torch ve la GPU dentro de `api`

```powershell
docker exec nexaagent-api-1 python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

### Build pesado (torch + CUDA) y recreación

```powershell
docker compose -f x:\Nexa\nexaagent\docker-compose.yml build --no-cache api
docker compose -f x:\Nexa\nexaagent\docker-compose.yml up -d api
```

### Confirmar carga del re-ranker y convivencia de VRAM

```powershell
docker compose -f x:\Nexa\nexaagent\docker-compose.yml logs api | Select-String "reranker_load|reranker_ready"
docker exec nexaagent-ollama-1 nvidia-smi --query-gpu=memory.used,memory.total,memory.free --format=csv
```

### Medir calidad (harness) y costo (latencia)

```powershell
# Calidad: el harness contra el baseline
docker compose -f x:\Nexa\nexaagent\docker-compose.yml exec api python -m app.eval.retrieval

# Costo: una query RAG completa (dos llamadas al LLM) y su duration_ms
curl.exe -X POST http://localhost:8000/chat -H "Authorization: Bearer <TOKEN>" -H "X-Request-ID: lat-test" -H "Content-Type: application/json" -d '{\"message\": \"cual es el codigo del Proyecto Rubi?\"}'
docker compose -f x:\Nexa\nexaagent\docker-compose.yml logs api | Select-String "lat-test"
# Una query con DOS HTTP a Ollama = RAG completo (la medición válida).
# Una con UNA sola = tool-call no ejecutado (no mide el re-ranking).
```

---

## 9. Estado y siguientes pasos

### Funcionando ✅ (nuevo en la Parte 18)

- **Re-ranking con cross-encoder** (`bge-reranker-v2-m3`, GPU, fp16) sobre el top-20 del retriever, reordenando a top-8.
- **Calidad medida:** Recall@1 0.556→1.0, Recall@3 0.889→1.0, MRR 0.744→1.0, con Recall@8 = 1.0 (línea roja respetada).
- **Costo medido:** ~1.3s una query RAG completa, dominado por las inferencias del LLM, no por el re-ranking.
- **Convivencia GPU:** Ollama + bge-m3 en 8.6GB, 3.3GB libres.
- **Aislamiento limpio:** import lazy de torch (el worker no lo arrastra); `tests` sin rebuild.

### Cierre de la Fase 1 (Q&A documental al máximo)

- ✅ P17 — harness de evaluación + baseline.
- ✅ P18 — re-ranking: techo de ranking cerrado **sobre el corpus actual**.
- 🎯 **P19 (lo que falta de la Fase 1):** corpus sintético a escala (N proyectos de plantilla idéntica) — donde el Recall@1=1.0 de hoy se pone a prueba de verdad. Sabremos si el re-ranking *escala* o solo *funciona*. De paso: recalibrar el dedup (falso positivo Paco≈Bruno, P15).

### Deudas / pendientes 🔜

- **Tool-call intermitente del 7B** (~25% para ciertas queries): variabilidad del modelo, no bug. Mitigación preferida: parser tolerante. Clasificada, no perseguida.
- **Margen de VRAM 3.3GB:** vigilar si crece la carga (contexto de Ollama, modelo mayor, batches del re-ranker en P19).
- **Delta de latencia del re-ranking sin medir** (solo el costo absoluto ~1.3s): medible desactivando el re-ranker, pero de bajo valor dado el resultado.
- **Evaluación de generación** (fase pospuesta): el harness mide recuperación, no el agente end-to-end; un fallo como el tool-call solo aparecería ahí.
- **PyYAML transitiva** (P17): migrar el dataset a JSON para eliminar el cabo.
- **Heredadas:** `request_id: "-"` en la ingesta; `retry_backoff` de Celery; `path_separator` en `alembic.ini`; drift de docs; `SecurityWarning` de Celery como root.

### Más allá de la Fase 1

- **Fase 2 — Automatización:** herramientas con escritura e integraciones reales (el salto de "saber" a "actuar" del objetivo original; abre el problema de la idempotencia por herramienta — reintentar un "mandar correo" no debe mandarlo dos veces).
- **Fase 3 — Interfaz:** frontend sobre la API estable (las señales de herramienta del streaming siguen sin UI).

---

## 10. Temario de estudio (Parte 18)

> Conceptos transferibles que esta parte enseña. Para repaso.

### A. Re-ranking en RAG

- **El patrón retrieve-then-rerank**: el bi-encoder recupera rápido un top-N amplio (precomputable, O(1) por query con ANN); el cross-encoder reordena ese top-N por relevancia fina (no precomputable, ve query+doc juntos). El re-ranking se aplica solo al top-N, nunca a todo el corpus.
- **Por qué el cross-encoder es más preciso pero más caro**: no puede precomputar nada — necesita ver la consulta y el documento juntos. Esa es la fuente de su precisión y de su latencia.
- **El "cambio doble"**: re-ranking exige *ampliar la ventana de recuperación* (FETCH_N > k) además de reordenar. Un re-ranker no puede rescatar un documento que el retriever no trajo.
- **La firma "Recall@k alto + Recall@1 bajo"** como indicador de que el problema es de *ranking*, no de *cobertura* — el caso de uso canónico del re-ranking.
- **Modelos de reranking multilingües**: bge-reranker-v2-m3 como opción GPU + Apache 2.0; la familia `CrossEncoder` de sentence-transformers.

### B. Operación de modelos en GPU compartida

- **fp16 (half precision)**: reduce la VRAM a la mitad con pérdida despreciable en reranking; clave para que dos modelos convivan en una GPU.
- **Convivencia de modelos en una GPU**: medir `memory.free` con todo cargado; el margen como número a vigilar, no a dar por sentado.
- **El modo de fallo "cae a CPU en silencio"**: si la detección de CUDA falla, el modelo corre en CPU — todo funciona pero la latencia se dispara. Verificar `cuda.is_available()` como gate antes de medir calidad.
- **Import lazy de una dependencia pesada**: cargar torch dentro del singleton (no a nivel de módulo) para que los procesos que no lo usan (el worker) no lo arrastren y se mantengan ligeros.
- **`asyncio.to_thread`** para correr trabajo síncrono CPU/GPU-bound (el `predict` del cross-encoder) sin bloquear el event loop.

### C. Medir una mejora honestamente

- **Las dos mitades de un veredicto**: calidad (¿mejoró?) y costo (¿a qué precio?). Un re-ranker que sube la calidad pero dispara la latencia puede ser una victoria pírrica en un sistema interactivo.
- **Costo absoluto vs delta**: medir "1.3s con re-ranking" no es lo mismo que "el re-ranking añadió X ms"; el delta limpio requiere un control (sin re-ranker). Saber qué se midió.
- **Aislar dónde está el costo**: la traza (dos llamadas al LLM + retriever + re-ranker) muestra que el cuello de botella son las inferencias, no el cross-encoder.
- **Perfecto sobre corpus pequeño ≠ resuelto**: 1.0 sobre 4 entidades prueba el mecanismo; la escala se valida con un corpus mayor. Escepticismo sano ante una métrica perfecta.

### D. Diagnóstico de comportamiento intermitente

- **Reproducir para clasificar**: la misma entrada N veces distingue bug determinista (falla siempre), problema de forma (falla consistentemente para cierto input) y no-determinismo del modelo (falla intermitente con input idéntico).
- **No-determinismo de LLMs y tool-calling**: los modelos pequeños emiten a veces el tool-call en un formato no ejecutable; el muestreo hace que el mismo prompt dé salidas distintas. Es limitación del modelo, no bug del orquestador.
- **Qué capa falla**: un fallo de tool-calling vive en la capa del agente, no en la recuperación — por eso un harness de recuperación no lo ve, y por eso hace falta evaluación end-to-end para capturarlo.
- **Deuda entendida vs cabo suelto**: clasificar y cuantificar un problema (≈25%, causa conocida, mitigaciones anotadas) es cerrarlo en el sentido que importa, aunque no se arregle ahora.

### E. Gestión de dependencias y trade-offs

- **Romper una convención conscientemente**: "cero deps nuevas" se sostuvo mientras hubo alternativa a mano; el cross-encoder no la tiene. Aceptar torch + una imagen de ~10GB a cambio de Recall@1=1.0 es un trade-off explícito.
- **Cachear modelos descargados en un volumen** (`HF_HOME` + volumen): que un modelo de ~2.3GB se baje una vez y persista entre recreaciones del contenedor.
- **Pinear versiones** (`==`) también para dependencias pesadas (torch, sentence-transformers): la lección del Error 4 de la P1 aplica igual.

---

*Cierre de la Parte 18.*
