# NexaAgent — Bitácora de desarrollo (Parte 17)

> Continuación de la Parte 16. Objetivo de esta sesión: **evaluación sistemática de la recuperación** —
> construir un harness que mida el retriever con números objetivos (Recall@k y MRR) en vez de "probé
> los cuatro proyectos y pareció bien". El cimiento firme antes de evaluar generación (P18+).

**Estado al cierre de la Parte 17:** harness de evaluación de recuperación funcionando, medido sobre los 4 proyectos con huellas conocidas. Baseline limpio establecido: **Recall@1 = 0.556, Recall@3 = 0.889, Recall@5 = 1.000, Recall@8 = 1.000, MRR = 0.744** (sobre corpus single-copy). El harness, combinado con un dedup, expuso un problema que estaba invisible: el corpus triplicado degradaba la recuperación. Suite en 24 verdes + 1 skipped (el piso real, skip-gated). El número de Recall@1 = 0.556 cuantifica, por primera vez, el techo de ranking fino que se venía arrastrando como intuición desde la P10.

---

## 1. Objetivo y reencuadre de la sesión

Todo lo construido desde la P3 (subir k, búsqueda híbrida, chunking estructural, dedup, extracción atómica) se validó con **un** caso (los cuatro proyectos) y **a ojo**. Funcionaba, pero sin una vara objetiva: si se cambiara el modelo, el prompt, el umbral o el `chunk_size`, no había forma de saber si se mejoraba o empeoraba. Esta sesión construye esa vara.

**Reencuadre importante de alcance.** La idea inicial era arrancar el dataset de evaluación con el histórico de conversaciones ("el agente debe manejar grandes cantidades de información"). Se descartó tras separar dos conceptos que se confundían:

- **Corpus** = lo que el RAG indexa (los documentos). Que escale a "grandes cantidades" es propiedad del corpus.
- **Dataset de evaluación** = pares `{pregunta, respuesta_correcta_conocida}`. Su valor NO viene del tamaño, sino de que la respuesta correcta sea **verificable con certeza**.

El histórico tiene preguntas pero **no ground truth etiquetado**: habría que leer cada conversación y decidir a mano cuál era la respuesta correcta (lenta, subjetiva, y "¿era correcta o solo lo que el modelo respondió?"). Peor: muchas conversaciones del histórico son sobre los mismos 4 proyectos (P12), así que daría *más preguntas sobre las mismas huellas*, no cobertura nueva. El camino real a "evaluar a escala" no es minar el histórico (ruido sin etiquetar), sino **ampliar el corpus a propósito** con N proyectos sintéticos de huellas conocidas — pero eso es P18+. Esta sesión: cimiento firme sobre los 4 proyectos, con un harness diseñado para escalar.

---

## 2. Decisiones de diseño (cerradas antes de teclear)

| Decisión | Elección | Motivo |
|---|---|---|
| Qué se mide | Recall@k y MRR del **retriever puro** | Recall@k = ¿está el chunk correcto en el top-k? MRR = ¿en qué posición? Son los números que ya se anotaban a mano ("RB en [7]") |
| Valores de k | 1, 3, 5, 8 de una sola corrida | El agente usa k=8; medir varios muestra el margen. Recall@8 alto + Recall@1 bajo = "encuentra pero rankea flojo" |
| Qué es "correcto" | El chunk contiene la **huella exacta del proyecto preguntado** | Determinista, cero LLM. Menciones cruzadas (Ónix nombrando a Rubí) NO cuentan → mide el techo real |
| Mide retriever, no agente | Llama a `search()` directo | Aísla recuperación de generación (sin el LLM de por medio) |
| Formato del dataset | Archivo de datos (clave: **huella**, no `chunk_id`) | Sobrevive a un re-chunking; crece añadiendo líneas |
| Dónde corre el harness | Script (`python -m app.eval.retrieval`) | Necesita pgvector real con chunks + Ollama (embeddings); no es test unitario puro |
| Tests | Dos niveles: aritmética determinista (CI) + piso real skip-gated | La regla P15: nada de Ollama/no-determinismo en CI |

