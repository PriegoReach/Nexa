"""add idempotency_key to tasks"""
from alembic import op
import sqlalchemy as sa

revision = "tasks_idempotency_key"
down_revision = "0003_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable a propósito: tareas viejas (las 3 actuales) no tienen clave.
    # El índice único ignora NULLs en Postgres, así que conviven sin choque.
    op.add_column("tasks", sa.Column("idempotency_key", sa.String(length=64), nullable=True))
    op.create_index(
        "uq_tasks_idempotency_key", "tasks", ["idempotency_key"], unique=True
    )


def downgrade() -> None:
    op.drop_index("uq_tasks_idempotency_key", table_name="tasks")
    op.drop_column("tasks", "idempotency_key")
