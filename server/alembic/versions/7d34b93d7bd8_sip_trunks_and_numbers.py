"""sip_trunks_and_numbers

Revision ID: 7d34b93d7bd8
Revises: 1f38074872ac
Create Date: 2026-04-17 11:28:59.422439
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '7d34b93d7bd8'
down_revision: Union[str, None] = '1f38074872ac'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Trunks first so sip_numbers can FK to it.
    op.create_table(
        'sip_trunks',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('label', sa.String(length=128), nullable=False),
        sa.Column('sip_host', sa.String(length=256), nullable=False),
        sa.Column('sip_port', sa.Integer(), nullable=False),
        # NULL sip_user/password = IP-auth trunk (DIDWW etc.)
        sa.Column('sip_user', sa.String(length=128), nullable=True),
        sa.Column('sip_password', sa.String(length=256), nullable=True),
        sa.Column('from_uri', sa.String(length=256), nullable=True),
        sa.Column('transport', sa.String(length=8), nullable=False),
        sa.Column('registration_interval_s', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'sip_numbers',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('trunk_id', sa.Integer(), nullable=False),
        sa.Column('did', sa.String(length=64), nullable=False),
        sa.Column('label', sa.String(length=128), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['trunk_id'], ['sip_trunks.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('did'),
    )
    op.create_index(op.f('ix_sip_numbers_trunk_id'), 'sip_numbers', ['trunk_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_sip_numbers_trunk_id'), table_name='sip_numbers')
    op.drop_table('sip_numbers')
    op.drop_table('sip_trunks')