**La definición honesta de "correcto" es deliberada.** Un acierto es el primer chunk del top-8 que contiene la huella exacta del proyecto preguntado. Las menciones cruzadas (el chunk de Ónix que nombra a Rubí, visto en P9/P10) **no** cuentan como acierto para una pregunta sobre Rubí. Esto hace el harness honesto sobre el techo de ranking conocido, no complaciente — un harness que contara las menciones cruzadas inflaría el número y mentiría sobre la debilidad real.

---

## 3. Las tres piezas

### 3.1 Dataset — `app/eval/datasets/retrieval_proyectos.yaml`

Datos puros, editable, sin código. 9 entradas / 4 proyectos. Cada entrada: `pregunta`, `huella`, `proyecto`.

- **Clave de diseño:** guarda la **huella** (`RB-2023-Q7`), no un `chunk_id`. Por eso sobrevive a un re-chunking — si cambia `chunk_size`, los chunks cambian pero las huellas no. Regresión durable, no foto de hoy.
- **Robustez por redacción:** varias frases por proyecto — con tilde / sin tilde (`rubi`), por nombre, por código, y una multi-intento ("quién es responsable de Rubí y su código interno", la que destapó el problema de ranking).
- **Agnóstico al tamaño:** 9 o 90 entradas, mismo código.

### 3.2 Harness — `app/eval/retrieval.py`

Mide el **retriever puro** (`search` directo), sin agente/LLM de por medio.

- **Contrato verificado, no asumido:** se leyó `app/rag/retriever.py` antes de escribir nada y se confirmó `search(query, k) -> list[str]` (devuelve los `content` de los chunks). Por eso el acierto es `case["huella"] in chunk` (subcadena exacta sobre el texto). Si `search` hubiera devuelto objetos o tuplas, ese `in` mediría mentiras — pero no es el caso. (Fiel a la metodología: verificar el contrato real, no suponerlo.)
- **Métricas:** Recall@k para k ∈ {1,3,5,8} y MRR (media de 1/rank, 0 si miss).
- **El oro es el desglose por caso:** `_format()` imprime el rank del chunk correcto por pregunta (`[5]`, `[-]`…) — la versión formal de lo que se anotaba a mano. Es lo que localizó el miss de Rubí.
- **Corre suelto:** usa `search` → `worker_session()`, que crea su propio engine `NullPool` leyendo `DATABASE_URL` del entorno, así que funciona en un `asyncio.run` aislado sin chocar con el loop de FastAPI (la trampa de la P2). Necesita BD con chunks y Ollama vivo (`search` llama a `embed_query`) → por eso no va en CI determinista. Acepta `sys.argv[1]` como dataset alternativo.

### 3.3 Tests — `tests/test_retrieval_eval.py`

Dos niveles, por la regla P15 (nada de Ollama/no-determinismo en CI):

- **`test_recall_mrr_math`** (en la suite, siempre corre): monkeypatchea `retrieval.search` con chunks sintéticos (ranks `[1, 3, None]`) y verifica la aritmética — Recall@1 = 1/3, Recall@3 = 2/3, MRR = (1 + 1/3 + 0)/3. Determinista, sin Ollama ni BD. Es el guardián de que el cálculo no regresione: si el harness empieza a "medir mentiras", esto lo caza.
- **`test_retrieval_recall_floor_real`** (skip-gated por `EVAL_REAL=1`): piso de regresión sobre datos reales, `assert recall[8] == 1.0` tras el dedup. Skippeado por defecto (el servicio `tests` no tiene corpus ni Ollama); su docstring documenta el baseline limpio y el hallazgo del triplicado.

