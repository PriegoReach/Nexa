"""Entorno de Alembic para NexaAgent.

Alembic corre síncrono (operaciones de esquema puntuales), así que tomamos la
DATABASE_URL async del proyecto y la convertimos a un driver síncrono (psycopg2)
solo aquí. La app sigue usando asyncpg sin cambios.
"""

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# --- Metadata del proyecto -------------------------------------------------
# Importar Base y TODOS los modelos para que Base.metadata los registre.
from app.core.config import settings
from app.db.base import Base
from app.db import models  # noqa: F401  (registra las tablas en Base.metadata)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _sync_url() -> str:
    """Convierte la URL async del proyecto a una síncrona para Alembic."""
    url = settings.database_url
    # postgresql+asyncpg://...  ->  postgresql+psycopg2://...
    return url.replace("+asyncpg", "+psycopg2")


def run_migrations_offline() -> None:
    """Modo offline: emite SQL sin conectarse."""
    context.configure(
        url=_sync_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Modo online: se conecta y aplica."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _sync_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
