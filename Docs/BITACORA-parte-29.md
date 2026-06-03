# NexaAgent — Bitácora de desarrollo (Parte 29)

> Sesión de **diagnóstico medido** (cero cambios en producción), en la familia de la P17:
> medir antes de cambiar. P28 destapó un problema cross-lingual — una pregunta en español sobre
> un documento en inglés recupera el doc equivocado. Antes de elegir una palanca (de barata a
> cara), esta sesión **aísla la causa por capa** del retriever y **mide** cuánto resuelve cada
> palanca candidata, para decidir con dato qué implementar en la P30 — exactamente como el
> reranking de la P18 se justificó con el Recall, no con intuición.

**Estado al cierre de la Parte 29:** la causa del problema cross-lingual aislada con precisión, y la palanca ganadora elegida por dato — sin tocar producción (solo `app/eval/`). **Diagnóstico:** el cuello de botella es la **recuperación al pool**, no el ranking. El embedding `nomic-embed-text` v1 (sesgado a inglés) hunde el doc inglés al **rank 22/22** con query española; la rama léxica **no lo matchea** (el AND de `plainto_tsquery` + cero solape de tokens es↔en); pero el **reranker bge-v2-m3 (multilingüe) lo pone #1** — ya sabe casar es↔en, solo que el doc nunca le llega. **Palancas refutadas por el dato:** la léxica `simple` (Causa 3, mi hipótesis previa — no era el stemming) y la expansión es+en concatenada (el español arrastra el vector). **Ganadora:** **doble búsqueda (dual)** — traducir la query a inglés con el LLM, lanzar una segunda rama vectorial, fusionar (RRF) con la rama española, y dejar que el reranker multilingüe ordene el pool combinado. R@1=1.0 cross-lingual sin degradar el español. La palanca cara (embedding multilingüe) **no hace falta**. Pendiente para P30: validar la métrica sobre un corpus inglés más grande (el mecanismo es nítido; los agregados son sobre una muestra diminuta).

---

## 1. El problema y por qué el diagnóstico va antes de la palanca

P28 dejó un caveat: la pregunta en español "datacenters de GFN en Estados Unidos" recuperó el doc equivocado ("Proyecto Ámbar"); el doc inglés correcto solo apareció con la query en inglés ("US-based datacenters"). Es la versión cross-lingual del techo de retriever que la P19 identificó (sensibilidad a la redacción), ahora entre idiomas.

"Cross-lingual" tiene **tres causas posibles con costos radicalmente distintos**, y atacar la equivocada desperdicia la sesión (el error de "pagar el 14B cuando era el prompt"):

| Causa | Palanca | Costo |
|---|---|---|
| El embedding no alinea es↔en | cambiar a embedding multilingüe | **alto** (re-embeddear todo + dependencia + papers dicen que el sesgo persiste) |
| El balance híbrido penaliza (rama léxica en español) | ajustar la rama léxica | bajo-medio |
| La query mal formada para recuperar | traducir/expandir la query | bajo |

Hechos verificados antes de medir (sección de contexto del diagnóstico): el embedding es `nomic-embed-text` **v1** (274 MB, sesgado a inglés — NO el v2-moe multilingüe); la rama léxica usa `plainto_tsquery('es_simple', ...)` y **filtra** con `WHERE content_tsv @@ ...`; y la literatura documenta el "English inclination problem" — incluso recuperadores multilingües priorizan docs ingleses no relacionados sobre el doc correcto en el idioma de la consulta, lo que vuelve la palanca cara **costosa y posiblemente insuficiente**. Por eso: medir cuánto resuelven las baratas antes de considerar la cara.

---

## 2. El método: harness cross-lingual + la firma k=20 de la P19

Toqué solo `app/eval/` (scripts + datasets de medición). `retriever.py`, el embedding y `config` intactos — diagnóstico puro.

