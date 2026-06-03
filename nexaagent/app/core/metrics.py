"""Métricas en formato de exposición Prometheus, hechas a mano.

Cero dependencias nuevas (misma filosofía que el JsonFormatter de la Parte 15:
la stdlib basta, y así evitamos el baile de rebuild `--no-cache` que costó las
Partes 5 y 12). Un `/metrics` raspable por Prometheus no necesita el cliente
oficial; necesita el texto en el formato correcto, y eso lo emitimos aquí.

Concurrencia: el `api` corre con UN solo proceso uvicorn (sin `--workers`), así
que hay un registro por proceso — que es justo el modelo de scrape de Prometheus.
Las actualizaciones ocurren síncronas dentro del event loop (sin `await` entre
leer y escribir), de modo que el GIL las hace atómicas y no hace falta lock. Con
varios workers habría que ir a un colector multiproceso; anotado, no necesario hoy.
"""

# Buckets del histograma, en segundos. Llegan a 60 a propósito: el cold-load del
# modelo a VRAM rondó los ~39s (Parte 15) y los buckets por defecto de Prometheus
# topan en 10s, que dejaría todo el cold-start aplastado en el +Inf.
_BUCKETS: tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0,
)

# Almacenes. Las claves son tuplas de etiquetas.
_requests_total: dict[tuple[str, str, str], int] = {}     # (method, path, status) -> n
_in_progress: dict[str, int] = {}                          # method -> n en curso
_dur_buckets: dict[tuple[str, str], list[int]] = {}        # (method, path) -> conteos cumulativos
_dur_sum: dict[tuple[str, str], float] = {}                # (method, path) -> suma de segundos
_dur_count: dict[tuple[str, str], int] = {}                # (method, path) -> nº de observaciones


def inc_in_progress(method: str) -> None:
    _in_progress[method] = _in_progress.get(method, 0) + 1


def dec_in_progress(method: str) -> None:
    n = _in_progress.get(method, 0) - 1
    if n <= 0:
        _in_progress.pop(method, None)
    else:
        _in_progress[method] = n


def observe(method: str, path: str, status: int, duration_s: float) -> None:
    """Registra una petición terminada: incrementa el contador y el histograma."""
    rk = (method, path, str(status))
    _requests_total[rk] = _requests_total.get(rk, 0) + 1

    dk = (method, path)
    counts = _dur_buckets.get(dk)
    if counts is None:
        counts = [0] * len(_BUCKETS)
        _dur_buckets[dk] = counts
        _dur_sum[dk] = 0.0
        _dur_count[dk] = 0
    # Buckets CUMULATIVOS: una observación incrementa todo bucket cuyo límite la
    # cubre (le >= duración) → counts[i] = nº de obs con duración <= límite[i].
    for i, boundary in enumerate(_BUCKETS):
        if duration_s <= boundary:
            counts[i] += 1
    _dur_sum[dk] += duration_s
    _dur_count[dk] += 1


def _esc(value: str) -> str:
    """Escape de un valor de etiqueta (backslash, comilla, salto de línea)."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt_le(boundary: float) -> str:
    """`le` como entero si es exacto ('1'), si no su repr corto ('2.5', '0.005')."""
    return str(int(boundary)) if boundary == int(boundary) else repr(boundary)


def render() -> str:
    """Devuelve el cuerpo de /metrics en formato de exposición Prometheus 0.0.4."""
    lines: list[str] = []

    lines.append("# HELP http_requests_total Total de peticiones HTTP por método, ruta y status.")
    lines.append("# TYPE http_requests_total counter")
    for (method, path, status), n in sorted(_requests_total.items()):
        lines.append(
            f'http_requests_total{{method="{_esc(method)}",'
            f'path="{_esc(path)}",status="{_esc(status)}"}} {n}'
        )

    lines.append("# HELP http_requests_in_progress Peticiones HTTP en curso por método.")
    lines.append("# TYPE http_requests_in_progress gauge")
    for method, n in sorted(_in_progress.items()):
        lines.append(f'http_requests_in_progress{{method="{_esc(method)}"}} {n}')

    lines.append("# HELP http_request_duration_seconds Latencia de las peticiones HTTP en segundos.")
    lines.append("# TYPE http_request_duration_seconds histogram")
    for (method, path) in sorted(_dur_buckets):
        counts = _dur_buckets[(method, path)]
        labels = f'method="{_esc(method)}",path="{_esc(path)}"'
        for i, boundary in enumerate(_BUCKETS):
            lines.append(
                f'http_request_duration_seconds_bucket{{{labels},'
                f'le="{_fmt_le(boundary)}"}} {counts[i]}'
            )
        total = _dur_count[(method, path)]
        lines.append(f'http_request_duration_seconds_bucket{{{labels},le="+Inf"}} {total}')
        lines.append(f'http_request_duration_seconds_sum{{{labels}}} {_dur_sum[(method, path)]!r}')
        lines.append(f'http_request_duration_seconds_count{{{labels}}} {total}')

    return "\n".join(lines) + "\n"
