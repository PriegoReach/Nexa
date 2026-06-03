# NexaAgent — Bitácora de desarrollo (Parte 3)

> Continuación de la Parte 2. Objetivo de esta sesión: **cerrar el RAG de verdad**,
> probándolo con un PDF grande que genere múltiples chunks — para validar el chunking real,
> la rama de extracción de PDF y, sobre todo, si la búsqueda **discrimina** entre varios candidatos.

**Estado al cierre de la Parte 3:** RAG validado con un documento multi-chunk real. El agente recupera y responde el dato correcto de un proyecto concreto sin filtrar los de otros proyectos. Se descubrió y mitigó el talón de Aquiles del RAG ingenuo: la similitud vectorial pura no discrimina bien por nombre de entidad cuando los chunks comparten redacción. Mitigación aplicada: subir `k` de 4 a 8.

---

## 1. Objetivo de la sesión

La prueba de la Parte 2 fue válida pero tenía un punto ciego: el documento era pequeño y generó **un solo chunk**. Con un único chunk, la búsqueda siempre "acierta" porque no hay competencia. Eso dejó sin probar tres cosas:

1. **Chunking real** — que un documento grande se trocea en varios pedazos.
2. **Discriminación de la búsqueda** — que al preguntar por un tema, pgvector devuelve el chunk *correcto* y no se confunde con los demás.
3. **La rama de PDF** — el branch `PdfReader` en `_read_file` de `ingest.py` nunca se había ejecutado (la prueba de la Parte 2 fue un `.txt`).

**Metodología (heredada):** verificar por capas, y diagnosticar con datos antes de tocar código.

---

## 2. El documento de prueba (cuatro huellas)

Se creó `prueba-rag-grande.pdf` (2 páginas, ~3 875 caracteres) con **cuatro proyectos inventados**, cada uno con su código distintivo:

| Proyecto | Código (huella) | Responsable |
|---|---|---|
| Esmeralda | ESM-2025-K3 | Ricardo Fuentes |
| Rubí | RB-2023-Q7 | Lucía Domínguez |
| Ámbar | AM-2026-T1 | Joaquín Beltrán |
| Ónix | ON-2024-B5 | Patricia Salgado |

**Por qué cuatro huellas:** con un solo dato no se puede probar discriminación. Con cuatro, al preguntar por uno la respuesta correcta debe traer *su* código y no los otros tres. Todos los párrafos comparten la misma plantilla ("El código interno de autorización del proyecto es..."), lo que fuerza al sistema a distinguir por el nombre del proyecto — justo donde falla la búsqueda ingenua.

**Antes de subir nada:** se verificó con el mismo `pypdf` del pipeline que el texto se extraía limpio y que las cuatro huellas estaban presentes, para que un fallo de extracción no contaminara la prueba.

**Distribución de chunks predicha (chunk_size=1000, overlap=150):** 5 chunks.

| Chunk | Huella(s) |
|---|---|
| 0 | (resumen, sin huella) |
| 1 | ESM-2025-K3 + RB-2023-Q7 (juntas) |
| 2 | AM-2026-T1 |
| 3 | (gobernanza, sin huella) |
| 4 | ON-2024-B5 |

Que dos huellas (Esmeralda y Rubí) cayeran en el mismo chunk resultó clave: permitió probar también la **extracción dentro de un chunk multi-dato**.

---

## 3. Lo que funcionó a la primera

- **Chunking real:** el log del worker mostró `succeeded ... : 5`. Cinco chunks, exactamente lo predicho. Status `ready`, count = 5 en `document_chunks`.
- **Rama de PDF estrenada:** el branch `PdfReader` de `ingest.py` se ejecutó por primera vez y extrajo el texto sin problema. El arreglo del engine `NullPool` de la Parte 2 aguantó perfecto con un documento más pesado.

---

## 4. El hallazgo principal: la recuperación NO discrimina bien

Esta es la lección central de la Parte 3.

### Síntoma 1 — la búsqueda de Ámbar trajo el chunk equivocado primero