- **Mini-harness cross-lingual:** 6 preguntas en español sobre el doc inglés ingestado (#20, GFN Datacenters), incluida la query exacta que falló en P28 + variantes naturales. Pares `(pregunta_es, chunk_correcto)`.
- **La firma k=20 (P19):** por cada par que falla, ¿el chunk correcto está en el top-20 del retriever pero mal rankeado (problema de orden) o **no está** (problema de recuperación)? Esta firma es la que distingue "se recupera mal" de "no se recupera" — y decide qué palancas pueden siquiera funcionar.

---

## 3. El hallazgo: aislamiento por capa

El diagnóstico no midió solo el agregado — **aisló qué hace cada capa** del pipeline con la query española sobre el doc inglés. Eso es lo que volvió el resultado nítido:

| Capa | Señal medida | Resultado (en los 6 casos) |
|---|---|---|
| **Embedding vectorial** | rank del doc inglés (de 22), query es | **22/22 (último)** — el sesgo-a-inglés de nomic v1, cuantificado |
| **Rama léxica** | ¿matchea el doc inglés? (`es_simple` y `simple`) | **False / False** — el AND de `plainto_tsquery` exige todas las palabras españolas; el doc inglés no tiene ninguna |
| **Reranker (bge-v2-m3)** | posición del doc inglés sobre todos los chunks, query es | **#1** — ya resuelve el cross-lingual si VE el doc |

**El diagnóstico de manual:** el reranker multilingüe **ya sabe casar español↔inglés perfectamente** — pone el doc correcto #1. El problema es que **el doc nunca llega al reranker**, porque ni el vector (lo hunde al 22) ni el léxico (no lo matchea) lo meten en el pool de candidatos que el reranker reordena. **El cuello de botella es la recuperación al pool, no el ranking.**

Esto tiene una consecuencia directa sobre la elección de palanca: **no hace falta un embedding multilingüe nuevo** (la palanca cara), porque ya existe un *reranker* multilingüe que hace el trabajo. Solo hay que **conseguir que el doc correcto entre al pool** para que el reranker lo encuentre. La solución es de recuperación, no de ranking ni de embedding.

---

## 4. Las palancas, medidas — dos refutadas, una ganadora

| Config | Cross-lingual R@1 / R@5 | Español (regresión) R@1 / R@5 |
|---|---|---|
| baseline | **0.000 / 0.000** | 1.000 / 1.000 |
| léxico `simple` (Causa 3) | 0.000 / 0.000 ✗ | — |
| expansión es+en concatenada | ~0.167 ✗ | — |
| translate (en-only) | 1.000 / 1.000 ✓ | 1.000 / 1.000 (engañoso*) |
| **dual (es + en-traducción, fusionado RRF)** | **1.000 / 1.000 ✓** | **1.000 / 1.000 ✓** |

*Con translate-only, los ranks del retriever para docs españoles caen a 14-18/22 — solo "pasan" porque el corpus tiene 22 chunks. En un corpus real, translate-only **rompería el español**. dual mantiene los ranks españoles en 1-12 (sano).

**Lo refutado por el dato (no implementar):**
- **Léxico `simple` (Causa 3):** 0.000, no ayuda nada. El problema NO era el stemming (mi hipótesis previa) sino el **AND de `plainto_tsquery`** + cero solape de tokens es↔en. Medirlo en vez de implementarlo ahorró trabajo desperdiciado.
- **Expansión es+en concatenada:** ~0.167. Concatenar los dos idiomas en *una* query produce un embedding promediado que **el español sigue dominando**. "Expandir" mezclando idiomas en un texto no funciona — el embedding es uno solo.

**Lo que distingue a la ganadora (dual) de la expansión fallida:** no se mezclan los idiomas en una query — se lanzan **dos búsquedas separadas** (una con la query española, una con su traducción inglesa), cada una en su propio espacio sin contaminar al otro, y se **fusionan los rankings (RRF)**. La rama española encuentra los docs españoles, la rama inglesa encuentra los ingleses, y el reranker multilingüe ordena el pool combinado. Por eso funciona donde la concatenación falló: separación de idiomas, no mezcla.

**Por qué translate-only no es la ganadora aunque dé 1.0:** traducir *reemplazando* la query sacrifica el español (los docs españoles caen en rank). La dual **añade** la rama inglesa sin quitar la española — cross-lingual resuelto Y español sano. La diferencia entre reemplazar y añadir.

**Coste de la dual:** +1 traducción LLM y +1 embed por query. La palanca cara (embedding multilingüe) **no hace falta** — la dual lleva el Recall@1 a 1.0. El orden de costos del diseño, confirmado: la última opción sigue siendo la última.

---

## 5. La honestidad sobre los límites (estilo P19)

El número es nítido en el mecanismo, débil en el agregado — y la distinción decide el siguiente paso:

- **Corpus diminuto:** 22 chunks, 1 solo doc inglés (1 chunk). Recall@20 sobre 22 discrimina poco, y el "translate R@1=1.0 en español" es un **artefacto del tamaño** (con 22 chunks, casi todo "pasa").
- **Lo que SÍ es sólido (no depende del tamaño):** el **mecanismo** — es-only=22, en-only=1-4, reranker=#1. Esos números son cualitativos y nítidos: dicen qué capa falla y cuál resuelve, independiente de cuántos chunks haya.
- **Lo que NO es sólido aún:** los **agregados** de la dual (el "1.0", el "español sano"). Deben revalidarse en un corpus inglés más grande y variado antes de commitear.
- **Los 6 pares apuntan al mismo doc/chunk:** suficiente para diagnosticar el mecanismo, no para una métrica robusta.
- **dual añade latencia + dependencia del LLM** (modo de fallo: mala traducción). Aquí las traducciones salieron limpias, pero el 7B falla en síntesis (P25) — una query compleja podría traducirse mal.

**La conclusión que separa mecanismo de métrica:** el mecanismo está *probado* (el reranker resuelve, el doc no llega al pool, la dual lo mete) → justifica **elegir** la dual. Pero los *números* no son robustos → commitear la dual al retriever de producción (con su dependencia del LLM por query y su latencia) merece validación sobre un corpus real. Por eso P30 = implementar la dual **+ validarla** sobre más docs reales, no implementarla a ciegas sobre la señal diminuta.

---

## 6. Aprendizajes clave

1. **Aislar por capa convierte un síntoma difuso en un diagnóstico nítido.** "Cross-lingual no funciona" es difuso; "el embedding hunde al 22, el léxico no matchea, el reranker pone #1" señala exactamente dónde está el cuello (la recuperación al pool) y qué capa ya lo resuelve (el reranker). Medir cada capa por separado > medir el agregado.

2. **El cuello puede no estar donde duele.** El síntoma es cross-lingual (parece problema de embedding/idioma), pero la causa es de *recuperación al pool* — el reranker multilingüe ya casa es↔en; solo no ve el doc. La solución no es la que el síntoma sugiere (embedding nuevo) sino meter el doc al pool.

3. **Medir refuta hipótesis baratas también, no solo caras.** La léxica `simple` era mi hipótesis (Causa 3) — la medición la refutó (el problema era el AND, no el stemming). Medir ahorró implementar la palanca equivocada, aunque fuera barata. El método protege contra errores en ambas direcciones de costo.

4. **Mezclar idiomas en una query no es lo mismo que buscar en dos.** La expansión concatenada (es+en en un texto) falla porque el embedding promediado lo domina el español; la dual (dos ramas separadas + RRF) funciona porque cada idioma busca en su espacio. La estructura de la solución importa, no solo la idea de "usar ambos idiomas".

5. **Reemplazar vs añadir.** Translate-only resuelve cross-lingual pero sacrifica el español (reemplaza la query); la dual añade la rama inglesa sin quitar la española (resuelve sin regresión). Para no romper lo que funciona, añadir una rama > reemplazar la consulta.

6. **Reusar una capacidad que ya existe > construir una nueva.** No hace falta embedding multilingüe (caro) porque ya hay un reranker multilingüe (P18); la dual solo lo alimenta. Mirar qué piezas ya resuelven parte del problema antes de añadir una pieza cara.

7. **Mecanismo probado ≠ métrica robusta.** La señal diminuta prueba el mecanismo (qué capa falla/resuelve, independiente del tamaño) pero no la métrica (los agregados son artefactos de 22 chunks). Distinguir las dos decide si se puede implementar ya (mecanismo) o hace falta más datos (métrica) — aquí, validar antes de commitear.

---

## 7. Comandos de referencia (nuevos de esta parte)

### Diagnóstico por capa (qué hace cada una con la query es sobre el doc en)

```powershell
# rank vectorial del doc inglés con query española (¿lo hunde el embedding?)
docker exec nexaagent-api-1 python -c "import asyncio; from app.eval.crosslingual import vector_rank; print(asyncio.run(vector_rank('pregunta en español', doc_id=20)))"
# ¿la rama léxica matchea el doc inglés?
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "SELECT id FROM document_chunks WHERE document_id=20 AND content_tsv @@ plainto_tsquery('es_simple', 'pregunta español');"
# ¿el reranker lo pone #1 si lo ve? (pasarle todos los chunks)
docker exec nexaagent-api-1 python -c "from app.rag.reranker import rerank; ..."
```

### Correr el harness cross-lingual (baseline vs palancas)

```powershell
docker exec nexaagent-api-1 python -m app.eval.crosslingual_harness   # baseline / lex simple / expand / translate / dual
```

---

## 8. Estado y siguientes pasos

### Logrado en la Parte 29 ✅ (diagnóstico, sin cambios en producción)

- **Causa cross-lingual aislada por capa:** embedding hunde al 22, léxico no matchea, reranker pone #1. Cuello = recuperación al pool, no ranking.
- **Baseline cross-lingual medido:** R@1 = R@5 = 0.000 (el número a batir).
- **Firma k=20:** el doc inglés NO entra al top-20 (problema de recuperación, no de orden).
- **Palancas refutadas por dato:** léxico `simple` (el AND, no el stemming) y expansión concatenada (el español arrastra).
- **Ganadora elegida:** dual (dos ramas + RRF + reranker multilingüe) — R@1=1.0 cross-lingual, español sano.
- **Palanca cara descartada:** el embedding multilingüe no hace falta (el reranker ya casa es↔en).

### Deudas / pendientes 🔜

- **Validar la dual sobre corpus real (lo primero de P30):** el mecanismo es nítido, los agregados son sobre 22 chunks. Requiere más docs reales en inglés + preguntas reales (insumo del desarrollador).
- **El modo de fallo de la traducción** en la dual: el 7B traduce la query; una query compleja podría salir torcida (el 7B falla en síntesis, P25). Validar con preguntas reales variadas.
- **Latencia de la dual:** +1 traducción LLM + 1 embed por query — medir si es tolerable en uso real.
- **Heredadas:** el 14B (tres señales en contra, P28); clasificador de fallos de envío (P27); tokens en claro (P25); OOM del host (P27); `request_id`, `retry_backoff`, `path_separator`, docs drift, Celery root.

### Lo que sigue

- **P30 — implementar la dual + validarla:** sobre 3-5 docs reales en inglés de Drive + 8-10 preguntas reales en español. Implementar la doble búsqueda en `retriever.py` (segunda rama vectorial con la query traducida, fusión RRF, reranker sobre el pool), medir el antes/después con el harness ampliado, y commitear solo si la métrica robusta confirma el mecanismo. Vigilar la regresión del español y la latencia.
- **Fase 3 — Interfaz** y **completitud de integraciones:** las direcciones de mayor valor tras cerrar el hilo cross-lingual.

---

## 9. Temario de estudio (Parte 29)

> Conceptos transferibles que esta parte enseña. Para repaso.

### A. Diagnóstico por capa

- **Aislar cada etapa del pipeline**: medir qué hace el embedding, la rama léxica y el reranker por separado con el mismo caso convierte un síntoma difuso en un cuello identificado. El agregado oculta; las capas revelan.
- **El cuello no siempre está donde apunta el síntoma**: un problema "cross-lingual" resultó ser de recuperación al pool, no de embedding — el reranker ya casaba idiomas; el doc no le llegaba.
- **La firma k=20 (de P19)**: ¿el correcto está en el pool pero mal ordenado, o no está? Distingue problema de ranking de problema de recuperación, y decide qué palancas pueden funcionar.

### B. Recuperación cross-lingual

- **El "English inclination problem"**: recuperadores (incluso multilingües) priorizan docs ingleses no relacionados sobre el doc correcto en el idioma de la consulta. Un fenómeno documentado, no un bug propio.
- **Mezclar idiomas en una query vs dos búsquedas separadas**: concatenar es+en en un texto produce un embedding que un idioma domina; lanzar dos ramas (cada una en su espacio) + fusionar las recupera ambos. La estructura importa.
- **Reemplazar vs añadir la consulta**: traducir reemplazando rompe el idioma original; añadir una rama traducida preserva ambos. Para no regresar lo que funciona, añadir.
- **Reusar el reranker multilingüe**: si ya hay una capa que casa idiomas (el reranker), la solución es alimentarla (meter el doc al pool), no añadir una pieza cara (embedding multilingüe).

### C. Disciplina de medición (continuación de P17/P18)

- **Medir refuta hipótesis de cualquier costo**: la palanca barata equivocada (léxico simple) se refutó con dato igual que se descartaría una cara. El método protege en ambas direcciones.
- **Mecanismo probado ≠ métrica robusta**: una muestra diminuta puede probar el mecanismo (qué capa falla, independiente del tamaño) sin dar una métrica fiable (agregados como artefactos del tamaño). La distinción decide si implementar ya o validar más.
- **Diagnóstico sin tocar producción**: medir en `app/eval/` con variantes temporales, decidir la palanca con el número, e implementar en una sesión posterior — la disciplina de P18/P19.

---

*Cierre de la Parte 29. Diagnóstico cross-lingual cerrado; la dual elegida por dato, pendiente de validación robusta en P30.*
