"""0014_generate_jobs_llm_calls.sql"""
from lab_db.migrate import apply_migration, read_migration

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

MIGRATION = read_migration("0014_generate_jobs_llm_calls.sql")


def upgrade() -> None:
    apply_migration(MIGRATION)


def downgrade() -> None:
    raise NotImplementedError("migrations are forward-only (SPEC 10.1): restore from backup")
