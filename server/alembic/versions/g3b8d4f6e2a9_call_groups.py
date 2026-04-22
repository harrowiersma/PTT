"""call_groups + user_call_groups + channels.call_group_id

Revision ID: g3b8d4f6e2a9
Revises: f2a9c3b7e4d1
Create Date: 2026-04-22
"""
from alembic import op
import sqlalchemy as sa

revision = "g3b8d4f6e2a9"
down_revision = "f2a9c3b7e4d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "call_groups",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column("description", sa.String(256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "user_call_groups",
        sa.Column("user_id", sa.Integer, nullable=False),
        sa.Column("call_group_id", sa.Integer, nullable=False),
        sa.PrimaryKeyConstraint("user_id", "call_group_id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["call_group_id"], ["call_groups.id"], ondelete="CASCADE"),
    )

    op.add_column(
        "channels",
        sa.Column("call_group_id", sa.Integer, nullable=True),
    )
    op.create_foreign_key(
        "fk_channels_call_group_id",
        "channels", "call_groups",
        ["call_group_id"], ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_channels_call_group_id", "channels", type_="foreignkey")
    op.drop_column("channels", "call_group_id")
    op.drop_table("user_call_groups")
    op.drop_table("call_groups")
