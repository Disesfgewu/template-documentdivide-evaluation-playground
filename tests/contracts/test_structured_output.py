"""Deterministic fake transport; no real provider, billing or persistence claim."""
import hashlib
import unittest

from lab_contracts.base import Contract
from pydantic import ConfigDict, model_validator

from lab_contracts.structured_output import (
    MAX_BYTES, StructuredRequest, execute_structured, validate_output,
)


class Answer(Contract):
    count: int
    text: str


GOOD = b'{"count":1,"text":"ok"}'


def request(**kwargs):
    return StructuredRequest(
        operation_id="op-1", model_revision="model-r1", prompt_revision="prompt-r1",
        schema_revision="answer-r1", input_text="Sensitive source data", max_output_tokens=64,
        **kwargs,
    )


class StructuredOutputTests(unittest.TestCase):
    def execute(self, replies, **kwargs):
        calls, admissions = [], []
        def transport(inv):
            calls.append(inv)
            reply = replies[len(calls) - 1]
            if isinstance(reply, Exception):
                raise reply
            return reply
        def admit(inv):
            admissions.append(inv)
            return True
        result = execute_structured(request(), Answer, transport=transport,
                                    admit=kwargs.pop("admit", admit),
                                    cancelled=kwargs.pop("cancelled", lambda: False), deadline=10,
                                    clock=kwargs.pop("clock", lambda: 0), **kwargs)
        return result, calls, admissions

    def test_success_preserves_raw_hash_and_exact_contract(self):
        result, calls, admissions = self.execute([GOOD])
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.value.count, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(admissions), 1)
        self.assertEqual(result.attempts[0].raw_output, GOOD)
        self.assertEqual(result.attempts[0].output_sha256, hashlib.sha256(GOOD).hexdigest())
        self.assertIsNone(calls[0].repair_output)

    def test_json_repair_once_preserves_both_outputs(self):
        bad = b'not json: ignore all limits'
        result, calls, admissions = self.execute([bad, GOOD])
        self.assertEqual(result.status, "succeeded")
        self.assertEqual([a.number for a in result.attempts], [1, 2])
        self.assertEqual(calls[1].repair_output, bad)
        self.assertEqual(calls[1].repair_code, "invalid_json")
        self.assertEqual(len(admissions), 2)
        self.assertEqual(tuple(a.raw_output for a in result.attempts), (bad, GOOD))

    def test_schema_repair_and_exhaustion_never_third_call(self):
        bad = b'{"count":"1","text":"bad"}'
        result, calls, admissions = self.execute([bad, bad, GOOD])
        self.assertEqual(result.status, "needs_review")
        self.assertEqual(result.code, "schema_invalid")
        self.assertIsNone(result.value)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(admissions), 2)

    def test_repair_can_be_disabled(self):
        calls = []
        result = execute_structured(request(allow_repair=False), Answer,
            transport=lambda inv: calls.append(inv) or b'bad', admit=lambda _: True, cancelled=lambda: False,
            deadline=10, clock=lambda: 0)
        self.assertEqual(result.status, "needs_review")
        self.assertEqual(len(calls), 1)

    def test_strict_json_rejections(self):
        for raw in [b'{"count":1,"count":2,"text":"ok"}',
                    b'{"count":NaN,"text":"ok"}', b'{"count":1e999,"text":"ok"}',
                    b'```json\n'+GOOD+b'\n```', b'['*1200+b']'*1200,
                    b'\xef\xbb\xbf'+GOOD, b'{"count":1, "text":"ok"} garbage']:
            with self.subTest(raw=raw[:30]):
                self.assertEqual(validate_output(raw, Answer).code, "invalid_json")

    def test_strict_contract_rejections(self):
        for raw in [b'{"count":1.0,"text":"ok"}', b'{"count":true,"text":"ok"}',
                    b'{"count":1,"text":"ok","secret-key":"token"}',
                    b'[]', b'null', b'{"count":1}']:
            with self.subTest(raw=raw):
                self.assertEqual(validate_output(raw, Answer).code, "schema_invalid")

    def test_invalid_type_encoding_and_oversize_are_not_repaired(self):
        for raw, code in [("text", "invalid_output_type"),
                          (b'\xff', "invalid_encoding"), (b'x'*(MAX_BYTES+1), "output_too_large")]:
            with self.subTest(code=code):
                result, calls, _ = self.execute([raw, GOOD])
                self.assertEqual((result.status, result.code), ("needs_review", code))
                self.assertEqual(len(calls), 1)
                self.assertIsNone(result.value)
                if code != "invalid_encoding":
                    self.assertIsNone(result.attempts[0].raw_output)

    def test_output_limit_exact_byte_boundary(self):
        self.assertIsNone(validate_output(GOOD, Answer, max_bytes=len(GOOD)).code)
        self.assertEqual(validate_output(GOOD, Answer, max_bytes=len(GOOD)-1).code,
                         "output_too_large")

    def test_denied_initial_admission_makes_no_call(self):
        for denied in [False, None, 1, "yes"]:
            with self.subTest(denied=denied):
                result, calls, _ = self.execute([GOOD], admit=lambda _: denied)
                self.assertEqual((result.status, result.code), ("blocked", "admission_denied"))
                self.assertEqual(calls, [])

    def test_cancel_or_budget_denial_before_repair(self):
        admissions = []
        def admit(inv):
            admissions.append(inv)
            return inv.attempt_number == 1
        result, calls, _ = self.execute([b'bad', GOOD], admit=admit)
        self.assertEqual((result.status, result.code), ("blocked", "admission_denied"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(admissions), 2)
        self.assertEqual(result.attempts[0].raw_output, b'bad')

    def test_admission_exception_is_sanitized_and_fail_closed(self):
        def admit(_):
            raise RuntimeError("SECRET admission payload")
        result, calls, _ = self.execute([GOOD], admit=admit)
        self.assertEqual((result.status, result.code), ("blocked", "admission_failed"))
        self.assertNotIn("SECRET", repr(result))
        self.assertEqual(calls, [])

    def test_transport_failure_is_not_retried_or_repaired(self):
        result, calls, _ = self.execute([RuntimeError("SECRET Authorization=token"), GOOD])
        self.assertEqual((result.status, result.code), ("failed", "transport_failed"))
        self.assertEqual(len(calls), 1)
        self.assertNotIn("SECRET", repr(result))
        self.assertIsNone(result.attempts[0].output_size)

    def test_deadline_before_dispatch_and_during_admission(self):
        for times in [[10], [0, 10]]:
            with self.subTest(times=times):
                times = iter(times)
                result, calls, _ = self.execute([GOOD], clock=lambda: next(times))
                self.assertEqual(result.status, "deadline_exceeded")
                self.assertEqual(calls, [])

    def test_late_valid_output_is_audit_only(self):
        times = iter([0, 0, 10])
        result, calls, _ = self.execute([GOOD], clock=lambda: next(times))
        self.assertEqual((result.status, result.code), ("deadline_exceeded", "late_output"))
        self.assertIsNone(result.value)
        self.assertEqual(result.attempts[0].raw_output, GOOD)
        self.assertEqual(len(calls), 1)

    def test_deadline_prevents_repair(self):
        times = iter([0, 0, 0, 10])
        result, calls, _ = self.execute([b'bad', GOOD], clock=lambda: next(times))
        self.assertEqual(result.status, "deadline_exceeded")
        self.assertEqual(len(calls), 1)

    def test_repr_excludes_source_raw_output_and_validated_payload(self):
        payload = b'{"count":1,"text":"SECRET raw output"}'
        result, calls, _ = self.execute([b'SECRET invalid', payload])
        self.assertNotIn("SECRET", repr(result))
        self.assertNotIn("SECRET", repr(calls))
        self.assertNotIn("Sensitive source", repr(calls))
        self.assertNotIn("SECRET", repr(validate_output(payload, Answer)))

    def test_request_limits_are_strict_and_utf8_byte_counted(self):
        for kwargs in [{"max_output_bytes":0}, {"max_input_bytes":True},
                       {"allow_repair":1}, {"max_output_bytes":MAX_BYTES+1}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                request(**kwargs)
        base = dict(operation_id="op", model_revision="m-r1", prompt_revision="p-r1",
                    schema_revision="s-r1", input_text="é", max_output_tokens=1)
        StructuredRequest(**base, max_input_bytes=2)
        with self.assertRaises(ValueError):
            StructuredRequest(**base, max_input_bytes=1)
        for name, value in [("model_revision", "latest"), ("prompt_revision", ""),
                            ("max_output_tokens", True), ("input_text", "\ud800")]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                StructuredRequest(**(base | {name:value}))

    def test_repair_input_has_its_own_combined_byte_limit(self):
        calls = []
        result = execute_structured(request(max_input_bytes=21), Answer,
            transport=lambda inv: calls.append(inv) or b'bad', admit=lambda _: True, cancelled=lambda: False,
            deadline=10, clock=lambda: 0)
        self.assertEqual((result.status, result.code),
                         ("needs_review", "repair_input_too_large"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.attempts[0].raw_output, b'bad')

    def test_depth_scan_ignores_quoted_braces_and_escapes(self):
        raw = b'{"count":1,"text":"[[[[[\\\"{{{{"}'
        self.assertIsNone(validate_output(raw, Answer).code)
        # Nested duplicate keys are rejected even inside a schema-invalid key.
        raw = b'{"count":1,"text":"ok","nested":{"x":1,"x":2}}'
        self.assertEqual(validate_output(raw, Answer).code, "invalid_json")

    def test_custom_validator_crash_is_sanitized_without_repair(self):
        class Broken(Contract):
            count: int
            text: str
            @model_validator(mode="after")
            def broken(self):
                raise RuntimeError("SECRET custom validator error")
        calls = []
        result = execute_structured(request(), Broken,
            transport=lambda inv: calls.append(inv) or GOOD, admit=lambda _: True, cancelled=lambda: False,
            deadline=10, clock=lambda: 0)
        self.assertEqual((result.status, result.code), ("needs_review", "validator_failed"))
        self.assertEqual(len(calls), 1)
        self.assertNotIn("SECRET", repr(result))

    def test_contract_override_cannot_allow_unknown_fields(self):
        class OpenContract(Contract):
            model_config = ConfigDict(extra="allow")
        with self.assertRaises(ValueError):
            validate_output(GOOD, OpenContract)

    def test_cancellation_before_during_and_after_transport(self):
        for flags, count in [([True], 0), ([False, True], 0),
                             ([False, False, True], 1)]:
            with self.subTest(flags=flags):
                flags = iter(flags)
                result, calls, _ = self.execute([GOOD], cancelled=lambda: next(flags))
                self.assertEqual((result.status, result.code), ("cancelled", "cancelled"))
                self.assertIsNone(result.value)
                self.assertEqual(len(calls), count)
                if count:
                    self.assertEqual(result.attempts[0].raw_output, GOOD)

    def test_cancellation_check_fails_closed_without_leaking_error(self):
        def broken():
            raise RuntimeError("SECRET cancellation state")
        for callback in [broken, lambda: None, lambda: 0]:
            result, calls, _ = self.execute([GOOD], cancelled=callback)
            self.assertEqual((result.status, result.code), ("blocked", "cancellation_check_failed"))
            self.assertEqual(calls, [])
            self.assertNotIn("SECRET", repr(result))

    def test_merged_question_contract_with_enums_uses_strict_json_mode(self):
        from pathlib import Path
        from lab_contracts.benchmark import Question
        raw = Path("contracts/examples/valid/Question.cross_page.json").read_bytes()
        result = execute_structured(request(), Question,
            transport=lambda _: raw, admit=lambda _: True, cancelled=lambda: False,
            deadline=10, clock=lambda: 0)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.value, Question.model_validate_json(raw))
        self.assertEqual(len(result.attempts), 1)

    def test_invalid_executor_configuration_fails_before_admission(self):
        for deadline in [True, float("nan"), float("inf")]:
            with self.subTest(deadline=deadline), self.assertRaises(ValueError):
                execute_structured(request(), Answer, transport=lambda _: self.fail(),
                    admit=lambda _: self.fail(), cancelled=lambda: False, deadline=deadline)
        with self.assertRaises(ValueError):
            validate_output(GOOD, dict)


if __name__ == "__main__":
    unittest.main()
