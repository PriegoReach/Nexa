# NexaAgent — Bitácora de desarrollo (Parte 19)

> Continuación de la Parte 18. Objetivo: **poner a prueba el re-ranking a escala**. La P18 dio
> Recall@1 = 1.0 sobre 4 entidades, pero con el asterisco explícito: "1.0 sobre 4 prueba el
> mecanismo, no que escala". Esta parte construye un corpus sintético grande para responder
> la pregunta abierta — y la respuesta resultó más matizada (y más valiosa) que un sí/no.

**Estado al cierre de la Parte 19:** la Fase 1 (Q&A documental al máximo) queda cerrada con un diagnóstico honesto, no con un número de victoria. Hallazgo central: **el re-ranking escala y desambigua semántica a 50 entidades de plantilla idéntica (✓), pero el techo real del sistema es la sensibilidad del *retriever* a la redacción de la consulta** — ciertas frases no recuperan el chunk correcto ni al top-20, así que el re-ranker nunca lo ve. Recall@1 = 0.820 sobre el peor caso adversarial (cinco entidades casi calcadas por sector). El camino hasta este número incluyó dos tests inválidos cazados por gates y un 1.0 sospechoso desenmascarado por una corrida de control. Esta bitácora documenta el método tanto como el resultado, porque los tropiezos y cómo se atraparon son la parte instructiva.

---

## 1. Punto de partida y reencuadre

La P18 cerró con Recall@1 = 1.0 sobre los 4 proyectos reales, y una pregunta explícita: ¿el re-ranking *escala* o solo *funciona* sobre un corpus pequeño? Con 4 entidades muy distintas, el cross-encoder lo tiene fácil; la prueba real es a escala con entidades similares.

**Reencuadre corpus-vs-dataset (decisión de diseño).** "Evaluar a escala" no significa minar el histórico de conversaciones — eso da volumen sin ground truth etiquetado (ya discutido en la P17). El camino correcto es **generar un corpus sintético** de N entidades con huellas conocidas: ground truth por construcción, escala controlada. La distinción que define la prueba:

- **N entidades variadas** (distinto sector, redacción, campos) → NO estresa nada. Cincuenta entidades distintas entre sí son cincuenta problemas fáciles.
- **N entidades de plantilla idéntica** (misma estructura, varían solo nombre/huella/atributos) → ESTO es la prueba. Frases casi idénticas = el infierno de los falsos amigos léxicos a escala, donde el bi-encoder se ahoga y el cross-encoder debe ganarse el sueldo.

Decisión de la curva (no un punto único): medir en **N ∈ {10, 25, 50, 100}** para ver la *forma* de la degradación, no solo un número. Y la inyección con **marcador** (`synthetic_eval_`) en el filename, para limpieza disciplinada entre puntos (como los docs 9/10 de la P15).

---

## 2. Tropiezo #1: el corpus que nunca se inyectó

Primer intento de la curva. El generador imprimió "n=10: 20 preguntas → synthetic_eval_10.json", y se midió de inmediato:

```
Recall@1: 0.000 ... todas las filas [-]
```

**No era un hallazgo — era un test inválido.** Dos comandos lo gritaron: el `count` de chunks sintéticos dio **0**, y el `DELETE` final borró **0 filas**. Se midió recuperación sobre un corpus que no existía en la BD. El retriever no tenía nada que traer → todo `[-]` → Recall 0.000.

**La causa raíz:** generar el archivo/dataset ≠ inyectarlo en la BD. Se saltó el paso de ingesta. Un `find` lo confirmó: existían los cuatro `.json` (datasets) pero **ningún `.txt`** (corpus) — el generador escribía las preguntas pero no el corpus a inyectar.

**La lección, hecha protocolo:** el `count > 0` es un *gate* que va **antes** de medir, no una comprobación posterior. Es la misma trampa del "test inválido" de la P6 y los "logs fósiles" de la P15: medir sobre lo que crees que está, no sobre lo que está. Esta vez se incorporó al código (sección 4).

---

## 3. Tropiezo #2 → el giro a la Opción B

Investigando cómo inyectar, se confirmó con el código real (`_read_file`) que el pipeline **sí acepta `.txt`** (si el sufijo no es `.pdf`, hace `read_text`). Pero todo el enredo —¿dónde está el archivo?, ¿se generó?, ¿lo subo por Swagger o lo ingiero a mano?— venía de tratar un corpus *generado por código* como un documento subido por un humano.

