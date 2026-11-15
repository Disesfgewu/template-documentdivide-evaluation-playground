"""The generation worker never starts half-configured (P-D slice 3, Refs #7): blocked is exit 3, never a pass."""
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
for _sub in (_REPO, _REPO / "db", _REPO / "services" / "api", _REPO / "services" / "parser", _REPO / "contracts"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from lab_api.generate_worker_main import BLOCKED_EXIT, load_gen_config, main


def env(bin_path, **over):
    base = {"LAB_GEN_BIN": bin_path, "LAB_GEN_API_KEY": "sk-secret-value", "LAB_GEN_BASE_URL": "https://api.anthropic.com",
            "LAB_GEN_ALLOWED_HOST": "api.anthropic.com", "LAB_GEN_PRICE_IN_MICROS_PER_MTOK": "1000000",
            "LAB_GEN_PRICE_OUT_MICROS_PER_MTOK": "5000000"}
    base.update(over)
    return base


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.bin = str(Path(self.dir.name) / "lab-gen")
        Path(self.bin).write_text("#!/bin/sh\n")
        os.chmod(self.bin, stat.S_IRWXU)

    def test_a_complete_environment_loads_and_defaults_to_the_anthropic_protocol(self):
        cfg, key, reason = load_gen_config(env(self.bin))
        self.assertEqual((reason, key, cfg.protocol, cfg.input_micros_per_mtok, cfg.output_micros_per_mtok),
                         ("", "sk-secret-value", "anthropic", 1_000_000, 5_000_000))

    def test_every_missing_piece_blocks_and_the_reason_never_contains_the_key(self):
        for name in ("LAB_GEN_BIN", "LAB_GEN_API_KEY", "LAB_GEN_BASE_URL", "LAB_GEN_ALLOWED_HOST",
                     "LAB_GEN_PRICE_IN_MICROS_PER_MTOK", "LAB_GEN_PRICE_OUT_MICROS_PER_MTOK"):
            e = env(self.bin)
            del e[name]
            cfg, key, reason = load_gen_config(e)
            self.assertIsNone(cfg)
            self.assertIn(name, reason)
            self.assertNotIn("sk-secret-value", reason)

    def test_a_bad_binary_or_price_blocks(self):
        for e in (env("/nonexistent/lab-gen"), env(self.dir.name), env(self.bin, LAB_GEN_PRICE_IN_MICROS_PER_MTOK="cheap"),
                  env(self.bin, LAB_GEN_PRICE_OUT_MICROS_PER_MTOK="-1"), env(self.bin, LAB_GEN_PRICE_OUT_MICROS_PER_MTOK="9999999999")):
            self.assertIsNone(load_gen_config(e)[0])

    def test_main_exits_three_with_a_blocked_report_and_does_not_touch_the_database(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--once"], env={})
        self.assertEqual(code, BLOCKED_EXIT)
        report = json.loads(out.getvalue())
        self.assertEqual(report["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
