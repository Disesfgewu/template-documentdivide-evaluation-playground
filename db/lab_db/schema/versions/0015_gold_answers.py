"""0015_gold_answers.sql"""
from lab_db.migrate import apply_migration, read_migration

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

MIGRATION = read_migration("0015_gold_answers.sql")


def upgrade() -> None:
    apply_migration(MIGRATION)


def downgrade() -> None:
    raise NotImplementedError("migrations are forward-only (SPEC 10.1): restore from backup")
