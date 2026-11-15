"""Unit tests for audit export (SPEC 10.3)."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import unittest

from lab_db.export import export_rows
from lab_db.logs import redact_value


class TestExportRows(unittest.TestCase):
    def setUp(self):
        self.t_start = datetime(2026, 11, 5, 10, 0, 0, tzinfo=timezone.utc)
        self.t_end = datetime(2026, 11, 5, 18, 0, 0, tzinfo=timezone.utc)
        self.schema = "audit_events_v1"
        self.exporter = "test_user_01"
        self.filters = {"action": "login", "status": "success"}

    def test_jsonl_column_order_and_missing_values(self):
        """JSONL: verify column order preservation, missing fields as null, and ignoring extra fields."""
        columns = ["col_z", "col_a", "col_m"]
        rows = [
            # Row 1: has all columns plus an extra field; dictionary insertion order is scrambled
            {"col_a": "alpha", "extra_col": "should_be_ignored", "col_m": "mike", "col_z": "zulu"},
            # Row 2: lacks col_z and col_m
            {"col_a": "alpha_only"},
            # Row 3: lacks all columns
            {"unrelated": 123},
        ]

        data_bytes, manifest = export_rows(
            rows,
            fmt="jsonl",
            schema=self.schema,
            columns=columns,
            exporter=self.exporter,
            time_range=(self.t_start, self.t_end),
            filters=self.filters,
            masked_fields=[],
        )

        lines = data_bytes.decode("utf-8").splitlines(keepends=True)
        self.assertEqual(len(lines), 3)
        for line in lines:
            self.assertTrue(line.endswith("\n"))

        # Verify row 1
        obj1 = json.loads(lines[0])
        self.assertEqual(list(obj1.keys()), columns)
        self.assertEqual(obj1, {"col_z": "zulu", "col_a": "alpha", "col_m": "mike"})
        self.assertNotIn("extra_col", obj1)

        # Verify row 2 (col_z and col_m are missing -> null)
        obj2 = json.loads(lines[1])
        self.assertEqual(list(obj2.keys()), columns)
        self.assertIsNone(obj2["col_z"])
        self.assertEqual(obj2["col_a"], "alpha_only")
        self.assertIsNone(obj2["col_m"])

        # Verify row 3 (all columns missing -> null)
        obj3 = json.loads(lines[2])
        self.assertEqual(list(obj3.keys()), columns)
        self.assertEqual(obj3, {"col_z": None, "col_a": None, "col_m": None})

    def test_csv_formula_injection(self):
        """CSV: verify formula injection mitigation on dangerous string prefixes, and non-strings unescaped."""
        columns = [
            "c_eq",
            "c_space_minus",
            "c_at",
            "c_plus",
            "c_normal",
            "c_neg_num",
            "c_tab",
            "c_cr",
            "c_space_tab",
            "c_space_cr",
            "c_pos_num",
            "c_float_neg",
            "c_bool_true",
            "c_bool_false",
        ]
        row = {
            "c_eq": "=1+1",
            "c_space_minus": " -cmd",
            "c_at": "@SUM(A1)",
            "c_plus": "+x",
            "c_normal": "普通字串",
            "c_neg_num": -3,
            "c_tab": "\tcalc",
            "c_cr": "\rcmd",
            "c_space_tab": " \tcmd",
            "c_space_cr": " \rcmd",
            "c_pos_num": 100,
            "c_float_neg": -3.14,
            "c_bool_true": True,
            "c_bool_false": False,
        }

        data_bytes, manifest = export_rows(
            [row],
            fmt="csv",
            schema=self.schema,
            columns=columns,
            exporter=self.exporter,
            time_range=(self.t_start, self.t_end),
            filters=self.filters,
            masked_fields=[],
        )

        csv_text = data_bytes.decode("utf-8")
        reader = list(csv.reader(io.StringIO(csv_text)))
        self.assertEqual(len(reader), 2)  # Header + 1 data row

        header, data_row = reader[0], reader[1]
        self.assertEqual(header, columns)

        row_dict = dict(zip(header, data_row))

        # Strings with dangerous prefixes must be prepended with single quote (')
        self.assertEqual(row_dict["c_eq"], "'=1+1")
        self.assertEqual(row_dict["c_space_minus"], "' -cmd")
        self.assertEqual(row_dict["c_at"], "'@SUM(A1)")
        self.assertEqual(row_dict["c_plus"], "'+x")
        self.assertEqual(row_dict["c_tab"], "'\tcalc")
        self.assertEqual(row_dict["c_cr"], "'\rcmd")
        self.assertEqual(row_dict["c_space_tab"], "' \tcmd")
        self.assertEqual(row_dict["c_space_cr"], "' \rcmd")

        # Normal strings must NOT be prepended
        self.assertEqual(row_dict["c_normal"], "普通字串")

        # Non-string values (numbers, booleans) must NOT be prepended with single quote
        self.assertEqual(row_dict["c_neg_num"], "-3")
        self.assertEqual(row_dict["c_pos_num"], "100")
        self.assertEqual(row_dict["c_float_neg"], "-3.14")
        self.assertEqual(row_dict["c_bool_true"], "True")
        self.assertEqual(row_dict["c_bool_false"], "False")

    def test_masked_fields(self):
        """Verify fields in masked_fields are unconditionally replaced with '[REDACTED]' in JSONL and CSV."""
        columns = ["username", "password", "token", "notes"]
        masked = ["password", "token"]
        row = {
            "username": "martin",
            "password": "super_secret_password_123",
            "token": "ghp_abcdef1234567890",
            "notes": "public information",
        }

        # Test JSONL
        jsonl_bytes, _ = export_rows(
            [row],
            fmt="jsonl",
            schema=self.schema,
            columns=columns,
            exporter=self.exporter,
            time_range=(self.t_start, self.t_end),
            filters=self.filters,
            masked_fields=masked,
        )
        jsonl_obj = json.loads(jsonl_bytes.decode("utf-8").strip())
        self.assertEqual(jsonl_obj["username"], "martin")
        self.assertEqual(jsonl_obj["password"], "[REDACTED]")
        self.assertEqual(jsonl_obj["token"], "[REDACTED]")
        self.assertEqual(jsonl_obj["notes"], "public information")
        self.assertNotIn("super_secret_password_123", jsonl_bytes.decode("utf-8"))
        self.assertNotIn("ghp_abcdef1234567890", jsonl_bytes.decode("utf-8"))

        # Test CSV
        csv_bytes, _ = export_rows(
            [row],
            fmt="csv",
            schema=self.schema,
            columns=columns,
            exporter=self.exporter,
            time_range=(self.t_start, self.t_end),
            filters=self.filters,
            masked_fields=masked,
        )
        csv_rows = list(csv.reader(io.StringIO(csv_bytes.decode("utf-8"))))
        csv_dict = dict(zip(csv_rows[0], csv_rows[1]))
        self.assertEqual(csv_dict["username"], "martin")
        self.assertEqual(csv_dict["password"], "[REDACTED]")
        self.assertEqual(csv_dict["token"], "[REDACTED]")
        self.assertEqual(csv_dict["notes"], "public information")

    def test_redact_value_takes_effect(self):
        """Verify redact_value processes unmasked fields (e.g. 'Authorization: Bearer abcdefghijkl' is redacted)."""
        columns = ["event_id", "headers"]
        # 'headers' is NOT in masked_fields, so it must be processed by redact_value
        sensitive_token = "abcdefghijkl"
        auth_string = f"Authorization: Bearer {sensitive_token}"
        row = {"event_id": "evt_001", "headers": auth_string}

        # JSONL
        jsonl_bytes, _ = export_rows(
            [row],
            fmt="jsonl",
            schema=self.schema,
            columns=columns,
            exporter=self.exporter,
            time_range=(self.t_start, self.t_end),
            filters=self.filters,
            masked_fields=[],
        )
        jsonl_text = jsonl_bytes.decode("utf-8")
        jsonl_obj = json.loads(jsonl_text.strip())
        self.assertEqual(jsonl_obj["event_id"], "evt_001")
        self.assertEqual(jsonl_obj["headers"], redact_value(auth_string))
        self.assertNotIn(sensitive_token, jsonl_text)

        # CSV
        csv_bytes, _ = export_rows(
            [row],
            fmt="csv",
            schema=self.schema,
            columns=columns,
            exporter=self.exporter,
            time_range=(self.t_start, self.t_end),
            filters=self.filters,
            masked_fields=[],
        )
        csv_text = csv_bytes.decode("utf-8")
        csv_rows = list(csv.reader(io.StringIO(csv_text)))
        csv_dict = dict(zip(csv_rows[0], csv_rows[1]))
        self.assertEqual(csv_dict["headers"], redact_value(auth_string))
        self.assertNotIn(sensitive_token, csv_text)

    def test_manifest_metadata_and_hash_consistency(self):
        """Verify manifest row_count, sha256 hex digest, and metadata consistency."""
        columns = ["id", "val"]
        rows = [{"id": 1, "val": "one"}, {"id": 2, "val": "two"}, {"id": 3, "val": "three"}]

        # JSONL format
        jsonl_bytes, manifest_jsonl = export_rows(
            rows,
            fmt="jsonl",
            schema="audit_events_v2",
            columns=columns,
            exporter="auditor_bob",
            time_range=(self.t_start, self.t_end),
            filters={"source": "api"},
            masked_fields=["val"],
        )

        self.assertEqual(manifest_jsonl["schema"], "audit_events_v2")
        self.assertEqual(manifest_jsonl["format"], "jsonl")
        self.assertEqual(manifest_jsonl["columns"], columns)
        self.assertEqual(manifest_jsonl["exporter"], "auditor_bob")
        self.assertEqual(
            manifest_jsonl["time_range"],
            (self.t_start.astimezone(timezone.utc).isoformat(), self.t_end.astimezone(timezone.utc).isoformat()),
        )
        self.assertEqual(manifest_jsonl["filters"], {"source": "api"})
        self.assertEqual(manifest_jsonl["row_count"], 3)
        self.assertEqual(manifest_jsonl["sha256"], hashlib.sha256(jsonl_bytes).hexdigest())
        self.assertEqual(manifest_jsonl["masked_fields"], ["val"])

        gen_at = datetime.fromisoformat(manifest_jsonl["generated_at"])
        self.assertIsNotNone(gen_at.tzinfo)

        # CSV format with empty rows
        csv_bytes, manifest_csv = export_rows(
            [],
            fmt="csv",
            schema="audit_events_v2",
            columns=columns,
            exporter="auditor_bob",
            time_range=(self.t_start, self.t_end),
            filters={},
            masked_fields=[],
        )
        self.assertEqual(manifest_csv["row_count"], 0)
        self.assertEqual(manifest_csv["sha256"], hashlib.sha256(csv_bytes).hexdigest())
        self.assertEqual(manifest_csv["format"], "csv")

    def test_naive_datetime_and_invalid_time_range_exceptions(self):
        """Verify ValueError is raised on naive datetimes and invalid time ranges or columns."""
        valid_columns = ["id", "timestamp"]

        # 1. time_range start later than end
        with self.assertRaises(ValueError):
            export_rows(
                [],
                fmt="jsonl",
                schema=self.schema,
                columns=valid_columns,
                exporter=self.exporter,
                time_range=(self.t_end, self.t_start),  # start > end
                filters=self.filters,
                masked_fields=[],
            )

        # 2. time_range start is naive datetime
        naive_dt = datetime(2026, 11, 5, 10, 0, 0)
        with self.assertRaises(ValueError):
            export_rows(
                [],
                fmt="jsonl",
                schema=self.schema,
                columns=valid_columns,
                exporter=self.exporter,
                time_range=(naive_dt, self.t_end),
                filters=self.filters,
                masked_fields=[],
            )

        # 3. time_range end is naive datetime
        with self.assertRaises(ValueError):
            export_rows(
                [],
                fmt="jsonl",
                schema=self.schema,
                columns=valid_columns,
                exporter=self.exporter,
                time_range=(self.t_start, naive_dt),
                filters=self.filters,
                masked_fields=[],
            )

        # 4. Naive datetime inside a row (JSONL)
        with self.assertRaises(ValueError):
            export_rows(
                [{"id": 1, "timestamp": naive_dt}],
                fmt="jsonl",
                schema=self.schema,
                columns=valid_columns,
                exporter=self.exporter,
                time_range=(self.t_start, self.t_end),
                filters=self.filters,
                masked_fields=[],
            )

        # 5. Naive datetime inside a row (CSV)
        with self.assertRaises(ValueError):
            export_rows(
                [{"id": 1, "timestamp": naive_dt}],
                fmt="csv",
                schema=self.schema,
                columns=valid_columns,
                exporter=self.exporter,
                time_range=(self.t_start, self.t_end),
                filters=self.filters,
                masked_fields=[],
            )

        # 6. Empty columns list
        with self.assertRaises(ValueError):
            export_rows(
                [],
                fmt="jsonl",
                schema=self.schema,
                columns=[],
                exporter=self.exporter,
                time_range=(self.t_start, self.t_end),
                filters=self.filters,
                masked_fields=[],
            )

        # 7. Duplicate columns
        with self.assertRaises(ValueError):
            export_rows(
                [],
                fmt="jsonl",
                schema=self.schema,
                columns=["id", "name", "id"],
                exporter=self.exporter,
                time_range=(self.t_start, self.t_end),
                filters=self.filters,
                masked_fields=[],
            )

        # 8. Unsupported export format
        with self.assertRaises(ValueError):
            export_rows(
                [],
                fmt="xml",  # type: ignore
                schema=self.schema,
                columns=valid_columns,
                exporter=self.exporter,
                time_range=(self.t_start, self.t_end),
                filters=self.filters,
                masked_fields=[],
            )

    def test_csv_round_trip_and_column_count_consistency(self):
        """Verify CSV can be parsed back with csv.reader and all rows have consistent column counts."""
        columns = ["id", "name", "note", "ts", "flag", "extra_info"]
        sample_time = datetime(2026, 11, 5, 12, 34, 56, tzinfo=timezone.utc)
        rows = [
            {
                "id": 1,
                "name": "Line\nBreak, with \"quotes\"",
                "note": "Standard note",
                "ts": sample_time,
                "flag": True,
                "extra_info": {"key": "val"},
            },
            {
                "id": 2,
                "name": "Missing some fields",
                # note, ts, flag, extra_info are absent
            },
            {
                "id": 3,
                "name": "=1+1 formula",
                "note": None,
                "ts": sample_time,
                "flag": False,
                "extra_info": " -cmd",
            },
        ]

        csv_bytes, manifest = export_rows(
            rows,
            fmt="csv",
            schema=self.schema,
            columns=columns,
            exporter=self.exporter,
            time_range=(self.t_start, self.t_end),
            filters=self.filters,
            masked_fields=[],
        )

        csv_text = csv_bytes.decode("utf-8")
        reader = list(csv.reader(io.StringIO(csv_text)))

        # Header + 3 data rows = 4 rows
        self.assertEqual(len(reader), 4)

        # All rows must have exactly len(columns) items
        for idx, row in enumerate(reader):
            self.assertEqual(
                len(row),
                len(columns),
                f"Row {idx} column count ({len(row)}) does not match expected ({len(columns)})",
            )

        # Header matches
        self.assertEqual(reader[0], columns)

        # Check row 1
        r1 = dict(zip(columns, reader[1]))
        self.assertEqual(r1["id"], "1")
        self.assertEqual(r1["name"], "Line\nBreak, with \"quotes\"")
        self.assertEqual(r1["ts"], "2026-11-05T12:34:56+00:00")
        self.assertEqual(r1["flag"], "True")

        # Check row 2 (missing fields are empty strings)
        r2 = dict(zip(columns, reader[2]))
        self.assertEqual(r2["id"], "2")
        self.assertEqual(r2["name"], "Missing some fields")
        self.assertEqual(r2["note"], "")
        self.assertEqual(r2["ts"], "")
        self.assertEqual(r2["flag"], "")
        self.assertEqual(r2["extra_info"], "")

        # Check row 3 (escaped formula and None as empty string)
        r3 = dict(zip(columns, reader[3]))
        self.assertEqual(r3["id"], "3")
        self.assertEqual(r3["name"], "'=1+1 formula")
        self.assertEqual(r3["note"], "")
        self.assertEqual(r3["flag"], "False")
        self.assertEqual(r3["extra_info"], "' -cmd")

    def test_timezone_conversion_to_utc(self):
        """Verify non-UTC timezone-aware datetimes are converted to ISO-8601 UTC."""
        tz_plus_8 = timezone(timedelta(hours=8))
        local_dt = datetime(2026, 11, 5, 20, 0, 0, tzinfo=tz_plus_8)
        # 20:00 UTC+8 is 12:00 UTC
        expected_utc_iso = "2026-11-05T12:00:00+00:00"

        columns = ["ts"]
        data_bytes, manifest = export_rows(
            [{"ts": local_dt}],
            fmt="jsonl",
            schema=self.schema,
            columns=columns,
            exporter=self.exporter,
            time_range=(local_dt, local_dt),
            filters={},
            masked_fields=[],
        )

        obj = json.loads(data_bytes.decode("utf-8").strip())
        self.assertEqual(obj["ts"], expected_utc_iso)
        self.assertEqual(manifest["time_range"], (expected_utc_iso, expected_utc_iso))


if __name__ == "__main__":
    unittest.main()
