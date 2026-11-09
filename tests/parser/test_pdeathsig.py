"""A parser child must not outlive a parent that dies without cleaning up (Linux PDEATHSIG)."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

LIB = str(Path(__file__).resolve().parents[2] / "services" / "parser")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie still answers kill(0); read its state
        return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False


@unittest.skipUnless(sys.platform == "linux", "PDEATHSIG is Linux only")
class DieWithParentTests(unittest.TestCase):
    def check(self, module: str):
        child = (f"import sys; sys.path.insert(0, {LIB!r})\n"
                 f"from lab_parser.{module} import die_with_parent\n"
                 "die_with_parent()\n"
                 "import time; time.sleep(60)\n")
        parent = ("import os, subprocess, sys\n"
                  f"c = subprocess.Popen([sys.executable, '-c', {child!r}], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                  "print(c.pid, flush=True)\n"
                  "import time; time.sleep(1.5)\n"      # let the child install the signal
                  "os._exit(0)\n")                       # the parent dies without any cleanup
        out = subprocess.run([sys.executable, "-c", parent], capture_output=True, text=True, timeout=60)
        pid = int(out.stdout.strip())
        deadline = time.monotonic() + 10
        while alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        try:
            self.assertFalse(alive(pid), f"{module} child outlived its parent")
        finally:
            if alive(pid):
                os.kill(pid, 9)

    def test_unstructured_child_dies_with_its_parent(self):
        self.check("_unstructured_child")

    def test_isolation_child_dies_with_its_parent(self):
        self.check("_isolation_child")

    def test_without_the_helper_the_child_would_survive(self):
        child = "import time; time.sleep(60)\n"
        parent = ("import os, subprocess, sys\n"
                  f"c = subprocess.Popen([sys.executable, '-c', {child!r}], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                  "print(c.pid, flush=True)\nimport time; time.sleep(0.5)\nos._exit(0)\n")
        out = subprocess.run([sys.executable, "-c", parent], capture_output=True, text=True, timeout=60)
        pid = int(out.stdout.strip())
        try:
            time.sleep(1.0)
            self.assertTrue(alive(pid), "control: an unprotected orphan keeps running")
        finally:
            os.kill(pid, 9)


if __name__ == "__main__":
    unittest.main()