**Decisión: Opción B — el generador inserta los chunks + embeddings directo en la BD**, saltándose el archivo y el parseo. Razón de fondo, y es honesta: lo que el harness evalúa es *¿el retriever encuentra el chunk correcto?*, y eso depende **exclusivamente** de que el chunk y su vector estén en la BD, calculados con el mismo modelo — no de cómo llegaron ahí. Un chunk insertado por INSERT directo es indistinguible, para el retriever, de uno que vino de un PDF. B no falsea nada; elimina ceremonia irrelevante que ya costó dos pasos fallidos.

### 3.1 Los puntos críticos de B, resueltos contra el código real

1. **Columna full-text:** `content_tsv` es `GENERATED ALWAYS AS to_tsvector('es_simple', content) STORED`. Insertar `content` la puebla **sola** → el lado léxico del retriever híbrido encuentra los chunks gratis. No se mide medio retriever (era el riesgo silencioso más feo de B).
2. **Formato del vector:** el ORM `Vector` de pgvector acepta la lista de floats directa (ni `str()` ni placeholder).
3. **Función de embedding:** `get_embeddings()` de `app.rag.embeddings` — la **misma** que la ingesta real → mismo espacio vectorial que `embed_query`. (El sketch inicial decía `app.rag.ingest`; se corrigió contra el código.)
4. **Bonus cazado leyendo el esquema:** `documents.created_at` es `NOT NULL` sin server default → un `INSERT (filename, status)` crudo habría reventado. Se usa el ORM para que aplique `default=_now`. Es justo el tipo de detalle que solo se conoce leyendo el código, no asumiendo.

### 3.2 El gate, ahora dentro del código

`curve.py` por cada N: `limpiar()` → `generar_e_insertar` → **GATE** (`chunks==N` y `con_vector==N`, si no → `SystemExit`, no mide) → `evaluate` → `limpiar()`. La lección del tropiezo #1 hecha código: el gate va antes de medir, y el código lo impone. Ya no se puede medir sobre el vacío.

---

## 4. La prueba de humo que salvó la corrida

Antes de lanzar la curva completa, una verificación de cordura: insertar N=10 y hacer UN `search` manual de un proyecto sintético.

**Primera vez (con la Opción A fallida, antes de B):** `search("sintético 7")` devolvió **Rubí, Esmeralda y Ámbar** — los proyectos reales. El gate (`count`) confirmó 0 chunks sintéticos: no se habían insertado. El smoke atrapó el problema antes de medir 4 puntos falsos.

**Tras la Opción B:** gate `chunks=10, con_vector=10` ✓, y `search("sintético 7")` trajo el **Sintético 7 con `SYN-007-X7` en posición 1**, seguido del 8 y el 6 (sus vecinos numéricos). Pipeline B funcionando end-to-end. Y un dato gratis: los 4 reales NO contaminaron el top-3 → a escala pequeña los sintéticos dominan su propia búsqueda, no hace falta sacarlos.

---

## 5. La curva con faro numérico → el 1.0 sospechoso

Con el pipeline validado, la curva completa:

```
   N      R@1      R@3      R@5      R@8      MRR
  10    1.000    1.000    1.000    1.000    1.000
  25    1.000    1.000    1.000    1.000    1.000
  50    1.000    1.000    1.000    1.000    1.000
 100    1.000    1.000    1.000    1.000    1.000
```

Recall@1 = 1.0 hasta 100 entidades. A primera vista: el re-ranking escala perfecto. **Pero un 1.0 perfecto merece la misma duda que el de la P18.** Tres señales de que el corpus era más fácil de lo diseñado:

1. **La huella llevaba el número y el nombre también.** "Proyecto Sintético 7" + huella `SYN-007-X7`: el token "7" aparecía *exacto* en pregunta y chunk. Eso es un faro para el lado **full-text** del retriever — el match léxico del "7" anclaba el chunk correcto *antes* de que el re-ranker entrara. Posiblemente el 1.0 no venía del cross-encoder, sino del número como identificador único.
2. **Un proyecto = un chunk corto y autocontenido** → caso más limpio posible.
3. **El número de la pregunta = el número del nombre** → cero desajuste de vocabulario, lo contrario del caso difícil real ("responsable de Rubí", P17, donde pregunta y chunk usaban palabras distintas).