Resultado: **24 passed, 1 skipped**. El ligero pasa siempre; el real se salta. La medición real del piso queda probada por el script (`exec api`), no por pytest, por la limitación de entorno (`api` no trae pytest; `tests` usa BD vacía).

---

## 4. El dividendo del harness: el corpus triplicado degradaba la recuperación

**El hallazgo más valioso de la sesión, y era completamente invisible antes.** La primera corrida del harness dio un resultado contaminado, porque la BD tenía los docs 6/7/8 — tres copias **idénticas** del mismo PDF (`prueba-rag-grande.pdf`, subido tres veces en las pruebas de partes anteriores). Cada chunk correcto existía por triplicado.

**El resultado es contraintuitivo:** uno esperaría que más copias del documento *ayuden* a la recuperación (más oportunidades de encontrar el chunk). Pasó lo opuesto — las tres copias idénticas amontonaban el top-k con chunks redundantes y **expulsaban** un chunk correcto fuera de la ventana de 8. La duplicación no añade señal; añade ruido que compite por los mismos puestos.

### El antes / después (tras el dedup)

| Métrica | Triplicado (antes) | Single-copy (tras dedup) |
|---|---|---|
| Recall@8 | 0.889 (una huella perdida) | **1.000** |
| Recall@5 | 0.889 | **1.000** |
| Recall@3 | 0.556 | **0.889** |
| MRR | 0.639 | **0.744** |
| Rubí "responsable" | `[-]` fuera de top-8 | **rank 5** |

**Dos lecturas clave:**

1. **El `Recall@8 == 1.0` del test de regresión era correcto desde el principio — el triplicado lo *escondía*, no lo contradecía.** La copia redundante expulsaba el chunk de "responsable de Rubí" del top-8; al limpiar, reapareció. El piso del test, restaurado, ahora está **justificado por medición sobre corpus limpio**, no aspiracional.

2. **El miss de "responsable de Rubí" era, en parte, artefacto de la triplicación.** Pasó de `[-]` a `rank 5` tras el dedup — es decir, las copias lo empujaban fuera y al limpiarlas reapareció. *(La hipótesis previa era que el miss sería puro problema de redacción y no se movería con el dedup; el dato la corrigió — sí se movió. Pero que reaparezca en rank 5 y no rank 1 confirma que **también** hay componente de redacción: "responsable" rankea más flojo que "código/autorización" para la misma huella. Ambas cosas eran ciertas a la vez. Medir > intuir.)*

---

## 5. Cambios en la BD real (operación destructiva, confirmada)

Se borraron los docs 6 y 7 (duplicados), dejando solo el doc 8 como corpus único de los 4 proyectos. El `CASCADE` de las FKs (verificado en P13) arrastró sus 14 chunks sin tocarlos a mano.

```powershell
# 1. Inspección previa (no destructiva): confirmar que cada doc tiene las 4 huellas y el FK es CASCADE
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "SELECT document_id, count(*) FILTER (WHERE content LIKE '%RB-2023-Q7%') rubi, count(*) FILTER (WHERE content LIKE '%ESM-2025-K3%') esm, count(*) FILTER (WHERE content LIKE '%AM-2026-T1%') ambar, count(*) FILTER (WHERE content LIKE '%ON-2024-B5%') onix, count(*) total FROM document_chunks GROUP BY document_id ORDER BY document_id;"
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "SELECT conname, confdeltype FROM pg_constraint WHERE conrelid='document_chunks'::regclass AND contype='f';"
# → docs 6/7/8 con 1|1|1|1|7 cada uno; FK confdeltype = c (CASCADE).

# 2. Dedup (destructivo) + verificación
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "DELETE FROM documents WHERE id IN (6, 7);"
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "SELECT id, filename, status FROM documents ORDER BY id;"
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "SELECT count(*) AS total_chunks FROM document_chunks;"
# → DELETE 2; queda solo doc 8; total_chunks = 7. El CASCADE borró los 14 chunks de 6 y 7.
```