Al probar el retriever aislado con "¿código de autorización del Proyecto **Ámbar**?", el **primer** resultado (el más cercano) NO fue el chunk de Ámbar, sino el de Esmeralda/Rubí. El chunk correcto de Ámbar llegó segundo.

### Síntoma 2 — el agente no encontró Rubí (con k=4)

La pregunta por Rubí devolvió "no encontré información" y, en su lugar, ofreció Zafiro y Ámbar. Diagnóstico con datos: se reprodujo la búsqueda `k=4` que usa el agente y **ninguno de los 4 chunks contenía RB-2023-Q7 ni a Lucía Domínguez**. El modelo no falló — el retriever no le entregó el chunk correcto. (Confirmando el principio de la Parte 2: "no encontró" se diagnostica viendo qué recibió, no suponiendo.)

### Causa raíz

Dos problemas combinados:

1. **El embedding matchea por redacción, no por entidad.** La query "código de autorización del Proyecto Rubí" se parece *en redacción* a todos los párrafos de proyecto, porque todos usan la misma plantilla. El nombre "Rubí" es una palabra entre muchas y se diluye. `nomic-embed-text` promedia el parecido de toda la frase; no entiende que el nombre del proyecto es la llave. Por eso Ámbar y Ónix (redacción casi idéntica) ganaron al chunk correcto.

2. **El chunking partió el dato de un proyecto.** El código de Rubí quedó en el chunk compartido con Esmeralda (que *empieza* hablando de Esmeralda), lo que lo hace puntuar bajo para una query sobre "Rubí".

Esto no es un bug del código: es el **talón de Aquiles clásico del RAG ingenuo** — similitud vectorial pura sobre chunks de tamaño fijo. El test multi-chunk fue precisamente lo que lo sacó a la luz; el test de un solo chunk de la Parte 2 nunca habría podido revelarlo.

---

## 5. La mitigación aplicada (Palanca A: subir `k`)

### Diagnóstico antes de tocar código

Se reprodujo la búsqueda de Rubí con `k=8` aislada para *ver* si el chunk correcto entraba en una ventana mayor:

```powershell
# (one-liner sin '<' para no pelear con PowerShell — ver nota abajo)
... search('...Proyecto Rubi...', k=8) ... '** RUBI AQUI **' if 'RB-2023-Q7' in c ...
```

Resultado: el chunk de Rubí aparecía en **posición [4]** — justo fuera del corte de `k=4`, pero al alcance de `k=8`. Eso confirmó que subir `k` resolvía el caso sin necesidad de re-chunking.

### El cambio (una línea)

En `app/agent/tools/knowledge_base.py`:

```python
results = _run_async(search(query, k=8))   # antes: k=4
```

### Verificación end-to-end

```powershell
curl.exe -X POST http://localhost:8000/chat -H "x-api-key: <API_KEY>" -H "Content-Type: application/json" -d '{\"message\": \"Segun la cartera de proyectos, cual es el codigo de autorizacion del Proyecto Rubi y quien es su responsable?\"}'
```

Respuesta final:

```
"El código de autorización del Proyecto Rubí es RB-2023-Q7 y su responsable es Lucía Domínguez, de la dirección de Finanzas."
```

Doble éxito: (1) trae el dato correcto, y (2) NO filtra ESM-2025-K3 ni a Ricardo Fuentes, aunque ambos venían en el mismo chunk [4] recuperado. Eso prueba que qwen2.5 entresaca el dato pedido dentro de un chunk con ruido.

---

## 6. Aprendizajes clave

1. **El RAG de un solo chunk no prueba nada de la calidad de búsqueda.** Sin competencia entre chunks, la recuperación siempre "acierta". Hay que probar con varios chunks que compartan plantilla para ver si la búsqueda realmente discrimina.

2. **La similitud vectorial pura matchea por parecido de redacción, no por entidad.** Si todos tus chunks usan la misma plantilla, el nombre que los distingue (el del proyecto, persona, etc.) se diluye y la búsqueda confunde candidatos. Este es el talón de Aquiles del RAG ingenuo.

