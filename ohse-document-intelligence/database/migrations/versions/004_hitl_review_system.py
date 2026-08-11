"""Human-in-the-loop review system — tasks, decisions, corrections, candidates, validation runs."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "004_hitl_review_system"
down_revision = "003_evidence_pipeline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "extraction_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("candidate_type", sa.String(32), nullable=False),
        sa.Column("stable_id", sa.String(128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("parent_version_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("extraction_candidates.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="candidate"),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("storage_path", sa.String(1024), nullable=True),
        sa.Column("pipeline_version", sa.String(32), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("created_by_decision_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("document_id", "candidate_type", "stable_id", "version", name="uq_candidate_version"),
    )
    op.create_index("ix_extraction_candidates_document_id", "extraction_candidates", ["document_id"])
    op.create_index("ix_extraction_candidates_stable_id", "extraction_candidates", ["stable_id"])
    op.create_index("ix_extraction_candidates_status", "extraction_candidates", ["status"])
    op.create_index("ix_extraction_candidates_page_number", "extraction_candidates", ["page_number"])

    op.create_table(
        "validation_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("extraction_candidates.id", ondelete="SET NULL"), nullable=True),
        sa.Column("target_type", sa.String(32), nullable=True),
        sa.Column("target_id", sa.String(128), nullable=True),
        sa.Column("run_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("validation_report", postgresql.JSONB(), nullable=False),
        sa.Column("acceptance_contract", postgresql.JSONB(), nullable=True),
        sa.Column("overall_status", sa.String(32), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("pipeline_version", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_validation_runs_document_id", "validation_runs", ["document_id"])
    op.create_index("ix_validation_runs_candidate_id", "validation_runs", ["candidate_id"])
    op.create_index("ix_validation_runs_target_id", "validation_runs", ["target_id"])

    op.create_table(
        "validation_issues",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("validation_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("validation_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("issue_code", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("field_name", sa.String(128), nullable=True),
        sa.Column("target_type", sa.String(32), nullable=True),
        sa.Column("target_id", sa.String(128), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_validation_issues_validation_run_id", "validation_issues", ["validation_run_id"])
    op.create_index("ix_validation_issues_issue_code", "validation_issues", ["issue_code"])

    op.create_table(
        "review_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("table_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("extracted_tables.id", ondelete="SET NULL"), nullable=True),
        sa.Column("cell_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("table_cells.id", ondelete="SET NULL"), nullable=True),
        sa.Column("stable_table_id", sa.String(128), nullable=True),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("extraction_candidates.id", ondelete="SET NULL"), nullable=True),
        sa.Column("validation_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("validation_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_id", sa.String(128), nullable=False),
        sa.Column("primary_issue_code", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("priority", sa.String(16), nullable=False, server_default="medium"),
        sa.Column("assigned_to", sa.String(256), nullable=True),
        sa.Column("reviewer_type", sa.String(16), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pipeline_version", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(512), nullable=False),
        sa.Column("evidence_reference", postgresql.JSONB(), nullable=True),
        sa.Column("evidence_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("issue_metadata", postgresql.JSONB(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_review_task_idempotency"),
    )
    op.create_index("ix_review_tasks_status", "review_tasks", ["status"])
    op.create_index("ix_review_tasks_priority_status", "review_tasks", ["priority", "status"])
    op.create_index("ix_review_tasks_assigned_status", "review_tasks", ["assigned_to", "status"])
    op.create_index("ix_review_tasks_document_page", "review_tasks", ["document_id", "page_number"])
    op.create_index("ix_review_tasks_stable_table_id", "review_tasks", ["stable_table_id"])
    op.create_index("ix_review_tasks_primary_issue_code", "review_tasks", ["primary_issue_code"])
    op.create_index("ix_review_tasks_target_id", "review_tasks", ["target_id"])
    op.create_index("ix_review_tasks_created_at", "review_tasks", ["created_at"])

    op.create_table(
        "review_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("review_task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("review_tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("reviewer_id", sa.String(256), nullable=False),
        sa.Column("reviewer_type", sa.String(16), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("previous_status", sa.String(32), nullable=False),
        sa.Column("new_status", sa.String(32), nullable=False),
        sa.Column("validation_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("validation_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_review_decisions_review_task_id", "review_decisions", ["review_task_id"])

    op.create_table(
        "review_corrections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("review_decision_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("review_decisions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("review_task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("review_tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_id", sa.String(128), nullable=False),
        sa.Column("field_name", sa.String(128), nullable=False),
        sa.Column("original_value", sa.Text(), nullable=True),
        sa.Column("corrected_value", sa.Text(), nullable=True),
        sa.Column("correction_type", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("evidence_reference", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_review_corrections_review_task_id", "review_corrections", ["review_task_id"])

    op.create_table(
        "review_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("review_task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("review_tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(256), nullable=False),
        sa.Column("actor_type", sa.String(16), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_review_events_review_task_id", "review_events", ["review_task_id"])
    op.create_index("ix_review_events_task_created", "review_events", ["review_task_id", "created_at"])


def downgrade() -> None:
    op.drop_table("review_events")
    op.drop_table("review_corrections")
    op.drop_table("review_decisions")
    op.drop_table("review_tasks")
    op.drop_table("validation_issues")
    op.drop_table("validation_runs")
    op.drop_table("extraction_candidates")