La inspección previa con `count(*) FILTER (WHERE content LIKE ...)` confirmó **antes de borrar** que los tres docs eran efectivamente el mismo contenido (1 de cada huella, 7 chunks) — verificar antes de una operación destructiva, no después.

---

## 6. Verificación

```powershell
# Re-medición sobre corpus limpio (este SÍ es el baseline real)
docker compose -f x:\Nexa\nexaagent\docker-compose.yml exec api python -m app.eval.retrieval
# → Recall@1: 0.556 · Recall@3: 0.889 · Recall@5: 1.000 · Recall@8: 1.000 · MRR: 0.744

# La suite (24 + 1)
docker compose -f x:\Nexa\nexaagent\docker-compose.yml run --rm tests
# → 24 passed, 1 skipped
```

**Por qué `exec api` funciona para el harness:** el contenedor `api` monta `./app:/app/app`, así que ve `app/eval/` recién creado en vivo; su `DATABASE_URL` (de `.env`) apunta a la BD real `nexaagent` y alcanza Ollama por la red del compose. La corrida previa (con triplicado) fue el mismo comando, antes del `DELETE`. *(Nota operativa heredada de la P16: los comandos de compose se lanzan con `-f <ruta>` porque el manifiesto vive en `x:\Nexa\nexaagent`.)*

---

## 7. Aprendizajes clave

1. **Corpus ≠ dataset de evaluación.** El valor de un dataset de evaluación no es su tamaño, es que la respuesta correcta sea verificable con certeza. El histórico tiene volumen pero no ground truth etiquetado → es ruido sin etiquetar, no un camino a "evaluar a escala". El camino real es ampliar el corpus a propósito con huellas conocidas.

2. **Verificar el contrato de la función antes de medir sobre él.** `search()` devuelve `list[str]` (se leyó el código). Si hubiera devuelto objetos, el `huella in chunk` habría medido mentiras silenciosas. Un harness construido sobre una suposición del contrato mide basura con cara de dato.

3. **Definición honesta de "correcto" > número bonito.** Contar solo la huella exacta del proyecto preguntado (no las menciones cruzadas) hace que el harness refleje el techo real de ranking. Un evaluador complaciente que infla el número no sirve para nada — su única función es medir, y medir de más es peor que no medir.

4. **La duplicación degrada la recuperación, no la mejora.** Resultado contraintuitivo, ahora medido: tres copias idénticas del documento expulsaron un chunk correcto del top-k por redundancia que compite por los mismos puestos. Más copias = más ruido, no más señal.

5. **Medir corrige la intuición.** La hipótesis era que limpiar el corpus no movería el miss de "responsable de Rubí" (parecía puro problema de redacción). El dato la refutó: pasó de fuera-de-top-8 a rank 5. Que reaparezca en rank 5 y no rank 1 mostró que había **dos** causas a la vez (triplicación + redacción). Sin el harness, ninguna de las dos habría sido visible.

6. **El desglose por caso vale más que el número agregado.** `Recall@8 = 0.889` no dice *qué* falló; la fila `[-] Rubí "responsable"` sí. Es la versión formal de los ranks que se anotaban a mano, y es lo que convierte una métrica en un diagnóstico accionable.

7. **Un baseline contaminado no es un baseline.** Todo número sobre el corpus triplicado era ruido con decimales. El propósito de la sesión —una vara de medir limpia— exigía limpiar primero; aceptar el 0.889 contaminado habría sido construir el harness y negarse a usarlo bien.

8. **El piso de regresión se fija al número real, no al aspiracional.** El test guarda `Recall@8 == 1.0` porque la medición sobre corpus limpio lo justifica — no porque suene bien.

---

## 8. Comandos de referencia (nuevos de esta parte)

### Correr el harness de evaluación (contra la BD real con chunks)

