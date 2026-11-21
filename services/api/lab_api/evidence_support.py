"""Which evidence units SUPPORT a fact (Issue #197, part 1). Pure functions: no database, no network.

Before this, the gold evidence of a question was every unit of the page(s) the generator was shown (15 to 74 units, with the page
number, the running header and the author line), so an exact quote scored 2-11 % recall and a whole-page fallback scored 100 %.
Now the evidence set is the UNION of, for every fact, the fewest shown units that carry it: chosen greedily by how many of the
fact's content words and numbers each unit adds, never a running header/footer/page number, and only when the units together
cover the fact as the existing grounding check does (``MIN_FACT_COVERAGE`` of its content words and EVERY number). A fact that
cannot be attributed to units this way makes the question invalid (it is not written)."""
from __future__ import annotations

from typing import Sequence

from lab_api.validate_questions import MIN_FACT_COVERAGE, _NUMBER, _norm, content_tokens

MAX_UNITS_PER_FACT = 4
HEADER_BAND = 0.08          # a unit that lies wholly inside the top / bottom 8 % of the page is a running header / footer
FOOTER_BAND = 0.92


class NotAttributable(ValueError):
    """A fact is not carried by any small set of body units of the shown pages."""


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUMBER.findall(_norm(text))}


def _bare_number(text: str) -> bool:
    """A page number: digits, a roman numeral, or 'page N' and nothing else."""
    t = _norm(text).strip(" .-–—()[]")
    if not t:
        return True
    if t.replace(",", "").isdigit():
        return True
    words = t.split()
    return (len(words) == 2 and words[0] in ("page", "p", "p.") and words[1].isdigit()) or \
        (len(words) == 1 and len(t) <= 5 and set(t) <= set("ivxlcdm"))


REPEAT_MIN_PAGES = 3        # text that is not in the band is furniture only when it sits at the same place on at least this many pages ...
REPEAT_MIN_SHARE = 0.30     # ... and on at least this share of the pages shown (a running header drifting out of the band; not a table cell)
REPEAT_Y_STEP = 0.02        # "the same place" = the same top edge within 2 % of the page height
ROW_MAX_CHARS = 40          # units this short that share a table row (overlapping y range) are one candidate in ``support_units``
ROW_MIN_OVERLAP = 0.5


def _top(u) -> float:
    return min(r.bbox[1] for r in u.regions)


def _in_band(u) -> bool:
    return all(r.bbox[3] <= HEADER_BAND or r.bbox[1] >= FOOTER_BAND for r in u.regions)


def boilerplate_ids(pages: dict[int, Sequence]) -> frozenset[str]:
    """Evidence ids of units that are page furniture, never gold (Issue #212: on a table-heavy manual the old rule, a bare number anywhere or
    any text on two or more pages, threw away 79 % of the body). Furniture is: every unit inside the header/footer band (page numbers, running
    headers and footers live there), and text OUTSIDE the band that sits at the same vertical position (``REPEAT_Y_STEP``) on at least
    ``REPEAT_MIN_PAGES`` pages and ``REPEAT_MIN_SHARE`` of the pages shown (a running header that drifted below the band). A bare number or a
    short cell in the body (``900``, ``Mean``, ``W``, ``%``) is body text: it repeats across pages at different heights."""
    where: dict[tuple[str, int], set[int]] = {}
    for page, units in pages.items():
        for u in units:
            where.setdefault((_norm(u.normalized_content).strip(), round(_top(u) / REPEAT_Y_STEP)), set()).add(page)
    need = max(REPEAT_MIN_PAGES, REPEAT_MIN_SHARE * len(pages))
    out = set()
    for page, units in pages.items():
        for u in units:
            key = _norm(u.normalized_content).strip()
            if _in_band(u) or (len(key) >= 3 and len(where[(key, round(_top(u) / REPEAT_Y_STEP))]) >= need):
                out.add(u.evidence_id)
    return frozenset(out)


def _same_row(a, b) -> bool:
    if a.regions[0].page_index != b.regions[0].page_index:
        return False
    (ay0, ay1), (by0, by1) = (min(r.bbox[1] for r in a.regions), max(r.bbox[3] for r in a.regions)), \
        (min(r.bbox[1] for r in b.regions), max(r.bbox[3] for r in b.regions))
    overlap = min(ay1, by1) - max(ay0, by0)
    return overlap > 0 and overlap >= ROW_MIN_OVERLAP * min(ay1 - ay0, by1 - by0)


def _rows(cands: list) -> list[list]:
    """Group short cells that share a table row into one candidate (a row label, its value and its unit are one table fact); every other
    unit is a candidate of its own. ``cands`` is in reading order, so a row is a run of neighbouring short units."""
    out: list[list] = []
    for c in cands:
        short = len(c[1].normalized_content) <= ROW_MAX_CHARS
        if out and short and all(len(m[1].normalized_content) <= ROW_MAX_CHARS and _same_row(m[1], c[1]) for m in out[-1]):
            out[-1].append(c)
        else:
            out.append([c])
    return out


def support_units(statement: str, units: Sequence, exclude: frozenset[str] = frozenset()) -> list:
    """The fewest units (in reading order) that together carry ``statement``, or raise ``NotAttributable``."""
    words = set(content_tokens(statement))
    numbers = _numbers(statement)
    if not words and not numbers:
        raise NotAttributable("empty_fact")
    candidates = []
    for pos, u in enumerate(units):
        if u.evidence_id in exclude:
            continue
        text = u.normalized_content
        candidates.append((pos, u, words & set(content_tokens(text)), numbers & _numbers(text)))
    groups = _rows(candidates)
    chosen: list = []          # groups, each one slot of MAX_UNITS_PER_FACT
    got_words: set[str] = set()
    got_numbers: set[str] = set()

    def gain(g):
        w = set().union(*(m[2] for m in g))
        n = set().union(*(m[3] for m in g))
        return w, n

    def done() -> bool:
        return numbers <= got_numbers and (not words or len(got_words) / len(words) >= MIN_FACT_COVERAGE)

    while not done() and len(chosen) < MAX_UNITS_PER_FACT:
        best = None
        for gi, g in enumerate(groups):
            if gi in chosen:
                continue
            w, n = gain(g)
            if not (w - got_words or n - got_numbers):
                continue
            key = (len(w - got_words) + 2 * len(n - got_numbers), -g[0][0])
            if best is None or key > best[0]:
                best = (key, gi, w, n)
        if best is None:
            break
        chosen.append(best[1])
        got_words |= best[2]
        got_numbers |= best[3]
    if not done():
        raise NotAttributable("fact_not_carried_by_body_units")
    # gold stays precise: from a chosen row keep only the cells that carry something of the fact
    picked = [m for gi in chosen for m in groups[gi] if (m[2] or m[3])]
    return [m[1] for m in sorted(picked, key=lambda m: m[0])]
