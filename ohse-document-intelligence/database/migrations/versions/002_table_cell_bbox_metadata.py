"""Add bbox metadata columns to table_cells."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "002_table_cell_bbox_metadata"
down_revision = "001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("table_cells", sa.Column("bbox_source", sa.String(length=32), nullable=True))
    op.add_column("table_cells", sa.Column("bbox_confidence", sa.Float(), nullable=True))
    op.add_column("table_cells", sa.Column("source_reference", sa.dialects.postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("table_cells", "source_reference")
    op.drop_column("table_cells", "bbox_confidence")
    op.drop_column("table_cells", "bbox_source")