Conclusión: diseñamos plantilla idéntica para crear falsos amigos léxicos, pero **el número distintivo deshizo la dificultad sin querer.** El re-ranking quizá ni se ejercitaba. El 1.0 medía "match de identificadores únicos", no "desambiguación semántica". Había que quitar el faro.

---

## 6. La corrida de control: quitar el faro numérico

**Diseño de la variante por atributo** (Camino A, escala 50, atributos rediseñados):

- La pregunta identifica por **sector + responsable**, sin número: "¿cuál es el código del proyecto del sector turismo cuyo responsable es Elena Gil?".
- El corpus pierde el ordinal del nombre: "Proyecto de cartera interna. Pertenece al sector {sector} y su responsable es {resp}. El código interno de autorización es {huella}."
- **El diseño quedó más afilado de lo pedido:** dentro de cada sector, los 5 hermanos comparten **nombre de pila** y difieren solo en apellido (turismo = Elena Gil/Lima/Ruiz/Sáez/Vega). Solape léxico máximo justo en el conjunto confundible — el cross-encoder no tiene dónde esconderse.

**Invariantes verificados offline (sin BD):** 0 dígitos-faro en el `content` (solo la huella, que nunca aparece en la pregunta), 50 responsables únicos, 5 por sector en los 10 sectores, 50 huellas únicas, 0 preguntas con dígito. El punto crítico #1 (sin faro) quedó blindado por construcción.

**Lectura binaria fijada de antemano:** R@1 ~1.0 sin faro → el re-ranking desambigua por semántica, 1.0 sin asterisco. R@1 baja → el faro inflaba el control, techo real al descubierto. Y si además R@8 baja → el correcto no entra al top-20 → palanca = el retriever, no el re-ranker.

---

## 7. El resultado real: 0.820, y el hallazgo dentro del hallazgo

Gate `chunks=50, con_vector=50` ✓. El resultado:

```
Recall@1: 0.820   Recall@3: 0.820   Recall@5: 0.820   Recall@8: 0.820   MRR: 0.820
```

**Recall@1 cayó de 1.000 (con faro) a 0.820 (sin faro).** Confirmado: el 1.0 de la curva era, en buena parte, el full-text encontrando el número como faro, no el re-ranking desambiguando. El asterisco que sospechábamos era real.

### 7.1 El smoke ACERTÓ — el re-ranking sí desambigua

Pero el smoke de Elena Gil reveló algo contraintuitivo:

```
search("...sector turismo...Elena Gil") →
  [1] Elena Gil    SYN-027-X7   ← CORRECTO, posición 1
  [2] Elena Ruiz   SYN-007-X7
  [3] Elena Lima   SYN-037-X7
```

**Elena Gil quedó en posición 1, distinguida de Ruiz y Lima.** El re-ranking *sí* desambigua por apellido en el caso brutal de cinco-Elenas-mismo-sector. Entonces, ¿por qué el global es 0.82 y no más?

### 7.2 El patrón de los fallos: es la redacción, no la desambiguación

El desglose por caso mostró que los fallos (`[-]`) NO son aleatorios:

- Casi todos son la **segunda redacción** ("codigo de autorizacion del proyecto del sector..."), no la primera ("¿cuál es el código...").
- Se concentran en ciertos sectores: **logística, minería, educación, energía** fallan repetidamente en su variante "codigo de autorizacion...".

Esto cambió el diagnóstico por completo: **no es que el re-ranking no desambigüe apellidos** (el smoke probó que sí, ~80 filas `[1]` lo confirman). Es que **una redacción específica falla sistemáticamente para ciertos sectores.** Mismo proyecto, misma respuesta, distinta forma de preguntar → resultado distinto. Es **el mismo fenómeno de "responsable de Rubí" de la P17**: sensibilidad del retriever al vocabulario de la consulta.

### 7.3 R@1 = R@8 = 0.82 → es el retriever, no el re-ranker

El dato decisivo: las cuatro métricas son idénticas (0.820). Si fuera un problema de *ordenamiento* del re-ranker (trae el chunk pero lo rankea 3º en vez de 1º), R@1 sería bajo pero R@8 alto. Que **R@1 = R@8** significa: cuando falla, el chunk correcto **ni siquiera entra al top-8**. Según la regla fijada, eso apunta al **retriever**: para esas redacciones, el chunk correcto no entra al top-20 (FETCH_N), así que el re-ranker nunca lo ve.

