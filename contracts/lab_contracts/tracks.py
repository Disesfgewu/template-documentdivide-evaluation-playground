"""raw_pdf_track vs shared_parse_track: input leak check and comparability (SPEC 19.5, T103).

Participants receive the original PDF and explicitly allowed public assets only:
never verified chart values, relations, gold crop hints or generation summaries.
If the study also supplies one shared parse result, that is a separate
``shared_parse_track`` with its own leaderboard; it tests search and answering
and must not be presented as an independent check of the participant's own
parsing. Methods are formally compared only with the same track, benchmark and
budget. Pure functions over plain data; they never read a benchmark.

``find_gold_leaks`` is a deterministic guard, not proof: it finds forbidden field
names anywhere in a nested payload and values equal to known gold identifiers
or hashes. It cannot see a leak that is paraphrased into free text, so it
complements, and does not replace, the access isolation and human spot checks.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping, Optional

from pydantic import TypeAdapter, ValidationError

from .base import ResourceId, Sha256
from .parse import ParseResult

__all__ = [
    "Comparability",
    "FORBIDDEN_FIELDS",
    "Leak",
    "PARSE_RESULT_FIELDS",
    "Track",
    "allowed_claims",
    "check_track_input",
    "compare_tracks",
    "find_gold_leaks",
    "leaderboard_key",
]


class Track(str, Enum):
    raw_pdf_track = "raw_pdf_track"
    shared_parse_track = "shared_parse_track"


# Names that carry gold-derived or generation-side content (SPEC 19.5: verified chart
# values, relations, gold crop hints, generation summaries, and the four gold parts).
FORBIDDEN_FIELDS = frozenset({
    "verified_values", "verified_visual_values", "verified_relations", "visual_relations",
    "gold", "gold_answer", "gold_crops", "gold_crop_hints", "crop_hints",
    "generation_summary", "generation_context", "build_summary", "builder_notes",
    "reference_answer", "required_facts", "required_claims", "fact_support_sets",
    "evidence_sets", "supporting_evidence_ids", "semantic_answers", "textual_answers",
    "source_answers", "numeric_checks", "text_checks", "estimate_low", "estimate_high",
})

# Outputs of a parser: allowed only as a declared shared parse, never in the raw track.
# Besides the hand-listed names, the dedicated output fields of the current ``ParseResult``
# contract are taken from the contract itself (``operation_id`` and ``pages`` are too generic
# to refuse as names; a whole ``ParseResult`` is caught structurally in ``check_track_input``),
# so the guard follows the parser contract when it changes.
_GENERIC_PARSE_FIELDS = frozenset({"operation_id", "pages"})
PARSE_RESULT_FIELDS = frozenset({
    "parse_bundle", "parsed_pages", "ocr_text", "structured_content", "middle_json",
    "layout", "table_cells",
}) | (frozenset(ParseResult.model_fields) - _GENERIC_PARSE_FIELDS)

_RESOURCE_ID = TypeAdapter(ResourceId)
_SHA256 = TypeAdapter(Sha256)

SHARED_PARSE_DECLARATION = ("bundle_id", "parser_revision", "bundle_sha256")


@dataclass(frozen=True)
class Leak:
    path: str
    code: str  # forbidden_field | parse_result_in_raw_track | gold_identifier | undeclared_shared_parse | invalid_shared_parse_declaration


def _walk(value: Any, path: str) -> Iterable[tuple[str, Optional[str], Any]]:
    """Yield (path, key-or-None, value) for every node; ``key`` is set for dict entries."""
    if isinstance(value, Mapping):
        for k, v in value.items():
            sub = f"{path}.{k}" if path else str(k)
            yield sub, str(k), v
            yield from _walk(v, sub)
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            sub = f"{path}[{i}]"
            yield sub, None, v
            yield from _walk(v, sub)


def find_gold_leaks(
    payload: Any,
    *,
    gold_identifiers: Iterable[str] = (),
    forbidden: frozenset[str] = FORBIDDEN_FIELDS,
) -> list[Leak]:
    """Forbidden field names (any depth, case-insensitive) and string values equal to a
    known gold identifier or hash. Paths only; leaked values are never copied out."""
    ids = {g for g in gold_identifiers if isinstance(g, str) and g}
    lowered = {f.lower() for f in forbidden}
    leaks: list[Leak] = []
    for path, key, value in _walk(payload, ""):
        if key is not None and key.lower() in lowered:
            leaks.append(Leak(path, "forbidden_field"))
        if isinstance(value, str) and value in ids:
            leaks.append(Leak(path, "gold_identifier"))
    return leaks


def check_track_input(
    track: Track,
    payload: Mapping[str, Any],
    *,
    gold_identifiers: Iterable[str] = (),
) -> list[Leak]:
    """Problems that stop a payload from being given to a method on ``track``.

    Both tracks: no gold-derived content. ``raw_pdf_track``: no parser output at all.
    ``shared_parse_track``: the shared parse must be declared (bundle id, parser
    revision, bundle hash) so the result is attributable and the track is visible.
    An empty list means the guard found nothing, not that the payload is safe.
    """
    if not isinstance(track, Track):
        raise ValueError("invalid_track")
    if not isinstance(payload, Mapping):
        raise ValueError("invalid_payload")
    problems = find_gold_leaks(payload, gold_identifiers=gold_identifiers)
    parse_fields = {f.lower() for f in PARSE_RESULT_FIELDS}
    if track is Track.raw_pdf_track:
        for path, key, value in _walk(payload, ""):
            if key is not None and key.lower() in parse_fields:
                problems.append(Leak(path, "parse_result_in_raw_track"))
            elif isinstance(value, Mapping) and _is_parse_result(value):
                problems.append(Leak(path, "parse_result_in_raw_track"))
    else:
        problems.extend(_shared_declaration_problems(payload.get("parse_bundle")))
    return problems


def _is_parse_result(node: Mapping[str, Any]) -> bool:
    """Whether a nested object is a valid current ``ParseResult`` (checked by the contract itself)."""
    if not {"manifest_ref", "raw_result_refs", "pages"} <= set(node):  # cheap pre-filter
        return False
    try:
        ParseResult.model_validate(node)
    except ValidationError:
        return False
    return True


def _valid(adapter: TypeAdapter, value: object) -> bool:
    try:
        adapter.validate_python(value, strict=True)
    except ValidationError:
        return False
    return True


def _shared_declaration_problems(declared: Any) -> list[Leak]:
    """The shared parse must be declared with a resource id, a parser revision and a lowercase
    64-hex bundle hash, validated with the same types the contracts use (SPEC 16.1)."""
    if not isinstance(declared, Mapping) or any(key not in declared for key in SHARED_PARSE_DECLARATION):
        return [Leak("parse_bundle", "undeclared_shared_parse")]
    found = []
    for key, adapter in (("bundle_id", _RESOURCE_ID), ("parser_revision", _RESOURCE_ID), ("bundle_sha256", _SHA256)):
        if not _valid(adapter, declared[key]):
            found.append(Leak(f"parse_bundle.{key}", "invalid_shared_parse_declaration"))
    return found


def leaderboard_key(track: Track, benchmark_revision_id: str, budget_profile: str) -> tuple[str, str, str]:
    """Runs share a leaderboard only when this key is equal."""
    if not isinstance(track, Track):
        raise ValueError("invalid_track")
    for value in (benchmark_revision_id, budget_profile):
        if not isinstance(value, str) or not value:
            raise ValueError("invalid_key")
    return (track.value, benchmark_revision_id, budget_profile)


@dataclass(frozen=True)
class Comparability:
    comparable: bool
    blockers: tuple[str, ...]
    limitations: tuple[str, ...]


def allowed_claims(track: Track) -> frozenset[str]:
    """What a result on ``track`` may be presented as measuring."""
    if track is Track.raw_pdf_track:
        return frozenset({"end_to_end_search_and_answer", "participant_parsing_quality"})
    return frozenset({"search_and_answer_on_shared_parse"})


def compare_tracks(
    a: tuple[Track, str, str],
    b: tuple[Track, str, str],
) -> Comparability:
    """Formal comparison needs the same track, benchmark revision and budget profile.
    Each argument is ``(track, benchmark_revision_id, budget_profile)``. Limitations are
    reported whenever a shared-parse result is involved, comparable or not."""
    keys = [leaderboard_key(*a), leaderboard_key(*b)]
    blockers = tuple(
        name for name, x, y in zip(("track", "benchmark_revision", "budget_profile"), keys[0], keys[1]) if x != y
    )
    limitations: list[str] = []
    if Track.shared_parse_track.value in (keys[0][0], keys[1][0]):
        limitations.append("shared_parse_not_independent_parser_quality")
    if "track" in blockers:
        limitations.append("separate_leaderboards")
    return Comparability(not blockers, blockers, tuple(limitations))
