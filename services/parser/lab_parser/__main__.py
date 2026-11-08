"""Local inspection and parser-service configuration commands."""
from __future__ import annotations

import sys

from .config import ConfigError, load_settings


def main(argv: list[str]) -> int:
    if argv and argv[0] == "inspect":
        from .inspection_cli import inspect
        return inspect(argv[1:])
    if argv != ["check-config"]:
        print("usage: python -m lab_parser check-config | inspect <path> [--max-bytes N] [--max-pages N]", file=sys.stderr)
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