### 7.4 La confirmación a prueba de dudas (k=20)

Para convertir "creo que es el retriever" en "sé que es el retriever", un `search` manual con k=20 de un caso que falló ("codigo de autorizacion del proyecto del sector logistica cuyo responsable es Hugo Gil"):

**El chunk de Hugo Gil/logística NO apareció en NINGUNA posición del top-20.** El retriever trajo manufactura, agroindustria, educación, energía... de todo menos logística. Y un detalle irónico que afila el hallazgo: en las **posiciones 16 y 20 aparecieron los proyectos reales** (Ónix, "Resumen ejecutivo") — para "logística + Hugo Gil", el retriever prefirió traer chunks reales no relacionados antes que el sintético correcto. La recuperación para esa redacción está genuinamente perdida: el bi-encoder no ancla "logística" como debería.

**Diagnóstico cerrado:** el re-ranker nunca tuvo el chunk para reordenar. No puedes reordenar lo que no se recuperó. El techo es del retriever.

---

## 8. La conclusión honesta de la Fase 1

El diagnóstico completo, en tres capas:

1. **El re-ranking escala y desambigua semántica.** Smoke de Elena Gil (clavó Gil sobre Ruiz/Lima en el cluster confundible) + ~80 filas `[1]`. El cross-encoder hace su trabajo cuando recibe el chunk correcto.
2. **El techo real es la recuperación, sensible a la redacción de la consulta.** Ciertas frases no recuperan el chunk correcto ni al top-20 para varios sectores. Mismo fenómeno de "responsable de Rubí" (P17), ahora cuantificado. La palanca es el retriever (embedding de consulta, peso del full-text, expansión de consulta), **no** el re-ranker.
3. **0.82 es el piso del peor caso, no el rendimiento real.** Cinco entidades calcadas por sector + redacción adversarial. Sobre documentos reales variados, mucho mejor.

Esto es la Fase 1 cerrada con honestidad — y un resultado **infinitamente más útil que el 1.0 que casi celebramos.** Se sabe qué funciona (re-ranking), dónde está el límite (recuperación sensible a redacción), y cuál es la palanca futura (mejorar el retriever). El faro numérico habría escondido todo esto bajo un 1.0 tranquilizador y falso.

---

## 9. Aprendizajes clave

1. **Un test que mide sobre el vacío da 0.000, no un hallazgo.** El gate `count > 0` (chunks en la BD) va *antes* de medir. Generar un archivo/dataset ≠ inyectarlo. Misma familia que "logs fósiles" (P15) y "test inválido" (P6).

2. **Para datos generados por código, insertar en la BD directo es más limpio que el pipeline de archivos.** Lo que el harness mide (¿el retriever encuentra el chunk?) depende solo de que el chunk + vector estén en la BD con el embedding correcto, no de cómo llegaron. La Opción B eliminó el enredo de archivo/parseo/formato que costó dos tropiezos.

3. **Leer el esquema real evita INSERTs que revientan.** `content_tsv` generada (puebla el full-text sola), `created_at NOT NULL` sin default (obliga al ORM), el formato de vector pgvector. Ninguno se podía asumir; todos se confirmaron contra el código.

4. **Un 1.0 perfecto merece escepticismo, no celebración.** El faro numérico (el ordinal en nombre + huella, coincidiendo con la pregunta) inflaba el resultado vía full-text. Quitarlo reveló el número honesto (0.82). Diseñar un stress test es fácil de arruinar sin querer: un identificador demasiado distintivo deshace la dificultad.

5. **El smoke de un solo caso puede revelar más que la métrica agregada.** Elena Gil en posición 1 probó que el re-ranking SÍ desambigua — lo que redirigió el diagnóstico de "el re-ranker falla" a "la recuperación falla para ciertas redacciones".

6. **R@1 = R@8 distingue un problema de ordenamiento de uno de recuperación.** Si R@1 es bajo pero R@8 alto → el re-ranker no ordena bien (lo tiene, lo coloca mal). Si R@1 = R@8 → el chunk no entra al top-k en absoluto → es el retriever. Esa igualdad fue la pista que apuntó al retriever, confirmada con el k=20.

7. **No puedes reordenar lo que no recuperaste.** El re-ranking solo opera sobre el top-N del retriever. Si el retriever no trae el chunk correcto (por sensibilidad a la redacción), ningún re-ranker lo rescata. El techo del sistema lo pone el componente más débil del pipeline, y aquí es la recuperación.

