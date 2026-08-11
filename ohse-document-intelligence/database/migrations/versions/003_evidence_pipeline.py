"""Evidence pipeline columns and validated row store."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "003_evidence_pipeline"
down_revision = "002_table_cell_bbox_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("table_cells", sa.Column("evidence_cell_id", sa.String(length=128), nullable=True))
    op.add_column("table_cells", sa.Column("merged_cell", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("table_cells", sa.Column("source", sa.String(length=64), nullable=True))
    op.create_index("ix_table_cells_evidence_cell_id", "table_cells", ["evidence_cell_id"], unique=False)

    op.add_column("extracted_tables", sa.Column("stable_table_id", sa.String(length=64), nullable=True))
    op.add_column("extracted_tables", sa.Column("schema_id", sa.String(length=64), nullable=True))
    op.add_column("extracted_tables", sa.Column("recovery_method", sa.String(length=64), nullable=True))
    op.create_index("ix_extracted_tables_stable_table_id", "extracted_tables", ["stable_table_id"], unique=False)

    op.create_table(
        "document_evidence_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=False),
        sa.Column("page_end", sa.Integer(), nullable=False),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("storage_path", sa.String(length=1024), nullable=False),
        sa.Column("processor_format", sa.String(length=64), nullable=True),
        sa.Column("immutable", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("document_id", "page_start", "page_end", "snapshot_hash", name="uq_evidence_snapshot"),
    )

    op.create_table(
        "validated_table_rows",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("table_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("extracted_tables.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("row_index", sa.Integer(), nullable=False),
        sa.Column("schema_id", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.String(length=512), nullable=True),
        sa.Column("row_data", postgresql.JSONB(), nullable=False),
        sa.Column("field_provenance", postgresql.JSONB(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("requires_review", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("validation_issues", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("table_id", "row_index", name="uq_validated_row_per_table"),
    )
    op.create_index("ix_validated_table_rows_document_page", "validated_table_rows", ["document_id", "page_number"])

    op.add_column("oel_chemical_limits", sa.Column("source_cell_provenance", postgresql.JSONB(), nullable=True))
    op.add_column("oel_chemical_limits", sa.Column("page_number", sa.Integer(), nullable=True))
    op.add_column("oel_chemical_limits", sa.Column("persian_name", sa.String(length=512), nullable=True))
    op.add_column("oel_chemical_limits", sa.Column("english_name", sa.String(length=512), nullable=True))


def downgrade() -> None:
    op.drop_column("oel_chemical_limits", "english_name")
    op.drop_column("oel_chemical_limits", "persian_name")
    op.drop_column("oel_chemical_limits", "page_number")
    op.drop_column("oel_chemical_limits", "source_cell_provenance")
    op.drop_table("validated_table_rows")
    op.drop_table("document_evidence_snapshots")
    op.drop_column("extracted_tables", "recovery_method")
    op.drop_column("extracted_tables", "schema_id")
    op.drop_index("ix_extracted_tables_stable_table_id", table_name="extracted_tables")
    op.drop_column("extracted_tables", "stable_table_id")
    op.drop_index("ix_table_cells_evidence_cell_id", table_name="table_cells")
    op.drop_column("table_cells", "source")
    op.drop_column("table_cells", "merged_cell")
    op.drop_column("table_cells", "evidence_cell_id")
