"""0023_external_submissions.sql"""
from lab_db.migrate import apply_migration, read_migration

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

MIGRATION = read_migration("0023_external_submissions.sql")


def upgrade() -> None:
    apply_migration(MIGRATION)


def downgrade() -> None:
    raise NotImplementedError("migrations are forward-only (SPEC 10.1): restore from backup")
