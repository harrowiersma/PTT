"""feature_flags: seed call_groups_hiding (enabled=False)

Revision ID: i5d8a2f4c6b7
Revises: h4c9e5a7f3b2
Create Date: 2026-04-22
"""
from alembic import op
import sqlalchemy as sa

revision = "i5d8a2f4c6b7"
down_revision = "h4c9e5a7f3b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent UPSERT — Postgres-only syntax is fine; this migration
    # only targets the Postgres admin DB. Default off: deploying the
    # code must not flip the ACL hiding behaviour.
    op.execute(
        sa.text(
            "INSERT INTO feature_flags (key, enabled) "
            "VALUES ('call_groups_hiding', false) "
            "ON CONFLICT (key) DO NOTHING;"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM feature_flags WHERE key = 'call_groups_hiding';"
        )
    )
