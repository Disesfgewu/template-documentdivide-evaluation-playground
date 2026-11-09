"""Synthetic deterministic gate checks; no real verifier/human/parser claim."""
from __future__ import annotations

import hashlib
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from pydantic import ValidationError

from lab_contracts.verification import (
    MAX_IMAGE_BYTES, Risk, Verification, VisualSubject,
    check_visual_publication, subject_hash,
)

IMAGE = b'synthetic committed image bytes: decoder is upstream'


def subject():
    return VisualSubject.model_validate_json(json.dumps({
        'subject_id': 'visual-1', 'revision': 'revision-1',
        'producer_operation_ids': ['produce-1'], 'risks': [], 'unresolved': False,
        'documents': [{'document_id': 'doc1', 'sha256': 'a' * 64, 'page_count': 300}],
        'evidence': [{
            'evidence_id': 'e1', 'regions': [{
                'document_id': 'doc1', 'document_sha256': 'a' * 64,
                'page_index': 18, 'bbox': [.1, .2, .8, .9],
                'kind': 'figure', 'quote': None}],
            'raw_content': 'observed label 12', 'normalized_content': 'observed label 12',
            'extractor_revision': 'parser-1', 'ocr_confidence': None,
            'asset_refs': ['image-1']}],
        'images': [{'artifact_id': 'image-1', 'sha256': hashlib.sha256(IMAGE).hexdigest(),
                    'size_bytes': len(IMAGE), 'region_refs': [{'evidence_id': 'e1', 'region_index': 0}]}],
    }))


def record(s, **overrides):
    values = dict(verification_id='v1', subject_sha256=subject_hash(s),
                  operation_id='verify-1', oracle_revision='verifier-1',
                  oracle='automated', verdict='pass', reason='checked original image',
                  checked_evidence_ids=['e1'], checked_image_refs=['image-1'],
                  checked_risks=list(s.risks), additional_risks=[], reviewer_id=None,
                  reviewed_at=datetime(2026, 11, 7, tzinfo=timezone.utc))
    values.update(overrides)
    return Verification(**values)


def check(s, records, reader=None, authorize=None):
    return check_visual_publication(s, records, reader or (lambda i, limit: IMAGE),
                                    authorize or (lambda r: True))


