"""Runs INSIDE the worker container (``docker exec -i <container> python - < deploy/worker_smoke.py``), Refs #7.

Proves the deployed image end to end with a SYNTHETIC one-page PDF: create a project and an owner in the
database the worker polls, upload through the real API app, queue a parse, wait for the running worker
(the container's own entrypoint, real pinned Unstructured fast) to finish the job, read the stored result
back through the API. Prints one JSON report line; exit status 0 only if the page text came back.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid

import psycopg
from fastapi.testclient import TestClient

from lab_api.auth import get_signing_secret, issue_actor_token
from lab_api.config import load_settings
from lab_api.main import create_app

LINES = ["Synthetic worker image smoke page", "The quick brown fox jumps over the lazy dog."]


def synthetic_pdf() -> bytes:
    ops = ["BT", "/F1 14 Tf", "72 720 Td", "18 TL"] + [f"({line}) Tj T*" for line in LINES] + ["ET"]
    stream = "\n".join(ops).encode("ascii")
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def main() -> int:
    settings = load_settings()
    report = {"synthetic": True, "uid": os.getuid()}
    project, owner = uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(settings.database_url.get_secret_value()) as conn, conn.transaction():
        conn.execute("INSERT INTO projects (id, name) VALUES (%s, 'worker-smoke')", (project,))
        conn.execute("INSERT INTO actors (id, kind, display) VALUES (%s, 'user', 'smoke')", (owner,))
        conn.execute("INSERT INTO project_members VALUES (%s, %s, 'owner')", (project, owner))
    client = TestClient(create_app(settings=settings, checks={}))
    headers = {"Authorization": f"Bearer {issue_actor_token(owner, get_signing_secret(settings))}"}
    began = time.monotonic()
    res = client.post(f"/api/v1/documents?project_id={project}&logical_id=worker-smoke",
                      headers={**headers, "Content-Type": "application/pdf"}, content=synthetic_pdf())
    report["upload_status"] = res.status_code
    if res.status_code != 201:
        print(json.dumps(report))
        return 1
    document = res.json()["id"]
    res = client.post(f"/api/v1/documents/{document}/parse", headers=headers, json={})
    report["queue_status"] = res.status_code
    job = res.json()["job_id"]
    status = "queued"
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        status = client.get(f"/api/v1/jobs/{job}", headers=headers).json().get("status")
        if status in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(2)
    report.update(job_status=status, wall_seconds=round(time.monotonic() - began, 1))
    if status != "succeeded":
        print(json.dumps(report))
        return 1
    page = client.get(f"/api/v1/documents/{document}/parse-results/{job}/pages/0", headers=headers).json()
    text = "".join("".join(o["raw_content"].split()) for o in page.get("observations", []))
    report.update(page_status=page.get("status"), evidence_units=len(page.get("evidence", [])),
                  text_found="Syntheticworkerimagesmokepage" in text)
    print(json.dumps(report))
    return 0 if report["text_found"] and report["evidence_units"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
