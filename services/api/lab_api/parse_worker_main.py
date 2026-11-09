"""Runnable worker for ``document.parse`` jobs: ``python -m lab_api.parse_worker_main`` (Refs #7).

Reads the same settings as the API (``LAB_DATABASE_URL``, ``LAB_ARTIFACT_ROOT``, ...). With
``--once`` it processes every job that is claimable right now and exits; without it it polls
until SIGTERM or SIGINT.

* Polling interval is bounded (``--interval`` 1..60 s); an idle pass sleeps one interval.
* Concurrency is bounded (``--concurrency`` 1..4 jobs at once, each with its own connection).
* Stop is graceful: no new job is claimed; running jobs get ``--grace`` seconds (default 60) and
  anything still running then is left to lease expiry (the job is retaken, burning one attempt).
* A transient failure (``queued`` outcome) is not retried before an in-memory exponential
  backoff (``--backoff`` base seconds, doubling per attempt, capped at 5 min). The delay is lost
  on restart; there is no not-before column.
* Unreachable database: the loop logs a fixed code and retries after a bounded sleep, it never
  crashes on a dead connection.

Logs carry fixed codes and ids only, never document or parser text.
"""
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import json
import logging
import signal
import sys
import threading
import time
import uuid
from typing import Callable

import psycopg

from .config import load_settings
from .parse_results import make_persist
from .parse_worker import SUPPORTED_PROFILE, ParseOutcome, is_infrastructure, process_parse_job
from lab_db import jobs  # noqa: E402
from lab_db.storage import LocalArtifactStore  # noqa: E402

LOG = logging.getLogger("lab.parse_worker")
MAX_BACKOFF_SECONDS = 300.0
MAX_PDF_BYTES = 512 * 1024 * 1024


def claimable(conn: psycopg.Connection, limit: int, skip: set[uuid.UUID]) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """(project_id, job_id) of queued parse jobs and of running ones whose lease expired, oldest first."""
    with conn.transaction():
        rows = conn.execute(
            """
            SELECT project_id, id FROM jobs
            WHERE kind = 'document.parse'
              AND (status = 'queued' OR (status = 'running' AND lease_expires_at < clock_timestamp()))
            ORDER BY id
            LIMIT %s
            """,
            (limit + len(skip),),
        ).fetchall()
    return [(p, j) for p, j in rows if j not in skip][:limit]


class Backoff:
    """In-memory not-before per job; doubles with each consecutive transient outcome."""

    def __init__(self, base: float, clock: Callable[[], float] = time.monotonic):
        self.base, self.clock, self._until, self._strikes = base, clock, {}, {}

    def hit(self, job_id: uuid.UUID) -> float:
        n = self._strikes[job_id] = self._strikes.get(job_id, 0) + 1
        delay = min(self.base * 2 ** (n - 1), MAX_BACKOFF_SECONDS)
        self._until[job_id] = self.clock() + delay
        return delay

    def clear(self, job_id: uuid.UUID) -> None:
        self._until.pop(job_id, None)
        self._strikes.pop(job_id, None)

    def waiting(self) -> set[uuid.UUID]:
        now = self.clock()
        for job_id in [j for j, t in self._until.items() if t <= now]:
            self._until.pop(job_id)  # strikes are kept so the next hit doubles
        return set(self._until)


