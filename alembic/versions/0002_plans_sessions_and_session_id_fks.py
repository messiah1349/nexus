"""plans, sessions, and session_id FKs

Adds the v1 plan-driven, session-bounded shape on top of the 0001 base
schema:
- plans table (with self-FK for revision chain via superseded_by)
- sessions table (no summary_id — the link is one-directional via
  summaries.session_id, see docs/schema.md)
- messages.session_id and summaries.session_id added with FKs.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plans",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("horizon", sa.Text(), nullable=False),
        sa.Column(
            "status", sa.Text(), nullable=False, server_default=sa.text("'active'")
        ),
        sa.Column(
            "items",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("target_date", sa.Date(), nullable=True),
        sa.Column("superseded_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_plans_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["superseded_by"], ["plans.id"], name="fk_plans_superseded_by_plans"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_plans"),
    )
    op.create_index(
        "ix_plans_project_status_horizon",
        "plans",
        ["project_id", "status", "horizon"],
    )
    op.create_index(
        "ix_plans_project_horizon_active",
        "plans",
        ["project_id", "horizon"],
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "sessions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("plan_item_index", sa.Integer(), nullable=True),
        sa.Column(
            "kind", sa.Text(), nullable=False, server_default=sa.text("'lesson'")
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.Text(), nullable=True),
        sa.Column(
            "status", sa.Text(), nullable=False, server_default=sa.text("'active'")
        ),
        sa.Column(
            "attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_sessions_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["plans.id"], name="fk_sessions_plan_id_plans"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sessions"),
    )
    op.create_index("ix_sessions_project_started", "sessions", ["project_id", "started_at"])
    op.create_index(
        "ix_sessions_project_active",
        "sessions",
        ["project_id"],
        postgresql_where=sa.text("status = 'active'"),
    )

    op.add_column(
        "messages",
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_messages_session_id_sessions",
        "messages",
        "sessions",
        ["session_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_messages_session_occurred", "messages", ["session_id", "occurred_at"])

    op.add_column(
        "summaries",
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_summaries_session_id_sessions",
        "summaries",
        "sessions",
        ["session_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_summaries_session", "summaries", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_summaries_session", table_name="summaries")
    op.drop_constraint(
        "fk_summaries_session_id_sessions", "summaries", type_="foreignkey"
    )
    op.drop_column("summaries", "session_id")

    op.drop_index("ix_messages_session_occurred", table_name="messages")
    op.drop_constraint(
        "fk_messages_session_id_sessions", "messages", type_="foreignkey"
    )
    op.drop_column("messages", "session_id")

    op.drop_index("ix_sessions_project_active", table_name="sessions")
    op.drop_index("ix_sessions_project_started", table_name="sessions")
    op.drop_table("sessions")

    op.drop_index("ix_plans_project_horizon_active", table_name="plans")
    op.drop_index("ix_plans_project_status_horizon", table_name="plans")
    op.drop_table("plans")
