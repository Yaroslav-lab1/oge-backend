"""Move one-use audio tokens into independent grants.

Revision ID: 9ce04fc9152d
Revises: 64114cd14d81
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "9ce04fc9152d"
down_revision: str | Sequence[str] | None = "64114cd14d81"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SQLITE_NAMES = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def _play_count_check() -> sa.CheckConstraint:
    return sa.CheckConstraint("play_count >= 0 AND play_count <= 2")


def _drop_legacy_token_columns() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # The original SQLite constraint has no name. Batch mode gives it a
        # deterministic name while keeping the original anonymous CHECK.
        with op.batch_alter_table(
            "audio_playbacks",
            naming_convention=_SQLITE_NAMES,
            table_args=[_play_count_check()],
        ) as batch:
            batch.drop_constraint("uq_audio_playbacks_token_hash", type_="unique")
            batch.drop_column("token_hash")
            batch.drop_column("token_expires_at")
            batch.drop_column("token_consumed_at")
        return

    constraints = sa.inspect(bind).get_unique_constraints("audio_playbacks")
    token_unique = next(
        item["name"] for item in constraints if item["column_names"] == ["token_hash"]
    )
    op.drop_constraint(token_unique, "audio_playbacks", type_="unique")
    op.drop_column("audio_playbacks", "token_hash")
    op.drop_column("audio_playbacks", "token_expires_at")
    op.drop_column("audio_playbacks", "token_consumed_at")


def _restore_legacy_token_columns() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table(
            "audio_playbacks",
            naming_convention=_SQLITE_NAMES,
            table_args=[_play_count_check()],
        ) as batch:
            batch.add_column(sa.Column("token_hash", sa.String(64), nullable=True))
            batch.add_column(
                sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True)
            )
            batch.add_column(
                sa.Column("token_consumed_at", sa.DateTime(timezone=True), nullable=True)
            )
            batch.create_unique_constraint("uq_audio_playbacks_token_hash", ["token_hash"])
        return

    op.add_column("audio_playbacks", sa.Column("token_hash", sa.String(64), nullable=True))
    op.add_column(
        "audio_playbacks",
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "audio_playbacks",
        sa.Column("token_consumed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint("audio_playbacks_token_hash_key", "audio_playbacks", ["token_hash"])


def upgrade() -> None:
    bind = op.get_bind()
    legacy_tokens = list(
        bind.execute(
            sa.text(
                "SELECT id, token_hash, token_expires_at, token_consumed_at "
                "FROM audio_playbacks WHERE token_hash IS NOT NULL"
            )
        ).mappings()
    )

    _drop_legacy_token_columns()
    op.create_table(
        "audio_grants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "playback_id",
            sa.String(36),
            sa.ForeignKey("audio_playbacks.id"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audio_grants_playback_id", "audio_grants", ["playback_id"])

    now = datetime.now(UTC)
    expired = datetime(1970, 1, 1, tzinfo=UTC)
    for token in legacy_tokens:
        # A legacy token with no expiry must never become a valid grant.
        bind.execute(
            sa.text(
                "INSERT INTO audio_grants "
                "(id, playback_id, token_hash, expires_at, consumed_at, created_at) "
                "VALUES (:id, :playback_id, :token_hash, :expires_at, :consumed_at, :created_at)"
            ),
            {
                "id": str(uuid4()),
                "playback_id": token["id"],
                "token_hash": token["token_hash"],
                "expires_at": token["token_expires_at"] or expired,
                "consumed_at": token["token_consumed_at"],
                "created_at": now,
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    grants = list(
        bind.execute(
            sa.text(
                "SELECT playback_id, token_hash, expires_at, consumed_at "
                "FROM audio_grants ORDER BY created_at DESC, id DESC"
            )
        ).mappings()
    )
    op.drop_index("ix_audio_grants_playback_id", table_name="audio_grants")
    op.drop_table("audio_grants")
    _restore_legacy_token_columns()

    # The previous schema can store only one token per playback. Keep its
    # newest grant so a downgrade remains usable for existing playbacks.
    seen: set[str] = set()
    for grant in grants:
        playback_id = grant["playback_id"]
        if playback_id in seen:
            continue
        seen.add(playback_id)
        bind.execute(
            sa.text(
                "UPDATE audio_playbacks SET token_hash=:token_hash, "
                "token_expires_at=:expires_at, token_consumed_at=:consumed_at "
                "WHERE id=:playback_id"
            ),
            dict(grant),
        )
