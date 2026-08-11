"""Phase B knowledge schema — provenance columns, idempotency, vector index."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "005_phase_b_knowledge"
down_revision: Union[str, None] = "004_hitl_review_system"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # --- OEL limits: idempotent production keys + provenance metadata ---
    op.add_column("oel_chemical_limits", sa.Column("source_row_key", sa.String(length=128), nullable=True))
    op.add_column("oel_chemical_limits", sa.Column("source_table_id_str", sa.String(length=128), nullable=True))
    op.add_column("oel_chemical_limits", sa.Column("validation_status", sa.String(length=32), server_default="accepted", nullable=False))
    op.add_column("oel_chemical_limits", sa.Column("gold_artifact_path", sa.String(length=1024), nullable=True))
    op.add_column("oel_chemical_limits", sa.Column("gold_version", sa.String(length=64), nullable=True))
    op.add_column("oel_chemical_limits", sa.Column("knowledge_metadata", sa.dialects.postgresql.JSONB(), nullable=True))
    op.add_column("oel_chemical_limits", sa.Column("accepted_values", sa.dialects.postgresql.JSONB(), nullable=True))
    op.create_index("ix_oel_source_row_key", "oel_chemical_limits", ["source_row_key"])
    op.create_unique_constraint(
        "uq_oel_chemical_row",
        "oel_chemical_limits",
        ["chemical_id", "source_row_key"],
    )

    # --- Chemical registry: multilingual aliases ---
    op.add_column("chemical_registry", sa.Column("aliases", sa.dialects.postgresql.JSONB(), nullable=True))
    op.add_column("chemical_registry", sa.Column("validation_status", sa.String(length=32), server_default="accepted", nullable=False))
    op.add_column("chemical_registry", sa.Column("gold_artifact_path", sa.String(length=1024), nullable=True))

    # --- Document chunks: semantic store contract ---
    op.add_column("document_chunks", sa.Column("chunk_id", sa.String(length=128), nullable=True))
    op.add_column("document_chunks", sa.Column("printed_page_number", sa.Integer(), nullable=True))
    op.add_column("document_chunks", sa.Column("section_id", sa.String(length=128), nullable=True))
    op.add_column("document_chunks", sa.Column("section_title", sa.String(length=512), nullable=True))
    op.add_column("document_chunks", sa.Column("validation_status", sa.String(length=32), server_default="accepted", nullable=False))
    op.add_column("document_chunks", sa.Column("embedding_model", sa.String(length=128), nullable=True))
    op.add_column("document_chunks", sa.Column("embedding_version", sa.String(length=64), nullable=True))
    op.add_column("document_chunks", sa.Column("content_version", sa.String(length=64), nullable=True))
    op.add_column("document_chunks", sa.Column("embedding_dimension", sa.Integer(), nullable=True))
    op.add_column("document_chunks", sa.Column("provenance", sa.dialects.postgresql.JSONB(), nullable=True))
    op.add_column("document_chunks", sa.Column("gold_artifact_path", sa.String(length=1024), nullable=True))
    op.create_index("ix_document_chunks_chunk_id", "document_chunks", ["chunk_id"])
    op.create_unique_constraint(
        "uq_document_chunk_stable",
        "document_chunks",
        ["document_id", "chunk_id"],
    )

    # --- Formula registry extensions ---
    op.add_column("formulas", sa.Column("stable_formula_id", sa.String(length=128), nullable=True))
    op.add_column("formulas", sa.Column("persian_name", sa.String(length=512), nullable=True))
    op.add_column("formulas", sa.Column("domain", sa.String(length=128), nullable=True))
    op.add_column("formulas", sa.Column("validation_status", sa.String(length=32), server_default="accepted", nullable=False))
    op.add_column("formulas", sa.Column("formula_version", sa.String(length=64), nullable=True))
    op.add_column("formulas", sa.Column("source_reference", sa.dialects.postgresql.JSONB(), nullable=True))
    op.add_column("formulas", sa.Column("semantics", sa.dialects.postgresql.JSONB(), nullable=True))
    op.add_column("formulas", sa.Column("applicability_conditions", sa.Text(), nullable=True))
    op.add_column("formulas", sa.Column("gold_artifact_path", sa.String(length=1024), nullable=True))
    op.add_column("formulas", sa.Column("knowledge_metadata", sa.dialects.postgresql.JSONB(), nullable=True))
    op.create_index("ix_formulas_stable_formula_id", "formulas", ["stable_formula_id"], unique=True)
    op.create_index("ix_formulas_domain", "formulas", ["domain"])

    # --- Knowledge sync audit log ---
    op.create_table(
        "knowledge_sync_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("sync_type", sa.String(length=64), nullable=False),
        sa.Column("pipeline_version", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stats", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="running", nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_knowledge_sync_runs_type", "knowledge_sync_runs", ["sync_type"])

    # pgvector HNSW is limited to 2000 dimensions; gemini-embedding-001 uses 3072.
    # Sequential scan is used for cosine search until pgvector supports higher-dim indexes.
    # IVFFlat index can be added manually after validating pgvector version limits.


def downgrade() -> None:
    op.drop_index("ix_knowledge_sync_runs_type", table_name="knowledge_sync_runs")
    op.drop_table("knowledge_sync_runs")
    op.drop_index("ix_formulas_domain", table_name="formulas")
    op.drop_index("ix_formulas_stable_formula_id", table_name="formulas")
    for col in (
        "knowledge_metadata",
        "gold_artifact_path",
        "applicability_conditions",
        "semantics",
        "source_reference",
        "formula_version",
        "validation_status",
        "domain",
        "persian_name",
        "stable_formula_id",
    ):
        op.drop_column("formulas", col)
    op.drop_constraint("uq_document_chunk_stable", "document_chunks", type_="unique")
    op.drop_index("ix_document_chunks_chunk_id", table_name="document_chunks")
    for col in (
        "gold_artifact_path",
        "provenance",
        "embedding_dimension",
        "content_version",
        "embedding_version",
        "embedding_model",
        "validation_status",
        "section_title",
        "section_id",
        "printed_page_number",
        "chunk_id",
    ):
        op.drop_column("document_chunks", col)
    op.drop_column("chemical_registry", "gold_artifact_path")
    op.drop_column("chemical_registry", "validation_status")
    op.drop_column("chemical_registry", "aliases")
    op.drop_constraint("uq_oel_chemical_row", "oel_chemical_limits", type_="unique")
    op.drop_index("ix_oel_source_row_key", table_name="oel_chemical_limits")
    for col in (
        "accepted_values",
        "knowledge_metadata",
        "gold_version",
        "gold_artifact_path",
        "validation_status",
        "source_table_id_str",
        "source_row_key",
    ):
        op.drop_column("oel_chemical_limits", col)
