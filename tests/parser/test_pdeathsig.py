"""A parser child must not outlive a parent that dies without cleaning up (Linux PDEATHSIG)."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
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

    def test_a_parent_that_died_before_the_childs_first_getppid_is_still_detected(self):
        """The window the review found: the parent is gone before the child asks who its parent is."""
        child = (f"import os, sys; sys.path.insert(0, {LIB!r})\n"
                 "import time; time.sleep(1.0)\n"                       # the parent dies during this pause
                 "from lab_parser._pdeathsig import die_with_parent\n"
                 "die_with_parent()\n"                                  # LAB_PARENT_PID no longer matches getppid
                 "open(sys.argv[1], 'w').write('still running')\n")     # must never be reached
        marker = Path(tempfile.mkdtemp(), "reached")
        parent = ("import os, subprocess, sys\n"
                  f"env = dict(os.environ, LAB_PARENT_PID=str(os.getpid()))\n"
                  f"c = subprocess.Popen([sys.executable, '-c', {child!r}, {str(marker)!r}], env=env,\n"
                  "                     start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                  "print(c.pid, flush=True)\nos._exit(0)\n")
        out = subprocess.run([sys.executable, "-c", parent], capture_output=True, text=True, timeout=60)
        pid = int(out.stdout.strip())
        time.sleep(2.5)
        self.assertFalse(marker.exists(), "an orphan that never had a chance to arm PDEATHSIG kept running")
        self.assertFalse(alive(pid))

    def test_the_real_child_scripts_arm_pdeathsig_before_anything_else(self):
        """main() of each real bootstrap calls die_with_parent first (the helper tests above call it directly)."""
        for script in ("_isolation_child.py", "_unstructured_child.py"):
            with self.subTest(script), tempfile.TemporaryDirectory() as directory:
                record = Path(directory, "order")
                runner = (
                    "import importlib.util, sys\n"
                    f"spec = importlib.util.spec_from_file_location('child', {str(Path(LIB, 'lab_parser', script))!r})\n"
                    "child = importlib.util.module_from_spec(spec); spec.loader.exec_module(child)\n"
                    f"log = open({str(record)!r}, 'w')\n"
                    "child.die_with_parent = lambda: log.write('armed\\n') or log.flush()\n"
                    "if hasattr(child, 'apply_limits'):\n"
                    "    child.apply_limits = lambda *a: log.write('limits\\n') or log.flush()\n"
                    "sys.argv = ['child', '536870912', '5', '1048576', '2000'][:len(sys.argv) + 0] if False else "
                    "['child', '536870912', '5', '1048576'] + (['2000'] if 'isolation' in " + repr(script) + " else [])\n"
                    "try:\n    child.main()\nexcept BaseException:\n    pass\n")
                subprocess.run([sys.executable, "-c", runner], cwd=directory, capture_output=True, timeout=60)
                lines = record.read_text().split()
                self.assertTrue(lines and lines[0] == "armed", f"{script}: {lines}")

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
