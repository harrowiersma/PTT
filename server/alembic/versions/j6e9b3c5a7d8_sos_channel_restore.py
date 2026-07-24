"""sos_channel_restore: persist per-user return-to-channel across restarts

Revision ID: j6e9b3c5a7d8
Revises: i5d8a2f4c6b7
Create Date: 2026-07-24

Adds the sos_channel_restore table so an SOS trigger's move-all-to-
Emergency step writes each user's previous channel to the DB. Before
this, that dict lived only in the admin process — an admin restart
between /api/sos/trigger and /api/sos/ack left users stranded in
Emergency because the in-memory _original_channels map was gone.
See docs/ops-notes.md.
"""
from alembic import op
import sqlalchemy as sa

revision = "j6e9b3c5a7d8"
down_revision = "i5d8a2f4c6b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sos_channel_restore",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "sos_event_id",
            sa.Integer(),
            sa.ForeignKey("sos_events.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("sos_event_id", "username",
                            name="uq_sos_restore_event_user"),
    )


def downgrade() -> None:
    op.drop_table("sos_channel_restore")