8. **El peor caso adversarial da un piso, no el rendimiento esperado.** Cinco "Elena" por sector difiriendo solo en apellido es casi adversarial — más duro que documentos reales. 0.82 aquí probablemente sea casi perfecto en producción. Leer el número con esa lente.

9. **Un diagnóstico honesto vale más que un número de victoria.** "Re-ranking ✓, techo en el retriever, palanca = recuperación" es más accionable que un 1.0 que no se entiende. La Fase 1 cierra sabiendo *por qué* y *hasta dónde*.

---

## 10. Comandos de referencia (nuevos de esta parte)

### Generar e insertar corpus sintético (Opción B, en la BD)

```powershell
# Control con faro numérico:
docker exec nexaagent-api-1 python -m app.eval.generate_synthetic 10
# Variante por atributo (sin faro), escala 50:
docker exec nexaagent-api-1 python -m app.eval.generate_synthetic 50 atributo
```

### El gate (antes de medir, siempre)

```powershell
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "SELECT d.filename, count(c.id) chunks, count(c.embedding) con_vector FROM documents d LEFT JOIN document_chunks c ON c.document_id=d.id WHERE d.filename LIKE 'synthetic_eval_%' GROUP BY d.filename;"
# chunks==N y con_vector==N, o NO medir.
```

### Smoke: ¿el retriever distingue al proyecto correcto?

```powershell
docker exec nexaagent-api-1 python -c "import asyncio; from app.rag.retriever import search; print(asyncio.run(search('¿cual es el codigo del proyecto del sector turismo cuyo responsable es Elena Gil?', k=3)))"
```

### Diagnóstico retriever vs re-ranker: ¿está el chunk en el top-20?

```powershell
docker exec nexaagent-api-1 python -c "import asyncio; from app.rag.retriever import search; r=asyncio.run(search('<consulta que falló>', k=20)); [print(i, c[:80]) for i,c in enumerate(r,1)]"
# Si el chunk correcto NO está en top-20 -> problema del retriever, no del re-ranker.
```

### La curva completa (orquestada, con gate por punto)

```powershell
docker compose -f x:\Nexa\nexaagent\docker-compose.yml exec api python -m app.eval.curve            # control con faro
docker compose -f x:\Nexa\nexaagent\docker-compose.yml exec api python -m app.eval.curve atributo   # sin faro, N<=50
```

### Limpieza (el marcador hace seguro el borrado)

```powershell
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "DELETE FROM documents WHERE filename LIKE 'synthetic_eval_%';"
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "SELECT id, filename FROM documents ORDER BY id;"   # solo el doc 8 real
```

---

## 11. Estado y siguientes pasos

### La Fase 1 (Q&A documental al máximo): CERRADA ✅

- ✅ P17 — harness de evaluación + baseline (Recall@1 = 0.556 sobre 4 reales).
- ✅ P18 — re-ranking: Recall@1 0.556→1.0 sobre 4 reales; calidad y costo medidos.
- ✅ P19 — re-ranking validado a escala (desambigua 50 entidades en cluster confundible); **techo real identificado: la recuperación sensible a la redacción** (R@1 = 0.82 sobre el peor caso adversarial), confirmado con k=20 como problema del retriever, no del re-ranker.

### El techo encontrado → candidato de una "Fase 1.5" futura (opcional)

- **Mejorar la recuperación para redacciones difíciles.** El bi-encoder (`nomic-embed-text`) no ancla bien ciertas frases ("codigo de autorizacion del proyecto del sector X"). Palancas posibles: embedding de consulta distinto, ajustar el peso vectorial vs full-text en la búsqueda híbrida, o expansión/reformulación de consulta. **No es el re-ranker** — confirmado. Es opcional: el techo aparece en el peor caso adversarial; sobre documentos reales el rendimiento es mucho mejor.

### Deudas / pendientes 🔜

- **Tool-call intermitente del 7B** (~25%, P18): variabilidad del modelo; mitigación preferida parser tolerante.
- **PyYAML → JSON:** el dataset sintético ya nace en JSON; cerrar el resto del cabo (P17).
- **Margen VRAM 3.3GB** (P18): vigilar.
- **Heredadas:** `request_id: "-"` en la ingesta; `retry_backoff` de Celery; `path_separator` en `alembic.ini`; drift de docs; `SecurityWarning` de Celery como root.

