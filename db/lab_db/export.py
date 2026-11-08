"""Audit data export implementation (SPEC 10.3).

Pure Python implementation of JSONL and CSV audit export with manifest,
CSV formula injection protection, field masking, and secret redaction.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from typing import Any, Iterable, Literal, Mapping

from lab_db.logs import redact_value

DANGEROUS_CSV_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _json_default(obj: Any) -> Any:
    """JSON serializer fallback for objects like datetime."""
    if isinstance(obj, datetime):
        if obj.tzinfo is None or obj.tzinfo.utcoffset(obj) is None:
            raise ValueError(f"naive datetime encountered: {obj}")
        return obj.astimezone(timezone.utc).isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _sanitize_csv_cell(val: Any) -> Any:
    """Sanitize a CSV cell value against formula injection.

    String values that start with '=', '+', '-', '@', '\\t', '\\r'
    (or start with these after stripping leading whitespace)
    are prepended with a single quote (').
    Non-string values (numbers, booleans, None) are not prepended.
    """
    if val is None:
        return ""
    if isinstance(val, bool):
        # In Python, bool is a subclass of int, so check bool before int/float
        return val
    if isinstance(val, (int, float)):
        return val
    if isinstance(val, datetime):
        if val.tzinfo is None or val.tzinfo.utcoffset(val) is None:
            raise ValueError(f"naive datetime encountered: {val}")
        return val.astimezone(timezone.utc).isoformat()
    if isinstance(val, str):
        # Check the raw value, after stripping spaces only (so " \tcmd" is
        # still caught by its tab), and after stripping all whitespace.
        if (
            val.startswith(DANGEROUS_CSV_PREFIXES)
            or val.lstrip().startswith(DANGEROUS_CSV_PREFIXES)
            or val.lstrip(" ").startswith(DANGEROUS_CSV_PREFIXES)
        ):
            return "'" + val
        return val

    # Containers (dict, list, etc.) serialized to JSON
    serialized = json.dumps(val, ensure_ascii=False, default=_json_default)
    if (
        serialized.startswith(DANGEROUS_CSV_PREFIXES)
        or serialized.lstrip().startswith(DANGEROUS_CSV_PREFIXES)
        or serialized.lstrip(" ").startswith(DANGEROUS_CSV_PREFIXES)
    ):
        return "'" + serialized
    return serialized


def export_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    fmt: Literal["jsonl", "csv"],
    schema: str,
    columns: list[str],
    exporter: str,
    time_range: tuple[datetime, datetime],
    filters: Mapping[str, Any],
    masked_fields: list[str],
) -> tuple[bytes, dict]:
    """Export audit log rows in JSONL or CSV format with manifest.

    Args:
        rows: Iterable of row mappings.
        fmt: Export format, either "jsonl" or "csv".
        schema: Audit schema identifier.
        columns: Ordered list of columns to output.
        exporter: Identifier of user or service requesting export.
        time_range: Tuple of (start_datetime, end_datetime). Must be timezone-aware.
        filters: Applied query filters.
        masked_fields: Field names whose values must always be replaced with "[REDACTED]".

    Returns:
        tuple[bytes, dict]: Exported file bytes and manifest metadata dictionary.

    Raises:
        ValueError: If fmt is unsupported, columns is empty or contains duplicates,
                    time_range is invalid or naive, or any datetime in rows is naive.
    """
    if fmt not in ("jsonl", "csv"):
        raise ValueError(f"Unsupported export format: {fmt!r}. Must be 'jsonl' or 'csv'.")

    if not columns:
        raise ValueError("columns cannot be empty.")
    if len(columns) != len(set(columns)):
        raise ValueError("columns cannot contain duplicate names.")

    if not isinstance(time_range, (tuple, list)) or len(time_range) != 2:
        raise ValueError("time_range must be a 2-tuple of (start_datetime, end_datetime).")

    start_time, end_time = time_range
    if not isinstance(start_time, datetime) or not isinstance(end_time, datetime):
        raise ValueError("time_range elements must be datetime instances.")
    if start_time.tzinfo is None or start_time.tzinfo.utcoffset(start_time) is None:
        raise ValueError(f"time_range start time must be timezone-aware: {start_time}")
    if end_time.tzinfo is None or end_time.tzinfo.utcoffset(end_time) is None:
        raise ValueError(f"time_range end time must be timezone-aware: {end_time}")
    if start_time > end_time:
        raise ValueError(f"time_range start ({start_time}) cannot be after end ({end_time}).")

    start_iso = start_time.astimezone(timezone.utc).isoformat()
    end_iso = end_time.astimezone(timezone.utc).isoformat()

    masked_set = set(masked_fields)
    row_count = 0

    if fmt == "jsonl":
        output_lines: list[str] = []
        for row in rows:
            row_count += 1
            row_dict: dict[str, Any] = {}
            for col in columns:
                if col not in row:
                    row_dict[col] = None
                elif col in masked_set:
                    row_dict[col] = "[REDACTED]"
                else:
                    val = redact_value(row[col])
                    if isinstance(val, datetime):
                        if val.tzinfo is None or val.tzinfo.utcoffset(val) is None:
                            raise ValueError(f"naive datetime encountered for column '{col}': {val}")
                        val = val.astimezone(timezone.utc).isoformat()
                    row_dict[col] = val

            line = json.dumps(row_dict, ensure_ascii=False, sort_keys=False, default=_json_default)
            output_lines.append(line + "\n")

        data_bytes = "".join(output_lines).encode("utf-8")

    elif fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
        writer.writerow(columns)

        for row in rows:
            row_count += 1
            csv_row: list[Any] = []
            for col in columns:
                if col not in row:
                    val = ""
                elif col in masked_set:
                    val = "[REDACTED]"
                else:
                    raw_val = redact_value(row[col])
                    if raw_val is None:
                        val = ""
                    else:
                        val = raw_val

                sanitized = _sanitize_csv_cell(val)
                csv_row.append(sanitized)

            writer.writerow(csv_row)

        data_bytes = buf.getvalue().encode("utf-8")

    manifest = {
        "schema": schema,
        "format": fmt,
        "columns": list(columns),
        "exporter": exporter,
        "time_range": (start_iso, end_iso),
        "filters": dict(filters),
        "row_count": row_count,
        "sha256": hashlib.sha256(data_bytes).hexdigest(),
        "masked_fields": list(masked_fields),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    return data_bytes, manifest
