"""0026_document_version_flags.sql"""
from lab_db.migrate import apply_migration, read_migration

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

MIGRATION = read_migration("0026_document_version_flags.sql")


def upgrade() -> None:
    apply_migration(MIGRATION)


def downgrade() -> None:
    raise NotImplementedError("migrations are forward-only (SPEC 10.1): restore from backup")
