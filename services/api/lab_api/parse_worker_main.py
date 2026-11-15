"""Runnable worker for ``document.parse`` jobs: ``python -m lab_api.parse_worker_main`` (Refs #7).

Reads the same settings as the API (``LAB_DATABASE_URL``, ``LAB_ARTIFACT_ROOT``, ...). With
``--once`` it processes every job that is claimable right now and exits; without it it polls
until SIGTERM or SIGINT.

* Polling interval is bounded (``--interval`` 1..60 s); an idle pass sleeps one interval.
* Concurrency is bounded (``--concurrency`` 1..4 jobs at once, each with its own connection).
* Stop is graceful: no new job is claimed; running jobs get ``--grace`` seconds (default 60). After
  that the remaining parsers are cancelled (their children are killed and reaped) and each such job
  is RELEASED at once (running -> queued, ``released: worker shutdown``, only by the current lease
  holder) so the next worker takes it immediately; it still burns one attempt. If the release fails
  the job is left to lease expiry as before. Then the process exits. A second SIGTERM/SIGINT skips
  the grace period.
* Every non-terminal outcome (a transient ``queued`` failure, a job that could not be claimed,
  ``abandoned`` for any reason) backs that job off in memory: exponential from ``--backoff``
  seconds, doubling per consecutive hit, capped at 5 min, with +-50% jitter so workers do not
  retry in lockstep. The delay is lost on restart; there is no not-before column.
* Passes are at least ``MIN_PASS_GAP`` seconds apart and ``--once`` stops at ``--once-timeout``
  seconds, so a failing claim can never spin.
* Unreachable database: the loop logs a fixed code and retries after a bounded sleep, it never
  crashes on a dead connection.

* Liveness for a container healthcheck: with ``--heartbeat-file PATH`` (or ``LAB_WORKER_HEARTBEAT_FILE``)
  the loop rewrites that file with the current time at least once per pass, i.e. at least every
  ``--interval`` seconds, even while a long parse runs. ``--check-heartbeat PATH --max-age SECONDS``
  exits 0 when the file is fresh and 1 otherwise (missing, unreadable, malformed or stale); it opens
  no database connection and starts no worker.

Logs carry fixed codes and ids only, never document or parser text.
"""
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import json
import logging
import os
import random
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
MIN_PASS_GAP = 0.25
CANCEL_WAIT_SECONDS = 10.0
MAX_PDF_BYTES = 512 * 1024 * 1024


def claimable(conn: psycopg.Connection, limit: int, skip: set[uuid.UUID],
              kind: str = "document.parse") -> list[tuple[uuid.UUID, uuid.UUID]]:
    """(project_id, job_id) of queued jobs of ``kind`` and of running ones whose lease expired, oldest first."""
    with conn.transaction():
        rows = conn.execute(
            """
            SELECT project_id, id FROM jobs
            WHERE kind = %s
              AND (status = 'queued' OR (status = 'running' AND lease_expires_at < clock_timestamp()))
            ORDER BY id
            LIMIT %s
            """,
            (kind, limit + len(skip)),
        ).fetchall()
    return [(p, j) for p, j in rows if j not in skip][:limit]


class Backoff:
    """In-memory not-before per job; doubles with each consecutive hit, with jitter."""

    def __init__(self, base: float, clock: Callable[[], float] = time.monotonic,
                 jitter: Callable[[], float] | None = None):
        self.base, self.clock = base, clock
        self.jitter = jitter or (lambda: random.uniform(0.5, 1.5))
        self._until, self._strikes, self._seen = {}, {}, {}

    def hit(self, job_id: uuid.UUID) -> float:
        n = self._strikes[job_id] = self._strikes.get(job_id, 0) + 1
        delay = min(self.base * 2 ** (n - 1), MAX_BACKOFF_SECONDS) * self.jitter()
        self._until[job_id] = self.clock() + delay
        self._seen[job_id] = self.clock()
        return delay

    def clear(self, job_id: uuid.UUID) -> None:
        for table in (self._until, self._strikes, self._seen):
            table.pop(job_id, None)

    def waiting(self) -> set[uuid.UUID]:
        now = self.clock()
        for job_id in [j for j, t in self._until.items() if t <= now]:
            self._until.pop(job_id)  # strikes are kept so the next hit doubles
        for job_id in [j for j, t in self._seen.items() if now - t > 4 * MAX_BACKOFF_SECONDS]:
            self.clear(job_id)       # jobs that vanished or finished elsewhere do not accumulate
        return set(self._until)


