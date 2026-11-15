"""0001_core.sql"""
from lab_db.migrate import apply_migration, read_migration

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

MIGRATION = read_migration("0001_core.sql")


def upgrade() -> None:
    apply_migration(MIGRATION)


def downgrade() -> None:
    raise NotImplementedError("migrations are forward-only (SPEC 10.1): restore from backup")
