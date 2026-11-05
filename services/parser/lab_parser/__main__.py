"""``python -m lab_parser check-config`` validates LAB_PARSER_* settings."""
from __future__ import annotations

import sys

from .config import ConfigError, load_settings


def main(argv: list[str]) -> int:
    if argv != ["check-config"]:
        print("usage: python -m lab_parser check-config", file=sys.stderr)
        return 2
    try:
        s = load_settings()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"config ok: env={s.env.value}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
