"""${message}"""
from lab_db.migrate import apply_migration, read_migration

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = None
depends_on = None

# Edit: the SQL file this revision applies (created next to the other migrations).
MIGRATION = read_migration("${up_revision}_REPLACE_ME.sql")


def upgrade() -> None:
    apply_migration(MIGRATION)


def downgrade() -> None:
    raise NotImplementedError("migrations are forward-only (SPEC 10.1): restore from backup")
