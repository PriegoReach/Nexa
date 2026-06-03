"""webhook_events table

Revision ID: webhook_events
Revises: tasks_idempotency_key
Create Date: 2026-05-31

Registro emisor de los webhooks disparados (Bloque B, P22). La idempotencia vive
del LADO EMISOR: registramos qué eventos disparamos en NUESTRA BD antes de enviar,
y el constraint único sobre idempotency_key bloquea el replay — el patrón de la
P21 (create_task) aplicado a un efecto externo IRREVERSIBLE (un POST que ya salió).
status registra el resultado real del POST: 'pending' al insertar, luego 'sent'/'failed'.
"""
from alembic import op
import sqlalchemy as sa


revision = "webhook_events"
down_revision = "tasks_idempotency_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "webhook_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
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
    op.create_index(
        "uq_webhook_events_idempotency_key",
        "webhook_events",
        ["idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_webhook_events_idempotency_key", table_name="webhook_events")
    op.drop_table("webhook_events")
