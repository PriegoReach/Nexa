"""Registro de ACCIONES CONFIRMABLES (P26).

P24 introdujo el flujo de confirmación asistida con UNA sola acción (delete_task)
ejecutada de forma HARDCODEADA en el orquestador. P26 generaliza: cualquier acción
irreversible (delete_task, create_calendar_event, mañana gmail...) propone -> guarda
un intent agnóstico en pending.py -> y, tras un "sí", se ejecuta buscándola AQUÍ.

Contrato uniforme de un ejecutor:
    async def perform_fn(args: dict) -> str
        - recibe el `args` del intent pendiente (el mismo dict que la tool guardó),
        - DEVUELVE el mensaje de resultado YA LISTO para el usuario (string),
        - NUNCA propaga excepción al orquestador (mismo criterio que las tools):
          un error de la acción se traduce a un string legible.

El orquestador (_handle_confirmation) NO conoce a ninguna acción por nombre: tras
un "sí" hace CONFIRMABLE_ACTIONS[intent["action"]](intent["args"]). La heurística
sí/no/ambiguo de P24 NO cambia — lo único que cambia es QUÉ se ejecuta (lookup en
vez de hardcode).

Cada ejecutor se registra con el decorador @confirmable_action("nombre") en su
propio módulo (manage_tasks.py, calendar.py). El registro se puebla al importar
esos módulos, cosa que ya ocurre vía app/agent/tools/__init__.py (get_tools).
"""
import logging
from typing import Awaitable, Callable, Dict

logger = logging.getLogger("nexa.confirmable")

# action_name -> ejecutor async (args: dict) -> str
PerformFn = Callable[[dict], Awaitable[str]]
CONFIRMABLE_ACTIONS: Dict[str, PerformFn] = {}


def confirmable_action(name: str) -> Callable[[PerformFn], PerformFn]:
    """Registra una función ejecutora bajo `name`. Uso:

        @confirmable_action("delete_task")
        async def perform_delete(args: dict) -> str: ...

    El intent guardado en pending lleva ese mismo `action: name`; el orquestador
    lo usa para encontrar al ejecutor sin mencionarlo por su nombre Python.
    """
    def _register(fn: PerformFn) -> PerformFn:
        if name in CONFIRMABLE_ACTIONS:
            # Doble registro = bug (import duplicado o nombre repetido). Logueamos
            # en vez de reventar: el último gana, igual que un dict normal.
            logger.warning("confirmable action re-registered", extra={"action": name})
        CONFIRMABLE_ACTIONS[name] = fn
        logger.debug("confirmable action registered", extra={"action": name})
        return fn
    return _register
