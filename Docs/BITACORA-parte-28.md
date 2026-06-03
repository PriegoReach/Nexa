# NexaAgent — Bitácora de desarrollo (Parte 28)

> Primera extensión tras cerrar la Fase 2: **Google Drive → RAG**. Es la integración que
> **conecta dos sistemas ya construidos** — el OAuth de Google (P25-27) con el pipeline de
> ingesta y el RAG de la Fase 1 — cerrando la visión planteada en la P25: "busca el documento
> → Drive → trae → indexa → responde". A diferencia de la Fase 2, Drive es **lectura
> reversible**: no es irreversible, no necesita confirmación. La complejidad no está en la
> seguridad sino en los **formatos** (un Google Doc nativo se exporta, no se descarga) y en el
> **puente** descarga→ingesta.

**Estado al cierre de la Parte 28:** Drive conectado e ingestando al RAG existente. Dos tools nuevas: `list_drive_files` (buscar, inocuo) e `ingest_drive_file` (el puente Drive→pipeline). Los dos caminos de formato funcionan: PDF vía `files.get?alt=media` (download), Google Doc nativo vía `files.export?mimeType=text/plain` (export). El círculo cerrado: el agente respondió con contenido que existe **solo** en un Doc recién traído de Drive (caso d). La idempotencia de re-ingesta reusó la maquinaria de la P14 entera — una columna nueva `drive_file_id` mapea archivo-de-Drive → `document_id`, y el `DELETE` de chunks de la P14 hace el resto (re-ingestar no duplica). Routing del 7B sólido por cuarta parte consecutiva (P25-28). Hallazgo elevado: el caveat **cross-lingual** de recuperación (consulta en español, documento en inglés) es la versión cotidiana del techo de retriever que la P19 identificó como adversarial — y Drive lo vuelve frecuente al traer documentos en idiomas mezclados.

---

## 1. La naturaleza de Drive: lectura, no escritura

Tras la escalera de irreversibilidad de la Fase 2 (que culminó en `send_email`), Drive es un cambio de naturaleza: **lectura reversible**. No escribe en el mundo, no es irreversible, **no necesita confirmación**. Es más como la Fase 1 (ingesta y consulta) que como la Fase 2 (escritura con confirmación e idempotencia irreversible).

**El insight que reordenó el alcance:** la visión "busca → Drive → RAG → responde" son cuatro capacidades, pero **dos ya existen**. El pipeline de ingesta (`ingest_document`, P13/P14) y el RAG de consulta (`search_knowledge_base`, Fase 1) están construidos. Drive solo añade **listar** (capacidad 1) y **descargar/traer** (capacidad 2), más el **puente** entre traer e ingestar. La consulta (4) es RAG normal sobre lo ingestado.

**Flujo decidido: pasos separables.** Ingestar es un acto ("ingiere mis documentos de Drive sobre X" → busca, trae, indexa, confirma "indexé N"); preguntar es otro (RAG normal sobre lo ya ingestado). NO un flujo mágico de un paso. Razón: separa el trabajo costoso (descargar+chunk+embed, una vez) de la consulta (rápida, repetible) — como ya funciona la Fase 1 (subes documentos, luego preguntas), solo que ahora la fuente es Drive. Y evita encadenar 4 tools en una petición, donde el 7B es frágil.

---

## 2. Los dos caminos de formato — la complejidad real de P28

"De todo en Drive" (el diagnóstico mostró ~31 PDFs, 2 Google Docs, 1 .txt, 3 Sheets, decenas de fotos) obliga a distinguir dos categorías que tienen **endpoints de API distintos**:

| Categoría | Ejemplo | API | mimeType |
|---|---|---|---|
| **Binario subido** | PDF, .txt | `files.get?alt=media` (descarga bytes) | `application/pdf`, `text/plain` |
| **Google Doc nativo** | creado en Docs | `files.export?mimeType=...` (Google convierte) | `application/vnd.google-apps.document` |

La señal para distinguirlos: el campo **`mimeType`**, que `files.list` ya devuelve — así se sabe el camino *antes* de intentar traer el contenido. Un `files.get?alt=media` sobre un Doc nativo **falla**; hay que exportarlo.

