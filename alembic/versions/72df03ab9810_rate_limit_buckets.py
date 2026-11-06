"""Shared per-operation rate limits.

Revision ID: 72df03ab9810
Revises: 3f5dc1a5eeb3
"""

import sqlalchemy as sa

from alembic import op

revision = "72df03ab9810"
down_revision = "3f5dc1a5eeb3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rate_limit_buckets",
        sa.Column("scope", sa.String(32), primary_key=True),
        sa.Column("key_hash", sa.String(64), primary_key=True),
        sa.Column("window", sa.Integer(), primary_key=True),
        sa.Column("count", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("rate_limit_buckets")
