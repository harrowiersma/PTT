"""call_logs

Revision ID: d7e9a1f2b0c3
Revises: c1a2b3d4e5f6
Create Date: 2026-04-21
"""
from alembic import op
import sqlalchemy as sa


revision = "d7e9a1f2b0c3"
down_revision = "c1a2b3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "call_logs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("caller_id", sa.String(64), nullable=True),
        sa.Column("slot", sa.Integer, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answered_by", sa.String(64), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_s", sa.Integer, nullable=True),
    )
    # Index on started_at for the "most recent N calls" query the
    # dashboard uses; descending because we'll almost always scan newest-
    # first.
    op.create_index(
        "ix_call_logs_started_at", "call_logs", ["started_at"],
        unique=False, postgresql_ops={"started_at": "DESC"},
    )


def downgrade() -> None:
    op.drop_index("ix_call_logs_started_at", table_name="call_logs")
    op.drop_table("call_logs")
