# NexaAgent — Bitácora de desarrollo (Parte 10)

> Continuación de la Parte 9. Objetivo de esta sesión: **chunking estructural** — la cura
> de raíz del ranking imperfecto que el chunking de tamaño fijo (800/150) solo mitigaba.
> Que cada chunk respete los límites lógicos del documento Y lleve contexto (título +
> sección) para mejorar la recuperación.

**Estado al cierre de la Parte 10:** chunking estructural funcionando. Cada chunk ahora lleva un prefijo `[Título — Sección]`, troceo por sección antes que por tamaño, y degradación con gracia ante documentos sin estructura. El agente responde correcto y consistente para los cuatro proyectos, incluso en una sola pregunta combinada. El ranking interno mejoró (RB pasó de [7] → [2] → [1] a lo largo del proyecto) sin alcanzar el ideal teórico [0] — y la sección 5 explica honestamente por qué.

---

## 1. La tensión de diseño (cara a cara)

"Chunking estructural" suena obvio en abstracto, pero tiene una trampa que conviene reconocer: una solución que solo funcione con documentos que tengan TUS encabezados específicos no es estructural robusta — es un parser hecho a medida. Un RAG real recibe PDFs, texto corrido, emails, actas. La pregunta correcta no era "¿cómo troceo por proyecto?" sino **"¿qué estructura puedo detectar de forma general que mejore el chunking sin acoplarme a un formato?"**.

### Decisiones cerradas tras esa pregunta

| Decisión | Elección | Motivo |
|---|---|---|
| Tipos de documento esperados | Variados, sin estructura fija | Define el alcance honesto |
| Enfoque | Enriquecer chunks con contexto (heurística + prefijo) | Pragmático; degrada con gracia |
| Detección de encabezados | Conservadora | Cero falsos positivos medidos |
| Prefijo en el `content` | Sí, viaja al embedding y al agente | Es lo que mejora el ranking |

---

## 2. El diseño en dos capas

**Capa 1 — Contexto del documento (siempre disponible).** Cada chunk lleva el título del documento como prefijo: `[Cartera de Proyectos Confidenciales]`. Funciona para todo, incluso sin estructura, porque el filename siempre existe.

**Capa 2 — Encabezado de sección (cuando se detecte).** Si la heurística encuentra encabezados, el prefijo se enriquece: `[Cartera... — Proyecto Rubí]`. Si no se detectan, simplemente no se añade — degrada con gracia.

### Heurística conservadora de encabezados

Una línea es candidata a encabezado si: longitud ≤ 40, no termina en `.`, `,`, `:`, `;`, ≤ 5 palabras, y la siguiente línea no vacía es claramente un párrafo (> 80 chars). Sobre el PDF de cartera detectó los 6 encabezados reales (4 proyectos + Resumen + Notas) y CERO falsos positivos.

### Troceo por sección, no por offset

El primer intento (Arreglo A: trocear todo el texto y localizar la sección por offset) produjo un bug: con overlap, un chunk podía empezar en territorio de una sección y contener el código de otra. Resultado: chunk con `RB-2023-Q7` etiquetado como `[... — Proyecto Esmeralda]` — incoherente.

**El arreglo correcto (Arreglo B):** trocear el documento **primero en secciones** (límite duro), y dentro de cada sección aplicar el splitter de tamaño. Así cada chunk pertenece inequívocamente a UNA sección. La coherencia prefijo-contenido queda garantizada por construcción.

```python
def _split_into_sections(raw):
    # Recorre líneas, separa por encabezados detectados, devuelve [(titulo, cuerpo), ...]
    ...

def _enrich_chunks(raw, filename):
    doc_title, sections = _split_into_sections(raw)
    splitter = RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=150, ...)
    out = []
    for section_title, body in sections:
        prefix = f"[{title} — {section_title}]" if section_title else f"[{title}]"
        for piece in splitter.split_text(body):
            out.append(f"{prefix}\n{piece}")
    return out
```

### El bug del título inferido

