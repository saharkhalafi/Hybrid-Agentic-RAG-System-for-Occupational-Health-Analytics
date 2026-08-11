"""Initial schema for OHSE Document Intelligence Platform."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

from config.settings import get_settings

revision: str = "001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

VECTOR_DIM = get_settings().vector_dimension


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "documents",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("processing_version", sa.String(length=32), nullable=False),
        sa.Column("gcs_uri", sa.String(length=1024), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("metadata", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("content_hash"),
    )

    op.create_table(
        "document_pages",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column("ocr_json", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("layout_json", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("page_type", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("image_path", sa.String(length=1024), nullable=True),
        sa.Column("has_digital_text", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("triage_metadata", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "page_number", name="uq_document_page"),
    )
    op.create_index("ix_document_pages_document_id", "document_pages", ["document_id"])

    op.create_table(
        "extracted_tables",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("table_type", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=True),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("raw_json", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("raw_markdown", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("source_processor", sa.String(length=64), nullable=False, server_default="document_ai"),
        sa.Column("bbox", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_extracted_tables_document_id", "extracted_tables", ["document_id"])
    op.create_index("ix_extracted_tables_page_number", "extracted_tables", ["page_number"])

    op.create_table(
        "table_cells",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("table_id", sa.UUID(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("row_index", sa.Integer(), nullable=False),
        sa.Column("column_index", sa.Integer(), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column("original_value", sa.Text(), nullable=True),
        sa.Column("normalized_value", sa.Text(), nullable=True),
        sa.Column("unit", sa.String(length=64), nullable=True),
        sa.Column("row_span", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("column_span", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("bbox", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["table_id"], ["extracted_tables.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("table_id", "row_index", "column_index", name="uq_table_cell_position"),
    )
    op.create_index("ix_table_cells_table_id", "table_cells", ["table_id"])

    op.create_table(
        "formulas",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("table_id", sa.UUID(), nullable=True),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("formula_name", sa.String(length=256), nullable=True),
        sa.Column("original_expression", sa.Text(), nullable=False),
        sa.Column("normalized_expression", sa.Text(), nullable=False),
        sa.Column("variables", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("unit", sa.String(length=64), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("bbox", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["table_id"], ["extracted_tables.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "review_queue",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("object_type", sa.String(length=64), nullable=False),
        sa.Column("object_id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("bbox", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("issue_type", sa.String(length=64), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("assigned_user", sa.String(length=256), nullable=True),
        sa.Column("review_result", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "chemical_registry",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("cas", sa.String(length=32), nullable=False),
        sa.Column("english_name", sa.String(length=512), nullable=True),
        sa.Column("persian_name", sa.String(length=512), nullable=True),
        sa.Column("synonyms", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("molecular_weight", sa.Float(), nullable=True),
        sa.Column("hazard_class", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cas"),
    )

    op.create_table(
        "oel_chemical_limits",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("chemical_id", sa.UUID(), nullable=False),
        sa.Column("twa", sa.Float(), nullable=True),
        sa.Column("stel", sa.Float(), nullable=True),
        sa.Column("ceiling", sa.Float(), nullable=True),
        sa.Column("unit", sa.String(length=64), nullable=True),
        sa.Column("standard_reference", sa.String(length=256), nullable=True),
        sa.Column("source_table_id", sa.UUID(), nullable=True),
        sa.Column("original_values", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["chemical_id"], ["chemical_registry.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_table_id"], ["extracted_tables.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "vibration_limits",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("parameter", sa.String(length=128), nullable=True),
        sa.Column("axis", sa.String(length=32), nullable=True),
        sa.Column("a8", sa.Float(), nullable=True),
        sa.Column("vdv", sa.Float(), nullable=True),
        sa.Column("action_limit", sa.Float(), nullable=True),
        sa.Column("exposure_limit", sa.Float(), nullable=True),
        sa.Column("unit", sa.String(length=64), nullable=True),
        sa.Column("standard_reference", sa.String(length=256), nullable=True),
        sa.Column("source_table_id", sa.UUID(), nullable=True),
        sa.Column("original_values", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["source_table_id"], ["extracted_tables.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "noise_limits",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("laeq", sa.Float(), nullable=True),
        sa.Column("dose", sa.Float(), nullable=True),
        sa.Column("duration", sa.Float(), nullable=True),
        sa.Column("criterion_level", sa.Float(), nullable=True),
        sa.Column("unit", sa.String(length=64), nullable=True),
        sa.Column("standard_reference", sa.String(length=256), nullable=True),
        sa.Column("source_table_id", sa.UUID(), nullable=True),
        sa.Column("original_values", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["source_table_id"], ["extracted_tables.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "biological_exposure_limits",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("chemical_id", sa.UUID(), nullable=True),
        sa.Column("indicator", sa.String(length=256), nullable=True),
        sa.Column("specimen", sa.String(length=128), nullable=True),
        sa.Column("bei_value", sa.Float(), nullable=True),
        sa.Column("unit", sa.String(length=64), nullable=True),
        sa.Column("sampling_time", sa.String(length=128), nullable=True),
        sa.Column("standard_reference", sa.String(length=256), nullable=True),
        sa.Column("source_table_id", sa.UUID(), nullable=True),
        sa.Column("original_values", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["chemical_id"], ["chemical_registry.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_table_id"], ["extracted_tables.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "regulatory_constraints",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("related_object_type", sa.String(length=64), nullable=False),
        sa.Column("related_object_id", sa.UUID(), nullable=True),
        sa.Column("condition", sa.Text(), nullable=False),
        sa.Column("excluded_context", sa.Text(), nullable=True),
        sa.Column("severity", sa.String(length=32), nullable=False, server_default="warning"),
        sa.Column("source_page", sa.Integer(), nullable=True),
        sa.Column("metadata", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "document_chunks",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("section", sa.String(length=512), nullable=True),
        sa.Column("topic", sa.String(length=256), nullable=True),
        sa.Column("hazard", sa.String(length=256), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("chunk_type", sa.String(length=32), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("source_type", sa.String(length=64), nullable=True),
        sa.Column("standard_reference", sa.String(length=256), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("metadata", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("embedding", Vector(VECTOR_DIM), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_document_chunks_document_id", "document_chunks", ["document_id"])

    op.create_table(
        "extraction_validation_reports",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("ocr_confidence", sa.Float(), nullable=True),
        sa.Column("table_structure_confidence", sa.Float(), nullable=True),
        sa.Column("schema_mapping_confidence", sa.Float(), nullable=True),
        sa.Column("composite_confidence", sa.Float(), nullable=True),
        sa.Column("passed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("details", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    for table in [
        "extraction_validation_reports",
        "document_chunks",
        "regulatory_constraints",
        "biological_exposure_limits",
        "noise_limits",
        "vibration_limits",
        "oel_chemical_limits",
        "chemical_registry",
        "review_queue",
        "formulas",
        "table_cells",
        "extracted_tables",
        "document_pages",
        "documents",
    ]:
        op.drop_table(table)
