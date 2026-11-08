"""Tests for the strict bounded multipart parser."""
from __future__ import annotations
import sys, unittest
from pathlib import Path
_REPO_ROOT = Path(__file__).resolve().parents[2]
for _sub in (_REPO_ROOT, _REPO_ROOT / "services" / "api", _REPO_ROOT / "contracts"):
    if _sub.exists() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))
from lab_api.multipart_strict import MultipartError, ParsedUpload, boundary_from_content_type, parse_upload
from typing import Optional

CT = "multipart/form-data; boundary=BOUNDARY"
PDF = b"%PDF-1.4\n" + bytes(range(256)) * 4


def part(
    name: str,
    body: bytes = b"",
    filename: Optional[str] = None,
    ctype: Optional[str] = None,
    extra_headers: tuple[bytes | str, ...] | list[bytes | str] = (),
) -> bytes:
    lines = [f'Content-Disposition: form-data; name="{name}"'.encode("utf-8")]
    if filename is not None:
        lines[0] += f'; filename="{filename}"'.encode("utf-8")
    lines[0] += b"\r\n"
    if ctype is not None:
        lines.append(f"Content-Type: {ctype}\r\n".encode("utf-8"))
    for h in extra_headers:
        if isinstance(h, str):
            h_bytes = h.encode("utf-8")
        else:
            h_bytes = h
        if not h_bytes.endswith(b"\r\n"):
            h_bytes += b"\r\n"
        lines.append(h_bytes)
    return b"".join(lines) + b"\r\n" + body


def build(
    *parts: bytes,
    boundary: bytes = b"BOUNDARY",
    closing: Optional[bytes] = None,
) -> bytes:
    dash = b"--" + boundary
    actual_closing = dash + b"--\r\n" if closing is None else closing
    if not parts:
        return dash + b"\r\n" + actual_closing
    return dash + b"\r\n" + (b"\r\n" + dash + b"\r\n").join(parts) + b"\r\n" + actual_closing


def ok(body: bytes | bytearray, limit: int = 10_000_000) -> ParsedUpload:
    return parse_upload(CT, body, limit)


def err(
    self: unittest.TestCase,
    body: bytes | bytearray,
    status: int,
    code: str,
    limit: int = 10_000_000,
    ct: str = CT,
) -> MultipartError:
    with self.assertRaises(MultipartError) as c:
        parse_upload(ct, body, limit)
    self.assertEqual(c.exception.status_code, status)
    self.assertEqual(c.exception.code, code)
    return c.exception