class Worker:
    KIND = "document.parse"      # subclasses (generation) change the job kind and ``_process``

    def __init__(self, connect: Callable[[], psycopg.Connection], store: LocalArtifactStore, *,
                 name: str, concurrency: int = 1, interval: float = 5.0, grace: float = 60.0,
                 backoff: float = 5.0, ttl_seconds: int = 60, python: str | None = None,
                 run=None, stop: threading.Event | None = None, once_timeout: float = 3600.0,
                 jitter: Callable[[], float] | None = None, heartbeat_file: str | None = None):
        if not 1 <= concurrency <= 4 or not 1 <= interval <= 60 or not 0 < backoff <= 60 or grace < 0:
            raise ValueError("concurrency 1..4, interval 1..60 s, backoff 0..60 s, grace >= 0")
        self.connect, self.store, self.name = connect, store, name
        self.concurrency, self.interval, self.grace = concurrency, interval, grace
        self.ttl, self.python, self.run = ttl_seconds, python, run
        self.stop = stop or threading.Event()
        self.abort = threading.Event()   # set to kill running parsers (grace expired or second signal)
        self.once_timeout = once_timeout
        self.heartbeat_file = heartbeat_file
        self._last_pass = 0.0
        self.backoff = Backoff(backoff, jitter=jitter)
        self.counts: dict[str, int] = {}
        self._lock = threading.Lock()
        self._inflight: set[uuid.UUID] = set()

    def beat(self) -> None:
        """Record that the loop is alive (a container healthcheck reads this). Never raises."""
        if not self.heartbeat_file:
            return
        try:
            tmp = f"{self.heartbeat_file}.tmp"
            with open(tmp, "w", encoding="ascii") as stream:
                stream.write(f"{time.time():.3f}\n")
            os.replace(tmp, self.heartbeat_file)
        except OSError:
            LOG.warning("heartbeat file not writable")

    def _run_with_abort(self):
        base = self.run
        if base is None:
            from lab_parser.batch_parse import parse_in_batches as base

        def run(data, cancelled=None, **kw):
            inner = cancelled or (lambda: False)
            return base(data, cancelled=lambda: inner() or self.abort.is_set(), **kw)
        return run

    def _process(self, conn: psycopg.Connection, project_id: uuid.UUID, job_id: uuid.UUID) -> ParseOutcome:
        return process_parse_job(
            conn, project_id, job_id, store=self.store, persist=make_persist(self.store),
            worker=self.name, max_pdf_bytes=MAX_PDF_BYTES, connect=self.connect,
            ttl_seconds=self.ttl, python=self.python, run=self._run_with_abort(),
            shutdown=self.abort.is_set)

    def _handle(self, project_id: uuid.UUID, job_id: uuid.UUID) -> ParseOutcome | None:
        outcome = None
        try:
            with self.connect() as conn:
                outcome = self._process(conn, project_id, job_id)
        except jobs.LeaseUnavailable:
            LOG.info("job %s not claimable", job_id)  # someone else took it first
        except Exception as exc:
            code = "infrastructure_error" if is_infrastructure(exc) else "worker_error"
            LOG.warning("job %s: %s", job_id, code)
            outcome = ParseOutcome(job_id, "abandoned", code)
        finally:
            with self._lock:
                self._inflight.discard(job_id)
        if outcome is not None and outcome.status in ("succeeded", "failed", "cancelled"):
            with self._lock:
                self.backoff.clear(job_id)
            key = f"{outcome.status}:{outcome.reason}"
            LOG.info("job %s %s (%s)", job_id, outcome.status, outcome.reason)
        else:  # not claimable, queued again, or abandoned: never retried before its backoff
            reason = "not_claimable" if outcome is None else outcome.reason
            key = f"{'queued' if outcome is None else outcome.status}:{reason}"
            with self._lock:
                delay = self.backoff.hit(job_id)
            LOG.info("job %s %s, backing off %.1fs", job_id, reason, delay)
        with self._lock:
            self.counts[key] = self.counts.get(key, 0) + 1
        return outcome

    def _pick(self, conn, free: int) -> list[tuple[uuid.UUID, uuid.UUID]]:
        with self._lock:
            skip = self.backoff.waiting() | self._inflight
        return claimable(conn, free, skip, self.KIND)

    def pass_once(self, pool: ThreadPoolExecutor, futures: set[Future]) -> int:
        """Submit up to the free capacity; returns how many jobs were submitted."""
        self.beat()
        free = self.concurrency - len(futures)
        if free <= 0:
            return 0
        gap = MIN_PASS_GAP - (time.monotonic() - self._last_pass)
        if gap > 0:
            self.stop.wait(gap)
        self._last_pass = time.monotonic()
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
        """Process what is claimable right now, then return. A job that backs off is not waited
        for (run ``--once`` again later); the call also ends at ``once_timeout`` seconds."""
        deadline = time.monotonic() + self.once_timeout
        pool = ThreadPoolExecutor(self.concurrency, thread_name_prefix="parse")
        futures: set[Future] = set()
        try:
            while not self.stop.is_set() and time.monotonic() < deadline:
                submitted = self.pass_once(pool, futures)
                if futures:
                    _, futures = wait(futures, timeout=self.interval, return_when=FIRST_COMPLETED)
                elif not submitted:
                    break
            self._drain(pool, futures)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return dict(self.counts)

    def run_forever(self) -> dict[str, int]:
        pool = ThreadPoolExecutor(self.concurrency, thread_name_prefix="parse")
        futures: set[Future] = set()
        try:
            while not self.stop.is_set():
                submitted = self.pass_once(pool, futures)
                if futures:
                    _, futures = wait(futures, timeout=self.interval, return_when=FIRST_COMPLETED)
                elif not submitted:
                    self.stop.wait(self.interval)
            self._drain(pool, futures)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return dict(self.counts)

    def _drain(self, pool: ThreadPoolExecutor, futures: set[Future]) -> None:
        """Wait for running jobs: the grace period unless a second signal already set ``abort``;
        then cancel their parsers and give the threads a short time to return."""
        if not futures:
            return
        pending = set(futures)
        deadline = time.monotonic() + self.grace
        # Poll so that a second signal (abort) ends the grace wait at once.
        while pending and not self.abort.is_set() and time.monotonic() < deadline:
            _, pending = wait(pending, timeout=min(0.2, max(0.0, deadline - time.monotonic())))
        if pending:
            LOG.warning("stopping with %d job(s) still running: cancelling their parsers; each is released for the next worker (or, if that fails, its lease expires)",
                        len(pending))
            self.abort.set()
            _, pending = wait(pending, timeout=CANCEL_WAIT_SECONDS)
            if pending:
                self.forced = True
                LOG.error("%d job thread(s) did not return; exiting anyway", len(pending))

    forced = False


