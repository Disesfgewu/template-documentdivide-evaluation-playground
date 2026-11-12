"""Runnable worker for ``document.generate`` jobs: ``python -m lab_api.generate_worker_main`` (P-D slice 3, Refs #7).

Same loop, backoff, lease, stop and shutdown-release behaviour as the parse worker (it IS that worker with another job
kind and another processor). It never starts without everything it needs; a missing piece is BLOCKED (exit 3), never a
pass or a silent no-op:

* ``LAB_GEN_BIN``                     path of the lab-gen binary (built from services/worker/agentflow/cmd/lab-gen)
* ``LAB_GEN_API_KEY``                 the provider key; passed only into the child's environment, never logged
* ``LAB_GEN_BASE_URL``                https endpoint (the child refuses anything but https and the allowed host)
* ``LAB_GEN_ALLOWED_HOST``            the only host the key may reach
* ``LAB_GEN_PRICE_IN_MICROS_PER_MTOK`` / ``LAB_GEN_PRICE_OUT_MICROS_PER_MTOK``   configured prices (never assumed)
* ``LAB_GEN_PROTOCOL``                agentKit protocol, default ``anthropic``

The model name, the 500-token cap and the budget cap come from the job row (``generate_jobs``)."""
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
from . import validate_questions
from .generate_questions import make_build_calls, make_on_results
from .generate_worker import GenModelConfig, process_generate_job
from .parse_worker import ParseOutcome
from .parse_worker_main import Worker

LOG = logging.getLogger("lab.generate_worker")
BLOCKED_EXIT = 3
_REQUIRED = ("LAB_GEN_BIN", "LAB_GEN_API_KEY", "LAB_GEN_BASE_URL", "LAB_GEN_ALLOWED_HOST",
             "LAB_GEN_PRICE_IN_MICROS_PER_MTOK", "LAB_GEN_PRICE_OUT_MICROS_PER_MTOK")


def load_gen_config(env) -> tuple[GenModelConfig | None, str | None, str]:
    """(config, api key, "") or (None, None, reason it is blocked). Never returns or logs the key in the reason."""
    missing = [k for k in _REQUIRED if not env.get(k)]
    if missing:
        return None, None, "blocked: missing " + ", ".join(missing)
    if not os.path.isfile(env["LAB_GEN_BIN"]) or not os.access(env["LAB_GEN_BIN"], os.X_OK):
        return None, None, "blocked: LAB_GEN_BIN is not an executable file"
    try:
        p_in, p_out = int(env["LAB_GEN_PRICE_IN_MICROS_PER_MTOK"]), int(env["LAB_GEN_PRICE_OUT_MICROS_PER_MTOK"])
    except ValueError:
        return None, None, "blocked: prices must be integers (micro-currency per million tokens)"
    if not 0 <= p_in <= 1_000_000_000 or not 0 <= p_out <= 1_000_000_000:
        return None, None, "blocked: prices are out of range"
    cfg = GenModelConfig(protocol=env.get("LAB_GEN_PROTOCOL", "anthropic"), base_url=env["LAB_GEN_BASE_URL"],
                         allowed_host=env["LAB_GEN_ALLOWED_HOST"], input_micros_per_mtok=p_in, output_micros_per_mtok=p_out)
    return cfg, env["LAB_GEN_API_KEY"], ""


class GenerateWorker(Worker):
    KIND = "document.generate"

    def __init__(self, connect, *, lab_gen_cmd, cfg: GenModelConfig, api_key: str, child_timeout: float = 600.0, extra_env: dict | None = None, **kw):
        super().__init__(connect, None, **kw)
        self.lab_gen_cmd, self.cfg, self.api_key, self.child_timeout = lab_gen_cmd, cfg, api_key, child_timeout
        self.extra_env = extra_env      # tests only (the fake child's mode); production passes nothing
        self.build_calls, self.on_results = make_build_calls(), make_on_results()
        self.validate_build_calls, self.validate_on_results = validate_questions.make_build_calls(), validate_questions.make_on_results()

    def _process(self, conn, project_id: uuid.UUID, job_id: uuid.UUID) -> ParseOutcome:
        phase = conn.execute("SELECT phase FROM generate_jobs WHERE project_id = %s AND job_id = %s",
                             (project_id, job_id)).fetchone()
        validating = phase is not None and phase[0] == "validate"
        return process_generate_job(
            conn, project_id, job_id, worker=self.name, lab_gen_cmd=self.lab_gen_cmd, cfg=self.cfg,
            build_calls=self.validate_build_calls if validating else self.build_calls, connect=self.connect,
            api_key=self.api_key, on_results=self.validate_on_results if validating else self.on_results,
            ttl_seconds=self.ttl, child_timeout=self.child_timeout, extra_env=self.extra_env, shutdown=self.abort.is_set)


def main(argv: list[str] | None = None, env=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lab_api.generate_worker_main", description=__doc__.split("\n")[0])
    parser.add_argument("--once", action="store_true", help="process what is claimable now, then exit")
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--grace", type=float, default=60.0)
    parser.add_argument("--backoff", type=float, default=5.0)
    parser.add_argument("--once-timeout", type=float, default=3600.0)
    parser.add_argument("--child-timeout", type=float, default=600.0, help="seconds one lab-gen batch may run")
    parser.add_argument("--heartbeat-file", default=None)
    args = parser.parse_args(argv)
    cfg, key, reason = load_gen_config(os.environ if env is None else env)
    if cfg is None:
        print(json.dumps({"status": "blocked", "reason": reason}), flush=True)
        return BLOCKED_EXIT
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
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
    worker = GenerateWorker(lambda: psycopg.connect(url, connect_timeout=5), lab_gen_cmd=[(env or os.environ)["LAB_GEN_BIN"]],
                            cfg=cfg, api_key=key, child_timeout=args.child_timeout, name=f"generate-worker-{uuid.uuid4().hex[:8]}",
                            concurrency=1, interval=args.interval, grace=args.grace, backoff=args.backoff, stop=stop,
                            once_timeout=args.once_timeout, heartbeat_file=args.heartbeat_file)
    holder["worker"] = worker
    LOG.info("started (kind %s)", worker.KIND)
    counts = worker.run_once() if args.once else worker.run_forever()
    print(json.dumps({"outcomes": counts}, sort_keys=True), flush=True)
    if worker.forced:
        logging.shutdown()
        os._exit(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
