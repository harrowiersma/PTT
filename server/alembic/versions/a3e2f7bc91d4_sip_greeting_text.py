"""sip_greeting_text

Adds a nullable greeting_text column to sip_trunks. NULL means "fall
back to the GREETING_TEXT env var on sip-bridge"; a value overrides
that. Editable from the admin dashboard → immediate regeneration + push
to sip-bridge's asterisk sounds dir so the next call uses the new audio
without a container restart.

Revision ID: a3e2f7bc91d4
Revises: 1f38074872ac
Create Date: 2026-04-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a3e2f7bc91d4'
down_revision: Union[str, None] = '7d34b93d7bd8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'sip_trunks',
        sa.Column('greeting_text', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('sip_trunks', 'greeting_text')
