"""Vision boundary contracts/faults, plus real PDFium input (not real VLM)."""
from dataclasses import replace
import hashlib
import json
import struct
import unittest
import zlib

from pydantic import ValidationError

from lab_parser.render import RenderedAsset, encode_png_rgb, render_region, _png_chunk, PNG_SIGNATURE
from lab_parser.vision import (VisionCall, VisionCapability, VisionError, VisionImage,
                               VisionResponse, read_vision)
from tests.parser.pdfgen import build_pdf


def image(width=2, height=1, color=255):
    png = encode_png_rgb(width, height, bytes([color]) * (width * height * 3))
    asset = RenderedAsset(png, hashlib.sha256(png).hexdigest(), width, height, 150,
                          0, (0.0, 0.0, 1.0, 1.0), {
        "renderer": "pdfium", "renderer_version": "test-fixture-v1", "dpi": 150,
        "scale": 150/72, "page_size_pt": [width*72/150, height*72/150], "crop_pt": [0, 0, 0, 0],
        "page_index": 0, "width_px": width, "height_px": height,
        "region": [0.0, 0.0, 1.0, 1.0]})
    return VisionImage.from_rendered(asset, artifact_ref="crop-1", document_id="doc-1",
                                     document_sha256="a" * 64, page_count=1)


def call():
    return VisionCall("op-1", "model-v1", "prompt-v1", "Read visible labels.", (image(),), 20)


def capability(**overrides):
    fields = dict(adapter_revision="adapter-v1", model_revision="model-v1", vision=True,
                  max_images=8, max_total_pixels=16_000_000, max_output_tokens=100)
    return VisionCapability(**(fields | overrides))


def response(req, **overrides):
    body = dict(operation_id=req.operation_id, model_revision=req.model_revision,
                prompt_revision=req.prompt_revision,
                image_hashes=[i.png_sha256 for i in req.images],
                observations=[dict(image_sha256=req.images[0].png_sha256,
                                   kind="visible_text", text="synthetic label",
                                   confidence=None, verification="unverified")],
                input_tokens=None, output_tokens=None, provider_request_id=None)
    return json.dumps(body | overrides).encode()


def changed_png(img, png):
    return replace(img, png=png, png_sha256=hashlib.sha256(png).hexdigest())


def custom_png(width, height, scanlines, *, tail=b"", filter_method=0):
    return (PNG_SIGNATURE + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, filter_method, 0))
            + _png_chunk(b"IDAT", zlib.compress(scanlines) + tail) + _png_chunk(b"IEND", b""))


