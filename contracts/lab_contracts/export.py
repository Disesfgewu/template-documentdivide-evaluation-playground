"""Generate (or --check) contracts/schemas/<SCHEMA_VERSION>/<Name>.schema.json."""
import json
import sys
from pathlib import Path

from . import SCHEMA_VERSION
from .registry import MODELS
from . import openapi

OUT = Path(__file__).resolve().parent.parent / "schemas" / SCHEMA_VERSION
OPENAPI = Path(__file__).resolve().parent.parent / "openapi" / "method-service.v1.json"


def render(name, model) -> str:
    schema = model.model_json_schema()
    schema = {"$schema": "https://json-schema.org/draft/2020-12/schema",
              "$id": f"lab-contracts/{SCHEMA_VERSION}/{name}.schema.json", **schema}
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv) -> int:
    check = "--check" in argv[1:]
    expected = {f"{n}.schema.json": render(n, m) for n, m in MODELS.items()}
    openapi_text = json.dumps(openapi.build(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if check and (not OPENAPI.is_file() or OPENAPI.read_text() != openapi_text):
        print(f"openapi drift (run python -m lab_contracts.export): {OPENAPI.name}", file=sys.stderr)
        return 1
    if not check:
        OPENAPI.parent.mkdir(parents=True, exist_ok=True)
        OPENAPI.write_text(openapi_text)
    if check:
        actual = {p.name for p in OUT.glob("*.schema.json")}
        bad = sorted(set(expected) ^ actual)
        bad += sorted(n for n in expected if n in actual and (OUT / n).read_text() != expected[n])
        if bad:
            print(f"schema drift (run python -m lab_contracts.export): {bad}", file=sys.stderr)
            return 1
        print(f"{len(expected)} schemas up to date")
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    for n, text in expected.items():
        (OUT / n).write_text(text)
    print(f"wrote {len(expected)} schemas to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
