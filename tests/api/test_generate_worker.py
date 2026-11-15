"""generate_worker without a database (P-D slice 2, Refs #7): strict output parsing, the bounded child, accounting math."""
import hashlib
import json
import os
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
for _sub in (_REPO, _REPO / "db", _REPO / "services" / "api", _REPO / "services" / "parser", _REPO / "contracts"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from lab_api.generate_worker import CallSpec, LabGenOutputInvalid, parse_lab_gen_output, run_lab_gen
from lab_db.generation import request_sha256, worst_case_micros

STUB = [sys.executable, str(_REPO / "tests" / "fixtures" / "fake_lab_gen.py")]
CALLS = [CallSpec("c0", "sys", "first question prompt"), CallSpec("c1", "sys", "second question prompt")]
MODEL = {"name": "fake-model", "protocol": "openai_chat", "base_url": "https://x.example-llm.com", "allowed_host": "x.example-llm.com",
         "max_output_tokens": 500, "input_micros_per_mtok": 3_000_000, "output_micros_per_mtok": 15_000_000, "currency": "USD",
         "pricing_revision": "t1", "timeout_seconds": 5, "max_retries": 1, "requests_per_minute": 60}


def payload(limit=100_000):
    return {"model": MODEL, "budget": {"limit_micros": limit},
            "calls": [{"id": c.id, "system": c.system, "user": c.user} for c in CALLS]}


def run(mode, **kw):
    out, why = run_lab_gen(STUB, payload(), key="k", timeout=kw.pop("timeout", 20), extra_env={"FAKE_LAB_GEN_MODE": mode}, **kw)
    return out, why


def parse(raw, limit=100_000):
    return parse_lab_gen_output(raw, CALLS, model="fake-model", max_output_tokens=500, budget_limit=limit)


class WorstCaseTests(unittest.TestCase):
    def test_worst_case_rounds_up_once_and_is_never_zero(self):
        self.assertEqual(worst_case_micros(10, 500, 3_000_000, 15_000_000), 7530)
        self.assertEqual(worst_case_micros(0, 1, 0, 0), 1)
        with self.assertRaises(ValueError):
            worst_case_micros(-1, 1, 1, 1)

    def test_request_hash_matches_the_go_definition(self):
        self.assertEqual(request_sha256("m", "s", "u"), hashlib.sha256(b"m\x00s\x00u").hexdigest())


class ParseOutputTests(unittest.TestCase):
    def test_a_good_batch_is_parsed_into_rows(self):
        raw, why = run("ok")
        self.assertEqual(why, "")
        res = parse(raw)
        self.assertEqual(res["status"], "ok")
        self.assertEqual([r["call_id"] for r in res["calls"]], ["c0", "c1"])
        self.assertTrue(all(r["charged_micros"] == 315 and r["held_micros"] == 0 and r["text"] for r in res["calls"]))
        self.assertEqual(res["calls"][0]["response_length"], len("reply 0"))

    def test_unknown_usage_is_held_never_zero(self):
        res = parse(run("unknown_usage")[0])
        for r in res["calls"]:
            self.assertEqual((r["usage_known"], r["charged_micros"]), (False, -1))
            self.assertGreater(r["held_micros"], 0)

    def test_a_partial_batch_keeps_the_failed_call_without_text(self):
        res = parse(run("partial")[0])
        self.assertEqual([r["status"] for r in res["calls"]], ["ok", "failed"])
        self.assertEqual((res["calls"][1]["text"], res["calls"][1]["held_micros"]), ("", 0))

    def test_refused_carries_no_calls(self):
        self.assertEqual(parse(run("refused")[0])["calls"], [])

    def test_contract_violations_are_rejected(self):
        for mode in ("wrong_hash", "overspend"):
            with self.assertRaises(LabGenOutputInvalid, msg=mode):
                parse(run(mode)[0])
        good = json.loads(run("ok")[0])
        mutate = {
            "unknown top-level key": lambda o: o.update(extra=1),
            "missing call": lambda o: o["calls"].pop(),
            "swapped ids": lambda o: o["calls"].reverse(),
            "unknown status": lambda o: o["calls"][0].update(status="weird"),
            "output tokens above the cap": lambda o: o["calls"][0].update(output_tokens=501),
            "negative tokens": lambda o: o["calls"][0].update(input_tokens=-1),
            "bool as number": lambda o: o["calls"][0].update(input_tokens=True),
            "text hash mismatch": lambda o: o["calls"][0].update(text="tampered"),
            "known usage without a charge": lambda o: o["calls"][0].update(charged_micros=-1),
            "unknown usage as zero": lambda o: o["calls"][0].update(usage_known=False, charged_micros=0, held_micros=0),
            "unknown usage not held": lambda o: o["calls"][0].update(usage_known=False, charged_micros=-1, held_micros=0),
        }
        for name, fn in mutate.items():
            o = json.loads(json.dumps(good))
            fn(o)
            with self.assertRaises(LabGenOutputInvalid, msg=name):
                parse(json.dumps(o).encode())
        for raw in (b"", b"not json", b"[]", b'{"status":"ok"}'):
            with self.assertRaises(LabGenOutputInvalid):
                parse(raw)


class RunChildTests(unittest.TestCase):
    def test_a_crash_a_timeout_and_a_cancel_return_a_reason_and_no_output(self):
        self.assertEqual(run("crash"), (None, "exit_3"))
        self.assertEqual(run("sleep", timeout=0.6), (None, "timeout"))
        self.assertEqual(run("sleep", cancelled=lambda: True), (None, "cancelled"))
        self.assertEqual(run_lab_gen(["/nonexistent/lab-gen"], payload(), key=None, timeout=1), (None, "spawn_failed"))

    def test_oversized_output_is_cut_off_and_never_buffered_whole(self):
        script = "import sys\nsys.stdout.write('x' * (5 * 1024 * 1024))"
        out, why = run_lab_gen([sys.executable, "-c", script], payload(), key=None, timeout=20, max_stdout=64 * 1024)
        self.assertEqual((out, why), (None, "output_too_large"))

    def test_a_timeout_kills_the_whole_process_group_not_only_the_child(self):
        import tempfile, time
        marker = Path(tempfile.mkdtemp()) / "grandchild-survived"
        script = ("import subprocess, sys, time\n"
                  f"subprocess.Popen([sys.executable, '-c', \"import time, pathlib; time.sleep(2); pathlib.Path(r'{marker}').write_text('x')\"])\n"
                  "time.sleep(60)")
        out, why = run_lab_gen([sys.executable, "-c", script], payload(), key=None, timeout=0.6)
        self.assertEqual((out, why), (None, "timeout"))
        time.sleep(3)
        self.assertFalse(marker.exists(), "a grandchild outlived the killed child")

    def test_the_child_gets_only_a_minimal_environment(self):
        os.environ["SOME_OTHER_SECRET"] = "leak-me"
        try:
            script = "import os,sys;print(sorted(k for k in os.environ if k in ('SOME_OTHER_SECRET','LAB_GEN_API_KEY')))"
            out, why = run_lab_gen([sys.executable, "-c", script], payload(), key="the-key", timeout=10)
        finally:
            del os.environ["SOME_OTHER_SECRET"]
        self.assertEqual(why, "")
        self.assertEqual(out.decode().strip(), "['LAB_GEN_API_KEY']")


if __name__ == "__main__":
    unittest.main()
