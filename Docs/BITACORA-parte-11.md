# NexaAgent — Bitácora de desarrollo (Parte 11)

> Continuación de la Parte 10. Objetivo de esta sesión: **deduplicación de memorias** —
> cerrar la deuda de la Parte 6 (la ventana móvil podía guardar hechos repetidos cuando
> el usuario menciona el mismo dato varias veces, posiblemente reformulado).

**Estado al cierre de la Parte 11:** dedup semántica funcionando. Antes de insertar un hecho nuevo, se busca el más cercano por similitud coseno en `long_term_memories`; si la distancia es menor al umbral (0.15), se omite. Calibrado con sonda sobre el modelo real de embeddings. Validado: un hecho parafraseado del existente quedó en distancia 0.145, fue detectado, omitido y registrado en el log con su comparación explícita.

---

## 1. El problema, con precisión

La memoria de largo plazo de la Parte 6 dispara la extracción cada 6 mensajes sobre los últimos 6 (ventana móvil). El solapamiento entre ventanas no es el problema; el problema es que **el LLM extrae hechos similares de momentos distintos**: si el usuario reafirma o reformula un dato en distintas conversaciones, el modelo lo capta una y otra vez. Y como qwen2.5 reformula el mismo hecho de formas distintas ("El usuario tiene un gato llamado X" vs "X es el gato del usuario"), comparar texto exacto no basta.

**Solución:** dedup semántica vía pgvector. Antes de insertar un candidato, buscar el más cercano y omitir si la similitud es alta.

### Decisiones de diseño

| Decisión | Elección | Motivo |
|---|---|---|
| Dónde | In situ, al insertar | Tabla siempre limpia; no jobs aparte |
| Qué hacer si hay duplicado | Omitir el nuevo (v1) | Simple, honesto, fácil de razonar |
| Alcance | Global (entre conversaciones) | La memoria es transversal por diseño |
| Umbral | Calibrar antes con sonda | No fijar a ciegas |

---

## 2. La sonda de calibración

Antes de fijar un umbral, se midió con el modelo real (`nomic-embed-text` vía Ollama) la distancia coseno entre tres categorías de frases respecto a un hecho de referencia ("El usuario solo alimenta a su gato con atún los jueves"):

| Categoría | min | max | media |
|---|---|---|---|
| **Duplicados** (paráfrasis del mismo hecho) | 0.005 | 0.184 | 0.099 |
| **Mismo dominio** (gato pero distinto dato) | 0.159 | 0.264 | 0.198 |
| **No relacionados** | 0.211 | 0.355 | 0.298 |

### Hallazgo crudo: las categorías SE SOLAPAN

El máximo de duplicados (0.184) está por encima del mínimo de mismo-dominio (0.159). Y el mínimo de no-relacionados (0.211) está por debajo del máximo de mismo-dominio. **No hay un umbral mágico** que separe perfecto duplicados de no-duplicados con `nomic-embed-text` — es un modelo de propósito general, no un detector fino de paráfrasis.

Ejemplo contraintuitivo: "El usuario vive en Madrid" (no-relacionado) salió a 0.211, *más cerca* que "El gato del usuario es de color naranja" (mismo dominio) a 0.264. El modelo pondera la estructura "El usuario [verbo] [algo]" sobre el tema concreto.

### La elección: 0.15 como compromiso consciente

| Umbral | Atrapa | Riesgo |
|---|---|---|
| 0.10 (conservador) | 3/4 paráfrasis | Duplicados ocasionales |
| **0.15 (elegido)** | 3/4 paráfrasis + casi la cuarta | Borde con mismo-dominio (0.159) |
| 0.18 (agresivo) | 4/4 paráfrasis | **Pierde** "Wenceslao" (0.171) y "tiene gato" (0.159) |

Razonamiento: duplicar es feo pero recuperable; perder un hecho es invisible y costoso. 0.15 prefiere lo primero.

---

## 3. Implementación

`memory_extraction.py` recibió un bloque de dedup dentro del bucle de inserción. El resto (prompt, lectura de mensajes, embeddings, engine NullPool) intacto.

```python
DEDUP_DISTANCE_THRESHOLD = 0.15  # calibrado con sonda; ver Parte 11

# Dentro del bucle for fact, vec in zip(facts, vectors):
nearest = await session.execute(
    text("SELECT content, embedding <=> :v AS dist "
         "FROM long_term_memories ORDER BY embedding <=> :v LIMIT 1"),
    {"v": vec_str},
)
row = nearest.first()
if row is not None and row.dist < DEDUP_DISTANCE_THRESHOLD:
    print(f"[memory dedup] SKIP (d={row.dist:.3f}) '{fact}' ≈ '{row.content}'")
    skipped += 1
    continue

# INSERT normal...
```

### Logging explícito (clave para futura recalibración)

Cada skip emite una línea con la distancia exacta, el hecho descartado y el hecho con el que coincide. Al final, un resumen `stored=N skipped=M`. Si el umbral resulta demasiado agresivo o laxo en producción, los logs dirán exactamente qué casos quedan en el borde.

---

## 4. Validación

### Provocar el duplicado controlado

