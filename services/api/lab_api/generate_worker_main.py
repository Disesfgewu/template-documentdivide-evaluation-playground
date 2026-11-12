"""Runnable worker for ``document.generate`` jobs: ``python -m lab_api.generate_worker_main`` (P-D slice 3, Refs #7).

The same loop as ``lab_api.parse_worker_main`` (bounded polling and concurrency, fenced leases with heartbeat, in-memory
backoff, graceful stop, shutdown release, ``--once``, heartbeat file) for the job kind ``document.generate``; each job
runs ``process_generate_job`` with the general-bucket ``build_calls``/``on_results`` of ``lab_api.generate_general``.

Configuration (environment only; nothing is assumed):
* ``LAB_DATABASE_URL`` etc. as for the API (``load_settings``);
* ``LAB_GEN_CMD``: path of the lab-gen binary (required);
* ``LAB_GEN_PROTOCOL``, ``LAB_GEN_BASE_URL``, ``LAB_GEN_ALLOWED_HOST`` (required; lab-gen enforces https + allowlist);
* ``LAB_GEN_INPUT_MICROS_PER_MTOK``, ``LAB_GEN_OUTPUT_MICROS_PER_MTOK``: configured prices (required, never assumed);
* ``LAB_GEN_API_KEY``: read here and handed ONLY to the child's environment; never logged or stored.
Missing configuration exits with status 2 before any job is claimed. Logs carry fixed codes and ids only."""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import uuid

import psycopg

from .config import load_settings
from .generate_general import build_calls, on_results
from .generate_worker import KEY_ENV, GenModelConfig, process_generate_job
from .parse_worker import ParseOutcome
from .parse_worker_main import Worker

LOG = logging.getLogger("lab.generate_worker")
_REQUIRED = ("LAB_GEN_CMD", "LAB_GEN_PROTOCOL", "LAB_GEN_BASE_URL", "LAB_GEN_ALLOWED_HOST",
             "LAB_GEN_INPUT_MICROS_PER_MTOK", "LAB_GEN_OUTPUT_MICROS_PER_MTOK")


class GenerateWorker(Worker):
    KIND = "document.generate"

    def __init__(self, connect, *, lab_gen_cmd, cfg: GenModelConfig, api_key, child_timeout: float = 300.0,
                 extra_env=None, **kw):
        super().__init__(connect, None, **kw)
        self.lab_gen_cmd, self.cfg, self.api_key = list(lab_gen_cmd), cfg, api_key
        self.child_timeout, self.extra_env = child_timeout, extra_env

    def _process(self, conn: psycopg.Connection, project_id: uuid.UUID, job_id: uuid.UUID) -> ParseOutcome:
        return process_generate_job(conn, project_id, job_id, worker=self.name, lab_gen_cmd=self.lab_gen_cmd,
                                    cfg=self.cfg, build_calls=build_calls, connect=self.connect, api_key=self.api_key,
                                    on_results=on_results, ttl_seconds=self.ttl, child_timeout=self.child_timeout,
                                    extra_env=self.extra_env, shutdown=self.abort.is_set)


def config_from_env(env=os.environ) -> tuple[list[str], GenModelConfig]:
    missing = [k for k in _REQUIRED if not env.get(k)]
    if missing:
        raise ValueError("missing configuration: " + ", ".join(missing))
    try:
        prices = int(env["LAB_GEN_INPUT_MICROS_PER_MTOK"]), int(env["LAB_GEN_OUTPUT_MICROS_PER_MTOK"])
    except ValueError:
        raise ValueError("prices must be integers (micro-currency per million tokens)") from None
    if min(prices) < 0:
        raise ValueError("prices must not be negative")
    cfg = GenModelConfig(protocol=env["LAB_GEN_PROTOCOL"], base_url=env["LAB_GEN_BASE_URL"],
                         allowed_host=env["LAB_GEN_ALLOWED_HOST"], input_micros_per_mtok=prices[0],
                         output_micros_per_mtok=prices[1])
    return [env["LAB_GEN_CMD"]], cfg


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lab_api.generate_worker_main", description=__doc__.split("\n")[0])
    parser.add_argument("--once", action="store_true", help="process what is claimable now, then exit")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--grace", type=float, default=60.0)
    parser.add_argument("--backoff", type=float, default=5.0)
    parser.add_argument("--once-timeout", type=float, default=3600.0)
    parser.add_argument("--child-timeout", type=float, default=300.0)
    parser.add_argument("--heartbeat-file", default=os.environ.get("LAB_WORKER_HEARTBEAT_FILE"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        cmd, cfg = config_from_env()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    settings = load_settings()
    url = settings.database_url.get_secret_value()
    stop = threading.Event()
    holder: dict = {}

    def on_signal(*_):
        if stop.is_set() and "worker" in holder:
            holder["worker"].abort.set()
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, on_signal)
    try:
        worker = GenerateWorker(lambda: psycopg.connect(url, connect_timeout=5), lab_gen_cmd=cmd, cfg=cfg,
                                api_key=os.environ.get(KEY_ENV) or None, child_timeout=args.child_timeout,
                                name=f"generate-worker-{uuid.uuid4().hex[:8]}", concurrency=args.concurrency,
                                interval=args.interval, grace=args.grace, backoff=args.backoff, stop=stop,
                                once_timeout=args.once_timeout, heartbeat_file=args.heartbeat_file)
    except ValueError as exc:
        parser.error(str(exc))
    holder["worker"] = worker
    LOG.info("started (kind %s, concurrency %d)", GenerateWorker.KIND, args.concurrency)
    counts = worker.run_once() if args.once else worker.run_forever()
    print(json.dumps({"outcomes": counts}, sort_keys=True), flush=True)
    if worker.forced:
        logging.shutdown()
        os._exit(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
