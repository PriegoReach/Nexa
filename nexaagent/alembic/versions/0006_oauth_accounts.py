"""oauth_accounts table

Revision ID: oauth_accounts
Revises: webhook_events
Create Date: 2026-05-31

Cuentas OAuth de proveedores externos (P25, empezando por Google Calendar).
Aquí viven los TOKENS dinámicos (access/refresh) que NO pueden ir en secrets
(esos son estáticos: client_id/secret). El refresh_token llega solo en la 1a
autorización con access_type=offline.

Migración escrita A MANO a propósito: autogenerate no es de fiar en este proyecto
(trampa de la P6 — document_chunks tiene columnas que el modelo declarativo no
refleja del todo, y aquí ni siquiera hay modelo ORM para oauth_accounts: se opera
con SQL crudo vía worker_session, igual que tasks/webhook_events).

UNIQUE(provider) materializa el Modelo B (cliente único, P12): UNA sola cuenta por
proveedor, sobre la que se hace UPSERT. Cuando haya multiusuario se añadirá user_id
y el unique pasará a (provider, user_id); hoy NO.
"""
from alembic import op
import sqlalchemy as sa


revision = "oauth_accounts"
down_revision = "webhook_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oauth_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(length=20), nullable=False),  # 'google'
        sa.Column("access_token", sa.Text(), nullable=False),
        # refresh_token es nullable: Google solo lo entrega en la 1a autorización
        # (con access_type=offline + prompt=consent). En reautorizaciones puede
        # faltar -> conservamos el que ya teníamos (lógica de UPSERT en el endpoint).
        sa.Column("refresh_token", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "uq_oauth_accounts_provider",
        "oauth_accounts",
        ["provider"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_oauth_accounts_provider", table_name="oauth_accounts")
    op.drop_table("oauth_accounts")