```powershell
docker compose -f x:\Nexa\nexaagent\docker-compose.yml exec api python -m app.eval.retrieval
# Opcional: dataset alternativo como argumento
docker compose -f x:\Nexa\nexaagent\docker-compose.yml exec api python -m app.eval.retrieval app/eval/datasets/otro.yaml
```

### Correr el piso de regresión real (skip-gated)

```powershell
# El test real se salta por defecto; se activa con EVAL_REAL=1 en un entorno con corpus + Ollama
# (hoy: el servicio 'tests' no los tiene; se corre vía el script de arriba)
```

### Inspeccionar huellas y duplicados antes de un dedup

```powershell
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "SELECT document_id, count(*) FILTER (WHERE content LIKE '%RB-2023-Q7%') rubi, count(*) total FROM document_chunks GROUP BY document_id ORDER BY document_id;"
```

### Verificar que un FK es CASCADE antes de borrar el padre

```powershell
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "SELECT conname, confdeltype FROM pg_constraint WHERE conrelid='document_chunks'::regclass AND contype='f';"
# confdeltype = c  → CASCADE (borrar el documento arrastra sus chunks)
```

---

## 9. Estado y siguientes pasos

### Funcionando ✅ (nuevo en la Parte 17)

- **Harness de evaluación de recuperación** (`app/eval/retrieval.py`): Recall@k (1/3/5/8) + MRR + desglose por caso, sobre el retriever puro.
- **Dataset por huellas** (9 entradas / 4 proyectos), editable, agnóstico al tamaño, durable ante re-chunking.
- **Test de dos niveles:** aritmética determinista en CI + piso real skip-gated (`EVAL_REAL=1`).
- **Baseline limpio establecido:** Recall@1 = 0.556, Recall@3 = 0.889, Recall@5 = 1.000, Recall@8 = 1.000, MRR = 0.744.
- **BD depurada:** docs duplicados 6/7 borrados; doc 8 como corpus único single-copy.
- **Suite: 24 passed, 1 skipped.**

### El siguiente frente, ahora cuantificado 🎯

- **Re-ranking con cross-encoder** (pendiente desde la P10): el **Recall@1 = 0.556 / Recall@3 = 0.889** es la firma clásica de "el retriever encuentra pero no ordena". Las cuatro frases "código de autorización de X" compiten como falsos amigos léxicos: el bi-encoder (`nomic-embed-text`) colapsa la frase en un vector y pierde *cuál* entidad; un cross-encoder mira pregunta+chunk juntos y desempata. Por primera vez hay un **baseline contra el que medir** si el re-ranking funciona — justo lo que no había cuando se anotó en la P10. Candidato natural para la P18.

### Deudas / pendientes 🔜

- **Piso real en CI:** hoy se verifica con el script, no con pytest (`api` no trae pytest; `tests` usa BD vacía). Si se quiere corrible en CI-contra-real, añadir un servicio `eval` al compose (dev deps + URL real + `depends_on: ollama`). Anotado como opción futura, no urgente.
- **PyYAML transitiva** (6.0.3, funciona) sin pinear en `pyproject.toml`: frágil (depende de que otra dependencia la arrastre — lección del Error 4 de la P1). Salida limpia y fiel a "cero deps": **migrar el dataset a JSON** (stdlib) y eliminar el cabo, en vez de pinear.
- **Ampliar el dataset a escala (P18+):** corpus sintético de N proyectos con huellas conocidas, evaluado con *este mismo* harness — la versión a escala, con ground truth por construcción.
- **Heredadas:** `request_id: "-"` en la ingesta (P15/P16); `retry_backoff` de Celery (P15); falso positivo de dedup Paco≈Bruno (P15); `path_separator` en `alembic.ini`; drift de docs; `SecurityWarning` de Celery como root.

---

## 10. Temario de estudio (Parte 17)

> Conceptos transferibles que esta parte enseña. Para repaso.