class Worker:
    def __init__(self, connect: Callable[[], psycopg.Connection], store: LocalArtifactStore, *,
                 name: str, concurrency: int = 1, interval: float = 5.0, grace: float = 60.0,
                 backoff: float = 5.0, ttl_seconds: int = 60, python: str | None = None,
                 run=None, stop: threading.Event | None = None):
        if not 1 <= concurrency <= 4 or not 1 <= interval <= 60 or not 0 < backoff <= 60 or grace < 0:
            raise ValueError("concurrency 1..4, interval 1..60 s, backoff 0..60 s, grace >= 0")
        self.connect, self.store, self.name = connect, store, name
        self.concurrency, self.interval, self.grace = concurrency, interval, grace
        self.ttl, self.python, self.run = ttl_seconds, python, run
        self.stop = stop or threading.Event()
        self.backoff = Backoff(backoff)
        self.counts: dict[str, int] = {}
        self._lock = threading.Lock()
        self._inflight: set[uuid.UUID] = set()

    def _handle(self, project_id: uuid.UUID, job_id: uuid.UUID) -> ParseOutcome | None:
        try:
            with self.connect() as conn:
                kwargs = {} if self.run is None else {"run": self.run}
                outcome = process_parse_job(
                    conn, project_id, job_id, store=self.store, persist=make_persist(self.store),
                    worker=self.name, max_pdf_bytes=MAX_PDF_BYTES, connect=self.connect,
                    ttl_seconds=self.ttl, python=self.python, **kwargs)
        except jobs.LeaseUnavailable:
            return None  # someone else took it first
        except Exception as exc:
            code = "infrastructure_error" if is_infrastructure(exc) else "worker_error"
            LOG.warning("job %s: %s", job_id, code)
            outcome = ParseOutcome(job_id, "abandoned", code)
        finally:
            with self._lock:
                self._inflight.discard(job_id)
        key = f"{outcome.status}:{outcome.reason}"
        with self._lock:
            self.counts[key] = self.counts.get(key, 0) + 1
        if outcome.status == "queued":
            LOG.info("job %s requeued (%s), backing off %.0fs", job_id, outcome.reason, self.backoff.hit(job_id))
        else:
            self.backoff.clear(job_id)
            LOG.info("job %s %s (%s)", job_id, outcome.status, outcome.reason)
        return outcome

    def _pick(self, conn, free: int) -> list[tuple[uuid.UUID, uuid.UUID]]:
        with self._lock:
            skip = self.backoff.waiting() | self._inflight
        return claimable(conn, free, skip)

    def pass_once(self, pool: ThreadPoolExecutor, futures: set[Future]) -> int:
        """Submit up to the free capacity; returns how many jobs were submitted."""
        free = self.concurrency - len(futures)
        if free <= 0:
            return 0
        try:
            with self.connect() as conn:
                picked = self._pick(conn, free)
        except Exception as exc:
            LOG.warning("polling failed: %s", "infrastructure_error" if is_infrastructure(exc) else "worker_error")
            return 0
        for project_id, job_id in picked:
            with self._lock:
                self._inflight.add(job_id)
            futures.add(pool.submit(self._handle, project_id, job_id))
        return len(picked)

    def run_once(self) -> dict[str, int]:
        """Process what is claimable now (including jobs that become claimable by retry
        after the backoff) until a pass finds nothing to do and nothing is running."""
        with ThreadPoolExecutor(self.concurrency, thread_name_prefix="parse") as pool:
            futures: set[Future] = set()
            while not self.stop.is_set():
                submitted = self.pass_once(pool, futures)
                if futures:
                    done, futures = wait(futures, timeout=self.interval, return_when=FIRST_COMPLETED)
                elif not submitted:
                    break
            self._drain(futures)
        return dict(self.counts)

    def run_forever(self) -> dict[str, int]:
        with ThreadPoolExecutor(self.concurrency, thread_name_prefix="parse") as pool:
            futures: set[Future] = set()
            while not self.stop.is_set():
                submitted = self.pass_once(pool, futures)
                if futures:
                    done, futures = wait(futures, timeout=self.interval, return_when=FIRST_COMPLETED)
                elif not submitted:
                    self.stop.wait(self.interval)
            self._drain(futures)
        return dict(self.counts)

    def _drain(self, futures: set[Future]) -> None:
        if futures:
            _, pending = wait(futures, timeout=self.grace)
            if pending:
                LOG.warning("stopping with %d job(s) still running; their leases will expire", len(pending))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lab_api.parse_worker_main", description=__doc__.split("\n")[0])
    parser.add_argument("--once", action="store_true", help="process what is claimable now, then exit")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--grace", type=float, default=60.0)
    parser.add_argument("--backoff", type=float, default=5.0)
    parser.add_argument("--python", default=None, help="interpreter that has the pinned parser library")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_settings()
    url = settings.database_url.get_secret_value()
    store = LocalArtifactStore(settings.artifact_root)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        worker = Worker(lambda: psycopg.connect(url, connect_timeout=5), store,
                        name=f"parse-worker-{uuid.uuid4().hex[:8]}", concurrency=args.concurrency,
                        interval=args.interval, grace=args.grace, backoff=args.backoff,
                        python=args.python, stop=stop)
    except ValueError as exc:
        parser.error(str(exc))
    LOG.info("started (profile %s, concurrency %d)", SUPPORTED_PROFILE, args.concurrency)
    counts = worker.run_once() if args.once else worker.run_forever()
    print(json.dumps({"outcomes": counts}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
