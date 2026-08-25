"""No-data event log for HITL demand prioritization."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "006_no_data_events"
down_revision: Union[str, None] = "005_phase_b_knowledge"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "no_data_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("chemical_id", sa.UUID(), nullable=True),
        sa.Column("cas", sa.String(length=32), nullable=True),
        sa.Column("query_text", sa.Text(), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("intent", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["chemical_id"], ["chemical_registry.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_no_data_events_reason", "no_data_events", ["reason"])
    op.create_index("ix_no_data_events_chemical_id", "no_data_events", ["chemical_id"])
    op.create_index("ix_no_data_events_cas", "no_data_events", ["cas"])
    op.create_index("ix_no_data_events_trace_id", "no_data_events", ["trace_id"])
    op.create_index("ix_no_data_events_created_at", "no_data_events", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_no_data_events_created_at", table_name="no_data_events")
    op.drop_index("ix_no_data_events_trace_id", table_name="no_data_events")
    op.drop_index("ix_no_data_events_cas", table_name="no_data_events")
    op.drop_index("ix_no_data_events_chemical_id", table_name="no_data_events")
    op.drop_index("ix_no_data_events_reason", table_name="no_data_events")
    op.drop_table("no_data_events")
