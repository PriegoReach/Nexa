"""Helper para sesiones de SQLAlchemy desde corrutinas en loops efímeros.

El engine async global (SessionLocal en app/db/session.py) está atado al loop
de FastAPI. Funciona bien para los endpoints de la API. PERO falla con
"Future attached to a different loop" cuando se usa desde:

  - una tarea de Celery (asyncio.run crea un loop nuevo en cada invocación),
  - una herramienta del agente que se ejecuta en un ThreadPoolExecutor
    (cada hilo tiene su propio loop).

La solución probada (Partes 2, 4, 6, 9, 11): crear un engine con NullPool
DENTRO del loop activo y descartarlo al terminar. Este helper unifica ese
patrón, que estaba duplicado en cinco sitios:

    async with worker_session() as session:
        await session.execute(...)
        await session.commit()

Cada llamada crea su propio engine y lo desecha al cerrar el contexto.
No hay estado global compartido entre loops. Idempotente y seguro.
"""
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings


@asynccontextmanager
async def worker_session() -> AsyncIterator[AsyncSession]:
    """Abre una sesión async con engine NullPool propio.

    Toma la URL de settings.database_url (armada desde componentes + el secreto
    postgres_password; ya NO de DATABASE_URL en el entorno — P23). No usa el
    engine global; cada llamada tiene su propio engine, atado al loop activo.
    """
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        SessionFactory = async_sessionmaker(engine, expire_on_commit=False)
        async with SessionFactory() as session:
            yield session
    finally:
        await engine.dispose()
