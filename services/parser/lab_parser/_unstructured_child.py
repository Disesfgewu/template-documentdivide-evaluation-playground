"""Fixed subprocess bootstrap that runs pinned Unstructured ``fast`` on one PDF.

Not a public CLI. Started only by ``unstructured_runner`` with a validated argv, in an
empty private working directory containing ``source.pdf``. Writes ``elements.json``
(the OSS ``elements_to_dicts`` output) and ``meta.json`` (the installed version).
Diagnostics, document text and exception text are never written anywhere else.
"""
from __future__ import annotations

import os
import resource
import sys


def main() -> int:
    memory, cpu, output = map(int, sys.argv[1:])
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_FSIZE, (output, output))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    try:
        import importlib.metadata as md
        import json
        from unstructured.partition.pdf import partition_pdf
        from unstructured.staging.base import elements_to_dicts

        elements = partition_pdf(filename="source.pdf", strategy="fast")
        payload = json.dumps(elements_to_dicts(elements), allow_nan=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        meta = json.dumps({"unstructured": md.version("unstructured")}).encode("utf-8")
        with open("elements.json", "xb") as stream:
            stream.write(payload)
        with open("meta.json", "xb") as stream:
            stream.write(meta)
        return 0
    except BaseException:
        return 1


if __name__ == "__main__":
    sys.exit(main())