### A. Evaluación de recuperación (RAG)

- **Recall@k**: fracción de consultas cuyo documento/chunk correcto aparece en los primeros k resultados. Medir varios k (1/3/5/8) de una corrida muestra el margen entre "encontrar" y "rankear primero".
- **MRR (Mean Reciprocal Rank)**: media de 1/posición del primer acierto (0 si no aparece). Penaliza que el resultado correcto esté abajo; complementa a Recall@k.
- **La firma "Recall@8 alto + Recall@1 bajo"**: el retriever *encuentra* los chunks correctos pero no los *ordena* primero — problema de ranking fino, no de cobertura. Es el caso de uso canónico del re-ranking.
- **Bi-encoder vs cross-encoder**: el bi-encoder (embeddings) colapsa cada texto en un vector independiente y compara por distancia (rápido, pero pierde matiz entre frases de plantilla similar); el cross-encoder mira consulta+candidato juntos y puntúa relevancia fina (lento, pero desempata). El re-ranking aplica un cross-encoder sobre el top-k del bi-encoder.

### B. Diseño de un dataset de evaluación

- **Corpus vs dataset de evaluación**: el corpus es lo que se indexa; el dataset son pares pregunta→respuesta-conocida. El valor del dataset es el **ground truth verificable**, no el volumen.
- **Ground truth por construcción**: usar huellas distintivas inventadas (`RB-2023-Q7`) da una verdad conocida sin etiquetado manual — superior a minar datos reales sin etiquetar.
- **Anclar el dataset a algo durable**: guardar la **huella** (no un `chunk_id`) hace que el dataset sobreviva a un re-chunking. Una referencia que cambia con cada re-proceso no sirve como regresión.
- **Robustez por redacción**: varias frases por entidad (con/sin tilde, por nombre, por código, redacciones distintas) ejercitan la sensibilidad del retriever al vocabulario.
- **Definición honesta de "correcto"**: contar solo el acierto estricto (la huella del proyecto preguntado, no menciones cruzadas) mide el techo real en vez de inflarlo.

### C. Testing de un evaluador

- **Probar la aritmética del evaluador con datos sintéticos** (ranks conocidos `[1, 3, None]` → Recall/MRR esperados): el guardián de que el cálculo no "mida mentiras", determinista y sin dependencias.
- **Separar el test determinista (CI) del piso contra datos reales (skip-gated)**: la regla de no meter no-determinismo (LLM/embeddings) ni dependencias pesadas en la suite estándar; el piso real se activa con una env var.
- **Verificar el contrato de la función bajo prueba** (`search -> list[str]`) antes de construir el evaluador sobre él.

### D. Higiene de datos y su efecto en métricas

- **La duplicación degrada la recuperación**: copias idénticas compiten por los puestos del top-k y expulsan chunks correctos — más copias es más ruido, no más señal.
- **Un baseline contaminado no es un baseline**: medir sobre un corpus que no representa producción (triplicado) produce números sin significado; limpiar precede a medir.
- **Verificar antes de una operación destructiva**: confirmar contenido (`count FILTER`) y el comportamiento del FK (`confdeltype = c`, CASCADE) *antes* del `DELETE`, no después.
- **El piso de regresión se fija al número medido**, no a uno aspiracional.

### E. Metodología (transversal)

- **Medir corrige la intuición**: la hipótesis "el dedup no moverá el miss de Rubí" fue refutada por el dato (se movió a rank 5), y reveló dos causas simultáneas. El harness existe precisamente para esto.
- **El desglose por caso convierte una métrica en un diagnóstico**: el número agregado dice *cuánto*; el desglose dice *qué* falló y *dónde*.
- **Construir la herramienta de medición sobre terreno conocido antes de escalar**: 4 proyectos que se entienden, luego el corpus grande sobre la herramienta ya validada.

---

*Cierre de la Parte 17.*
