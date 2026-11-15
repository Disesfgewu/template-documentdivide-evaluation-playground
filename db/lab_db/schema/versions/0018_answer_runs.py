"""0018_answer_runs.sql"""
from lab_db.migrate import apply_migration, read_migration

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

MIGRATION = read_migration("0018_answer_runs.sql")


def upgrade() -> None:
    apply_migration(MIGRATION)


def downgrade() -> None:
    raise NotImplementedError("migrations are forward-only (SPEC 10.1): restore from backup")