class TestMultipartStrict(unittest.TestCase):
    err = err

    def test_01_single_pdf_file_part(self) -> None:
        p = part("file", PDF, filename="a.pdf", ctype="application/pdf")
        res = ok(build(p))
        self.assertIsInstance(res, ParsedUpload)
        self.assertEqual(res.pdf, PDF)
        self.assertIsNone(res.logical_id)
        self.assertIs(type(res.pdf), bytes)

    def test_02_bytearray_body(self) -> None:
        p = part("file", PDF, filename="a.pdf", ctype="application/pdf")
        body_bytes = build(p)
        body_ba = bytearray(body_bytes)
        res = ok(body_ba)
        self.assertIsInstance(res, ParsedUpload)
        self.assertEqual(res.pdf, PDF)
        self.assertIsNone(res.logical_id)
        self.assertIs(type(res.pdf), bytes)
        self.assertEqual(res, ok(body_bytes))

    def test_03_field_names_and_media_types(self) -> None:
        for field in ("pdf", "document"):
            with self.subTest(field=field):
                p = part(field, PDF, filename="doc.pdf", ctype="application/pdf")
                res = ok(build(p))
                self.assertEqual(res.pdf, PDF)

        with self.subTest(case="x1_with_pdf_ctype"):
            p = part("x1", PDF, ctype="application/pdf")
            res = ok(build(p))
            self.assertEqual(res.pdf, PDF)

        with self.subTest(case="octet_stream_scan_PDF"):
            p = part("file", PDF, filename="scan.PDF", ctype="application/octet-stream")
            res = ok(build(p))
            self.assertEqual(res.pdf, PDF)

        with self.subTest(case="octet_stream_scan_bin"):
            p = part("file", PDF, filename="scan.bin", ctype="application/octet-stream")
            self.err(build(p), 415, "unsupported_media_type")

    def test_04_logical_id_part(self) -> None:
        p_file = part("file", PDF, filename="a.pdf", ctype="application/pdf")

        with self.subTest(case="before_with_ctype"):
            p_id = part("logical_id", b"  doc-1 ", ctype="text/plain; charset=utf-8")
            res = ok(build(p_id, p_file))
            self.assertEqual(res.logical_id, "doc-1")
            self.assertEqual(res.pdf, PDF)

        with self.subTest(case="after_without_ctype"):
            p_id = part("logical_id", b"  doc-1 ")
            res = ok(build(p_file, p_id))
            self.assertEqual(res.logical_id, "doc-1")
            self.assertEqual(res.pdf, PDF)

        with self.subTest(case="utf8_value"):
            p_id = part("logical_id", "文件-1".encode("utf-8"))
            res = ok(build(p_file, p_id))
            self.assertEqual(res.logical_id, "文件-1")
            self.assertEqual(res.pdf, PDF)

    def test_05_crlf_and_blank_lines_in_payload(self) -> None:
        payload = (
            PDF
            + b"\r\n\r\n"
            + b"Content-Disposition: form-data; name=\"fake\"\r\n\r\n"
            + b"binary-data\r\n\r\n"
            + bytes(range(256))
        )
        p = part("file", payload, filename="a.pdf", ctype="application/pdf")
        res = ok(build(p))
        self.assertEqual(res.pdf, payload)

    def test_06_closing_boundary_trailing_bytes(self) -> None:
        p = part("file", PDF, filename="a.pdf", ctype="application/pdf")

        with self.subTest(case="no_trailing_bytes"):
            res = ok(build(p, closing=b"--BOUNDARY--"))
            self.assertEqual(res.pdf, PDF)

        with self.subTest(case="one_trailing_crlf"):
            res = ok(build(p, closing=b"--BOUNDARY--\r\n"))
            self.assertEqual(res.pdf, PDF)

    def test_07_quoted_boundary(self) -> None:
        ct_quoted = 'multipart/form-data; boundary="BOUNDARY"'
        b = boundary_from_content_type(ct_quoted)
        self.assertEqual(b, b"BOUNDARY")
        p = part("file", PDF, filename="a.pdf", ctype="application/pdf")
        res = parse_upload(ct_quoted, build(p), 10_000_000)
        self.assertEqual(res.pdf, PDF)

    def test_08_max_pdf_bytes_limit(self) -> None:
        limit = len(PDF)
        p = part("file", PDF, filename="a.pdf", ctype="application/pdf")

        with self.subTest(case="exact_limit"):
            res = ok(build(p), limit=limit)
            self.assertEqual(res.pdf, PDF)

        with self.subTest(case="limit_plus_one"):
            p_over = part("file", PDF + b"X", filename="a.pdf", ctype="application/pdf")
            self.err(build(p_over), 413, "payload_too_large", limit=limit)

    def test_09_body_not_starting_with_opening_boundary(self) -> None:
        p = part("file", PDF, filename="a.pdf", ctype="application/pdf")

        with self.subTest(case="preamble_text"):
            body = b"preamble before boundary\r\n" + build(p)
            self.err(body, 400, "invalid_request")

        with self.subTest(case="missing_opening"):
            body = p + b"\r\n--BOUNDARY--\r\n"
            self.err(body, 400, "invalid_request")

        with self.subTest(case="wrong_boundary_name"):
            body = build(p, boundary=b"WRONG")
            self.err(body, 400, "invalid_request")

    def test_10_closing_boundary_errors(self) -> None:
        p = part("file", PDF, filename="a.pdf", ctype="application/pdf")

        with self.subTest(case="truncated_no_closing"):
            body = b"--BOUNDARY\r\n" + p
            self.err(body, 400, "invalid_request")

        with self.subTest(case="extra_data_after_closing"):
            body = build(p, closing=b"--BOUNDARY--extra\r\n")
            self.err(body, 400, "invalid_request")

        with self.subTest(case="double_crlf_after_closing"):
            body = build(p, closing=b"--BOUNDARY--\r\n\r\n")
            self.err(body, 400, "invalid_request")

    def test_11_header_block_structure_limits(self) -> None:
        with self.subTest(case="header_block_never_ends"):
            unterminated = (
                b"--BOUNDARY\r\n"
                b"Content-Disposition: form-data; name=\"file\"\r\n"
                b"Content-Type: application/pdf\r\n"
                + PDF
                + b"\r\n--BOUNDARY--\r\n"
            )
            self.err(unterminated, 400, "invalid_request")

        with self.subTest(case="header_block_oversized"):
            huge_header = b"X-Large: " + (b"a" * 9000) + b"\r\n"
            p = part("file", PDF, filename="a.pdf", ctype="application/pdf", extra_headers=[huge_header])
            self.err(build(p), 400, "invalid_request")

        with self.subTest(case="more_than_eight_headers"):
            extra_headers = [f"X-Extra-{i}: val{i}\r\n".encode("ascii") for i in range(7)]
            p = part("file", PDF, filename="a.pdf", ctype="application/pdf", extra_headers=extra_headers)
            self.err(build(p), 400, "invalid_request")

    def test_12_malformed_headers(self) -> None:
        with self.subTest(case="folded_header_space"):
            p = part("file", PDF, filename="a.pdf", ctype="application/pdf", extra_headers=[b" folded: value"])
            self.err(build(p), 400, "invalid_request")

        with self.subTest(case="folded_header_tab"):
            p = part("file", PDF, filename="a.pdf", ctype="application/pdf", extra_headers=[b"\tfolded: value"])
            self.err(build(p), 400, "invalid_request")

        with self.subTest(case="header_without_colon"):
            p = part("file", PDF, filename="a.pdf", ctype="application/pdf", extra_headers=[b"NoColonHeader"])
            self.err(build(p), 400, "invalid_request")

        with self.subTest(case="duplicate_content_disposition"):
            p = part(
                "file",
                PDF,
                filename="a.pdf",
                ctype="application/pdf",
                extra_headers=[b'Content-Disposition: form-data; name="duplicate"'],
            )
            self.err(build(p), 400, "invalid_request")

        with self.subTest(case="duplicate_content_type"):
            p = part(
                "file",
                PDF,
                filename="a.pdf",
                ctype="application/pdf",
                extra_headers=[b"Content-Type: application/pdf"],
            )
            self.err(build(p), 400, "invalid_request")

        with self.subTest(case="non_utf8_in_headers"):
            p = part("file", PDF, filename="a.pdf", ctype="application/pdf", extra_headers=[b"X-Bad: \xff\r\n"])
            self.err(build(p), 400, "invalid_request")

    def test_13_content_disposition_errors(self) -> None:
        with self.subTest(case="missing_content_disposition"):
            p = b"Content-Type: application/pdf\r\n\r\n" + PDF
            self.err(build(p), 400, "invalid_request")

        with self.subTest(case="disposition_attachment"):
            p = (
                b'Content-Disposition: attachment; filename="a.pdf"\r\n'
                b"Content-Type: application/pdf\r\n\r\n"
                + PDF
            )
            self.err(build(p), 400, "invalid_request")

        with self.subTest(case="form_data_without_name"):
            p = (
                b'Content-Disposition: form-data; filename="a.pdf"\r\n'
                b"Content-Type: application/pdf\r\n\r\n"
                + PDF
            )
            self.err(build(p), 400, "invalid_request")

    def test_14_content_transfer_encoding(self) -> None:
        with self.subTest(case="base64_rejected"):
            p = part(
                "file",
                PDF,
                filename="a.pdf",
                ctype="application/pdf",
                extra_headers=[b"Content-Transfer-Encoding: base64"],
            )
            self.err(build(p), 400, "invalid_request")

        with self.subTest(case="quoted_printable_rejected"):
            p = part(
                "file",
                PDF,
                filename="a.pdf",
                ctype="application/pdf",
                extra_headers=[b"Content-Transfer-Encoding: quoted-printable"],
            )
            self.err(build(p), 400, "invalid_request")

        with self.subTest(case="binary_accepted"):
            p = part(
                "file",
                PDF,
                filename="a.pdf",
                ctype="application/pdf",
                extra_headers=[b"Content-Transfer-Encoding: binary"],
            )
            res = ok(build(p))
            self.assertEqual(res.pdf, PDF)

        with self.subTest(case="7bit_accepted"):
            p = part(
                "file",
                PDF,
                filename="a.pdf",
                ctype="application/pdf",
                extra_headers=[b"Content-Transfer-Encoding: 7bit"],
            )
            res = ok(build(p))
            self.assertEqual(res.pdf, PDF)

        with self.subTest(case="8bit_accepted"):
            p = part(
                "file",
                PDF,
                filename="a.pdf",
                ctype="application/pdf",
                extra_headers=[b"Content-Transfer-Encoding: 8bit"],
            )
            res = ok(build(p))
            self.assertEqual(res.pdf, PDF)

    def test_15_unexpected_part_name(self) -> None:
        p_file = part("file", PDF, filename="a.pdf", ctype="application/pdf")
        p_extra = part("extra", b"random content", ctype="text/plain")

        with self.subTest(case="extra_after_file"):
            exc = self.err(build(p_file, p_extra), 400, "invalid_request")
            self.assertIn("unexpected multipart part", exc.message)

        with self.subTest(case="extra_before_file"):
            exc = self.err(build(p_extra, p_file), 400, "invalid_request")
            self.assertIn("unexpected multipart part", exc.message)

    def test_16_duplicate_pdf_and_non_pdf_file(self) -> None:
        with self.subTest(case="two_pdf_parts"):
            p1 = part("file", PDF, filename="a.pdf", ctype="application/pdf")
            p2 = part("pdf", PDF, filename="b.pdf", ctype="application/pdf")
            self.err(build(p1, p2), 400, "invalid_request")

        with self.subTest(case="file_named_with_text_plain"):
            p_txt = part("file", b"text payload", ctype="text/plain")
            self.err(build(p_txt), 415, "unsupported_media_type")

        with self.subTest(case="file_named_without_ctype"):
            p_no_ct = part("file", b"untyped payload")
            self.err(build(p_no_ct), 415, "unsupported_media_type")

    def test_17_no_pdf_part_at_all(self) -> None:
        p_id = part("logical_id", b"doc-1")
        exc = self.err(build(p_id), 400, "invalid_request")
        self.assertIn("multipart body does not contain a PDF file", exc.message)

    def test_18_logical_id_errors(self) -> None:
        p_file = part("file", PDF, filename="a.pdf", ctype="application/pdf")

        with self.subTest(case="duplicate_logical_id"):
            p1 = part("logical_id", b"doc-1")
            p2 = part("logical_id", b"doc-2")
            self.err(build(p_file, p1, p2), 400, "invalid_request")

        with self.subTest(case="logical_id_with_filename"):
            p_fn = part("logical_id", b"doc-1", filename="id.txt")
            self.err(build(p_file, p_fn), 400, "invalid_request")

        with self.subTest(case="logical_id_with_json_ctype"):
            p_json = part("logical_id", b'{"id": "doc-1"}', ctype="application/json")
            self.err(build(p_file, p_json), 400, "invalid_request")

        with self.subTest(case="logical_id_longer_than_512_bytes"):
            p_long = part("logical_id", b"a" * 513)
            self.err(build(p_file, p_long), 400, "invalid_request")

        with self.subTest(case="logical_id_exact_512_bytes_accepted"):
            p_512 = part("logical_id", b"a" * 512)
            res = ok(build(p_file, p_512))
            self.assertEqual(res.logical_id, "a" * 512)

        with self.subTest(case="logical_id_invalid_utf8"):
            p_bad_utf8 = part("logical_id", b"\xff\xfe")
            self.err(build(p_file, p_bad_utf8), 400, "invalid_request")

    def test_19_more_than_four_parts(self) -> None:
        p_file = part("file", PDF, filename="a.pdf", ctype="application/pdf")
        p2 = part("logical_id", b"id2")
        p3 = part("logical_id", b"id3")
        p4 = part("logical_id", b"id4")
        p5 = part("logical_id", b"id5")
        self.err(build(p_file, p2, p3, p4, p5), 400, "invalid_request")

    def test_20_content_type_and_boundary_problems(self) -> None:
        p = part("file", PDF, filename="a.pdf", ctype="application/pdf")
        valid_body = build(p)

        with self.subTest(case="application_json"):
            ct = "application/json"
            with self.assertRaises(MultipartError) as c:
                boundary_from_content_type(ct)
            self.assertEqual((c.exception.status_code, c.exception.code), (415, "unsupported_media_type"))
            self.err(valid_body, 415, "unsupported_media_type", ct=ct)

        with self.subTest(case="no_boundary"):
            ct = "multipart/form-data"
            with self.assertRaises(MultipartError) as c:
                boundary_from_content_type(ct)
            self.assertEqual((c.exception.status_code, c.exception.code), (400, "invalid_request"))
            self.err(valid_body, 400, "invalid_request", ct=ct)

        with self.subTest(case="two_boundaries"):
            ct = "multipart/form-data; boundary=b1; boundary=b2"
            with self.assertRaises(MultipartError) as c:
                boundary_from_content_type(ct)
            self.assertEqual((c.exception.status_code, c.exception.code), (400, "invalid_request"))
            self.err(valid_body, 400, "invalid_request", ct=ct)

        with self.subTest(case="boundary_longer_than_70_chars"):
            ct = "multipart/form-data; boundary=" + ("A" * 71)
            with self.assertRaises(MultipartError) as c:
                boundary_from_content_type(ct)
            self.assertEqual((c.exception.status_code, c.exception.code), (400, "invalid_request"))
            self.err(valid_body, 400, "invalid_request", ct=ct)

        with self.subTest(case="boundary_containing_space"):
            ct = 'multipart/form-data; boundary="BOUND ARY"'
            with self.assertRaises(MultipartError) as c:
                boundary_from_content_type(ct)
            self.assertEqual((c.exception.status_code, c.exception.code), (400, "invalid_request"))
            self.err(valid_body, 400, "invalid_request", ct=ct)

        with self.subTest(case="boundary_containing_quote"):
            ct = 'multipart/form-data; boundary="BOUND\\"ARY"'
            with self.assertRaises(MultipartError) as c:
                boundary_from_content_type(ct)
            self.assertEqual((c.exception.status_code, c.exception.code), (400, "invalid_request"))
            self.err(valid_body, 400, "invalid_request", ct=ct)

        with self.subTest(case="boundary_containing_backslash"):
            ct = 'multipart/form-data; boundary="BOUND\\\\ARY"'
            with self.assertRaises(MultipartError) as c:
                boundary_from_content_type(ct)
            self.assertEqual((c.exception.status_code, c.exception.code), (400, "invalid_request"))
            self.err(valid_body, 400, "invalid_request", ct=ct)

        with self.subTest(case="content_type_longer_than_512_chars"):
            ct = "multipart/form-data; boundary=BOUNDARY; extra=" + ("x" * 500)
            with self.assertRaises(MultipartError) as c:
                boundary_from_content_type(ct)
            self.assertEqual((c.exception.status_code, c.exception.code), (400, "invalid_request"))
            self.err(valid_body, 400, "invalid_request", ct=ct)

        with self.subTest(case="non_ascii_in_content_type"):
            ct = "multipart/form-data; boundary=BOUNDARY; comment=é"
            with self.assertRaises(MultipartError) as c:
                boundary_from_content_type(ct)
            self.assertEqual((c.exception.status_code, c.exception.code), (400, "invalid_request"))
            self.err(valid_body, 400, "invalid_request", ct=ct)

    def test_21_critical_boundary_smuggling_cases(self) -> None:
        with self.subTest(case="closing_boundary_in_headers"):
            body_smuggle_headers = (
                b"--BOUNDARY\r\n"
                b'Content-Disposition: form-data; name="file"\r\n'
                b"--BOUNDARY--\r\n\r\n"
                + PDF
                + b"\r\n--BOUNDARY--\r\n"
            )
            with self.assertRaises(MultipartError):
                parse_upload(CT, body_smuggle_headers, 10_000_000)

        with self.subTest(case="pdf_payload_containing_delimiter"):
            pdf_prefix = b"%PDF-1.4\nclean-prefix-pdf-data"
            injected_smuggle = (
                b"\r\n--BOUNDARY\r\n"
                b'Content-Disposition: form-data; name="logical_id"\r\n'
                b"\r\n"
                b"evil\r\n"
            )
            payload = pdf_prefix + injected_smuggle
            p = part("file", payload, filename="a.pdf", ctype="application/pdf")
            smuggle_body = b"--BOUNDARY\r\n" + p + b"--BOUNDARY--\r\n"
            try:
                res = parse_upload(CT, smuggle_body, 10_000_000)
            except MultipartError:
                pass
            else:
                self.assertEqual(res.pdf, pdf_prefix)
                self.assertEqual(res.logical_id, "evil")

    def test_22_memory_copy_and_immutability(self) -> None:
        five_mib = 5 * 1024 * 1024
        payload = b"%PDF-1.4\n" + (b"X" * (five_mib - 9))
        p = part("file", payload, filename="huge.pdf", ctype="application/pdf")
        body_bytes = build(p)
        body_ba = bytearray(body_bytes)
        before = bytes(body_ba)
        res = parse_upload(CT, body_ba, five_mib + 1024)
        after = bytes(body_ba)
        self.assertEqual(before, after)
        self.assertEqual(res.pdf, payload)
        self.assertIs(type(res.pdf), bytes)


    def test_part_count_is_capped_before_classification(self) -> None:
        """A huge body of tiny parts must stop at the cap, not after a Python
        loop over millions of parts. Six logical_id parts would otherwise be
        rejected later as 'duplicate logical_id'."""
        body = build(*[part("logical_id", b"x") for _ in range(6)])
        exc = self.assertRaisesMessage(body, "too many multipart parts")
        self.assertEqual((exc.status_code, exc.code), (400, "invalid_request"))
        # Four parts are still within the cap (rejected for another reason).
        four = build(part("logical_id", b"x"), part("file", PDF, "a.pdf", "application/pdf"),
                     part("logical_id", b"y"), part("logical_id", b"z"))
        self.assertNotEqual(self.assertRaisesMessage(four, None).message, "too many multipart parts")

    def test_preamble_and_wrong_opening_have_a_specific_error(self) -> None:
        valid = build(part("file", PDF, "a.pdf", "application/pdf"))
        for label, body in {
            "preamble text": b"junk\r\n" + valid,
            "leading CRLF": b"\r\n" + valid,
            "other boundary": valid.replace(b"BOUNDARY", b"OTHER", 1),
        }.items():
            with self.subTest(label):
                exc = self.assertRaisesMessage(body, "multipart body must start with the boundary")
                self.assertEqual((exc.status_code, exc.code), (400, "invalid_request"))

    def assertRaisesMessage(self, body: bytes, message):
        with self.assertRaises(MultipartError) as ctx:
            parse_upload(CT, body, 10_000_000)
        if message is not None:
            self.assertEqual(ctx.exception.message, message)
        return ctx.exception


    def test_whitespace_between_header_name_and_colon_is_rejected(self) -> None:
        """RFC 9110 5.1: `Content-Disposition : ...` is malformed, and the
        part's headers are never normalised into something acceptable."""
        for label, header in {
            "space before colon": b'Content-Disposition : form-data; name="file"; filename="a.pdf"\r\n',
            "tab before colon": b'Content-Disposition\t: form-data; name="file"; filename="a.pdf"\r\n',
        }.items():
            with self.subTest(label):
                body = (b"--BOUNDARY\r\n" + header + b"Content-Type: application/pdf\r\n\r\n" + PDF
                        + b"\r\n--BOUNDARY--\r\n")
                exc = self.assertRaisesMessage(body, "malformed part header")
                self.assertEqual((exc.status_code, exc.code), (400, "invalid_request"))
        # Whitespace after the colon stays fine.
        ok_body = (b'--BOUNDARY\r\nContent-Disposition:   form-data; name="file"; filename="a.pdf"\r\n'
                   b"Content-Type:application/pdf\r\n\r\n" + PDF + b"\r\n--BOUNDARY--\r\n")
        self.assertEqual(parse_upload(CT, ok_body, 10_000_000).pdf, PDF)


if __name__ == "__main__":
    unittest.main()