**La decisión afinada leyendo el pipeline: exportar Google Docs a `text/plain`.** El `_read_file` de la P13 hace: si `.pdf` → `PdfReader`, **cualquier otra cosa → `read_text`** (texto plano). Así que exportar un Doc nativo a `text/plain` es el camino *más corto sin pérdida*: el pipeline lo trata como un `.txt` y va directo al chunking, sin re-parseo. (Exportar a PDF y re-parsear sería una vuelta innecesaria.) Solo se supo eligiendo este camino leyendo el código real de `_read_file`.

**Alcance: PDF + .txt (download) + Google Docs (export a texto).** Diferidos — formato no-texto o que necesita OCR: **Sheets, Slides, imágenes**. El RAG de texto no los aprovecha bien; son extensión futura. La tool los rechaza con mensaje claro, sin crash (caso f).

---

## 3. El puente Drive → pipeline, y la idempotencia reusada

### El puente (la firma recibe un path)

`ingest_document(document_id, file_path)` recibe un **path**, no bytes. Así que el puente:

```
1. files.get?fields=mimeType → saber el tipo
2. según mimeType: download (bytes) o export (texto)
3. escribir lo traído a un archivo temporal /tmp/drive_<file_id>.<ext>
   (la EXTENSIÓN importa: _read_file decide el parser por el sufijo —
    .pdf→PdfReader, otro→read_text. Binario→.pdf, Doc exportado→.txt)
4. resolver el document_id (ver idempotencia) → ingest_document(doc_id, tmp_path)
5. borrar el temporal
```

### La idempotencia: la maquinaria de la P14, reusada entera

El mejor hallazgo del diseño: **`ingest_document` YA es idempotente** (P14) — hace `DELETE FROM document_chunks WHERE document_id = :doc_id` antes de insertar ("correr la tarea N veces == correrla 1 vez"). Así que la idempotencia de re-ingesta **ya existe a nivel de `document_id`**. Lo único que P28 añade es la **llave** para reconocer "este archivo de Drive ya lo ingesté":

- Columna nueva **`drive_file_id`** en `documents` (migración a mano, P6; nullable — los docs subidos a mano no la tienen).
- Al ingestar de Drive: buscar una fila con ese `drive_file_id`. Si existe, **reusar su `document_id`** (el `DELETE` de la P14 reemplaza sus chunks). Si no, crear una fila nueva (vía el ORM, para el `created_at` default — lección P19).

Resultado: re-ingestar el mismo archivo de Drive **reemplaza, no duplica**, reusando la idempotencia que ya estaba construida. El hilo de la idempotencia de la Fase 2 reaparece, pero la maquinaria pesada ya existía — P28 solo añadió la llave de búsqueda.

---

## 4. Las dos tools

- **`list_drive_files(query)`** — el `list_calendar_events` de Drive: GET `files.list` con `q` (name contains query), pidiendo `id, name, mimeType`, excluyendo carpetas y papelera. Devuelve nombre + tipo + id. El `mimeType` se incluye porque dice qué camino tomará la ingesta. Inocuo (solo metadatos), sin confirmación.

- **`ingest_drive_file(file_id)`** — el puente de la sección 3. Mira el mimeType, elige download/export, trae, resuelve el `document_id` (reusa o crea), llama `ingest_document`. Devuelve "Indexé '<nombre>' (<N> fragmentos)". Rechaza tipos no soportados con mensaje claro.

Más: scope `drive.readonly` (leer, no modificar — mínimo) añadido a los de Calendar/Gmail, con re-autorización (el `prompt=consent` la fuerza). Reuso total de `ingest_document` y `search_knowledge_base` — sin tocarlos.

---

## 5. Verificación — los seis casos, en dos tiempos

Como en la P25, listar es el hito antes de ingestar. Tras re-autorizar (scope `drive.readonly` confirmado):

| Caso | Resultado |
|---|---|
| **(a) Listar** | el 7B eligió `list_drive_files`, trajo 6 PDFs reales sobre "Django" (nombre + tipo + id) ✓ |
| **(b) PDF (download)** | el 7B eligió `ingest_drive_file` con el id; doc #19 ready, 14 chunks; camino `alt=media` ✓ |
| **(c) Google Doc (export)** ⚡ | camino distinto: `files.export?mimeType=text/plain`; doc #20, texto real (ciudades US) ✓ |
| **(d) RAG sobre Drive** ⚡ | el agente respondió las 9 ciudades exactas del Doc ingestado (contenido que solo existe en ese archivo) — **círculo cerrado** ✓ |
| **(e) Re-ingesta** | mismo `document_id` #19, 14→14 chunks (no 28); el `DELETE` de la P14 reemplazó ✓ |
| **(f) No soportado** | Sheet e imagen → "no soportado, solo PDF/texto/Docs", sin crash, 0 filas ✓ |

