"""Add local embedding vectors to document chunks.

Revision ID: eb13f97a8c21
Revises: dba0f6053fbf
"""

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

from alembic import op

revision: str = "eb13f97a8c21"
down_revision: str | None = "dba0f6053fbf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.add_column(
        "document_chunks",
        sa.Column("embedding", Vector(768), nullable=True),
    )
    op.execute(
        "CREATE INDEX ix_document_chunks_embedding "
        "ON document_chunks USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_embedding")
    op.drop_column("document_chunks", "embedding")
