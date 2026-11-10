"""Page-level detector for text that a reader cannot see (PM-DECISION on gap G8, Refs #7).

Loaded by path inside the isolation child (``python -I`` keeps this directory off ``sys.path``), so it
imports nothing from the package. For each page it walks the content stream, and the Form XObjects the
page invokes, tracking the text rendering mode, which is part of the graphics state: it survives
``BT``/``ET`` and is restored by ``Q``. A page is ``text_not_visible`` when any text-showing operator
runs while the mode is 3 (invisible) or 7 (clip only, nothing painted). It is ``content_unparseable``
when the work cannot be bounded or interpreted: the stream does not parse, an invoked XObject is missing
or nested too deep, or the operator budget is spent. The caller treats both as fail closed.

NOT detected here (that is what a render cross-check is for): white text on a white page, text covered
by other content, text outside the page or clipped away, and invisible text that ``Tr`` does not control.
"""
from __future__ import annotations

HIDDEN = "text_not_visible"
UNPARSEABLE = "content_unparseable"
MAX_OPERATORS = 200_000   # per page, shared by the page stream and every form it invokes
MAX_DEPTH = 4             # nesting of Form XObjects
_SHOW = (b"Tj", b"TJ", b"'", b'"')
_HIDING_MODES = (3, 7)


class _Unparseable(Exception):
    pass


def _operations(stream, reader):
    from pypdf.generic import ContentStream

    return ContentStream(stream, reader).operations


def _walk(operations, resources, reader, mode, depth, budget, cache) -> bool:
    """True when text is shown while hidden. ``mode`` is the rendering mode on entry; ``budget`` is a
    one-element list holding the operators still allowed."""
    stack: list[int] = []
    for operands, operator in operations:
        budget[0] -= 1
        if budget[0] < 0:
            raise _Unparseable
        if operator == b"q":
            stack.append(mode)
            if len(stack) > 256:
                raise _Unparseable
        elif operator == b"Q":
            if stack:
                mode = stack.pop()
        elif operator == b"Tr":
            if len(operands) != 1:
                raise _Unparseable
            try:
                mode = int(operands[0])
            except (TypeError, ValueError):
                raise _Unparseable from None
        elif operator in _SHOW:
            if mode in _HIDING_MODES:
                return True
        elif operator == b"Do":
            if len(operands) != 1:
                raise _Unparseable
            xobjects = (resources or {}).get("/XObject")
            xobjects = xobjects.get_object() if xobjects is not None else {}
            target = xobjects.get(operands[0])
            if target is None:
                raise _Unparseable
            target = target.get_object()
            if target.get("/Subtype") != "/Form":
                continue   # an image draws no text
            if depth >= MAX_DEPTH:
                raise _Unparseable
            key = id(target)
            if key not in cache:
                cache[key] = _operations(target, reader)
            inner = target.get("/Resources")
            inner = inner.get_object() if inner is not None else resources
            if _walk(cache[key], inner, reader, mode, depth + 1, budget, cache):
                return True
    return False


def scan_page(page, reader) -> str | None:
    """``None`` for a page with no hidden text, else ``text_not_visible`` or ``content_unparseable``."""
    try:
        contents = page.get_contents()
        if contents is None:
            return None
        resources = page.get("/Resources")
        resources = resources.get_object() if resources is not None else {}
        hidden = _walk(contents.operations, resources, reader, 0, 0, [MAX_OPERATORS], {})
    except _Unparseable:
        return UNPARSEABLE
    except Exception:  # noqa: BLE001 - anything unexpected is not proof that the page is clean
        return UNPARSEABLE
    return HIDDEN if hidden else None


def scan_document(reader) -> list[list]:
    """``[[page_index, code], ...]`` for the pages that are not clean, in order."""
    found = []
    for index, page in enumerate(reader.pages):
        code = scan_page(page, reader)
        if code is not None:
            found.append([index, code])
    return found
