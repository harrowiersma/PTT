"""users: mumble_cert_hash + mumble_registered_user_id

Revision ID: h4c9e5a7f3b2
Revises: g3b8d4f6e2a9
Create Date: 2026-04-22
"""
from alembic import op
import sqlalchemy as sa

revision = "h4c9e5a7f3b2"
down_revision = "g3b8d4f6e2a9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("mumble_cert_hash", sa.String(128), nullable=True))
    op.add_column("users", sa.Column("mumble_registered_user_id", sa.Integer, nullable=True))
    # Partial unique on Postgres; on SQLite the partial clause is still
    # honored and multiple NULLs are allowed under the standard NULL-not-equal
    # rule. Either way: NULLs coexist, non-NULLs are unique.
    op.create_index(
        "uq_users_mumble_cert_hash",
        "users", ["mumble_cert_hash"],
        unique=True,
        postgresql_where=sa.text("mumble_cert_hash IS NOT NULL"),
    )
    op.create_index(
        "uq_users_mumble_registered_user_id",
        "users", ["mumble_registered_user_id"],
        unique=True,
        postgresql_where=sa.text("mumble_registered_user_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_users_mumble_registered_user_id", table_name="users")
    op.drop_index("uq_users_mumble_cert_hash", table_name="users")
    op.drop_column("users", "mumble_registered_user_id")
    op.drop_column("users", "mumble_cert_hash")
