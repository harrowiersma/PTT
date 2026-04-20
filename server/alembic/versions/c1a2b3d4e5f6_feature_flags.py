"""feature_flags

Adds the feature_flags table that backs the admin-configurable module
toggles (lone_worker, sip, dispatch, weather, sos). Seeds all five
rows as enabled=True so existing deployments keep their current
behavior — operators opt out, they don't opt in.

Revision ID: c1a2b3d4e5f6
Revises: b7c5d9e0f1a2
Create Date: 2026-04-20
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c1a2b3d4e5f6"
down_revision: Union[str, None] = "b7c5d9e0f1a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "feature_flags",
        sa.Column("key", sa.String(length=32), nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
        ),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("key"),
    )
    for key in ("lone_worker", "sip", "dispatch", "weather", "sos"):
        op.execute(
            f"INSERT INTO feature_flags (key, enabled) VALUES ('{key}', TRUE)"
        )


def downgrade() -> None:
    op.drop_table("feature_flags")
