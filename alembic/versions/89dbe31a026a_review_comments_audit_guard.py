"""Criterion comments and database-level append-only audit protection."""

import sqlalchemy as sa

from alembic import op

revision = "89dbe31a026a"
down_revision = "72df03ab9810"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("review_scores", sa.Column("comment", sa.String(2000), nullable=True))
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""CREATE FUNCTION reject_audit_mutation() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION 'Audit is append only'; END;
            $$ LANGUAGE plpgsql""")
        op.execute("""CREATE TRIGGER audit_append_only BEFORE UPDATE OR DELETE ON audit_events
            FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation()""")
    else:
        for operation in ("UPDATE", "DELETE"):
            op.execute(f"""CREATE TRIGGER audit_no_{operation.lower()} BEFORE {operation}
                ON audit_events BEGIN SELECT RAISE(ABORT, 'Audit is append only'); END""")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER audit_append_only ON audit_events")
        op.execute("DROP FUNCTION reject_audit_mutation()")
    else:
        op.execute("DROP TRIGGER audit_no_update")
        op.execute("DROP TRIGGER audit_no_delete")
    op.drop_column("review_scores", "comment")
