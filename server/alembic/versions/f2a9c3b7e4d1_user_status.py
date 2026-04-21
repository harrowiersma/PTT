"""user status + audibility columns

Revision ID: f2a9c3b7e4d1
Revises: e1c4f8a3b5d6
Create Date: 2026-04-21
"""
from alembic import op
import sqlalchemy as sa

revision = "f2a9c3b7e4d1"
down_revision = "e1c4f8a3b5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("status_label", sa.String(16), nullable=True))
    op.add_column("users", sa.Column("status_updated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("is_audible", sa.Boolean, nullable=True))
    op.add_column("users", sa.Column("is_audible_updated_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "is_audible_updated_at")
    op.drop_column("users", "is_audible")
    op.drop_column("users", "status_updated_at")
    op.drop_column("users", "status_label")
