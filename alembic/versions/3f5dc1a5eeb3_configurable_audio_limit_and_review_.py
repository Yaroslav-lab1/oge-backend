"""Allow per-task audio limits and criterion-level dispute decisions.

Revision ID: 3f5dc1a5eeb3
Revises: 9ce04fc9152d
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "3f5dc1a5eeb3"
down_revision: str | Sequence[str] | None = "9ce04fc9152d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _replace_play_count_check(max_plays: int) -> None:
    bind = op.get_bind()
    expression = f"play_count >= 0 AND play_count <= {max_plays}"
    if bind.dialect.name == "sqlite":
        # The previous CHECK was anonymous; Alembic batch mode drops anonymous
        # checks during recreation and installs this replacement explicitly.
        with op.batch_alter_table(
            "audio_playbacks",
            recreate="always",
            table_args=[sa.CheckConstraint(expression)],
        ):
            pass
        return

    checks = sa.inspect(bind).get_check_constraints("audio_playbacks")
    for check in checks:
        if "play_count" in check.get("sqltext", ""):
            op.drop_constraint(check["name"], "audio_playbacks", type_="check")
    op.create_check_constraint("ck_audio_playbacks_play_count_limit", "audio_playbacks", expression)


def upgrade() -> None:
    _replace_play_count_check(10)
    op.add_column("review_disputes", sa.Column("final_criteria", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    excessive = bind.execute(
        sa.text("SELECT COUNT(*) FROM audio_playbacks WHERE play_count > 2")
    ).scalar_one()
    if excessive:
        raise RuntimeError("Cannot downgrade while an audio playback count exceeds two")
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("review_disputes") as batch:
            batch.drop_column("final_criteria")
    else:
        op.drop_column("review_disputes", "final_criteria")
    _replace_play_count_check(2)
