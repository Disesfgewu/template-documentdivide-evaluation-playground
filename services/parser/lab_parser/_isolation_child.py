"""Fixed Linux subprocess bootstrap; not a public CLI or arbitrary-code runner."""
from __future__ import annotations

import os
from pathlib import Path
import resource
import sys


def die_with_parent() -> None:
    """Load the shared implementation by path (``-I`` keeps this directory off ``sys.path``)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_pdeathsig", Path(__file__).with_name("_pdeathsig.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.die_with_parent()



def main() -> int:
    # Fixed argv comes only from the trusted parent's validated ProcessLimits.
    die_with_parent()
    memory, cpu, output, max_pages = map(int, sys.argv[1:])
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_FSIZE, (output, output))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    # -I ignores caller PYTHONPATH and user-site. The interpreter/venv is trusted.
    # Its own startup occurs before these limits. Add only this source root.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import hashlib
    import io
    import json
    import logging

    # Exact pypdf messages that are benign, fixed text (no PDF-controlled content) and well understood, mapped to the code recorded on the document
    # version (Issue #204, PM-DECISION). Any other warning, whatever it says, is still an invalid PDF. The stored bytes are never repaired.
    ALLOWED = {"Xref table not zero-indexed. ID numbers for objects will be corrected.": "xref_not_zero_indexed"}

    class WarningCounter(logging.Handler):
        warned = False                      # a warning that is NOT on the list
        allowed: set = set()

        def emit(self, record):
            # Never format or retain PDF-controlled diagnostic content: only a fixed template that is exactly on the list is recognised.
            try:
                template = record.msg if isinstance(record.msg, str) and not record.args else None
            except Exception:
                template = None
            code = ALLOWED.get(template) if template is not None else None
            if code is None:
                self.warned = True
            else:
                self.allowed.add(code)

    counter = WarningCounter(logging.WARNING)
    counter.allowed = set()
    logger = logging.getLogger("pypdf")
    logger.handlers = [counter]
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    try:
        from pypdf import PdfReader
        from lab_parser.pages import extract_pages
        data = Path("source.pdf").read_bytes()
        if not data.startswith(b"%PDF-"):
            result = {"error": "invalid_pdf"}
        else:
            reader = PdfReader(io.BytesIO(data), strict=True)
            if reader.is_encrypted:
                result = {"error": "encrypted_pdf"}
            elif not 1 <= len(reader.pages) <= max_pages:
                result = {"error": "page_limit_exceeded"}
            else:
                pages = extract_pages(data)
                # The renderer (PDFium) is the other reader of these bytes. A /Count that
                # disagrees with /Kids gives pypdf and PDFium different page counts, so the
                # geometry would describe pages the renderer does not see. Fail closed.
                import pypdfium2
                document = pypdfium2.PdfDocument(data)
                try:
                    renderer_pages = len(document)
                finally:
                    document.close()
                if not len(reader.pages) == len(pages) == renderer_pages:
                    raise ValueError("page_count_disagreement")
                # a text-layer sample (PDFium, at most 20 pages spread over the document): a scan has none (Issue #203). Best effort: a failure here
                # means "not measured", never a rejected document.
                sampled, with_text = [], 0
                try:
                    document = pypdfium2.PdfDocument(data)
                    try:
                        n = len(document)
                        for i in sorted({(k * n) // min(n, 20) for k in range(min(n, 20))}):
                            page = document[i]
                            try:
                                text = page.get_textpage()
                                try:
                                    has = text.count_chars() > 0
                                finally:
                                    text.close()
                            finally:
                                page.close()
                            sampled.append(i)
                            with_text += 1 if has else 0
                    finally:
                        document.close()
                except Exception:
                    sampled, with_text = [], 0
                trailing = data[data.rfind(b"%%EOF") + 5:].strip(b" \t\r\n\f\0") if b"%%EOF" in data else b""
                codes = set(counter.allowed)
                if trailing and not counter.warned:
                    codes.add("trailing_bytes_after_eof")
                result = {
                    "schema_version": "isolated-geometry-2",
                    "warnings": sorted(codes), "text_sample": {"pages": len(sampled), "with_text": with_text},
                    "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data),
                    "pages": [{"page_index": p.page_index, "page_label": p.page_label, "rotation": p.rotation,
                               "width_pt": p.display_width_pt, "height_pt": p.display_height_pt}
                              for p in pages],
                }
        if counter.warned:
            # readable, but the reader reported something that is not on the list: a different message from "cannot be read"
            result = {"error": "pdf_warning"} if result.get("error") is None else {"error": "invalid_pdf"}
    except MemoryError:
        result = {"error": "memory_limit"}
    except Exception:
        result = {"error": "invalid_pdf"}
    try:
        with open("result.json", "xb") as stream:
            stream.write(json.dumps(result, allow_nan=False, separators=(",", ":")).encode("utf-8"))
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
