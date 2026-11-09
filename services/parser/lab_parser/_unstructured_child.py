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


def die_with_parent() -> None:
    """Ask the kernel to SIGKILL this process when its parent dies (Linux PR_SET_PDEATHSIG).

    The child runs in its own session so a cancelled parent can kill its group, but a parent
    that is SIGKILLed or ``os._exit``ed never gets to; without this the parser would keep
    running (up to its CPU/wall limits) with nobody to read its result.
    """
    import ctypes
    import signal

    try:
        parent = os.getppid()
        if ctypes.CDLL(None, use_errno=True).prctl(1, int(signal.SIGKILL), 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
            return
        if os.getppid() != parent:  # the parent died before the call took effect
            os._exit(1)
    except Exception:
        return  # best effort: the limits and the parent's own cancel path still apply


def _page_info(path: str) -> list[dict]:
    """Per page: /Rotate (0, 90, 180, 270) and the MediaBox and CropBox, as pypdf resolves them.

    The parent decides what can be attested from this (Unstructured fast takes its coordinates from
    pdfminer, which applies /Rotate to the layout size but ignores /CropBox; measured on CI).
    """
    from pypdf import PdfReader

    reader = PdfReader(path, strict=True)
    return [{"rotation": page.rotation % 360,
             "mediabox": [float(v) for v in page.mediabox], "cropbox": [float(v) for v in page.cropbox]}
            for page in reader.pages]


def main() -> int:
    die_with_parent()
    apply_limits(*map(int, sys.argv[1:]))
    try:
        import importlib.metadata as md
        import json
        from unstructured.partition.pdf import partition_pdf
        from unstructured.staging.base import elements_to_dicts

        source = "source.pdf"
        if os.path.exists("pages.json"):
            # A batch: parse only these original pages, in this order (the parent validated them).
            from pypdf import PdfReader, PdfWriter

            with open("pages.json", "rb") as stream:
                pages = json.loads(stream.read(65536))
            reader = PdfReader("source.pdf", strict=True)
            if (type(pages) is not list or not pages or any(type(i) is not int or not 0 <= i < len(reader.pages)
                                                            for i in pages)):
                return 1
            writer = PdfWriter()
            for i in pages:
                writer.add_page(reader.pages[i])
            with open("subset.pdf", "xb") as stream:
                writer.write(stream)
            source = "subset.pdf"
        elements = partition_pdf(filename=source, strategy="fast")
        payload = json.dumps(elements_to_dicts(elements), allow_nan=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        info = _page_info(source)
        meta = json.dumps({"unstructured": md.version("unstructured"),
                           # Text units are intact only for upright (0) and upside-down (180) pages; 90 and
                           # 270 come out letter-scrambled (measured), so they are never attested.
                           "attestable_pages": [i["rotation"] in (0, 180) for i in info],
                           "page_geometry": info}).encode("utf-8")
        with open("elements.json", "xb") as stream:
            stream.write(payload)
        with open("meta.json", "xb") as stream:
            stream.write(meta)
        return 0
    except BaseException:
        return 1


if __name__ == "__main__":
    sys.exit(main())
