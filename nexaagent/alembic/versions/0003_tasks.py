"""tasks table

Revision ID: 0003_tasks
Revises: 0002_long_term_memories
Create Date: 2026-05-30

Tabla de tareas/recordatorios: lo que el usuario pide "recordar", "anotar" o
"agendar". La crea el modelo vía la tool create_task. due_date es DATE (sin
hora ni zona horaria) — lo más simple correcto para "recuérdame el viernes";
si algún día se quieren recordatorios con hora, se amplía a timestamptz.
"""
from alembic import op
import sqlalchemy as sa


revision = "0003_tasks"
down_revision = "0002_long_term_memories"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("tasks")
