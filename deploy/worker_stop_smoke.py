"""Runs INSIDE the worker container (``docker exec -i <container> python - < deploy/worker_stop_smoke.py``), Refs #7.

Queues a SYNTHETIC multi-page parse (default 80 pages, or the first argument) and returns as soon as the running worker has leased it (status
``running``), printing one JSON line with the job id. The CI step then runs ``docker compose stop`` while the
job is in flight and watches what happens to it (see the ``worker-image`` job). Nothing here parses anything.
"""
from __future__ import annotations

import json
import sys
import time
import uuid

import psycopg
from fastapi.testclient import TestClient

from lab_api.auth import get_signing_secret, issue_actor_token
from lab_api.config import load_settings
from lab_api.main import create_app

PAGES = int(sys.argv[1]) if len(sys.argv) > 1 else 80   # a bigger value makes the job outlast the worker grace period
LINE = "Synthetic mid-job stop page %d. The quick brown fox jumps over the lazy dog."


def synthetic_pdf(pages: int) -> bytes:
    objs = [b"", b""]
    kids = []
    font_id = 3 + 2 * pages
    for i in range(pages):
        page_id = 3 + 2 * i
        kids.append(page_id)
        stream = ("BT /F1 14 Tf 72 720 Td (%s) Tj ET" % (LINE % i)).encode("ascii")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {page_id + 1} 0 R "
                    f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode())
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objs[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>".encode()
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for n, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def main() -> int:
    settings = load_settings()
    project, owner = uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(settings.database_url.get_secret_value()) as conn, conn.transaction():
        conn.execute("INSERT INTO projects (id, name) VALUES (%s, 'worker-stop-smoke')", (project,))
        conn.execute("INSERT INTO actors (id, kind, display) VALUES (%s, 'user', 'smoke')", (owner,))
        conn.execute("INSERT INTO project_members VALUES (%s, %s, 'owner')", (project, owner))
    client = TestClient(create_app(settings=settings, checks={}))
    headers = {"Authorization": f"Bearer {issue_actor_token(owner, get_signing_secret(settings))}"}
    res = client.post(f"/api/v1/documents?project_id={project}&logical_id=worker-stop-smoke",
                      headers={**headers, "Content-Type": "application/pdf"}, content=synthetic_pdf(PAGES))
    report = {"synthetic": True, "pages": PAGES, "upload_status": res.status_code}
    if res.status_code != 201:
        print(json.dumps(report))
        return 1
    res = client.post(f"/api/v1/documents/{res.json()['id']}/parse", headers=headers, json={})
    report["queue_status"] = res.status_code
    job = res.json()["job_id"]
    report["job"] = job
    deadline, status = time.monotonic() + 120, "queued"
    while time.monotonic() < deadline:
        status = client.get(f"/api/v1/jobs/{job}", headers=headers).json().get("status")
        if status != "queued":
            break
        time.sleep(0.25)
    report["status_when_returned"] = status
    print(json.dumps(report))
    return 0 if status == "running" else 1


if __name__ == "__main__":
    sys.exit(main())
