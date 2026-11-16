"""Create OR REUSE the local owner session for the web workflow page (development only; Issue #194).

Run inside the api container by ``scripts/demo.sh ui``. On an empty database it inserts one project and one owner actor and
prints ``SESSION=created``; when the web project already exists (same name, an owner member) it keeps the SAME project and owner
and only issues a fresh token for that owner (``SESSION=reused``), so the documents and benchmarks stay reachable after a
restart or upgrade. Prints the project id and a bearer token to the local terminal only; it is a development token for the local
stack and must not be shared or committed."""
from __future__ import annotations

import uuid

import psycopg

from lab_api.auth import get_signing_secret, issue_actor_token
from lab_api.config import load_settings


def main() -> int:
    settings = load_settings()
    secret = get_signing_secret(settings)
    with psycopg.connect(settings.database_url.get_secret_value()) as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(194)")       # two `ui` runs at once must not both create one
        row = conn.execute(
            "SELECT p.id, m.actor_id FROM projects p JOIN project_members m ON m.project_id = p.id AND m.role::text = 'owner' "
            "JOIN actors a ON a.id = m.actor_id WHERE p.name = 'web-workflow' AND a.display = 'web owner' "
            "ORDER BY p.created_at, p.id LIMIT 1").fetchone()
        if row is not None:
            project, owner = row
            session = "reused"
        else:
            project, owner = uuid.uuid4(), uuid.uuid4()
            conn.execute("INSERT INTO projects (id, name) VALUES (%s, 'web-workflow')", (project,))
            conn.execute("INSERT INTO actors (id, kind, display) VALUES (%s, 'user', 'web owner')", (owner,))
            conn.execute("INSERT INTO project_members VALUES (%s, %s, 'owner')", (project, owner))
            session = "created"
    print(f"SESSION={session}")
    print(f"PROJECT_ID={project}")
    print(f"TOKEN={issue_actor_token(owner, secret)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
