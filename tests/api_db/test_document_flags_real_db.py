"""Listed reader notes and a missing text layer are accepted AND recorded on the document version; real rejections say why in words (Issues #204 and #203)."""
from __future__ import annotations

import hashlib
import unittest
import uuid

from tests.api_db.test_promote_real_db import ADMIN_URL, FreshDatabase, PromoteFixtures
from tests.parser.pdfgen import build_pdf
from tests.parser.test_pdf_flags import non_zero_indexed
from fixtures.generators import synthetic_pdf


@unittest.skipUnless(ADMIN_URL, "LAB_TEST_DATABASE_URL not set")
class DocumentFlagsRealDbTests(PromoteFixtures, FreshDatabase):
    def upload(self, content, name=None):
        with self.connect() as c:
            project, owner = self.make_project(c)
        res = self.client.post(f"/api/v1/documents?project_id={project}&logical_id={name or 'f' + uuid.uuid4().hex[:6]}",
                               headers=self.headers(owner, **{"Content-Type": "application/pdf"}), content=content)
        return project, owner, res

    def flags_in_db(self, doc):
        with self.connect() as c:
            return [code for (code,) in c.execute("SELECT code FROM document_version_flags WHERE document_version_id = %s ORDER BY code", (doc,))]

    # --- the listed notes -------------------------------------------------------------------------------------------------------------
    def test_the_paperport_shape_is_accepted_recorded_and_stored_byte_for_byte(self):
        original = non_zero_indexed(synthetic_pdf.build(3)) + b"%PaperPortPDFversion 11\n"
        project, owner, res = self.upload(original)
        self.assertEqual(res.status_code, 201, res.text)
        body = res.json()
        self.assertTrue(body["flagged"])
        self.assertEqual(sorted(body["flags"]), ["trailing_bytes_after_eof", "xref_not_zero_indexed"])
        self.assertEqual(body["sha256"], hashlib.sha256(original).hexdigest())                       # the sha of the ORIGINAL bytes
        doc = uuid.UUID(body["id"])
        self.assertEqual(self.flags_in_db(doc), ["trailing_bytes_after_eof", "xref_not_zero_indexed"])
        got = self.client.get(f"/api/v1/documents/{doc}/original", headers=self.headers(owner))
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.content, original)                                                      # no silent repair
        ws = self.client.get(f"/api/v1/workspace?project_id={project}", headers=self.headers(owner)).json()
        self.assertEqual(sorted(ws["documents"][0]["flags"]), ["trailing_bytes_after_eof", "xref_not_zero_indexed"])

    def test_a_clean_document_has_no_flags(self):
        project, owner, res = self.upload(synthetic_pdf.build(3))
        self.assertEqual(res.status_code, 201, res.text)
        self.assertEqual((res.json()["flagged"], res.json()["flags"]), (False, []))
        self.assertEqual(self.flags_in_db(uuid.UUID(res.json()["id"])), [])

    def test_the_flags_are_append_only(self):
        import psycopg
        project, owner, res = self.upload(non_zero_indexed(synthetic_pdf.build(3)))
        doc = uuid.UUID(res.json()["id"])
        with self.connect() as c:
            with self.assertRaises(psycopg.errors.IntegrityConstraintViolation):
                c.execute("DELETE FROM document_version_flags WHERE document_version_id = %s", (doc,))

    # --- real rejections say why in words -------------------------------------------------------------------------------------------------
    def test_a_truncated_file_is_unreadable_and_says_so(self):
        project, owner, res = self.upload(synthetic_pdf.build(3)[:200])
        self.assertEqual(res.status_code, 422)
        err = res.json()["error"]
        self.assertEqual(err["code"], "corrupt_pdf")
        self.assertIn("cannot be read", err["message"])

    def test_a_readable_file_with_an_unlisted_warning_has_its_own_code_and_words(self):
        data = synthetic_pdf.build(3).replace(b"0000000015 00000 n", b"0000000016 00000 n", 1)
        project, owner, res = self.upload(data)
        self.assertEqual(res.status_code, 422)
        err = res.json()["error"]
        self.assertIn(err["code"], ("pdf_parser_warning", "corrupt_pdf"))
        if err["code"] == "pdf_parser_warning":
            self.assertIn("not on the list of harmless ones", err["message"])
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM document_versions WHERE project_id = %s", (project,)).fetchone()[0], 0)       # nothing stored

    # --- no text layer ------------------------------------------------------------------------------------------------------------------
    def test_a_document_without_a_text_layer_is_accepted_flagged_and_the_plan_says_so(self):
        project, owner, res = self.upload(build_pdf([{"mediabox": (0, 0, 595, 842)}] * 3))
        self.assertEqual(res.status_code, 201, res.text)
        self.assertIn("no_text_layer", res.json()["flags"])
        doc = uuid.UUID(res.json()["id"])
        job = self.parsed(project, owner, doc, res.json()["sha256"], pages_with_text=())
        plan = self.plan_of(project, owner, doc)
        self.assertEqual(plan.status_code, 200, plan.text)
        self.assertTrue(plan.json()["no_text_layer"])
        self.assertIn("no_text_layer", plan.json()["document_flags"])

    def plan_of(self, project, owner, doc):
        made = self.client.post(f"/api/v1/benchmarks?project_id={project}", headers=self.headers(owner), json={"document_version_id": str(doc), "logical_id": "bench-flags"})
        self.assertEqual(made.status_code, 201, made.text)
        return self.client.post(f"/api/v1/benchmarks/bench-flags/revisions/1/plan?project_id={project}", headers=self.headers(owner), json={})

    def test_a_text_document_whose_parse_found_no_text_still_gets_the_plan_notice(self):
        project, owner, doc, sha = self.seed()                                        # a document WITH a text layer at upload
        job = self.parsed(project, owner, doc, sha, pages_with_text=())               # ... but the parse produced no text evidence
        plan = self.plan_of(project, owner, doc)
        self.assertEqual(plan.status_code, 200, plan.text)
        self.assertTrue(plan.json()["no_text_layer"])
        self.assertEqual(plan.json()["document_flags"], [])

    def test_a_normal_plan_has_no_notice(self):
        project, owner, doc, sha = self.seed()
        self.parsed(project, owner, doc, sha)
        plan = self.plan_of(project, owner, doc)
        self.assertEqual(plan.status_code, 200, plan.text)
        self.assertFalse(plan.json()["no_text_layer"])


if __name__ == "__main__":
    unittest.main()