3. **Subir `k` es la mitigación más barata** y suele bastar en corpus pequeños: si el chunk correcto está "cerca pero fuera" del top-k, ampliar la ventana lo recupera. Verificar primero con la búsqueda aislada si el chunk entra en `k` mayor, antes de reconstruir.

4. **El chunking puede partir el dato de una entidad en dos**, dejando el nombre/código en un chunk y el detalle en otro. Chunks más pequeños o un overlap mayor lo mitigan; una segmentación consciente de la estructura (por sección) lo resuelve mejor.

5. **Validar la extracción del PDF por separado, antes de subir.** Correr el mismo `pypdf` del pipeline sobre el documento generado descartó de antemano que un fallo de extracción contaminara el diagnóstico de recuperación.

6. **PowerShell interpreta `<` como redirección aunque esté entre comillas.** Para one-liners de diagnóstico, evitar `<`/`>` en los strings (usar marcadores de texto plano como `** AQUI **`) o pasar el script por stdin con `| docker exec -i ... python -`.

---

## 7. Comandos de referencia (nuevos de esta parte)

### Verificar chunking de un documento concreto

```powershell
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "SELECT count(*) FROM document_chunks WHERE document_id = 11;"
```

### Diagnosticar la recuperación aislada (ver qué chunks entran en la ventana)

```powershell
# Marca con '** RUBI AQUI **' el chunk que contiene la huella. Sin '<' por PowerShell.
docker exec nexaagent-api-1 python -c "import asyncio; from app.rag.retriever import search; r = asyncio.run(search('codigo de autorizacion del Proyecto Rubi y quien es su responsable', k=8)); print(chr(10).join(f'[{i}] ' + ('** RUBI AQUI ** ' if 'RB-2023-Q7' in c else '') + c[:90] for i,c in enumerate(r)))"
```

```powershell
# Alternativa robusta: pasar el script por stdin (sin pelear con el escapado)
"import asyncio; from app.rag.retriever import search; r = asyncio.run(search('...', k=8)); print(...)" | docker exec -i nexaagent-api-1 python -
```

---

## 8. Estado y siguientes pasos

### Funcionando ✅ (nuevo en la Parte 3)

- **RAG multi-chunk validado de verdad:** PDF de 2 páginas → 5 chunks → recuperación → extracción discriminante → respuesta correcta del agente.
- **Rama de extracción de PDF (`PdfReader`) estrenada** y funcionando.
- **Recuperación ajustada** con `k=8`, recuperando datos que con `k=4` quedaban fuera.
- Extracción confirmada: el agente entresaca el dato pedido aun cuando el chunk contiene varios proyectos.

### Pendiente de probar / construir 🔜 (heredado y nuevo)

- **Búsqueda híbrida (Palanca C):** combinar similitud vectorial con full-text de Postgres para que el nombre exacto de la entidad pese, no solo el parecido semántico. Es la solución robusta de producción para el hallazgo de hoy.
- **Re-chunking consciente de estructura (Palanca B):** trocear por sección/proyecto en lugar de por tamaño fijo, para no partir el dato de una entidad. Evaluar `chunk_size` menor.
- **Memoria de largo plazo:** resúmenes recuperables por similitud vectorial.
- **Endpoint para listar conversaciones.**
- **Streaming de respuestas (SSE).**
- **Seguridad de producción:** reemplazar el `x-api-key` simple por OAuth2/JWT.
- **Fijar versiones exactas** de dependencias (sobre todo LangChain).
- **Migraciones con Alembic** en lugar de auto-crear tablas al arrancar.
- **Refactor opcional:** unificar el patrón "engine NullPool propio" en un helper compartido (`ingest.py` + `retriever.py`).

### Nota sobre el alcance de la mitigación

Subir `k` resolvió el caso de hoy porque el corpus es pequeño y el chunk correcto estaba a un paso de la ventana. En un corpus grande, `k=8` no garantiza recuperar el chunk correcto si está mal rankeado por el problema de fondo (match por redacción). La solución duradera es la **búsqueda híbrida** (Palanca C). Mantener la prueba de los cuatro proyectos como test de regresión de la calidad de recuperación.

---

*Cierre de la Parte 3.*
