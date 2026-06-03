"""sent_emails table

Revision ID: sent_emails
Revises: oauth_accounts
Create Date: 2026-06-01

Registro emisor de los correos enviados (P27, Gmail). La idempotencia vive del
LADO EMISOR — y aquí NO hay alternativa: a diferencia de Calendar (events.insert
acepta un `id` de cliente -> 409 lado-receptor, P26), la Gmail API users.messages.send
NO admite ninguna clave de idempotencia de cliente: enviar el mismo raw dos veces
entrega DOS correos. Así que replicamos el patrón de webhook_events (P22-B):
registrar la intención en NUESTRA BD ANTES de enviar, con UNIQUE sobre
idempotency_key que bloquea el reenvío.

Sesgo a NO-DUPLICAR (registrar-primero): si el INSERT choca con la clave, NO se
reenvía. Para un correo (irreversible, va a una persona real) ese es el lado
correcto del trade-off — preferimos no reenviar a arriesgar un duplicado.

status: 'pending' al registrar, luego 'sent'/'failed' según el resultado real de
la API. Escrita A MANO (autogenerate no es de fiar aquí; sent_emails se opera con
SQL crudo vía worker_session, igual que tasks/webhook_events/oauth_accounts).

`to_addr` (no `to`): TO es palabra reservada en SQL; nombrarla `to` obligaría a
citar la columna ("to") en cada query cruda. Mismo dato, nombre sin footgun.
"""
from alembic import op
import sqlalchemy as sa


revision = "sent_emails"
down_revision = "oauth_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sent_emails",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("to_addr", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
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
        "uq_sent_emails_idempotency_key",
        "sent_emails",
        ["idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_sent_emails_idempotency_key", table_name="sent_emails")
    op.drop_table("sent_emails")