El primer `_detect_doc_title` se quedaba con la primera línea no vacía como título — incluso si era un párrafo largo de cuerpo. Para un email sin título, el prefijo terminaba siendo `[Hola, esto es un email corto sin estructura...]`. Arreglo: aceptar como título solo líneas cortas (≤ 80) que no terminen en puntuación de frase. Si no lo parece, cae al filename. Verificado en tres casos: PDF (mantiene su título), email plano (cae al filename → `[email_corto]`), texto con título corto (`[Reporte Q4]`).

---

## 3. Verificación

### Los chunks generados

7 chunks producidos, cada uno con su prefijo coherente:

```
[0] [Cartera de Proyectos Confidenciales]
[1] [Cartera de Proyectos Confidenciales — Resumen ejecutivo]
[2] [Cartera de Proyectos Confidenciales — Proyecto Esmeralda]   ← contiene ESM ✓
[3] [Cartera de Proyectos Confidenciales — Proyecto Rubí]        ← contiene RB  ✓
[4] [Cartera de Proyectos Confidenciales — Proyecto Ámbar]       ← contiene AM  ✓
[5] [Cartera de Proyectos Confidenciales — Proyecto Ónix]        ← contiene ON  ✓
[6] [Cartera de Proyectos Confidenciales — Notas de gobernanza]
```

Coherencia prefijo-contenido: 4/4 proyectos correctos.

### Recuperación aislada (Rubí)

| Sesión | Posición del chunk RB en el ranking |
|---|---|
| Parte 3 (size 1000, sin híbrida) | fuera del top-4 |
| Parte 8 (size 1000, híbrida) | [4] con k=8; código contaminado |
| Parte 9 (size 800/150, sin prefijo) | [2] |
| **Parte 10 (estructural + prefijo)** | **[1]** |

Subió un puesto. Lo conseguido es real y medible.

### El agente — la prueba que importa

Tres llamadas en conversaciones nuevas:

1. *"Cual es el codigo de autorizacion del Proyecto Rubi?"* → **RB-2023-Q7** ✓
2. *"Quien es el responsable de Rubi y cual es su codigo?"* → **Lucía Domínguez** + **RB-2023-Q7** ✓. Bonus: el modelo notó la falta de tilde y la mencionó — gracias al contexto del chunk con prefijo.
3. *"Dame los codigos de los cuatro proyectos."* → los **cuatro correctos** en una sola respuesta: ESM-2025-K3, RB-2023-Q7, AM-2026-T1, ON-2024-B5.

La prueba final (los cuatro de una tirada) es la más fuerte: prueba que el sistema discrimina chunks de múltiples entidades a la vez sin confundir códigos.

---

## 4. Aprendizajes clave

1. **"Chunking estructural" puede ser un parser a medida si no se cuida.** La pregunta correcta es qué se puede detectar de forma general, no cómo trocear este documento. Una heurística conservadora + degradación con gracia es más robusto que una detección agresiva acoplada a un formato.

2. **Trocear por sección PRIMERO, por tamaño DESPUÉS.** Si troceas todo de corrido y luego adivinas la sección por offset, el overlap te traiciona — un chunk puede empezar en una sección y contener datos de otra. Los límites de sección deben ser fronteras duras.

3. **El enriquecimiento con contexto mejora el ranking, no lo resuelve.** Anteponer `[Título — Sección]` a cada chunk hace que la búsqueda matchee mejor por entidad. Pero si dos chunks mencionan a la misma entidad (uno por título, otro en su cuerpo), el embedding los considera similares. El prefijo es palanca, no llave mágica.

4. **Una heurística necesita un fallback.** `_detect_doc_title` originalmente cogía la primera línea — bien si parecía título, mal si era un párrafo. La regla "aceptar solo si parece título; si no, caer al filename" cubre los dos casos sin frágilidad.

5. **El test más exigente vale por veinte triviales.** Pedirle los cuatro códigos en una sola pregunta ejercita más el sistema que cuatro preguntas separadas, porque exige discriminación simultánea entre entidades.

6. **Validar funciones puras con una sonda fuera del contenedor ahorra tiempo.** El chunking estructural se midió sobre el PDF en el entorno local antes de tocar el contenedor — pude iterar el bug del Arreglo A, el bug del título, y la heurística completa sin reiniciar nada.

