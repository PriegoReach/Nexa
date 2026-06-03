# NexaAgent — Bitácora de desarrollo (Parte 30)

> Implementación y validación de la **dual cross-lingual** diagnosticada en la P29 — el cierre
> del hilo de recuperación que viene desde la P19 (techo del retriever, mismo idioma) → P28
> (caveat cross-lingual al traer docs de Drive) → P29 (diagnóstico por capa: la palanca ganadora
> es la dual). Esta sesión la **construye**, la **valida sobre corpus real** (5 docs en inglés,
> no los 22 chunks de un doc de la P29), elige el `FETCH_N` con dato, y la **commitea** a
> producción tras confirmar que el mecanismo de la P29 se sostiene con métrica robusta.

**Estado al cierre de la Parte 30:** la dual en producción, validada y commiteada. **Recall@1 cross-lingual 0.222 → 0.778** sobre 9 preguntas reales en español contra documentos en inglés (excluyendo una pregunta ambigua, R@1 0.875 / R@5 1.000). El mecanismo de la P29 confirmado sobre corpus real: la dual mete el doc inglés al pool, y el reranker multilingüe —que ya casaba es↔en— lo encuentra. **El español NO se degrada** (R@1 = 1.0 antes y después). `FETCH_N` subido a 40 **solo en modo dual** (el path monolingüe mantiene 20), elegido midiendo 30-vs-40: ambos dan el mismo Recall, pero 40 **duplica el colchón** de los candidatos al borde contra dropout del pool — la tercera rama (`vec_en`) empuja los docs españoles hacia abajo, y ese margen los protege. Commit tras un flag (`dual=True`), verificado con un curl real (pregunta ES → doc EN del NIST, log `query translated` confirma que la rama inglesa disparó) y suite 24/24 verde. El hilo cross-lingual P19→P28→P29→P30, cerrado.

---

## 1. El hilo que se cierra

La recuperación ha sido el techo recurrente del proyecto:
- **P19** (cierre Fase 1): el techo es la **sensibilidad del retriever a la redacción** — frases distintas, mismo idioma, recuperan distinto. Caso adversarial de laboratorio.
- **P28** (Drive→RAG): el caveat **cross-lingual** — pregunta ES sobre doc EN recupera mal. La versión cotidiana del techo de la P19, frecuente al traer docs en inglés de Drive.
- **P29** (diagnóstico): aislado por capa — el cuello es la **recuperación al pool**, no el ranking. El embedding nomic v1 (sesgado a inglés) hunde el doc inglés; el reranker multilingüe lo pone #1 *si lo ve*. Palanca ganadora medida: **dual** (dos búsquedas vectoriales separadas es+en-traducción, fusión RRF, reranker sobre el pool). Pero los agregados eran sobre 22 chunks (1 doc) — mecanismo nítido, métrica débil.
- **P30** (esta): implementar la dual y **validar la métrica sobre corpus real** antes de commitear.

El método ha sido constante: medir antes de cambiar (P18/P29), no pagar la palanca cara cuando la barata basta (la dual, no un embedding multilingüe nuevo), y no fijar un número en una corona de borde sin medir la alternativa.

---

## 2. La dual implementada

En `app/rag/retriever.py`, detrás de un flag (`dual=False` por defecto → producción intacta hasta el commit). El diseño de la P29, construido:

1. **Traducir la query a inglés** con el LLM (qwen2.5, una llamada corta). **Degradación fail-safe:** si la traducción falla o tarda, se degrada a solo-español — la búsqueda no se rompe (el patrón de degradación de Redis del proyecto).
2. **Dos búsquedas vectoriales separadas:** una con la query original (es), otra con la traducción (en). NO se mezclan los idiomas en una query — eso falló en la P29 (el español arrastra el embedding promediado). Cada rama produce su propio ranking en su propio espacio.
3. **La rama léxica se queda en es** — no ayuda cross-lingual pero no estorba al español.
4. **Fusión RRF** de las tres ramas (`vec_es` + `vec_en` + `lex_es`) con la misma fórmula `1/(K+rank)` que ya usaba `_retrieve`.
5. **El reranker bge-v2-m3** (multilingüe, P18) ordena el pool fusionado — sin cambios; ya estaba después de `_retrieve`, y ya sabía casar es↔en.

