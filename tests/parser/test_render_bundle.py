"""Real local rendering/publication and deterministic filesystem failure probes."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from pypdf import PdfWriter

from lab_parser import render_bundle as bundle
from lab_parser.geometry import to_region_space
from tests.parser.pdfgen import build_pdf, decode_png_rgb, mean_luma


class RenderBundleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.pdf"
        self.data = build_pdf([{"mediabox": (0, 0, 200, 100), "rects": [(20, 10, 60, 30)]}])
        self.source.write_bytes(self.data)
        self.output = self.root / "output"

    def create(self, **kwargs):
        return bundle.create_bundle(self.source, self.output, page_index=0, **kwargs)

    def code(self, expected, **kwargs):
        with self.assertRaises(bundle.BundleError) as caught:
            self.create(**kwargs)
        self.assertEqual(str(caught.exception), expected)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".l0-stage-*")), [])

    def test_real_full_page_identity_manifest_modes_and_replay(self):
        result = self.create(dpi=72)
        saved = json.loads((self.output / "manifest.json").read_bytes())
        png = (self.output / "asset.png").read_bytes()
        self.assertEqual(json.loads(json.dumps(result)), saved)
        self.assertEqual(saved["status"], "unverified_l0")
        self.assertEqual(saved["source"]["sha256"], hashlib.sha256(self.data).hexdigest())
        self.assertEqual(saved["source"]["size_bytes"], len(self.data))
        self.assertEqual(saved["asset"]["sha256"], hashlib.sha256(png).hexdigest())
        self.assertEqual(saved["asset"]["size_bytes"], len(png))
        self.assertEqual(decode_png_rgb(png)[:2], (200, 100))
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o700)
        for entry in self.output.iterdir():
            self.assertEqual(stat.S_IMODE(entry.stat().st_mode), 0o600)
        before = {p.name: p.read_bytes() for p in self.output.iterdir()}
        with self.assertRaisesRegex(bundle.BundleError, "output_exists"):
            self.create()
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.output.iterdir()})
        self.assertEqual(self.source.read_bytes(), self.data)

    def test_real_crop_rotation_and_cropbox_roundtrip(self):
        visible, black = (50, 50, 250, 150), (70, 60, 110, 80)
        for rotation in (0, 90, 180, 270):
            with self.subTest(rotation=rotation):
                self.source.write_bytes(build_pdf([{"mediabox": (0, 0, 300, 200), "cropbox": visible,
                                                  "rotate": rotation, "rects": [black]}]))
                output = self.root / f"rot{rotation}"
                region = to_region_space(black, visible, rotation)
                manifest = bundle.create_bundle(self.source, output, page_index=0, bbox=region, dpi=144)
                width, height, rgb = decode_png_rgb((output / "asset.png").read_bytes())
                self.assertLess(mean_luma(width, height, rgb, (2, 2, width-2, height-2)), 40)
                self.assertEqual(manifest["source"]["bbox"], list(region))
                self.assertEqual(tuple(manifest["asset"]["transform"]["region"]), region)
                self.assertIn("renderer_version", manifest["asset"]["transform"])

    def test_actual_exact_byte_limit_and_limit_plus_one(self):
        self.create(max_bytes=len(self.data))
        self.output = self.root / "limited"
        with patch.object(bundle, "_geometry", side_effect=AssertionError("must not parse")):
            self.code("input_too_large", max_bytes=len(self.data)-1)

    def test_strict_profiles_and_page_bounds(self):
        for bad in (True, 0, -1, bundle.MAX_BYTES+1, "10"):
            with self.subTest(max_bytes=bad):
                self.code("invalid_byte_limit", max_bytes=bad)
        for profile in ({"dpi": 301}, {"dpi": True}, {"bbox": [0, 0, float("nan"), 1]},
                        {"bbox": [0, 0, True, 1]}, {"bbox": [0, 0, 0, 1]}):
            with self.subTest(profile=profile):
                self.code("invalid_render_profile", **profile)
        for index in (-1, True, 1.0):
            with self.assertRaisesRegex(bundle.BundleError, "invalid_page_index"):
                bundle.create_bundle(self.source, self.output, page_index=index)
        with self.assertRaisesRegex(bundle.BundleError, "page_out_of_range"):
            bundle.create_bundle(self.source, self.output, page_index=1)

    def test_pixel_limit_is_not_silently_downsampled(self):
        self.source.write_bytes(build_pdf([{"mediabox": (0, 0, 2000, 2000)}]))
        self.code("pixel_limit", dpi=300)

    def test_nonregular_symlink_and_missing_input(self):
        self.source.unlink()
        self.code("input_unavailable")
        self.source.mkdir()
        self.code("input_not_regular")
        self.source.rmdir()
        os.mkfifo(self.source)
        self.code("input_not_regular")
        self.source.unlink()
        target = self.root / "real.pdf"
        target.write_bytes(self.data)
        self.source.symlink_to(target)
        self.code("input_unavailable")
        self.assertEqual(target.read_bytes(), self.data)

    def test_existing_destinations_preserved_including_empty_dir(self):
        for kind in ("empty_dir", "file", "nonempty_dir", "symlink"):
            with self.subTest(kind=kind):
                output = self.root / kind
                if kind.endswith("dir"):
                    output.mkdir()
                    if kind == "nonempty_dir":
                        (output / "keep").write_bytes(b"keep")
                elif kind == "file":
                    output.write_bytes(b"keep")
                else:
                    output.symlink_to(self.root / "missing")
                with self.assertRaisesRegex(bundle.BundleError, "output_exists"):
                    bundle.create_bundle(self.source, output, page_index=0)
                self.assertTrue(os.path.lexists(output))
                if kind == "file":
                    self.assertEqual(output.read_bytes(), b"keep")
                elif kind == "nonempty_dir":
                    self.assertEqual((output / "keep").read_bytes(), b"keep")

    def test_destination_created_after_render_is_not_replaced(self):
        original = bundle.render_region
        def race(*args):
            asset = original(*args)
            self.output.mkdir()
            return asset
        with patch.object(bundle, "render_region", side_effect=race):
            with self.assertRaisesRegex(bundle.BundleError, "output_exists"):
                self.create()
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual(list(self.root.glob(".l0-stage-*")), [])

    def test_failed_write_or_publication_cleans_private_stage(self):
        with patch.object(bundle, "_publish", side_effect=bundle.BundleError("publication_unsupported")):
            self.code("publication_unsupported")
        with patch.object(bundle.os, "fsync", side_effect=OSError("secret diagnostic")):
            self.code("output_unavailable")
        self.output = self.root / "missing" / "output"
        self.code("output_unavailable")

    def test_postpublication_fsync_failure_preserves_bundle(self):
        fsync = bundle.os.fsync
        calls = 0
        def fail_last(fd):
            nonlocal calls
            calls += 1
            if calls == 4:
                raise OSError("secret diagnostic")
            return fsync(fd)
        with patch.object(bundle.os, "fsync", side_effect=fail_last):
            with self.assertRaisesRegex(bundle.BundleError, "publication_durability_unknown"):
                self.create()
        self.assertEqual(set(p.name for p in self.output.iterdir()), {"asset.png", "manifest.json"})
        self.assertEqual(list(self.root.glob(".l0-stage-*")), [])

    def test_actual_encrypted_empty_password_and_invalid_pdfs_rejected(self):
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.encrypt("")
        stream = io.BytesIO()
        writer.write(stream)
        self.source.write_bytes(stream.getvalue())
        self.code("encrypted_pdf")
        for data in (b"", b"fake", b"%PDF-1.7\nsecret document content"):
            self.source.write_bytes(data)
            self.code("invalid_pdf")

    def test_warning_recovery_is_rejected(self):
        self.assertIn(b"/Root 3 0 R", self.data)
        self.source.write_bytes(self.data.replace(b"/Root 3 0 R", b"/Root 9 0 R"))
        with self.assertRaises(bundle.BundleError) as error:
            self.create()
        self.assertIn(str(error.exception), {"invalid_pdf", "parser_warning"})
        self.assertFalse(self.output.exists())

    def test_actual_page_limit_and_invalid_selected_geometry(self):
        for count in (2000, 2001):
            data = build_pdf([{"mediabox": (0, 0, 72, 72)} for _ in range(count)])
            if count == 2000:
                self.assertEqual(bundle._geometry(data, 1999), (2000, [72.0, 72.0]))
            else:
                with self.assertRaisesRegex(bundle.BundleError, "page_limit"):
                    bundle._geometry(data, 0)
        for page in ({"mediabox": (0, 0, 100, 100), "rotate": 45},
                     {"mediabox": (0, 0, 100, 100), "cropbox": (200, 200, 300, 300)}):
            self.source.write_bytes(build_pdf([page]))
            self.code("invalid_geometry")

    def test_embedded_nul_paths_cannot_truncate_ffi_publication(self):
        for source, output in ((self.source, str(self.output) + "\0ignored"),
                               (str(self.source) + "\0ignored", self.output)):
            with self.assertRaisesRegex(bundle.BundleError, "invalid_path"):
                bundle.create_bundle(source, output, page_index=0)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".l0-stage-*")), [])
        self.assertEqual(self.source.read_bytes(), self.data)

    def test_cleanup_failure_emits_fixed_code_without_raw_canary(self):
        with patch.object(bundle, "_publish", side_effect=bundle.BundleError("publication_unsupported")), \
             patch.object(bundle.shutil, "rmtree", side_effect=OSError("credential-canary path")):
            with self.assertRaises(bundle.BundleError) as caught:
                self.create()
            self.assertEqual(str(caught.exception), "cleanup_failed")
        self.assertFalse(self.output.exists())
        # Exercise the CLI boundary in-process so fault injection can reach cleanup.
        import contextlib
        output = io.StringIO()
        with patch.object(bundle, "_publish", side_effect=bundle.BundleError("publication_unsupported")), \
             patch.object(bundle.shutil, "rmtree", side_effect=OSError("credential-canary path")), \
             contextlib.redirect_stdout(output):
            code = bundle.main([str(self.source), "--output-dir", str(self.output), "--page-index", "0"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["code"], "cleanup_failed")
        self.assertNotIn("credential-canary", output.getvalue())

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "lab_parser.render_bundle", *map(str, args)],
                              capture_output=True, timeout=10, check=False)

    def test_actual_cli_json_and_secret_free_errors(self):
        result = self.cli(self.source, "--output-dir", self.output, "--page-index", 0, "--dpi", 72)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "unverified_l0")
        self.assertEqual(result.stderr, b"")
        secret = self.root / "credential-canary.pdf"
        secret.write_bytes(b"%PDF-1.7\ncredential-canary inside broken PDF")
        result = self.cli(secret, "--output-dir", self.root / "new", "--page-index", 0)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["code"], "invalid_pdf")
        self.assertEqual(result.stderr, b"")
        self.assertNotIn(b"credential-canary", result.stdout)
        result = self.cli(secret, "--page-index", "credential-canary")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["code"], "invalid_arguments")
        self.assertEqual(result.stderr, b"")


if __name__ == "__main__":
    unittest.main()
