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


def apply_limits(memory: int, cpu: int, output: int) -> None:
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_FSIZE, (output, output))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))


def _trivial_pages(path: str) -> list[bool]:
    """True only where /Rotate is 0 and CropBox equals MediaBox.

    pdfminer (used by Unstructured fast) ignores CropBox and reports the unrotated
    MediaBox size, so only such pages have coordinates that can be attested.
    """
    from pypdf import PdfReader

    reader = PdfReader(path, strict=True)
    result = []
    for page in reader.pages:
        media = [float(v) for v in page.mediabox]
        crop = [float(v) for v in page.cropbox]
        result.append(page.rotation % 360 == 0 and media == crop)
    return result


def main() -> int:
    apply_limits(*map(int, sys.argv[1:]))
    try:
        import importlib.metadata as md
        import json
        from unstructured.partition.pdf import partition_pdf
        from unstructured.staging.base import elements_to_dicts

        elements = partition_pdf(filename="source.pdf", strategy="fast")
        payload = json.dumps(elements_to_dicts(elements), allow_nan=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        meta = json.dumps({"unstructured": md.version("unstructured"),
                           "attestable_pages": _trivial_pages("source.pdf")}).encode("utf-8")
        with open("elements.json", "xb") as stream:
            stream.write(payload)
        with open("meta.json", "xb") as stream:
            stream.write(meta)
        return 0
    except BaseException:
        return 1


if __name__ == "__main__":
    sys.exit(main())
