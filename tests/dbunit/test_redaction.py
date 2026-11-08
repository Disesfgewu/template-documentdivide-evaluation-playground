"""Redaction is pure Python; test it without a database."""
import unittest

from lab_db.logs import MASK, redact, redact_value


class RedactValueTests(unittest.TestCase):
    def test_nested_secret_keys_and_values(self):
        out = redact_value({"headers": {"Authorization": "Basic abc", "X-Trace": "t1"},
                            "items": ["ok", "url https://u:pw12345@host/x"], "n": 3})
        self.assertEqual(out["headers"]["Authorization"], MASK)
        self.assertEqual(out["headers"]["X-Trace"], "t1")
        self.assertNotIn("pw12345", out["items"][1])
        self.assertEqual(out["n"], 3)

    def test_plain_text_untouched(self):
        self.assertEqual(redact("page 3 of 10, value 42 ms"), "page 3 of 10, value 42 ms")


if __name__ == "__main__":
    unittest.main()
