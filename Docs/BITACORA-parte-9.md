# NexaAgent — Bitácora de desarrollo (Parte 9)

> Continuación de la Parte 8. Objetivo de esta sesión: **re-chunking** — resolver de raíz el
> problema del chunk contaminado (Esmeralda + Rubí juntos) que hacía que el agente entregara
> el código equivocado de forma intermitente.

**Estado al cierre de la Parte 9:** chunking ajustado de `1000/150` a `800/150`. La contaminación desapareció y el agente acierta consistentemente con los cuatro proyectos. El camino no fue recto: el primer intento (`600/80`) eliminó la contaminación pero introdujo fragmentación (separó el nombre del proyecto de su código), lo que destapó un problema de ranking. La medición iterativa llevó al punto óptimo.

---

## 1. El problema (heredado de las Partes 3-8)

`RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=150)` trocea por tamaño fijo de caracteres, ciego al significado. En el PDF de cartera, metía Esmeralda + Rubí en un mismo chunk (cabían en 1000 chars). Al preguntar por Rubí, el chunk recuperado traía dos códigos y qwen2.5 7B a veces tomaba el equivocado (Parte 8: devolvió ESM-2025-K3 en lugar de RB-2023-Q7).

---

## 2. La medición inicial (diagnosticar antes de cambiar)

Se midieron varias configuraciones sobre el PDF real, contando chunks y "contaminados" (chunks con más de un código de proyecto):

| Config | Chunks | Contaminados |
|---|---|---|
| 1000/150 (actual) | 5 | 1 ⚠️ Esmeralda+Rubí |
| 400/50 | 12 | 0 |
| 600/80 | 8 | 0 |

Conclusión inicial: bajar `chunk_size` elimina la contaminación, y 600/80 parecía el punto dulce (menos chunks-basura que 400). **Esta conclusión resultó incompleta** — ver sección 4.

---

## 3. Problema de proceso: volúmenes mal montados

Al aplicar el cambio y resubir, el worker falló con `FileNotFoundError: uploads/..._prueba-rag-grande.pdf`, aunque el archivo existía. Diagnóstico por capas:

- `ls /app/uploads` en el worker mostraba un archivo VIEJO; en la api, los nuevos. **No compartían la carpeta.**
- `cat /proc/mounts | findstr uploads` en el worker mostró una ruta corrupta con caracteres octales (`\134`, `\040`).
- **Causa:** en `docker-compose.yml`, el servicio `worker` tenía la línea `- ./uploads:/app/uploads` DUPLICADA y con indentación incorrecta (8 espacios en vez de 6). El YAML malformado produjo un montaje basura.
- **Solución:** dejar la sección `volumes` del worker limpia, una sola línea de uploads, indentación correcta (6 espacios). Verificar con `docker-compose config` antes de levantar.
- **Lección:** la indentación en YAML es estructura. Una línea mal sangrada corrompe el parseo del bloque entero. `docker-compose config` valida el YAML resuelto antes de aplicar.

---

## 4. El callejón del 600: cambiar un problema por otro

Tras arreglar los volúmenes y resubir con 600/80, el agente respondió `ON-2024-B5` (código de Ónix) a la pregunta de Rubí. Distinto error, pero error. Diagnóstico con la recuperación aislada:

```
[0] ON! ... (chunk de Ónix, que menciona "Proyecto Rubí" de pasada)
[7] RB! El código interno de autorización del proyecto es RB-2023-Q7...
```

**El chunk de Rubí quedó en posición [7] de 8 — el fondo.** Y, crítico: ese chunk **NO contenía la palabra "Rubí"**. El splitter de 600 cortó justo después del nombre del proyecto, dejando el nombre en un chunk y el código en el siguiente. Así, al buscar "Proyecto Rubí", el chunk con el código no se parece a la query (no nombra a Rubí) y pierde frente a otros que sí lo mencionan — como el de Ónix, que depende de Rubí y lo nombra.

**El re-chunking a 600 resolvió la contaminación pero creó fragmentación.** Cambió un problema (dos proyectos juntos) por otro (nombre separado de su código). Este es el dilema intrínseco del chunking de tamaño fijo: agrupar de más vs cortar de más.

La búsqueda híbrida de la Parte 4 no rescató el caso porque la query buscaba el nombre ("Rubí"), no el código exacto ("RB-2023-Q7"); el componente léxico solo ayuda con el término literal.

---

## 5. La medición correcta: nombre + código en el mismo chunk

Se midió una métrica nueva y más relevante: "código-sin-nombre" (chunks que tienen un código pero NO el nombre de su proyecto):

| Config | Contaminados | Código-sin-nombre |
|---|---|---|
| 600/80 | 0 | 3 |
| 600/150 | 0 | 3 |
| 600/200 | 0 | 2 |
| 600/300 | 0 | 4 (peor) |
| **800/150** | **0** | **1** |

Hallazgos:
- **Subir el overlap NO resuelve la fragmentación de forma fiable** (300 hasta la empeora). La intuición "más overlap reconecta nombre y código" era incorrecta para este documento.
- **800/150 es el óptimo real:** cero contaminados y solo 1 código-sin-nombre. 800 es lo bastante grande para mantener nombre + código juntos, pero lo bastante chico para no meter dos proyectos en un chunk.

El punto dulce no era ni el original (1000, contamina) ni el primer reflejo (600, fragmenta), sino un valor intermedio.

---

## 6. Verificación final

Con `chunk_size=800, chunk_overlap=150` (8 chunks, sin contaminados), tras limpiar y resubir:

- **Recuperación de Rubí:** el chunk RB! subió a [2] (antes [7]) y ahora contiene el código completo. (El de Ónix sigue en [0] por la mención cruzada — ver nota abajo.)
- **Agente, los cuatro proyectos:**
  - Rubí → RB-2023-Q7 ✓ (consistente en repeticiones)
  - Esmeralda → ESM-2025-K3 ✓ (con descripción y responsable correctos)
  - Ónix → ON-2024-B5 ✓ (Patricia Salgado)

Beneficio colateral: con el chunk completo (nombre + código + descripción + responsable juntos), las respuestas son más ricas y precisas.

### Honestidad sobre lo que NO se resolvió

El ranking sigue imperfecto: para una búsqueda sobre "Rubí", el chunk de Ónix encabeza (posición [0]) porque menciona "Proyecto Rubí" (Ónix depende de Rubí), y la similitud vectorial premia esa mención. El agente acierta porque `k=8` le da suficiente contexto y el chunk correcto está ahora arriba y completo — pero es una solución que se apoya en el `k` amplio, no en un ranking perfecto. Un corpus mucho mayor o un `k` menor podría volver a fallar.

**La cura de raíz** es el chunking estructural (un chunk por proyecto, detectando secciones), que mantendría cada entidad íntegra y aislada. Es más trabajo y merece su propia sesión. 800/150 es el mejor compromiso de tamaño fijo para este corpus.

---

## 7. Aprendizajes clave

1. **El chunking de tamaño fijo es un equilibrio entre dos males:** chunk grande agrupa entidades distintas (contaminación); chunk chico parte una entidad en pedazos (fragmentación). El óptimo está en medio y depende del documento.

2. **Para entidades con nombre + atributos, el chunk debe contener AMBOS.** Si el nombre del proyecto y su código quedan en chunks distintos, la búsqueda por nombre no encuentra el código. La métrica que importa no es solo "no contaminar" sino "mantener nombre + dato juntos".

3. **Subir el overlap no garantiza reconectar lo que el corte separó.** Hay que medir; la intuición falla.

4. **Resolver un problema puede destapar otro que estaba enmascarado.** El chunk de 1000 contaminaba pero mantenía nombre+código juntos; al bajar a 600 se arregló la contaminación y emergió un problema de ranking que el tamaño grande ocultaba.

5. **La indentación de YAML es estructura.** Una línea mal sangrada en `docker-compose.yml` corrompió el montaje de volúmenes. `docker-compose config` valida antes de aplicar.

6. **qwen2.5 7B es intrínsecamente variable.** A lo largo de las partes dio tres respuestas distintas a la misma clase de pregunta (acertó en P4, Esmeralda en P8, Ónix en P9 con 600). Parte del problema es de extracción del modelo, no solo del pipeline; un pipeline bueno reduce la probabilidad de error pero no la elimina con un 7B.

---

## 8. Comandos de referencia (nuevos de esta parte)

### Medir chunking sobre un PDF (sonda local, fuera del contenedor)

Script que trocea el PDF con varias configs y cuenta contaminados / código-sin-nombre. (Ver historial; usa pypdf + RecursiveCharacterTextSplitter.)

### Validar YAML del compose antes de aplicar

```powershell
docker-compose config
```

### Diagnosticar montajes de un contenedor

```powershell
docker exec nexaagent-worker-1 cat /proc/mounts | findstr uploads
docker exec nexaagent-worker-1 ls -la /app/uploads/
```

### Re-chunking: aplicar, limpiar y resubir

```powershell
# (editar chunk_size en ingest.py)
docker-compose up -d --build worker
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "DELETE FROM document_chunks; DELETE FROM documents;"
# resubir el PDF por Swagger
```

### Recuperación aislada con marcador de código (diagnóstico de ranking)

```powershell
docker exec nexaagent-api-1 python -c "import asyncio; from app.rag.retriever import search; r = asyncio.run(search('codigo de autorizacion del Proyecto Rubi', k=8)); print(chr(10).join(f'[{i}] ' + ('RB!' if 'RB-2023-Q7' in c else 'ON!' if 'ON-2024-B5' in c else '   ') + ' ' + c[:70] for i,c in enumerate(r)))"
```

---

## 9. Estado y siguientes pasos

### Funcionando ✅ (nuevo en la Parte 9)

- **Chunking ajustado a 800/150:** sin contaminación, nombre + código en el mismo chunk.
- El agente acierta consistentemente con los cuatro proyectos (Rubí incluido, sin desambiguar).
- Volúmenes del worker corregidos (YAML bien indentado).

### Pendiente de probar / construir 🔜

- **Chunking estructural (la cura de raíz del ranking):** trocear por sección/proyecto para que cada entidad quede íntegra y aislada, en vez de por tamaño fijo. Resolvería el ranking imperfecto que 800/150 solo mitiga. Candidato fuerte para una próxima sesión.
- **Deduplicación de memorias** (deuda de la Parte 6).
- **`DELETE /conversations/{id}`** (opcional).
- **Seguridad de producción:** OAuth2/JWT en vez de x-api-key.
- **Bug menor:** `OLLAMA_HOST=hhttp://` en docker-compose.yml (visible en `docker-compose config`).
- **Refactor opcional:** unificar el patrón "engine NullPool propio" (4 sitios).
- **Recordatorio Alembic:** cada `--autogenerate` futuro intentará borrar el full-text.

### Nota sobre el chunk_size

800/150 es óptimo para ESTE corpus (informe de proyectos con secciones de ~500 chars). Documentos con estructura distinta podrían necesitar otro valor. La sonda de medición de esta sesión es la herramienta para recalibrarlo ante un corpus nuevo. El chunking estructural eliminaría esta dependencia del tamaño.

---

*Cierre de la Parte 9.*