**El caso (d) cierra el círculo conceptual de P28** — y de la visión de la P25. Preguntar algo cuyo contenido existe *solo* en el doc recién traído de Drive, y que el RAG responda, prueba que Drive→RAG funciona de extremo a extremo: el agente buscó fuera, trajo, indexó, y respondió usando lo que trajo. Es el equivalente del "carácter por carácter" de la P27, para la lectura. **El caso (c) vs (b)** confirma que los dos caminos de API operan distinto (export para nativos, download para binarios), que era el riesgo técnico real.

**Routing del 7B (cuarta parte consecutiva sólida):** eligió `list_drive_files` (a) e `ingest_drive_file` (b) correctamente, copiando el id largo sin error — como en P25/P26/P27. Sigue inclinando la decisión del 14B hacia diferir.

**Bug encontrado y arreglado (honesto):** en (b), el primer intento dio 500 con cuerpo vacío. Causa: `logger.info(..., extra={"created": created})` — `created` es **atributo reservado de `logging.LogRecord`** → `KeyError`. Lo importante: **la ingesta había funcionado** (doc + 14 chunks commiteados); solo crasheó la línea de log *después* del commit. Es el primo de hallazgos previos (P20: la tool funcionó, la fecha del modelo no; P26: el control correcto, la narración mala): distinguir "qué falló" de "qué pareció fallar". El efecto real ocurrió; falló la observabilidad. Renombrado a `created_doc`.

**Notas operativas:** hubo que **habilitar la Google Drive API** en el proyecto Cloud (como Gmail en P27 — es API aparte de Calendar). Y se reitera el patrón: Uvicorn sin `--reload` → `docker restart` para que el proceso vivo cargue cada cambio.

---

## 6. El caveat cross-lingual: la versión cotidiana del techo de la P19

El hallazgo que vale más de lo que parece, y por eso se eleva (no se entierra como nota). El caso (d) **falló en el primer intento**: la pregunta en español "datacenters de GFN en Estados Unidos" recuperó el doc **equivocado** ("Proyecto Ámbar") por mayor similitud que el contenido **en inglés** del archivo correcto. Solo con vocabulario alineado ("US-based Datacenters") recuperó bien.

**No es un bug de P28** (la ingesta funcionó; `search_knowledge_base` directo recupera el chunk correcto con la frase alineada) — es una propiedad del **RAG existente**. Pero es significativo por cómo conecta con el cierre de la Fase 1:

- En la **P19**, el techo identificado fue la **sensibilidad del retriever a la redacción de la consulta** — frases distintas (mismo idioma) recuperan distinto. Era un caso *adversarial de laboratorio* (entidades calcadas).
- El caveat de P28 es la **versión cross-lingual** del mismo techo: el embedding `nomic-embed-text` cruza español↔inglés, y la distancia idiomática **agrava** la sensibilidad a la redacción.

**Por qué importa más ahora:** Drive trae documentos en **idiomas mezclados** (contenido en inglés, preguntas en español). Lo que en la Fase 1 era un caso adversarial de laboratorio ahora puede ser **cotidiano** — cada vez que se pregunte en español sobre un doc en inglés traído de Drive. La palanca que la P19 dejó pendiente ("mejorar la recuperación para redacciones difíciles") gana una motivación **concreta** (consultas cross-lingual sobre documentos de Drive), ya no solo teórica.

---

## 7. Aprendizajes clave

1. **Conectar dos sistemas construidos es barato si encajan.** Drive→RAG reusó el pipeline de ingesta y el RAG enteros; solo añadió listar, traer, y el puente. Un proyecto bien construido permite que una integración nueva sea "pegar dos piezas existentes", no reconstruir.