La clave del diseño (de la P29): **añadir** la rama inglesa, no **reemplazar** la española — translate-only resolvía cross-lingual pero rompía el español; la dual resuelve sin regresión.

---

## 3. La validación sobre corpus real

El corpus de la P29 era 1 doc inglés (22 chunks) — suficiente para el mecanismo, no para la métrica. Para P30 se ingestaron de Drive **5 docs reales en inglés**, variados (la ingesta de la P28):

| Doc | Chunks | Tema |
|---|---|---|
| #20 GFN Datacenters | 1 | ciudades de datacenters |
| #21 NIST CSWP.29 | 145 | ciberseguridad |
| #22 1706.03762 ("Attention Is All You Need") | 69 | transformers / ML |
| #23 wellarchitected | **3359** | AWS Well-Architected |
| #25 PMBOK | 1102 | gestión de proyectos |

(Un sexto, Algorithms-JeffE, falló la ingesta — PDF "2up" que `PdfReader` no extrajo, 0 chunks; excluido. El #23 con 3359 chunks es **anómalo** y domina el corpus — relevante para el margen del pool, sección 5.)

**Harness:** 9 pares `(pregunta_es, doc_id_correcto)` — preguntas reales en español sobre el contenido inglés (pilares de AWS, mecanismo de atención, funciones del NIST, propósito de una PMO, ciudades con datacenters).

### El resultado: el mecanismo confirmado con métrica robusta

| Métrica | Baseline | Dual |
|---|---|---|
| Recall@1 | 0.222 | **0.667 → 0.778*** |
| Recall@5 | 0.222 | **0.778 → 0.889*** |
| MRR | 0.222 | **0.722 → 0.833*** |
| Latencia/query | 0.23 s | 0.45 s |

*Los números mejoran al subir FETCH_N a 40 (sección 5). Excluyendo la pregunta ambigua (GFN, ver abajo): **R@1 0.875 / R@5 1.000**.

La dual **triplica el Recall cross-lingual**. Las 6 preguntas que arregla (transformers-atención, NIST ×2, PMBOK/PMO ×2, y mejora del pool en AWS) pasaron de **ausentes** (`-`, el doc inglés ni entraba al pool) a **recuperadas y #1 tras rerank** — exactamente el mecanismo de la P29 sobre corpus real.

### El caso PMO: mi hipótesis, refutada por el dato

En la P29 sospeché que el PMBOK (la Guía general) podría no cubrir el tema PMO, y pedí distinguir "el retriever falla" de "el doc no cubre el tema" con la firma k=20. **Los datos refutaron mi sospecha:** la dual recupera el PMBOK en ambas preguntas de PMO (pool rank 9→rerank #1, y rank 1→#1). El doc **sí** tiene contenido sobre PMO; el baseline simplemente no lo recuperaba — problema de retriever, no de cobertura. La firma k=20 pasó de ausente a presente #1. Bien que se midiera en vez de asumir.

### La pregunta ambigua (GFN-datacenters), no contada como fallo

"¿En qué ciudades hay centros de datos?" no recupera el doc #20 (1 chunk) — pero **no es un fallo de la dual.** El AWS Well-Architected (#23) habla extensamente de datacenters/regiones/AZs, así que es una **respuesta defendible** a una pregunta genérica. El "ground truth = #20" era una suposición; la pregunta no tiene un único doc correcto. Se trata como ambigua, no como fallo del mecanismo — el Recall *real* de la dual es mejor que el 0.778 que la incluye (de ahí el 0.875 excluyéndola).

---

## 4. La regresión del español: sin degradación, con una luz amarilla medida

| Métrica (docs ES, preguntas ES) | Baseline | Dual |
|---|---|---|
| R@1 / R@5 / MRR | 1.0 / 1.0 / 1.0 | 1.0 / 1.0 / 1.0 |

El español **no se degrada** — la dual añade la rama inglesa sin quitar la española, y el reranker rescata el doc español aunque la rama `vec_en` añada ruido al pool. Pero el dato fino reveló una **luz amarilla** que decidió el `FETCH_N` (sección 5): los docs españoles **bajaron en el pool** con la dual (proy-ámbar: rank 1 en baseline → 21 en dual). La tercera rama compite por los slots, empujando el español hacia abajo. Hoy el reranker lo salva, pero el margen era estrecho — un doc español a pocos slots de caerse del pool con más corpus.

---

## 5. La decisión `FETCH_N` 30-vs-40 — por margen, no por Recall

El reporte inicial de la dual usó `FETCH_N=20` y dejó dos candidatos al borde (TF-norecurrent en rank 21, proy-ámbar 16/20). Antes de fijar el número, se midió 30 vs 40 — porque "¿pasa?" no es lo mismo que "¿cuánto margen antes de caerse?":

| | FETCH_N=30 | FETCH_N=40 |
|---|---|---|
| R@1 / R@5 | 0.778 / 0.889 | 0.778 / 0.889 (idéntico) |
| MRR | 0.833 | 0.815 (−0.018) |
| TF-norecurrent #22 (rank/pool) | 21/30 — **9 slots** de colchón | 22/40 — **18 slots** |
| proy-ámbar #8 (rank/pool) | 21/30 — **9 slots** | 21/40 — **19 slots** |
| reranker final | #1 | #1 (ambos) |
| coste reranker | ~114 ms | ~170 ms (+55 ms) |

**El hallazgo que decidió:** el **rank absoluto** de los candidatos al borde casi no cambia (TF 21→22, ámbar 21→21) — su posición en la fusión RRF es **estable**. Lo que cambia es *el corte*: a 40, el mismo candidato queda más lejos del borde, así que **la holgura se duplica** (de ~9 a ~19 slots). 

El conflicto aparente (Recall pide ≥, margen pide 40) se resolvió: **R@1/R@5 —lo que el agente experimenta— son idénticos**; el único delta es MRR −0.018 por un reshuffle de NIST-identify de rank 2 a 3, que **se queda dentro del top-8 entregado** (el agente lo recibe igual). Cambiar un orden interno invisible al agente, a cambio de duplicar el colchón contra dropout del pool, es el trade-off correcto. **Margen real > reshuffle invisible.** El coste (+55 ms) es despreciable frente a los segundos de la generación del LLM.

→ **`FETCH_N=40` en modo dual.** Y solo en modo dual: el path monolingüe mantiene `FETCH_N=20` (la dual no impone su costo de pool al caso que no la necesita — respeta "el español se comporta como antes").

> Por qué el margen no era paranoia: la rama `vec_en` empuja el español hacia abajo (proy-ámbar 1→21). Con `FETCH_N=30`, ese doc quedaba a 9 slots de caerse; a 40, a 19. Sin esta medición se habría commiteado 30 con el español al borde, y el día que el corpus creciera, un doc español legítimo se habría caído del pool en silencio — el límite-al-borde de la P19, evitado por medir la alternativa.

---

## 6. El commit

- **`knowledge_base.py`:** la tool flipea a `search(query, k=8, dual=True)` — el cambio que activa la dual en producción.
- **`retriever.py`:** `DUAL_FETCH_N = 40` (con la razón documentada); `search()` selecciona `fetch_n = DUAL_FETCH_N if dual else FETCH_N`. El 40 aplica solo en dual; el path es-only no cambia.
- **Verificación end-to-end:** un curl real — "¿Cuáles son las funciones principales del marco de ciberseguridad del NIST?" → respondió las 6 funciones del CSF (Govern, Identify, Protect, Detect, Respond, Recover) en español, desde el doc inglés. El log `query translated` confirma que la rama `vec_en` disparó de verdad (no fue el monolingüe acertando por suerte).
- **Suite 24/24 verde** (1 skipped, el piso de siempre).

---

## 7. Aprendizajes clave

1. **Validar el mecanismo sobre corpus real, no solo diagnosticarlo.** La P29 probó el mecanismo sobre 22 chunks (nítido pero no robusto); P30 lo confirmó sobre 5 docs variados (R@1 0.22→0.78). Mecanismo y métrica son dos pruebas distintas — la segunda necesita datos que la primera no.

2. **Añadir una rama vs reemplazar la consulta.** La dual añade la búsqueda inglesa sin quitar la española → cross-lingual resuelto Y español sano. Reemplazar (translate-only) habría roto el español. Para no regresar lo que funciona, añadir.

3. **"¿Pasa?" no es "¿cuánto margen?".** Un candidato que pasa en rank 21 de 30 está al borde; el mismo en rank 22 de 40 tiene el doble de colchón. Medir el margen, no solo el éxito, evita un límite-al-borde que vuelve cuando el contexto crece (la lección de la P19).

4. **El rank en la fusión es estable; lo que mueves es el corte.** Subir FETCH_N no reordena los candidatos (su rank RRF apenas cambia) — aleja el corte, dando holgura. Entender qué mueve un parámetro (el corte, no el orden) decide cómo elegirlo.

5. **Una tercera rama compite por el pool.** La dual mete `vec_en` al pool, empujando el español hacia abajo (rank 1→21). El reranker lo rescata hoy, pero el margen del pool baja — un costo no obvio de la fusión que el FETCH_N mayor compensa.

6. **Recall que el agente experimenta vs métricas internas.** R@1/R@5 (lo que el agente recibe) idénticos entre 30 y 40; el MRR −0.018 es un reshuffle dentro del top-8 entregado, invisible al agente. Optimizar la métrica que importa de cara al usuario, no una interna que no cambia su experiencia.

7. **Medir refuta la propia hipótesis.** Sospeché que el PMBOK no cubría PMO; el dato mostró que sí (el baseline solo no lo recuperaba). La firma k=20 distinguió "retriever falla" de "doc no cubre" — y el método protegió contra atribuir mal el fallo.

8. **No todo "fallo" del harness es del mecanismo.** La pregunta de datacenters es ambigua (otro doc es respuesta defendible); contarla como fallo subestima la dual. Distinguir un falso negativo del harness de un fallo real del sistema.

---

## 8. Comandos de referencia (nuevos de esta parte)

### Activar/verificar la dual

```python
# knowledge_base.py — la tool ahora llama:
search(query, k=8, dual=True)   # dual activa en producción
# retriever.py: DUAL_FETCH_N = 40 aplica solo en modo dual; FETCH_N=20 para monolingüe
```

```powershell
# verificar end-to-end: pregunta ES sobre doc EN (debe responder bien + log "query translated")
curl.exe -X POST http://localhost:8000/chat -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" -d '{\"message\": \"¿Cuáles son las funciones principales del marco de ciberseguridad del NIST?\"}'
```

### Correr el harness de validación (apuntar a DUAL_FETCH_N para reflejar producción)

```powershell
docker exec nexaagent-api-1 python -m app.eval.dual_validation   # baseline vs dual, Recall@1/@5/MRR + regresión ES
# NOTA: el harness importa FETCH_N=20 para su columna dual; apuntarlo a DUAL_FETCH_N=40 para que
# refleje el pool de producción (deuda menor anotada).
```

---

## 9. Estado y siguientes pasos

### Funcionando ✅ (nuevo en la Parte 30)

- **Dual cross-lingual en producción:** R@1 cross-lingual 0.22→0.78 (0.875 excl. la pregunta ambigua), sin degradar el español, latencia +0.22 s (trivial frente a la generación).
- **`FETCH_N=40` en modo dual** (20 en monolingüe), elegido por margen de pool — duplica el colchón de los candidatos al borde.
- **Mecanismo de la P29 confirmado sobre corpus real** (5 docs variados, no 22 chunks).
- **El hilo cross-lingual cerrado** (P19→P28→P29→P30): la palanca barata (dual) resolvió lo que parecía pedir la cara (embedding multilingüe).

### Deudas / pendientes 🔜

- **El harness `dual_validation.py` importa `FETCH_N=20`** para su columna dual — ya no refleja el pool de producción (40). Apuntarlo a `DUAL_FETCH_N` antes de re-correrlo como referencia (deuda menor; el commit se verificó por curl real, no por el harness).
- **El doc #23 (wellarchitected, 3359 chunks) es anómalo y domina el pool** — fue lo que enterró candidatos y obligó a subir FETCH_N. Re-ingestarlo con mejor chunking haría respirar el pool (3359 chunks de un PDF es excesivo; posible problema de extracción/estructura como el de la P10).
- **Algorithms-JeffE quedó fuera** (0 chunks, PDF "2up" que `PdfReader` no extrae) — si se quisiera, necesitaría otro extractor (OCR o un parser de layout).
- **Heredadas:** el 14B (tres señales en contra, P28); clasificador de fallos de envío (P27); tokens en claro (P25); OOM del host (P27); CRUD de Calendar incompleto; `request_id`, `retry_backoff`, `path_separator`, docs drift, Celery root.

### Lo que sigue

- **Fase 3 — Interfaz:** la dirección de mayor valor ahora. El contrato de la API estable (Fase 2 cerrada), patrones ricos que mostrar (confirmación, OAuth, ingesta de Drive, y ahora recuperación cross-lingual), y el sistema dejaría de ser `curl`.
- **Completitud de integraciones:** CRUD de Calendar, Sheets/Slides de Drive (con el camino export + parseo tabular/OCR).
- **Mejoras de RAG pendientes:** el chunking del doc #23, o un embedding multilingüe si el cross-lingual se volviera central (hoy la dual basta).

---

## 10. Temario de estudio (Parte 30)

> Conceptos transferibles que esta parte enseña. Para repaso.

### A. Implementar y validar una palanca diagnosticada

- **Mecanismo vs métrica como pruebas distintas**: diagnosticar el mecanismo (P29, corpus diminuto) no es validar la métrica (P30, corpus real). Una palanca se elige por el mecanismo y se commitea por la métrica robusta.
- **Flag antes del commit**: implementar detrás de un flag (`dual=False`) deja producción intacta mientras se valida; commitear es flipear el flag tras ver la tabla. Separar "construido" de "activo".
- **Verificación end-to-end real**: un curl que dispara la rama nueva (log `query translated`) confirma que el mecanismo operó de verdad, no que el monolingüe acertó por suerte.

### B. Recuperación híbrida con múltiples ramas

- **Añadir una rama empuja a las demás**: una tercera rama (vec_en) compite por los slots del pool, bajando el rank de las otras (el español). El reranker rescata, pero el margen del pool baja — un costo no obvio.
- **El parámetro de pool mueve el corte, no el orden**: subir FETCH_N no reordena (el rank RRF es estable); aleja el corte, dando holgura. Entender qué mueve un parámetro decide cómo elegirlo.
- **Margen vs éxito**: "¿el candidato pasa?" (rank 21/30) ≠ "¿cuánto margen tiene?" (9 vs 19 slots). Medir el colchón contra dropout, no solo el éxito puntual.

### C. Decidir un parámetro con métricas en tensión

- **La métrica que el usuario experimenta manda**: R@1/R@5 (lo que el agente recibe) sobre MRR (orden interno) cuando el delta de MRR queda dentro del contexto entregado. Optimizar lo que el usuario nota.
- **Margen futuro > optimización puntual**: elegir el parámetro que protege contra dropout cuando el corpus crezca, no el que maximiza una métrica hoy por un pelo. El límite-al-borde de la P19, evitado.

### D. Honestidad en la evaluación

- **Refutar la propia hipótesis con dato**: la sospecha sobre el PMBOK/PMO se midió y refutó; la firma k=20 distinguió "retriever falla" de "doc no cubre". El método protege contra atribuir mal.
- **Distinguir falso negativo del harness de fallo real**: una pregunta ambigua (datacenters) cuenta como "fallo" en la tabla pero no es del mecanismo; reconocerlo evita subestimar la solución.

---

*Cierre de la Parte 30. El hilo cross-lingual (P19→P28→P29→P30) resuelto: la dual en producción, validada sobre corpus real, con la palanca barata donde parecía pedirse la cara.*
