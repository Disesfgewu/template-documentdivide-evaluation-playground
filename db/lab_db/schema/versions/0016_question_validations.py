"""0016_question_validations.sql"""
from lab_db.migrate import apply_migration, read_migration

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

MIGRATION = read_migration("0016_question_validations.sql")


def upgrade() -> None:
    apply_migration(MIGRATION)


def downgrade() -> None:
    raise NotImplementedError("migrations are forward-only (SPEC 10.1): restore from backup")
