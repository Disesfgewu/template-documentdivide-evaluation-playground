"""Create a local owner session for the web workflow page (development only).

Run inside the api container by ``scripts/demo.sh ui``: inserts one project and one owner actor (as deploy/demo_run.py does)
and prints the project id and a bearer token for that owner, to paste into the web page. The token is printed to the local
terminal only; it is a development token for the local stack and must not be shared or committed."""
from __future__ import annotations

import uuid

import psycopg

from lab_api.auth import get_signing_secret, issue_actor_token
from lab_api.config import load_settings


def main() -> int:
    settings = load_settings()
    secret = get_signing_secret(settings)
    project, owner = uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(settings.database_url.get_secret_value()) as conn, conn.transaction():
        conn.execute("INSERT INTO projects (id, name) VALUES (%s, 'web-workflow')", (project,))
        conn.execute("INSERT INTO actors (id, kind, display) VALUES (%s, 'user', 'web owner')", (owner,))
        conn.execute("INSERT INTO project_members VALUES (%s, %s, 'owner')", (project, owner))
    print(f"PROJECT_ID={project}")
    print(f"TOKEN={issue_actor_token(owner, secret)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