2. **Leer el pipeline decide el formato de export.** Exportar Google Docs a `text/plain` (no a PDF) porque `_read_file` come texto directo sin re-parseo. La decisión de formato salió de leer el código real, no de asumir — el camino más corto solo se ve conociendo qué espera el consumidor.

3. **El `mimeType` antes del contenido evita el error.** `files.list` devuelve el tipo, así que se sabe el camino (download vs export) antes de intentar traer — un `files.get?alt=media` sobre un Doc nativo falla. Distinguir el camino temprano, no por ensayo y error.

4. **La idempotencia se reusa si existe la maquinaria.** El `DELETE` por `document_id` de la P14 ya hacía la re-ingesta idempotente; P28 solo añadió la llave (`drive_file_id`) para reconocer el archivo. Reaprovechar un mecanismo construido > reconstruirlo.

5. **La extensión del archivo temporal importa cuando el parser decide por sufijo.** `_read_file` elige PdfReader o read_text por el sufijo; un binario va a `.pdf`, un Doc exportado a `.txt`. Un detalle del consumidor que el puente debe respetar.

6. **Un crash después del commit no es un fallo de la operación.** El `KeyError` del `created` reservado crasheó la línea de log tras el commit — la ingesta había ocurrido. Distinguir "el efecto falló" de "la observabilidad falló" (primo de P20/P26). El error visible no siempre es el error real.

7. **Un techo conocido puede reaparecer en una dimensión nueva.** La sensibilidad del retriever a la redacción (P19, mismo idioma, adversarial) reaparece cross-lingual (P28, español↔inglés, cotidiano). Drive la volvió frecuente al traer documentos en idiomas mezclados — el mismo límite, ahora con motivación concreta para atacarlo.

---

## 8. Comandos de referencia (nuevos de esta parte)

### Re-autorizar con el scope de Drive (añadido a Calendar + Gmail)

```powershell
curl.exe http://localhost:8000/oauth/google/start -H "Authorization: Bearer <TOKEN>"
# autorizar, copiar code, entregarlo a /oauth/google/callback
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "SELECT provider, scope FROM oauth_accounts;"  # drive.readonly + ...
```

### Listar e ingestar de Drive (vía el agente)

```powershell
# listar (el hito Drive conectado)
curl.exe -X POST http://localhost:8000/chat -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" -d '{\"message\": \"busca en mi Drive documentos sobre Django\"}'
# ingestar uno (con el id que devolvió list)
curl.exe -X POST http://localhost:8000/chat -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" -d '{\"message\": \"ingiere el documento <id>\"}'
```

### Verificar la ingesta y la idempotencia de re-ingesta

```powershell
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "SELECT id, filename, drive_file_id, status FROM documents WHERE drive_file_id IS NOT NULL;"
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "SELECT document_id, count(*) FROM document_chunks GROUP BY document_id;"  # re-ingestar no debe duplicar el conteo
```

### Diagnóstico cross-lingual (el caveat de la sección 6)

```powershell
# si una consulta en español no recupera un doc en inglés, probar search directo con vocabulario alineado:
docker exec nexaagent-api-1 python -c "import asyncio; from app.rag.retriever import search; print(asyncio.run(search('US-based datacenters', k=3)))"
```

---

## 9. Estado y siguientes pasos

### Funcionando ✅ (nuevo en la Parte 28)

- **Drive → RAG de extremo a extremo:** listar, traer (PDF download + Doc export), ingestar al pipeline, consultar. La visión de la P25 cerrada.
- **`list_drive_files` + `ingest_drive_file`** (las dos tools), reusando `ingest_document` y `search_knowledge_base` sin tocarlos.
- **Dos caminos de formato:** `alt=media` (binarios) y `export?mimeType=text/plain` (Docs nativos).
- **Idempotencia de re-ingesta:** `drive_file_id` mapea a `document_id`; el `DELETE` de la P14 reemplaza, no duplica.
- **Scope `drive.readonly`** (mínimo, lectura). Sin confirmación (reversible).
- **Routing del 7B sólido** (cuarta parte consecutiva, P25-28).

### Deudas / pendientes 🔜

