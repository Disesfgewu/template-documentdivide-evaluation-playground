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


def _partition_directly() -> dict:
    """Diagnostic only (runs when the stored page had no text): run the REAL child bootstrap by hand, with the
    same environment the worker gives it, and keep its stderr (the worker discards it on purpose)."""
    import subprocess
    import tempfile
    from pathlib import Path
    out = {}
    try:
        import lab_parser
        child = Path(lab_parser.__file__).with_name("_unstructured_child.py")
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "source.pdf").write_bytes(synthetic_pdf())
            run = subprocess.run([sys.executable, "-I", str(child), str(4 * 1024 ** 3), "60", str(16 * 1024 ** 2)],
                                 cwd=directory, env={"LANG": "C.UTF-8", "HOME": directory, "TMPDIR": directory},
                                 capture_output=True, timeout=180)
            out["child_exit"] = run.returncode
            out["child_stderr_tail"] = run.stderr.decode("utf-8", "replace")[-1500:]
            elements = Path(directory, "elements.json")
            out["elements_json"] = elements.read_text()[:600] if elements.exists() else None
            probe = (
                "import logging, sys\n"
                "logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)\n"
                "from pdfminer.high_level import extract_text\n"
                "print('pdfminer text:', repr(extract_text('source.pdf')[:200]))\n"
                "from unstructured.partition.pdf import partition_pdf\n"
                "els = partition_pdf(filename='source.pdf', strategy='fast')\n"
                "print('elements:', [(type(e).__name__, e.text) for e in els])\n"
                "import importlib.util\n"
                "print('libmagic:', importlib.util.find_spec('magic') is not None)\n"
            )
            more = subprocess.run([sys.executable, "-I", "-c", probe], cwd=directory,
                                  env={"LANG": "C.UTF-8", "HOME": directory, "TMPDIR": directory},
                                  capture_output=True, timeout=180)
            out["probe_exit"] = more.returncode
            out["probe_stdout"] = more.stdout.decode("utf-8", "replace")[-1200:]
            out["probe_stderr_tail"] = more.stderr.decode("utf-8", "replace")[-2500:]
    except BaseException as exc:  # noqa: BLE001 - a diagnostic must report, not raise
        out["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    return out


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
    report.update(page_status=page.get("status"), page_diagnostics=page.get("diagnostics"),
                  observations=len(page.get("observations", [])), evidence_units=len(page.get("evidence", [])),
                  text_found="Syntheticworkerimagesmokepage" in text)
    if not report["text_found"]:
        report["in_process_partition"] = _partition_directly()
    print(json.dumps(report))
    return 0 if report["text_found"] and report["evidence_units"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