### Las fases que siguen (del plan acordado)

- **Fase 2 — Automatización:** herramientas con escritura e integraciones reales (el salto de "saber" a "actuar"; abre la idempotencia por herramienta — reintentar un "mandar correo" no debe duplicarlo).
- **Fase 3 — Interfaz:** frontend sobre la API estable (las señales de herramienta del streaming siguen sin UI).

---

## 12. Temario de estudio (Parte 19)

> Conceptos transferibles que esta parte enseña. Para repaso.

### A. Evaluación a escala y stress testing

- **Stress test vs test fácil**: N entidades *de plantilla idéntica* estresan el ranking; N entidades *variadas* no prueban nada. La similitud entre entidades es lo que crea la dificultad.
- **El "faro" accidental**: un identificador demasiado distintivo (un número en el nombre que coincide con la pregunta) deshace la dificultad sin querer, vía match léxico exacto. Diseñar un stress test honesto exige eliminar esos atajos.
- **Corrida de control**: comparar con-faro vs sin-faro aísla qué componente da el resultado. El control reveló que el 1.0 era del full-text, no del re-ranking.
- **El peor caso da un piso, no el rendimiento esperado**: cinco entidades casi calcadas por grupo es adversarial; el número sobre datos reales variados es mucho mejor. Leer la métrica con esa lente.

### B. Diagnóstico retriever vs re-ranker

- **R@1 = R@k como firma diagnóstica**: si Recall@1 = Recall@8, el chunk correcto no entra al top-k → problema de *recuperación*. Si R@1 < R@8, el chunk está pero mal ordenado → problema de *ordenamiento* (re-ranker).
- **"No puedes reordenar lo que no recuperaste"**: el re-ranking opera solo sobre el top-N del retriever; el componente más débil del pipeline pone el techo.
- **Confirmar con un `search` de k amplio**: buscar un caso que falló con k=20 y ver si el chunk correcto aparece en *alguna* posición distingue definitivamente "el retriever no lo trae" de "el re-ranker lo descarta".
- **Sensibilidad del bi-encoder a la redacción de la consulta**: la misma pregunta fraseada distinto recupera distinto; un fenómeno de embeddings, no de contenido (el mismo de "responsable de Rubí", P17).

### C. Generación de corpus sintético para evaluación

- **Insertar en la BD directo vs pasar por el pipeline de archivos**: para datos generados por código, el INSERT directo (con la misma función de embedding) es honesto y elimina ceremonia — el retriever no distingue la procedencia del chunk.
- **Replicar el embedding real**: usar la MISMA función (`embed_documents`) que la ingesta, o el espacio vectorial no es comparable.
- **Columnas generadas** (`GENERATED ALWAYS AS ... STORED`): poblar `content` llena el `tsvector` de full-text solo; conocer el esquema evita medir medio retriever.
- **Ground truth por construcción**: huellas únicas generadas hacen el dataset auto-etiquetado, sin curación manual.

### D. Gates y verificación (método)

- **El gate antes de medir, no después**: `count > 0` (datos presentes) como precondición codificada (`SystemExit` si falla), no como comprobación opcional.
- **La prueba de humo de un caso**: un `search` manual antes de la corrida completa atrapa un pipeline roto (corpus no insertado) sin gastar 4 mediciones falsas — y a veces revela más que la métrica agregada.
- **Reconocer un test inválido**: medir sobre un corpus vacío da 0.000 que parece hallazgo pero no lo es; el `DELETE 0` y el `count 0` lo delatan.
- **Leer el código real antes de asumir**: firmas (`get_embeddings`), constraints (`created_at NOT NULL`), tipos (`Vector`), columnas generadas — todo verificado, nada supuesto.

### E. Honestidad en la interpretación

- **Escepticismo ante un resultado perfecto**: un 1.0 en todas las métricas dispara la pregunta "¿qué lo hace fácil?", no la celebración.
- **El smoke que redirige el diagnóstico**: un caso individual (Elena Gil acertada) puede refutar la hipótesis obvia ("el re-ranker falla") y reorientar hacia la causa real (la recuperación).
- **Un diagnóstico vale más que un número**: cerrar con "qué funciona, dónde está el límite, cuál es la palanca" es más accionable que un número de victoria sin entender.

---

*Cierre de la Parte 19. Fin de la Fase 1.*