class TestVisualVerification(unittest.TestCase):
    def codes(self, result):
        return {p.code for p in result.problems}

    def test_independent_low_risk_visual_is_eligible_without_mutation(self):
        s = subject()
        before = s.model_dump(mode='json')
        result = check(s, [record(s)])
        self.assertTrue(result.eligible)
        self.assertEqual(result.verification_ids, ('v1',))
        self.assertEqual(result.subject_sha256, subject_hash(s))
        self.assertEqual(s.model_dump(mode='json'), before)

    def test_every_high_risk_requires_bound_human_record(self):
        for risk in Risk:
            with self.subTest(risk=risk):
                s = subject().model_copy(update={'risks': [risk]})
                result = check(s, [record(s)])
                self.assertFalse(result.eligible)
                self.assertIn('human_verification_required', self.codes(result))
                human = record(s, oracle='human', reviewer_id='reviewer-1')
                self.assertTrue(check(s, [human]).eligible)

    def test_high_confidence_cannot_skip_human_requirement(self):
        s = subject().model_copy(update={'risks': [Risk.estimated_value]})
        s.evidence[0] = s.evidence[0].model_copy(update={'ocr_confidence': 1.0})
        self.assertFalse(check(s, [record(s)]).eligible)
        with self.assertRaises(ValidationError):
            record(s, confidence=1.0)

    def test_no_review_and_producer_self_attestation_block(self):
        s = subject()
        self.assertIn('independent_verification_required', self.codes(check(s, [])))
        result = check(s, [record(s, operation_id='produce-1')])
        self.assertIn('verifier_not_independent', self.codes(result))
        self.assertFalse(result.eligible)

    def test_authenticated_record_and_role_check_is_required(self):
        s = subject()
        calls = []
        def deny(r):
            calls.append((r.verification_id, r.oracle, r.reviewer_id))
            return False
        result = check(s, [record(s, oracle='human', reviewer_id='reviewer-1')], authorize=deny)
        self.assertEqual(calls, [('v1', 'human', 'reviewer-1')])
        self.assertIn('verification_unauthorized', self.codes(result))
        self.assertFalse(result.eligible)

    def test_authorization_error_and_truthy_non_boolean_fail_closed(self):
        s = subject()
        def error(r):
            raise RuntimeError('secret backend failure')
        for authorizer in [error, lambda r: 1, lambda r: 'yes']:
            result = check(s, [record(s)], authorize=authorizer)
            self.assertFalse(result.eligible)
            self.assertNotIn('secret', repr(result))

    def test_verifier_reported_new_risk_requires_new_subject_revision(self):
        s = subject()
        result = check(s, [record(s, additional_risks=[Risk.dual_axis])])
        self.assertIn('risk_revision_required', self.codes(result))
        updated = s.model_copy(update={'revision': 'revision-2', 'risks': [Risk.dual_axis]})
        self.assertFalse(check(updated, [record(s)]).eligible)
        self.assertTrue(check(updated, [record(updated, oracle='human', reviewer_id='r')]).eligible)

    def test_negative_record_blocks_even_with_passing_human(self):
        s = subject()
        passed = record(s, verification_id='human', operation_id='human-op',
                        oracle='human', reviewer_id='r')
        for verdict in ['fail', 'needs_review']:
            result = check(s, [record(s, verdict=verdict), passed])
            self.assertIn('verification_' + verdict, self.codes(result))
            self.assertFalse(result.eligible)

    def test_unresolved_conflict_blocks_even_human_pass(self):
        s = subject().model_copy(update={'unresolved': True, 'risks': [Risk.conflict]})
        result = check(s, [record(s, oracle='human', reviewer_id='r')])
        self.assertIn('unresolved_evidence', self.codes(result))
        self.assertFalse(result.eligible)

    def test_revision_region_raw_content_and_image_hash_invalidate_review(self):
        original = subject()
        old = record(original)
        for change in ['revision', 'region', 'content', 'image']:
            s = original.model_copy(deep=True)
            if change == 'revision':
                s = s.model_copy(update={'revision': 'revision-2'})
            elif change == 'region':
                s.evidence[0].regions[0] = s.evidence[0].regions[0].model_copy(update={'page_index': 247})
            elif change == 'content':
                s.evidence[0] = s.evidence[0].model_copy(update={'raw_content': 'different'})
            else:
                s.images[0] = s.images[0].model_copy(update={'sha256': 'b' * 64})
            with self.subTest(change=change):
                self.assertNotEqual(subject_hash(s), subject_hash(original))
                self.assertIn('verification_snapshot_mismatch', self.codes(check(s, [old])))

    def test_all_constituent_regions_and_all_images_required(self):
        s = subject()
        second = s.evidence[0].model_copy(deep=True)
        second = second.model_copy(update={'evidence_id': 'e2', 'asset_refs': ['image-2']})
        second.regions[0] = second.regions[0].model_copy(update={'page_index': 247})
        image = s.images[0].model_copy(update={'artifact_id': 'image-2', 'region_refs': [s.images[0].region_refs[0].model_copy(update={'evidence_id': 'e2'})]})
        s = s.model_copy(update={'evidence': [s.evidence[0], second], 'images': [s.images[0], image]})
        one_only = record(s)
        self.assertIn('verification_scope_mismatch', self.codes(check(s, [one_only])))
        complete = record(s, checked_evidence_ids=['e1', 'e2'], checked_image_refs=['image-1', 'image-2'])
        self.assertTrue(check(s, [complete]).eligible)
        self.assertEqual([e.regions[0].page_index for e in s.evidence], [18, 247])

    def test_missing_corrupt_empty_oversized_nonbytes_images_block(self):
        s = subject()
        def missing(i, limit):
            raise FileNotFoundError('private-path-secret')
        for reader, code in [(missing, 'image_unavailable'), (lambda i, n: b'', 'image_size_mismatch'),
                             (lambda i, n: IMAGE + b'x', 'image_size_mismatch'),
                             (lambda i, n: bytearray(IMAGE), 'image_size_mismatch'),
                             (lambda i, n: b'x' * len(IMAGE), 'image_hash_mismatch')]:
            with self.subTest(code=code):
                result = check(s, [record(s)], reader)
                self.assertIn(code, self.codes(result))
                self.assertFalse(result.eligible)
                self.assertNotIn('private-path-secret', repr(result))

    def test_reader_receives_exact_identity_and_bound(self):
        s = subject()
        calls = []
        def reader(identity, bound):
            calls.append((identity, bound))
            return IMAGE
        self.assertTrue(check(s, [record(s)], reader).eligible)
        self.assertEqual(calls, [('image-1', len(IMAGE) + 1)])

    def test_callback_mutation_cannot_change_snapshot_or_scope(self):
        s = subject()
        r = record(s)
        original_hash = subject_hash(s)
        def authorizer(copy_record):
            copy_record.checked_image_refs.clear()
            return True
        def reader(i, n):
            s.evidence.clear()
            r.checked_image_refs.clear()
            return IMAGE
        result = check(s, [r], reader, authorizer)
        self.assertTrue(result.eligible)
        self.assertEqual(result.subject_sha256, original_hash)

    def test_unknown_duplicate_and_missing_references_rejected(self):
        s = subject()
        for fields in [dict(images=[]), dict(evidence=[]), dict(images=s.images * 2),
                       dict(evidence=s.evidence * 2), dict(risks=[Risk.dual_axis] * 2),
                       dict(producer_operation_ids=['p', 'p'])]:
            with self.subTest(fields=tuple(fields)):
                with self.assertRaises(ValidationError):
                    subject_hash(s.model_copy(update=fields))
        image = s.images[0].model_copy(update={'region_refs': [s.images[0].region_refs[0].model_copy(update={'evidence_id': 'unknown'})]})
        with self.assertRaises(ValidationError):
            subject_hash(s.model_copy(update={'images': [image]}))
        evidence = s.evidence[0].model_copy(update={'asset_refs': []})
        with self.assertRaises(ValidationError):
            subject_hash(s.model_copy(update={'evidence': [evidence]}))

    def test_strict_types_finiteness_and_construct_bypass_revalidated(self):
        s = subject()
        for value in ['false', 0, None]:
            with self.assertRaises(ValueError):
                subject_hash(s.model_copy(update={'unresolved': value}))
        for size in [True, 1.0, 0, MAX_IMAGE_BYTES + 1]:
            with self.assertRaises(ValueError):
                subject_hash(s.model_copy(update={'images': [s.images[0].model_copy(update={'size_bytes': size})]}))
        region = s.evidence[0].regions[0].model_copy(update={'bbox': (float('nan'), .2, .8, .9)})
        s.evidence[0].regions[0] = region
        with self.assertRaises(ValueError):
            subject_hash(s)

    def test_duplicate_reviews_and_human_identity_pair_rejected(self):
        s = subject()
        r = record(s)
        for records in [[r, r], [r, record(s, verification_id='v2')]]:
            with self.assertRaises(ValueError):
                check(s, records)
        for fields in [dict(oracle='human'), dict(reviewer_id='fake-human')]:
            with self.assertRaises(ValidationError):
                record(s, **fields)
        self.assertIn('verification_scope_mismatch', self.codes(check(s, [record(s, checked_image_refs=['unknown'])])))

    def test_document_hash_identity_and_physical_page_scope(self):
        for field, value in [('document_id', 'unknown'), ('document_sha256', 'b' * 64),
                             ('page_index', 300), ('page_index', -1)]:
            s = subject()
            s.evidence[0].regions[0] = s.evidence[0].regions[0].model_copy(update={field: value})
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    subject_hash(s)

    def test_each_crosspage_visual_constituent_requires_image_mapping(self):
        s = subject()
        second = s.evidence[0].regions[0].model_copy(update={'page_index': 247})
        s.evidence[0].regions.append(second)
        with self.assertRaises(ValueError):
            subject_hash(s)
        second_ref = s.images[0].region_refs[0].model_copy(update={'region_index': 1})
        s.images[0].region_refs.append(second_ref)
        self.assertTrue(check(s, [record(s)]).eligible)
        self.assertEqual([r.page_index for r in s.evidence[0].regions], [18, 247])

    def test_unmapped_extra_asset_and_image_ref_index_rejected(self):
        s = subject()
        s.evidence[0].asset_refs.append('unavailable-extra')
        with self.assertRaises(ValueError):
            subject_hash(s)
        for index in [True, -1, 1, 128]:
            s = subject()
            s.images[0].region_refs[0] = s.images[0].region_refs[0].model_copy(update={'region_index': index})
            with self.subTest(index=index):
                with self.assertRaises(ValueError):
                    subject_hash(s)

    def test_nested_constructed_reference_lists_rejected_before_callbacks(self):
        s = subject()
        image = s.images[0].model_copy(update={'region_refs': s.images[0].region_refs * 129})
        with self.assertRaises(ValueError):
            subject_hash(s.model_copy(update={'images': [image]}))
        for field in ['checked_evidence_ids', 'checked_image_refs', 'checked_risks', 'additional_risks']:
            r = record(s).model_copy(update={field: ['x'] * 129})
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    check(s, [r], lambda i, n: self.fail('must not read image'))

    def test_constructed_scalar_containers_rejected_before_serialization(self):
        huge = ['x'] * 100000
        for field in ['unresolved', 'page_count', 'size_bytes', 'region_index',
                      'page_index', 'ocr_confidence', 'kind', 'bbox']:
            s = subject()
            if field == 'unresolved':
                s = s.model_copy(update={field: huge})
            elif field == 'page_count':
                s.documents[0] = s.documents[0].model_copy(update={field: huge})
            elif field == 'size_bytes':
                s.images[0] = s.images[0].model_copy(update={field: huge})
            elif field == 'region_index':
                s.images[0].region_refs[0] = s.images[0].region_refs[0].model_copy(update={field: huge})
            elif field == 'ocr_confidence':
                s.evidence[0] = s.evidence[0].model_copy(update={field: huge})
            else:
                s.evidence[0].regions[0] = s.evidence[0].regions[0].model_copy(update={field: huge})
            with self.subTest(field=field):
                with patch.object(VisualSubject, 'model_dump', side_effect=AssertionError('must preflight')):
                    with self.assertRaises(ValueError):
                        subject_hash(s)
        s = subject()
        for timestamp in [huge, datetime(2026, 11, 7)]:
            r = record(s).model_copy(update={'reviewed_at': timestamp})
            with patch.object(Verification, 'model_dump', side_effect=AssertionError('must preflight')):
                with self.assertRaises(ValueError):
                    check(s, [r])

    def test_bounded_records_references_and_content(self):
        s = subject()
        with self.assertRaises(ValueError):
            check(s, [record(s)] * 129)
        with self.assertRaises(ValueError):
            subject_hash(s.model_copy(update={'evidence': s.evidence * 129}))
        huge = s.evidence[0].model_copy(update={'raw_content': 'x' * (1024 * 1024 + 1)})
        with self.assertRaises(ValueError):
            subject_hash(s.model_copy(update={'evidence': [huge]}))


if __name__ == '__main__':
    unittest.main()
