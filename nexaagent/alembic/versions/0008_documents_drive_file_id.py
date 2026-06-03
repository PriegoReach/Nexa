"""add drive_file_id to documents

Revision ID: documents_drive_file_id
Revises: sent_emails
Create Date: 2026-06-01

Origen Drive de un documento (P28). NULL para los docs subidos a mano por
/documents/upload (la inmensa mayoría histórica); el file_id de Google Drive para
los traídos por ingest_drive_file. Con esta columna, re-ingestar el MISMO archivo
de Drive REUSA su document_id existente -> ingest_document hace el DELETE de los
chunks viejos (idempotencia P14) y los reemplaza, sin duplicar.

Nullable a propósito (como idempotency_key en 0004): las filas previas no tienen
origen Drive. Índice NO único: buscamos por él para decidir reuso vs alta; no se
impone unicidad a nivel BD (la tool ya reusa; un unique reintroduciría un punto de
fallo por carrera sin beneficio). Escrita a mano (autogenerate no es de fiar, P6).
"""
from alembic import op
import sqlalchemy as sa


revision = "documents_drive_file_id"
down_revision = "sent_emails"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("drive_file_id", sa.String(length=128), nullable=True),
    )
    op.create_index(
        "ix_documents_drive_file_id", "documents", ["drive_file_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_documents_drive_file_id", table_name="documents")
    op.drop_column("documents", "drive_file_id")