def check_heartbeat(path: str, max_age: float, now: Callable[[], float] = time.time) -> bool:
    """True when ``path`` holds a timestamp no older than ``max_age`` seconds (and not from the future)."""
    try:
        with open(path, "r", encoding="ascii") as stream:
            stamp = float(stream.read(64).strip())
    except (OSError, ValueError, UnicodeDecodeError):
        return False
    age = now() - stamp
    return -5.0 <= age <= max_age


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lab_api.parse_worker_main", description=__doc__.split("\n")[0])
    parser.add_argument("--once", action="store_true", help="process what is claimable now, then exit")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--grace", type=float, default=60.0)
    parser.add_argument("--backoff", type=float, default=5.0)
    parser.add_argument("--once-timeout", type=float, default=3600.0, help="--once gives up after this many seconds")
    parser.add_argument("--heartbeat-file", default=os.environ.get("LAB_WORKER_HEARTBEAT_FILE"),
                        help="rewritten with the current time every pass (for a container healthcheck)")
    parser.add_argument("--check-heartbeat", metavar="PATH", default=None,
                        help="probe: exit 0 if PATH was refreshed within --max-age seconds, else 1; starts no worker")
    parser.add_argument("--max-age", type=float, default=120.0)
    parser.add_argument("--python", default=None, help="interpreter that has the pinned parser library")
    args = parser.parse_args(argv)
    if args.check_heartbeat is not None:
        if not 1 <= args.max_age <= 3600:
            parser.error("--max-age must be between 1 and 3600 seconds")
        return 0 if check_heartbeat(args.check_heartbeat, args.max_age) else 1
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_settings()
    url = settings.database_url.get_secret_value()
    store = LocalArtifactStore(settings.artifact_root)
    stop = threading.Event()
    holder: dict = {}

    def on_signal(*_):
        if stop.is_set() and "worker" in holder:
            holder["worker"].abort.set()  # a second signal skips the grace period
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, on_signal)
    try:
        worker = Worker(lambda: psycopg.connect(url, connect_timeout=5), store,
                        name=f"parse-worker-{uuid.uuid4().hex[:8]}", concurrency=args.concurrency,
                        interval=args.interval, grace=args.grace, backoff=args.backoff,
                        python=args.python, stop=stop, once_timeout=args.once_timeout,
                        heartbeat_file=args.heartbeat_file)
    except ValueError as exc:
        parser.error(str(exc))
    holder["worker"] = worker
    LOG.info("started (profile %s, concurrency %d)", SUPPORTED_PROFILE, args.concurrency)
    counts = worker.run_once() if args.once else worker.run_forever()
    print(json.dumps({"outcomes": counts}, sort_keys=True), flush=True)
    if worker.forced:
        logging.shutdown()
        os._exit(1)  # a job thread is stuck; the interpreter would otherwise wait for it forever
    return 0


if __name__ == "__main__":
    sys.exit(main())