Conversación nueva (id 26) reafirmando el hecho ya guardado ("mi gato Wenceslao come atún únicamente los jueves"). Tres mensajes para cruzar el umbral de 6 y disparar la extracción.

### El log del worker — la prueba visible

```
[memory dedup] SKIP (d=0.145)
  'El usuario tiene una rutina donde su gato Wenceslao come atún
   únicamente los jueves.'
  ≈ 'El usuario solo alimenta a su gato con atún los jueves.'
[memory dedup] conv=26 stored=0 skipped=1
Task extract_memories[...] succeeded in 13.12s: 0
```

- Distancia 0.145, **justo dentro** del rango "duplicado" predicho por la sonda (max 0.184).
- Hecho nuevo más rico que el viejo (incluía "Wenceslao", "rutina") pero **claramente el mismo**.
- `stored=0 skipped=1`: dedup hizo su trabajo.

### La tabla, intacta

```
 id | conversation_id |                         content                         
----+-----------------+---------------------------------------------------------
  1 |               3 | El usuario solo alimenta a su gato con atún los jueves.
```

Un solo hecho. La base no creció con basura. La calibración fue precisa.

---

## 5. Aprendizajes clave

1. **Calibrar umbrales con sondas, no con intuición.** El umbral "típico" de 0.15 que recomiendan tutoriales por ahí *coincidió* con el bueno para tu modelo, pero solo lo sabes con certeza tras medir. Y la metodología (frases controladas en tres categorías) sirve para recalibrar cualquier sistema de dedup semántica futuro.

2. **`nomic-embed-text` solapa "mismo tema, distinto dato" con "duplicado".** No hay umbral mágico. Hay que elegir conscientemente qué error prefieres (duplicado ocasional vs pérdida ocasional), no buscar la cifra perfecta. Esto se debe al modelo, no al diseño.

3. **Logging explícito es deuda barata que se paga sola.** Una línea por skip con la distancia, el hecho nuevo y el matcheado convierte un parámetro abstracto (0.15) en algo auditable. Si mañana parece muy agresivo o muy laxo, los logs lo cuentan.

4. **El comportamiento del LLM al extraer importa tanto como la dedup.** En la prueba, el modelo combinó "atún los jueves" y "Wenceslao" en un solo hecho compuesto, en vez de dos separados. La dedup detectó la similitud con el hecho viejo y omitió todo — perdiendo de paso "Wenceslao". No es un bug del dedup, es granularidad del extractor. Pendiente: prompt de extracción más granular o post-procesamiento que parta hechos compuestos.

5. **El test exitoso no es un set vacío de duplicados, es un duplicado detectado.** Hacer la prueba consiste en provocar un duplicado y verlo registrado en el log de skip. Verificar la *ausencia* de duplicados sería ambiguo (¿no había, o no se detectaron?). La presencia explícita del skip prueba el comportamiento.

---

## 6. Comandos de referencia (nuevos de esta parte)

### Sonda de calibración (script reusable para otros modelos / casos)

`app/sonda_umbral.py` (temporal, borrar después). Embebe un hecho de referencia y mide distancias contra tres listas controladas:
- Paráfrasis del mismo hecho (esperado: distancias bajas).
- Mismo dominio pero datos distintos (esperado: distancias intermedias).
- Temas no relacionados (esperado: distancias altas).

Ejecutar dentro del contenedor api:

```powershell
docker exec -w /app nexaagent-api-1 python -m app.sonda_umbral
```

### Provocar y verificar un duplicado

```powershell
# 1. Conversación nueva que reafirme un hecho ya guardado, 3 intercambios para cruzar umbral
# 2. Mirar el log del worker:
docker-compose logs --tail 15 worker
# Buscar: "[memory dedup] SKIP (d=0.XXX) ..."
# 3. Verificar que la tabla no creció:
docker exec nexaagent-db-1 psql -U nexa -d nexaagent -c "SELECT id, conversation_id, content FROM long_term_memories;"
```

---

## 7. Estado y siguientes pasos

### Funcionando ✅ (nuevo en la Parte 11)

- **Dedup semántica de memorias** vía pgvector, umbral 0.15 calibrado con sonda.
- Logging explícito de cada skip (distancia + hechos comparados).
- Memoria de largo plazo libre de duplicados (los hechos parafraseados se filtran antes de insertar).

### Pendiente / deudas conocidas 🔜

- **Granularidad de extracción:** qwen2.5 a veces combina varios hechos en uno solo (perdiendo información cuando dedup omite el compuesto). Mejorables: prompt más estricto ("UN hecho atómico por línea") o post-procesamiento.
- **Re-calibración periódica del umbral** si en producción se ve mucho ruido. Los logs ya lo facilitan.
- **`DELETE /conversations/{id}`** (opcional).
- **Seguridad de producción:** OAuth2/JWT en vez de x-api-key.
- **Bug menor:** `OLLAMA_HOST=hhttp://` en `docker-compose.yml`.
- **Refactor opcional:** unificar el patrón "engine NullPool propio" (5 sitios ya, sumando memory_extraction).
- **Recordatorio Alembic:** cada `--autogenerate` futuro intentará borrar el full-text; revisar.

---

*Cierre de la Parte 11.*
