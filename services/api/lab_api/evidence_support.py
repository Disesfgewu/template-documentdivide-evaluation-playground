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


def boilerplate_ids(pages: dict[int, Sequence]) -> frozenset[str]:
    """Evidence ids of units that are page furniture, never gold: bare page numbers, units inside the header/footer band, and text that
    repeats on two or more pages (a running header, a footer line)."""
    seen: dict[str, set[int]] = {}
    for page, units in pages.items():
        for u in units:
            seen.setdefault(_norm(u.normalized_content).strip(), set()).add(page)
    out = set()
    for page, units in pages.items():
        for u in units:
            key = _norm(u.normalized_content).strip()
            in_band = all(r.bbox[3] <= HEADER_BAND or r.bbox[1] >= FOOTER_BAND for r in u.regions)
            if _bare_number(u.normalized_content) or in_band or (len(seen[key]) >= 2 and len(key) >= 3):
                out.add(u.evidence_id)
    return frozenset(out)


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
    chosen: list = []
    got_words: set[str] = set()
    got_numbers: set[str] = set()

    def done() -> bool:
        return numbers <= got_numbers and (not words or len(got_words) / len(words) >= MIN_FACT_COVERAGE)

    while not done() and len(chosen) < MAX_UNITS_PER_FACT:
        best = max(((len(w - got_words) + 2 * len(n - got_numbers), -pos, u, w, n) for pos, u, w, n in candidates
                    if u not in chosen and (w - got_words or n - got_numbers)), default=None, key=lambda t: t[:2])
        if best is None:
            break
        chosen.append(best[2])
        got_words |= best[3]
        got_numbers |= best[4]
    if not done():
        raise NotAttributable("fact_not_carried_by_body_units")
    order = {id(u): pos for pos, u, _, _ in candidates}
    return sorted(chosen, key=lambda u: order[id(u)])