---

## 5. Lo que NO se resolvió (honestidad)

**El ranking ideal "RB en posición [0]" no se alcanzó.** Ónix sigue encabezando la búsqueda sobre Rubí. La causa: el chunk de Ónix menciona "Proyecto Rubí" en su cuerpo (porque Ónix depende del avance de Rubí). Ahora *ambos* chunks contienen "Proyecto Rubí" — uno en el prefijo (el correcto), otro en una mención cruzada (el de Ónix). El embedding los considera similares.

Es un **techo intrínseco del embedding semántico**: si dos chunks hablan del mismo tema, los rankea parecido, independientemente de si la mención es "yo soy X" o "yo dependo de X". El prefijo es palanca, no llave mágica.

**¿Por qué entonces el agente acierta?** Porque `k=8` le da margen: el chunk correcto entra en los top-4, el modelo ve ambos, y discrimina (con un prefijo `[... — Proyecto Rubí]` que es señal directa). En la práctica funciona bien. En teoría, un corpus muchísimo mayor o un `k` menor podrían tensarlo.

### Próximas palancas (si en el futuro hace falta)

- **Re-ranking con un cross-encoder:** sobre los top-k candidatos del híbrido, un modelo más pesado los reordena considerando la query y el chunk juntos. Lo que un embedding no captura, esto sí.
- **Ajustar la ponderación de RRF para favorecer el match en prefijo vs cuerpo.** Trabajo no trivial; querría más medición primero.
- **Mover las menciones cruzadas a notas aparte en la fuente.** Una solución "de redacción", no técnica.

Ninguna es urgente. El sistema actual responde correcto y consistente. Documentado para no perderlo de vista.

---

## 6. Comandos de referencia (nuevos de esta parte)

### Verificar que los chunks llevan prefijo correcto

```powershell
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "SELECT id, left(content, 70) FROM document_chunks ORDER BY id;"
```

### Recuperación aislada con marcador de código (regresión clave)

```powershell
docker exec nexaagent-api-1 python -c "import asyncio; from app.rag.retriever import search; r = asyncio.run(search('codigo de autorizacion del Proyecto Rubi', k=8)); print(chr(10).join(f'[{i}] ' + ('RB!' if 'RB-2023-Q7' in c else 'ON!' if 'ON-2024-B5' in c else 'ESM' if 'ESM-2025-K3' in c else '   ') + ' ' + c[:75] for i,c in enumerate(r)))"
```

### Prueba final: los cuatro códigos en una sola pregunta

```powershell
curl.exe -X POST http://localhost:8000/chat -H "x-api-key: <API_KEY>" -H "Content-Type: application/json" -d '{\"message\": \"Dame los codigos de los cuatro proyectos.\"}'
```

---

## 7. Estado y siguientes pasos

### Funcionando ✅ (nuevo en la Parte 10)

- **Chunking estructural con enriquecimiento de contexto:** troceo por sección (frontera dura), tamaño 800/150 dentro de cada una, prefijo `[Título — Sección]` viajando al embedding y al agente.
- Heurística conservadora de encabezados, cero falsos positivos sobre el PDF real.
- Degradación con gracia: documentos sin estructura caen a solo `[filename]`.
- Agente responde correcto y consistente para los cuatro proyectos, incluso combinados en una sola pregunta.

### Pendiente / deudas conocidas 🔜

- **Re-ranking con cross-encoder** (si en el futuro el ranking imperfecto se vuelve un problema en un corpus mayor). No urgente.
- **Deduplicación de memorias** (deuda de la Parte 6).
- **`DELETE /conversations/{id}`** (opcional).
- **Seguridad de producción:** OAuth2/JWT en vez de x-api-key.
- **Bug menor:** `OLLAMA_HOST=hhttp://` en `docker-compose.yml`.
- **Refactor opcional:** unificar el patrón "engine NullPool propio" (sigue en 4 sitios).
- **Recordatorio Alembic:** cada `--autogenerate` futuro intentará borrar el full-text; revisar el archivo generado.

---

*Cierre de la Parte 10.*
