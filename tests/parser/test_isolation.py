"""Real PDF subprocess inspection plus controlled Linux resource-fault probes."""
from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from pypdf import PdfWriter
from pypdf.generic import NameObject, NumberObject
from lab_parser import isolation
from lab_parser.isolation import InspectionError, ProcessLimits, inspect_geometry
from tests.parser.pdfgen import build_pdf


class IsolatedGeometryTests(unittest.TestCase):
    def setUp(self):
        self.pdf = build_pdf([{"mediabox": (0, 0, 200, 100)},
                              {"mediabox": (0, 0, 300, 200), "rotate": 90}])

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(InspectionError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def test_real_pdf_geometry_identity_and_immutable_result(self):
        result = inspect_geometry(self.pdf)
        self.assertEqual(result.sha256, hashlib.sha256(self.pdf).hexdigest())
        self.assertEqual(result.size_bytes, len(self.pdf))
        self.assertEqual(result.page_count, 2)
        self.assertEqual([(p.page_index, p.rotation, p.width_pt, p.height_pt) for p in result.pages],
                         [(0, 0, 200, 100), (1, 90, 200, 300)])
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.pages[0].width_pt = 1

    def test_real_malformed_and_encrypted_pdf(self):
        self.assert_code("invalid_pdf", inspect_geometry, b"%PDF-CANARY-secret\n")
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.encrypt("CANARY-secret")
        stream = io.BytesIO()
        writer.write(stream)
        self.assert_code("encrypted_pdf", inspect_geometry, stream.getvalue())

    def test_real_warning_and_late_page_failure_publish_no_partial_geometry(self):
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.root_object[NameObject("/Type")] = NameObject("/InvalidCanary")
        stream = io.BytesIO()
        writer.write(stream)
        self.assert_code("invalid_pdf", inspect_geometry, stream.getvalue())
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        bad = writer.add_blank_page(width=100, height=100)
        bad[NameObject("/Rotate")] = NumberObject(17)
        stream = io.BytesIO()
        writer.write(stream)
        self.assert_code("invalid_pdf", inspect_geometry, stream.getvalue())

    def test_expired_or_cancelled_before_spawn(self):
        with patch.object(isolation.subprocess, "Popen") as spawn:
            self.assert_code("timeout", isolation._run_child, ["unused"], ".",
                             time.monotonic()-1, lambda: False)
            self.assert_code("cancelled", isolation._run_child, ["unused"], ".",
                             time.monotonic()+1, lambda: True)
            spawn.assert_not_called()

    def test_cancelled_or_expired_during_result_validation(self):
        original = isolation._validate
        event = threading.Event()
        def validate_then_cancel(*args):
            result = original(*args)
            event.set()
            return result
        with patch.object(isolation, "_validate", validate_then_cancel):
            self.assert_code("cancelled", inspect_geometry, self.pdf, cancelled=event.is_set)
        real_monotonic = time.monotonic
        offset = [0]
        def validate_then_expire(*args):
            result = original(*args)
            offset[0] = 100
            return result
        with patch.object(isolation, "_validate", validate_then_expire), patch.object(
                isolation.time, "monotonic", lambda: real_monotonic()+offset[0]):
            self.assert_code("timeout", inspect_geometry, self.pdf)

    def test_parent_rejects_oversized_or_symlink_child_output(self):
        with tempfile.TemporaryDirectory() as directory:
            child = Path(directory, "fake.py")
            child.write_text("open('result.json','wb').write(b'x'*1025)")
            with patch.object(isolation, "_CHILD", child):
                self.assert_code("invalid_child_result", inspect_geometry, self.pdf,
                                 ProcessLimits(output_bytes=1024))
            child.write_text("import os;os.symlink('source.pdf','result.json')")
            with patch.object(isolation, "_CHILD", child):
                self.assert_code("inspection_io_failed", inspect_geometry, self.pdf)

    def test_wall_failure_kills_group_before_reaping_leader(self):
        with tempfile.TemporaryDirectory() as directory:
            events = []
            real_stop = isolation._stop
            real_spawn = isolation.subprocess.Popen
            spawned = []
            def spawn(*args, **kwargs):
                process = real_spawn(*args, **kwargs)
                spawned.append(process)
                return process
            def stop(process):
                # The leader is deliberately unreaped, pinning group identity.
                events.append(process.returncode)
                real_stop(process)
            with patch.object(isolation.subprocess, "Popen", spawn), patch.object(isolation, "_stop", stop):
                self.assert_code("timeout", isolation._run_child,
                                 [sys.executable, "-I", "-c", "import time;time.sleep(10)"],
                                 directory, time.monotonic()+.1, lambda: False)
            self.assertEqual(events, [None])
            self.assertEqual(spawned[0].returncode, -signal.SIGKILL)
            with self.assertRaises(ChildProcessError):
                os.waitpid(spawned[0].pid, os.WNOHANG)

    def test_success_kills_lingering_same_group_descendant(self):
        with tempfile.TemporaryDirectory() as directory:
            program = (
                "import os,time;pid=os.fork();"
                "\nif pid==0: time.sleep(20);os._exit(0)"
                "\nopen('descendant.pid','w').write(str(pid))\nos._exit(0)"
            )
            spawned = []
            real_spawn = isolation.subprocess.Popen
            def spawn(*args, **kwargs):
                process = real_spawn(*args, **kwargs)
                spawned.append(process)
                return process
            with patch.object(isolation.subprocess, "Popen", spawn):
                isolation._run_child([sys.executable, "-I", "-c", program], directory,
                                     time.monotonic()+3, lambda: False)
            descendant = int(Path(directory, "descendant.pid").read_text())
            deadline = time.monotonic()+2
            while True:
                try:
                    # Zombies have terminated, but only their adopting parent
                    # can reap them; this API promises direct-child reaping.
                    state = Path(f"/proc/{descendant}/stat").read_text().split(") ", 1)[1][0]
                    stopped = state == "Z"
                except FileNotFoundError:
                    stopped = True
                if stopped or time.monotonic() >= deadline:
                    break
                time.sleep(.01)
            self.assertTrue(stopped, "same-group descendant survived cleanup")
            self.assertEqual(spawned[0].returncode, 0)
            with self.assertRaises(ChildProcessError):
                os.waitpid(spawned[0].pid, os.WNOHANG)

    def test_real_page_limit(self):
        self.assert_code("page_limit_exceeded", inspect_geometry, self.pdf, ProcessLimits(max_pages=1))

    def test_input_admission_never_spawns(self):
        with patch.object(isolation.subprocess, "Popen") as spawn:
            for value in (None, bytearray(self.pdf), "CANARY-secret"):
                self.assert_code("invalid_input", inspect_geometry, value)
            self.assert_code("input_size", inspect_geometry, b"")
            self.assert_code("input_size", inspect_geometry, self.pdf, ProcessLimits(max_pdf_bytes=1))
            self.assert_code("cancelled", inspect_geometry, self.pdf, cancelled=lambda: True)
            spawn.assert_not_called()

    def test_limits_validate_strictly(self):
        for name, value in (("memory_bytes", True), ("cpu_seconds", 0), ("cpu_seconds", 1.5),
                            ("wall_seconds", float("nan")), ("wall_seconds", float("inf")),
                            ("wall_seconds", True), ("wall_seconds", 181), ("max_pages", 2001),
                            ("output_bytes", 100), ("max_pdf_bytes", 2**30)):
            with self.subTest(name=name, value=value):
                self.assert_code("invalid_limits", ProcessLimits, **{name: value})

    def _fault_child(self, root: Path, behavior: str) -> Path:
        # Copy the actual bootstrap with controlled fake dependencies. This tests
        # OS limits set before imports, not parser correctness/provider quality.
        package = root / "lab_parser"
        package.mkdir()
        child = package / "_isolation_child.py"
        child.write_bytes(isolation._CHILD.read_bytes())
        (package / "__init__.py").write_text("")
        (root / "pypdf.py").write_text(
            "class PdfReader:\n"
            " def __init__(self, *a, **kw): self.is_encrypted=False; self.pages=[1]\n")
        (package / "pages.py").write_text("def extract_pages(data):\n" + behavior)
        return child

    def _run_fault(self, behavior, limits=ProcessLimits()):
        with tempfile.TemporaryDirectory() as directory:
            child = self._fault_child(Path(directory), behavior)
            with patch.object(isolation, "_CHILD", child):
                return inspect_geometry(self.pdf, limits)

    def test_actual_bootstrap_memory_limit(self):
        self.assert_code("memory_limit", self._run_fault,
                         " block=bytearray(1024*1024*1024)\n return []\n",
                         ProcessLimits(memory_bytes=128*1024*1024))

    def test_actual_bootstrap_cpu_limit_no_false_memory_diagnosis(self):
        start = time.monotonic()
        self.assert_code("child_failed", self._run_fault, " while True: pass\n",
                         ProcessLimits(cpu_seconds=1, wall_seconds=5))
        self.assertLess(time.monotonic() - start, 4)

    def test_actual_bootstrap_file_limit_and_no_partial_result(self):
        # The result's JSON exceeds RLIMIT_FSIZE; the child exits unsuccessfully.
        self.assert_code("child_failed", self._run_fault,
                         " class P:\n  page_index=0; rotation=0; display_width_pt='x'*10000; display_height_pt=1\n return [P()]\n",
                         ProcessLimits(output_bytes=1024))

    def test_wall_timeout_and_cancellation_reap(self):
        self.assert_code("timeout", self._run_fault, " import time\n time.sleep(5)\n return []\n",
                         ProcessLimits(wall_seconds=.15))
        event = threading.Event()
        timer = threading.Timer(.1, event.set)
        with tempfile.TemporaryDirectory() as directory:
            child = self._fault_child(Path(directory), " import time\n time.sleep(5)\n return []\n")
            timer.start()
            try:
                with patch.object(isolation, "_CHILD", child):
                    self.assert_code("cancelled", inspect_geometry, self.pdf, cancelled=event.is_set)
            finally:
                timer.cancel()

    def test_environment_and_file_descriptors_are_not_inherited(self):
        # Dependency probe would raise if a parent secret, PYTHONPATH hook or FD
        # were inherited. Resource limits/core/NOFILE are inspected in the child.
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory, "marker")
            descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT, 0o600)
            os.set_inheritable(descriptor, True)
            behavior = (
                " import os, resource\n"
                " assert 'LAB_SECRET_CANARY' not in os.environ\n"
                " assert 'PYTHONPATH' not in os.environ\n"
                " assert resource.getrlimit(resource.RLIMIT_CORE)==(0,0)\n"
                " assert resource.getrlimit(resource.RLIMIT_AS)==(536870912,536870912)\n"
                " assert resource.getrlimit(resource.RLIMIT_CPU)==(5,5)\n"
                " assert resource.getrlimit(resource.RLIMIT_FSIZE)==(1048576,1048576)\n"
                " assert resource.getrlimit(resource.RLIMIT_NOFILE)==(64,64)\n"
                f" try: os.write({descriptor}, b'leak')\n"
                " except OSError: pass\n"
                " else: raise AssertionError\n"
                " class P:\n  page_index=0; rotation=0; display_width_pt=2; display_height_pt=3\n return [P()]\n"
            )
            try:
                with patch.dict(os.environ, {"LAB_SECRET_CANARY": "secret", "PYTHONPATH": "BAD"}):
                    result = self._run_fault(behavior)
                self.assertEqual(result.page_count, 1)
                self.assertEqual(marker.read_bytes(), b"")
            finally:
                os.close(descriptor)

    def test_result_validator_rejects_ambiguous_and_nonfinite_metadata(self):
        digest = hashlib.sha256(self.pdf).hexdigest()
        valid = {"schema_version": "isolated-geometry-1", "sha256": digest, "size_bytes": len(self.pdf),
                 "pages": [{"page_index": 0, "rotation": 0, "width_pt": 1, "height_pt": 1}]}
        bad = [b'{"error":"invalid_pdf","error":"encrypted_pdf"}', b'{"error":[]}',
               b'{"error":"CANARY-secret"}', b'[]', b'\xff', b'{"error":NaN}']
        for change in ({"sha256": "0"*64}, {"size_bytes": True}, {"extra": 1},
                       {"pages": []}, {"pages": [{"page_index": 0, "rotation": False, "width_pt": 1, "height_pt": 1}]},
                       {"pages": [{"page_index": 0, "rotation": 0, "width_pt": float("inf"), "height_pt": 1}]}):
            bad.append(json.dumps(valid | change).encode())
        for raw in bad:
            with self.subTest(raw=raw):
                self.assert_code("invalid_child_result", isolation._validate, raw, digest, len(self.pdf), ProcessLimits())

    def test_child_nonzero_never_accepts_plausible_result(self):
        command = [sys.executable, "-I", "-c", "open('result.json','w').write('{}');raise SystemExit(1)"]
        with tempfile.TemporaryDirectory() as directory:
            self.assert_code("child_failed", isolation._run_child, command, directory,
                             time.monotonic()+2, lambda: False)

    def test_sanitized_diagnostics_and_private_temporary_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(isolation.tempfile, "tempdir", directory):
                self.assert_code("invalid_pdf", inspect_geometry, b"%PDF-CANARY-secret\n")
                self.assertEqual(list(Path(directory).iterdir()), [])
                inspect_geometry(self.pdf)
                self.assertEqual(list(Path(directory).iterdir()), [])
                self.assert_code("timeout", self._run_fault,
                                 " import time\n time.sleep(2)\n return []\n", ProcessLimits(wall_seconds=.05))
                self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