- **Recuperación cross-lingual (deuda elevada):** consultas en español sobre documentos en inglés recuperan mal — la versión cotidiana del techo de retriever de la P19, ahora frecuente por los documentos mezclados de Drive. Palancas (de la P19): embedding de consulta distinto, reformulación/traducción de consulta, ajustar peso vectorial vs full-text, o un re-ranker. Motivación ahora concreta.
- **Formatos diferidos:** Sheets, Slides, imágenes (no-texto / OCR). Extensión si el uso lo pide.
- **El 14B, con tres señales en contra acumuladas:** no cabe en VRAM (P23-diag), OOM con el 7B (P27), y routing sólido en P25-28 (no lo necesita). Diferido salvo caso costoso de routing.
- **Clasificar el fallo en la idempotencia de envío** (P27): 4xx pre-envío vs fallo ambiguo.
- **Tokens en claro en `oauth_accounts`** (P25); **refresh token caduca ~7 días** (Testing de Google).
- **Heredadas:** `request_id: "-"` en la ingesta; `retry_backoff` de Celery; `path_separator` en `alembic.ini`; drift de docs; `SecurityWarning` de Celery como root; margen de memoria del host (OOM de P27).

### Lo que sigue

- **Recuperación cross-lingual** (candidato fuerte ahora): la deuda de la P19 con motivación nueva. Una "Fase 1.5" — reformulación de consulta, embedding multilingüe, o re-ranker. Drive la hizo cotidiana.
- **CRUD de Calendar / Sheets de Drive / más proveedores:** completitud de las integraciones.
- **Fase 3 — Interfaz:** frontend sobre la API estable. Ahora hay patrones ricos que mostrar (confirmación, OAuth, ingesta de Drive). El contrato se estabilizó con la Fase 2.

---

## 10. Temario de estudio (Parte 28)

> Conceptos transferibles que esta parte enseña. Para repaso.

### A. Integrar sistemas construidos

- **Conectar vs reconstruir**: una integración nueva (Drive→RAG) que reusa pipelines existentes (ingesta, consulta) es barata si encajan; el trabajo es el puente, no las piezas.
- **Pasos separables vs flujo mágico**: separar el trabajo costoso (ingestar, una vez) de la consulta (repetible) evita encadenar muchas tools en una petición frágil, y refleja cómo ya funciona el sistema (subir, luego preguntar).
- **El consumidor decide el formato**: exportar a lo que el pipeline ya come (texto plano, no PDF) lo evita re-parsear. Leer el código del consumidor decide la elección de formato río arriba.

### B. APIs con caminos múltiples por tipo

- **Metadato antes que contenido**: el `mimeType` (de `files.list`) dice qué camino tomar (download vs export) antes de traer — un `alt=media` sobre un nativo falla. Conocer el tipo temprano evita el error por ensayo.
- **Download vs export**: archivos binarios se descargan tal cual; documentos nativos del proveedor se exportan (el proveedor los convierte). Dos endpoints, una decisión por tipo.
- **El sufijo del temporal importa**: si el parser río abajo decide por extensión, el puente debe nombrar el temporal con el sufijo correcto (.pdf vs .txt).

### C. Reusar idempotencia existente

- **La llave vs la maquinaria**: si el mecanismo idempotente ya existe (DELETE por id antes de insertar, P14), una fuente nueva solo necesita una llave para mapear su identificador al id existente (drive_file_id → document_id).
- **Reemplazar, no duplicar**: re-ingestar un documento debe reemplazar sus chunks, no acumularlos; reusar el id del documento más el DELETE hace la re-ingesta idempotente sin lógica nueva.

### D. El techo de recuperación, otra vez

- **Un límite reaparece en dimensiones nuevas**: la sensibilidad del retriever a la redacción (P19, mismo idioma) reaparece cross-lingual (P28, español↔inglés). El mismo fenómeno de embeddings, en un eje nuevo (idioma).
- **De adversarial a cotidiano**: lo que era un caso de laboratorio (entidades calcadas) se vuelve frecuente cuando la fuente de datos lo provoca (documentos en idiomas mezclados de Drive). El contexto de uso cambia la prioridad de una deuda.
- **Diagnosticar retriever vs ingesta**: un `search` directo con vocabulario alineado confirma que el chunk está bien ingestado y que el problema es la recuperación (la frase de la consulta), no el contenido — el método de diagnóstico de la P19.

---

*Cierre de la Parte 28. Primera extensión post-Fase 2: el agente trae documentos del mundo y los indexa.*
