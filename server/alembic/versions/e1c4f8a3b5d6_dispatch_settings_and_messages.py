"""dispatch_settings + dispatch_canned_messages

Revision ID: e1c4f8a3b5d6
Revises: d7e9a1f2b0c3
Create Date: 2026-04-21
"""
from alembic import op
import sqlalchemy as sa

revision = "e1c4f8a3b5d6"
down_revision = "d7e9a1f2b0c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dispatch_settings",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("map_home_lat", sa.Float, nullable=False),
        sa.Column("map_home_lng", sa.Float, nullable=False),
        sa.Column("map_home_zoom", sa.Integer, nullable=False, server_default="11"),
        sa.Column("max_workers", sa.Integer, nullable=False, server_default="10"),
        sa.Column("search_radius_m", sa.Integer, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_by", sa.String(64), nullable=True),
    )
    # Singleton seed: Lisbon centre, default search behaviour.
    op.execute(
        "INSERT INTO dispatch_settings (id, map_home_lat, map_home_lng, "
        "map_home_zoom, max_workers, search_radius_m) "
        "VALUES (1, 38.72, -9.14, 11, 10, NULL)"
    )

    op.create_table(
        "dispatch_canned_messages",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("label", sa.String(64), nullable=False),
        sa.Column("message", sa.String(500), nullable=False),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("dispatch_canned_messages")
    op.drop_table("dispatch_settings")
