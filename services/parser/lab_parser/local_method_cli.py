"""Bounded local participant examples. Run with --help for the two strategies."""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys

from lab_contracts.method import QueryRequest
from pydantic import ValidationError
from .local_methods import MethodConfig, prepare, query

MAX_REQUEST_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024


def read_regular(path: str, limit: int) -> bytes:
    # O_NONBLOCK avoids waiting on FIFO open; fstat checks the opened object.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_size > limit:
            raise ValueError("input_must_be_bounded_regular_file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("input_limit_exceeded")
        return data
    finally:
        os.close(fd)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _nonfinite(_):
    raise ValueError("nonfinite_json_value")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "query"))
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--document-id", required=True)
    parser.add_argument("--strategy", choices=("page", "window"), required=True)
    parser.add_argument("--window-chars", type=int, default=512)
    parser.add_argument("--request", help="QueryRequest JSON; required for query")
    args = parser.parse_args(argv)
    if (args.action == "query") != bool(args.request):
        parser.error("--request is required only for query")
    try:
        config = MethodConfig(strategy=args.strategy, window_chars=args.window_chars)
        index = prepare(read_regular(args.pdf, config.max_pdf_bytes), document_id=args.document_id,
                        expected_sha256=args.sha256, config=config)
        if args.action == "prepare":
            result = index.manifest().model_dump(mode="json")
        else:
            raw = read_regular(args.request, MAX_REQUEST_BYTES)
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_nonfinite)
            request = QueryRequest.model_validate_json(json.dumps(value, allow_nan=False))
            result = query(index, request).model_dump(mode="json")
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
        if len(encoded) > MAX_OUTPUT_BYTES:
            raise ValueError("output_limit_exceeded")
        sys.stdout.write(encoded.decode() + "\n")
        return 0
    except (OSError, ValueError, ValidationError, TimeoutError):
        # Do not disclose PDF contents, paths or raw validation payloads.
        sys.stderr.write("local_method_failed: invalid input, binding, limit, or extraction\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