class TestVision(unittest.TestCase):
    def run_reader(self, req=None, **kwargs):
        req = req or call()
        defaults = dict(capability=capability(), transport=lambda c, t: response(c),
                        admit=lambda c: True, deadline=100.0, clock=lambda: 1.0)
        return read_vision(req, **(defaults | kwargs))

    def assert_rejected_before_admission(self, req, code):
        entered = []
        with self.assertRaisesRegex(VisionError, f"^{code}$"):
            self.run_reader(req, admit=lambda c: entered.append(c),
                            transport=lambda c, t: self.fail("transport called"))
        self.assertEqual(entered, [])

    def test_real_pdfium_image_bytes_reach_vision_transport(self):
        pdf = build_pdf([{"mediabox": (0, 0, 72, 72), "rects": [(0, 0, 36, 36)]}])
        rendered = render_region(pdf, 0, dpi=72)
        img = VisionImage.from_rendered(rendered, artifact_ref="real-render", document_id="doc",
                                       document_sha256=hashlib.sha256(pdf).hexdigest(), page_count=1)
        req = replace(call(), images=(img,))
        invocations = []
        def transport(c, timeout):
            invocations.append((c, timeout))
            self.assertEqual(c.images[0].png, rendered.png)
            self.assertEqual(c.images[0].png_sha256, hashlib.sha256(rendered.png).hexdigest())
            self.assertEqual(json.loads(c.images[0].transform_json)["renderer"], "pdfium")
            return response(c)
        result = self.run_reader(req, transport=transport)
        self.assertEqual(len(invocations), 1)
        self.assertEqual(invocations[0][1], 99.0)
        self.assertEqual(result.status, "needs_review")
        self.assertIsNone(result.response.input_tokens)
        self.assertIsNone(result.response.observations[0].confidence)
        self.assertEqual(result.response_sha256, hashlib.sha256(result.raw_response).hexdigest())
        self.assertNotIn("synthetic label", repr(result))

    def test_real_fractional_crop_uses_pdfium_edge_rounding(self):
        for rotation in (0, 90, 180, 270):
            pdf = build_pdf([{"mediabox": (0, 0, 72, 72), "rotate": rotation}])
            asset = render_region(pdf, 0, region=(.001, .001, .999, .999), dpi=72)
            self.assertEqual((asset.width_px, asset.height_px), (70, 70))
            img = VisionImage.from_rendered(asset, artifact_ref="crop", document_id="doc", document_sha256=hashlib.sha256(pdf).hexdigest(), page_count=1)
            self.run_reader(replace(call(), images=(img,)))

    def test_metadata_snapshot_is_immutable(self):
        asset = render_region(build_pdf([{"mediabox": (0, 0, 10, 10)}]), 0, dpi=72)
        img = VisionImage.from_rendered(asset, artifact_ref="a", document_id="d", document_sha256="a"*64, page_count=1)
        original = img.transform_json
        asset.transform["width_px"] = 999
        self.assertEqual(img.transform_json, original)
        self.run_reader(replace(call(), images=(img,)))

    def test_text_only_and_unknown_capability_rejected(self):
        for flag in (False, "unknown"):
            with self.subTest(flag=flag), self.assertRaises(ValidationError):
                capability(vision=flag)
        with self.assertRaisesRegex(VisionError, "vision_capability_required"):
            self.run_reader(capability=object())

    def test_unpinned_or_wrong_model_rejected(self):
        for req, code in ((replace(call(), model_revision="other-v1"), "model_mismatch"),
                          (replace(call(), prompt_revision="latest"), "unpinned_revision")):
            self.assert_rejected_before_admission(req, code)
        with self.assertRaisesRegex(VisionError, "unpinned_revision"):
            self.run_reader(capability=capability(adapter_revision="unknown"))

    def test_empty_or_too_many_images_rejected(self):
        self.assert_rejected_before_admission(replace(call(), images=()), "image_limit")
        self.assert_rejected_before_admission(replace(call(), images=(image(),)*9), "image_limit")

    def test_duplicate_image_rejected(self):
        self.assert_rejected_before_admission(replace(call(), images=(image(), image())), "duplicate_image")

    def test_wrong_hash_or_dimensions_rejected(self):
        self.assert_rejected_before_admission(replace(call(), images=(replace(image(), png_sha256="0"*64),)), "image_hash_mismatch")
        img = image()
        transform = json.loads(img.transform_json) | {"width_px": 3, "page_size_pt": [3*72/150, 72/150]}
        img = replace(img, width_px=3, transform_json=json.dumps(transform))
        self.assert_rejected_before_admission(replace(call(), images=(img,)), "image_dimensions_mismatch")

    def test_source_region_and_physical_page_are_required(self):
        for img in (replace(image(), page_index=1), replace(image(), page_count=0),
                    replace(image(), document_sha256="not-a-hash")):
            with self.subTest(img=img.artifact_ref):
                self.assert_rejected_before_admission(replace(call(), images=(img,)), "invalid_provenance")
        self.assert_rejected_before_admission(replace(call(), images=(replace(image(), region=(0., 0., 0., 1.)),)), "invalid_region")
        self.assert_rejected_before_admission(replace(call(), images=(replace(image(), transform_json="{}"),)), "invalid_transform")

    def test_png_crc_truncation_and_trailing_data_rejected(self):
        img = image()
        corrupt = bytearray(img.png)
        corrupt[-1] ^= 1
        for png in (bytes(corrupt), img.png[:-1], img.png+b"trailing"):
            with self.subTest(length=len(png)):
                self.assert_rejected_before_admission(replace(call(), images=(changed_png(img, png),)), "invalid_png")

    def test_png_inflate_bomb_and_multiple_streams_rejected(self):
        for png in (custom_png(2, 1, b"\x00"*100000),
                    custom_png(2, 1, b"\x00"*7, tail=zlib.compress(b"extra")),
                    custom_png(2, 1, b"\x00"*6)):
            with self.subTest(length=len(png)):
                self.assert_rejected_before_admission(replace(call(), images=(changed_png(image(), png),)), "invalid_png")

    def test_png_unsupported_filter_or_profile_rejected(self):
        for png in (custom_png(2, 1, b"\x01"+b"\x00"*6), custom_png(2, 1, b"\x00"*7, filter_method=1)):
            self.assert_rejected_before_admission(replace(call(), images=(changed_png(image(), png),)), "unsupported_png")

    def test_declared_pixel_limit_enforced_before_admission(self):
        with self.assertRaisesRegex(VisionError, "pixel_limit"):
            self.run_reader(capability=capability(max_total_pixels=1), admit=lambda c: self.fail("admit"))
        req = replace(call(), images=(image(2, 1), image(2, 1, color=0)))
        with self.assertRaisesRegex(VisionError, "pixel_limit"):
            self.run_reader(req, capability=capability(max_total_pixels=3), admit=lambda c: self.fail("admit"))

    def test_png_side_limit_before_decompression(self):
        png = custom_png(4097, 1, b"")
        self.assert_rejected_before_admission(replace(call(), images=(changed_png(image(), png),)), "pixel_limit")

    def test_output_token_and_response_byte_limits(self):
        for tokens in (0, True, 101):
            self.assert_rejected_before_admission(replace(call(), max_output_tokens=tokens), "output_token_limit")
        with self.assertRaisesRegex(VisionError, "invalid_response_size"):
            self.run_reader(max_response_bytes=1)
        with self.assertRaisesRegex(VisionError, "response_token_limit"):
            self.run_reader(transport=lambda c, t: response(c, output_tokens=21))

    def test_no_call_after_admission_denial_and_truthy_not_permission(self):
        for value in (False, None, 1, "yes"):
            with self.subTest(value=value), self.assertRaisesRegex(VisionError, "admission_denied"):
                self.run_reader(admit=lambda c: value, transport=lambda c, t: self.fail("transport"))

    def test_admission_exception_is_sanitized(self):
        def admit(c):
            raise RuntimeError("secret-token")
        with self.assertRaisesRegex(VisionError, "^admission_failed$"):
            self.run_reader(admit=admit)

    def test_transport_failure_is_unknown_without_retry_and_sanitized(self):
        calls = []
        def transport(c, t):
            calls.append(c)
            raise RuntimeError("secret-token")
        with self.assertRaisesRegex(VisionError, "^transport_failed_outcome_unknown$"):
            self.run_reader(transport=transport)
        self.assertEqual(len(calls), 1)

    def test_cancel_or_expired_before_admission(self):
        for overrides, code in ((dict(cancelled=lambda: True), "cancelled"), (dict(deadline=1.0), "deadline_exceeded")):
            with self.subTest(code=code), self.assertRaisesRegex(VisionError, code):
                self.run_reader(admit=lambda c: self.fail("admission"), **overrides)

    def test_cancel_during_admission_suppresses_call(self):
        state = [False]
        def admit(c):
            state[0] = True
            return True
        with self.assertRaisesRegex(VisionError, "cancelled"):
            self.run_reader(admit=admit, cancelled=lambda: state[0], transport=lambda c, t: self.fail("transport"))

    def test_late_or_cancelled_response_withheld_with_restricted_audit(self):
        for mode in ("cancel", "deadline"):
            now = [1.0]
            stopped = [False]
            def transport(c, t):
                if mode == "cancel":
                    stopped[0] = True
                else:
                    now[0] = 100.0
                return response(c)
            with self.subTest(mode=mode), self.assertRaises(VisionError) as caught:
                self.run_reader(transport=transport, cancelled=lambda: stopped[0], clock=lambda: now[0])
            self.assertIsNotNone(caught.exception.restricted_response)
            self.assertNotIn("synthetic label", str(caught.exception))

    def test_mismatched_response_identity_and_observation_hash_rejected(self):
        for overrides in (dict(operation_id="other"), dict(model_revision="other"),
                          dict(prompt_revision="other"), dict(image_hashes=["b"*64]),
                          dict(observations=[dict(image_sha256="b"*64, kind="visible_text", text="x", confidence=None, verification="unverified")])):
            with self.subTest(overrides=overrides), self.assertRaisesRegex(VisionError, "response_binding_mismatch"):
                self.run_reader(transport=lambda c, t: response(c, **overrides))

    def test_invalid_response_json_schema_and_duplicate_keys(self):
        for raw in (b"not json", b"{\"secret\":\"token\"}", b"{\"a\":1,\"a\":2}", b"[NaN]", b"["*2000+b"]"*2000):
            with self.subTest(length=len(raw)), self.assertRaisesRegex(VisionError, "^invalid_response$"):
                self.run_reader(transport=lambda c, t: raw)

    def test_observations_never_claim_verified_gold(self):
        req = call()
        payload = json.loads(response(req))
        payload["observations"][0]["verification"] = "verified"
        with self.assertRaisesRegex(VisionError, "invalid_response"):
            self.run_reader(transport=lambda c, t: json.dumps(payload).encode())
        result = self.run_reader(transport=lambda c, t: response(c, observations=[]))
        self.assertEqual(result.response.observations, ())
        self.assertEqual(result.status, "needs_review")

    def test_surrogate_labels_and_nonfinite_transform_rejected(self):
        self.assert_rejected_before_admission(replace(call(), prompt="\ud800"), "invalid_call")
        self.assert_rejected_before_admission(replace(call(), prompt_revision="latest "), "invalid_call")
        self.assert_rejected_before_admission(replace(call(), operation_id=" op-1 "), "invalid_call")
        self.assert_rejected_before_admission(replace(call(), images=(replace(image(), region=(0., 0., 10**1000, 1.)),)), "invalid_call")
        with self.assertRaisesRegex(VisionError, "invalid_limits"):
            self.run_reader(deadline=10**1000)
        img = image()
        transform = json.loads(img.transform_json) | {"extra": float("inf")}
        self.assert_rejected_before_admission(replace(call(), images=(replace(img, transform_json=json.dumps(transform)),)), "invalid_transform")

    def test_invalid_transform_geometry_rejected(self):
        img = image()
        for fields in ({"crop_pt": [1, 0, 0, 0]}, {"scale": 9}, {"renderer_version": "latest"}, {"page_index": False}):
            transform = json.loads(img.transform_json) | fields
            self.assert_rejected_before_admission(replace(call(), images=(replace(img, transform_json=json.dumps(transform)),)), "invalid_transform")

    def test_response_is_immutable(self):
        result = self.run_reader()
        self.assertIsInstance(result.response.image_hashes, tuple)
        self.assertIsInstance(result.response.observations, tuple)
        with self.assertRaises(ValidationError):
            result.response.observations[0].text = "changed"


if __name__ == "__main__":
    unittest.main()
