"""provisioning_tokens

Adds the device_provisioning_tokens table that backs the one-click P50
provisioning short-link flow (`ptt.harro.ch/p/<slug>`). Each row is a
single-use, 24 h TTL handle for a field tech to download an OS-appropriate
ADB-driven setup script. The plaintext Mumble password is stored here
(revealed only once, at generation) because the users table only has a
bcrypt-hashed copy that the script cannot bake into Humla's mumble.db.

Revision ID: b7c5d9e0f1a2
Revises: a3e2f7bc91d4
Create Date: 2026-04-17
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7c5d9e0f1a2'
down_revision: Union[str, None] = 'a3e2f7bc91d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'device_provisioning_tokens',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('slug', sa.String(length=16), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('mumble_password_plaintext', sa.Text(), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('(CURRENT_TIMESTAMP)'),
            nullable=False,
        ),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('os_fetched', sa.String(length=16), nullable=True),
        sa.ForeignKeyConstraint(
            ['user_id'], ['users.id'], ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('slug'),
    )
    op.create_index(
        op.f('ix_device_provisioning_tokens_user_id'),
        'device_provisioning_tokens',
        ['user_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f('ix_device_provisioning_tokens_user_id'),
        table_name='device_provisioning_tokens',
    )
    op.drop_table('device_provisioning_tokens')
