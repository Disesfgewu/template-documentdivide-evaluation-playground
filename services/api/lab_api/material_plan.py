"""Deterministic MATERIAL plan for a benchmark built from one pinned parse (P-B, Refs #7, Refs #10).

No LLM and no questions: this only says what source material the pinned parse offers for each quota bucket and which
pages cannot host questions. Every statement is a NECESSARY condition ("material is present"), never a promise that
verified questions can be written, and never a compliance verdict.

* D4: pages that were not promoted (no geometry, ``needs_review``, hidden or unanalysable text, dropped regions) cannot
  host questions; they are reported with their fixed reason as insufficient material.
* D5: the fast strategy returns no table structure, so a ``chart_table`` bucket without table/figure/image evidence is
  reported ``insufficient_material`` (no guessing of cell rows from text).
* D6: human review is reported as ``skipped`` and the compliance projection as ``partial``; it is never ``passed`` or
  ``compliant`` here. Only the owner may freeze (freezing is a later gate).
"""
from __future__ import annotations

from lab_contracts.benchmark import target_counts

PLAN_VERSION = "material-plan-1"
VISUAL_KINDS = ("table", "figure", "image")
BUCKETS = ("general", "cross_page", "chart_table", "confusable")
HUMAN_REVIEW_NOTE = ("Human review was skipped by the owner's scope. This is 'partial', never 'passed': the wording "
                     "must say skipped, and only the owner may freeze.")


def build_material_plan(page_count: int, promoted_pages: list[int], skipped_pages: dict[int, str],
                        units_by_page: dict[int, dict[str, int]], residual_limit: str) -> dict:
    """``units_by_page``: promoted page index -> {region kind: number of evidence units}."""
    n_target, q_min = target_counts(page_count)
    usable_pages = sorted(p for p in promoted_pages if sum(units_by_page.get(p, {}).values()) > 0)
    text_units = sum(units_by_page.get(p, {}).get("text", 0) for p in usable_pages)
    visual_units = sum(units_by_page.get(p, {}).get(k, 0) for p in usable_pages for k in VISUAL_KINDS)
    all_units = sum(sum(units_by_page.get(p, {}).values()) for p in usable_pages)
    pages_with_text = [p for p in usable_pages if units_by_page.get(p, {}).get("text", 0) > 0]

    def bucket(required: int, ok: bool, material: dict, why_not: str) -> dict:
        return {"required_minimum": required, "material": material,
                "status": "material_available" if ok else "insufficient_material",
                "reason": None if ok else why_not}

    buckets = {
        "general": bucket(max(0, n_target - 3 * q_min), text_units >= 1,
                          {"text_units": text_units, "usable_pages": len(pages_with_text)},
                          "no usable text evidence on any clean page"),
        "cross_page": bucket(q_min, len(pages_with_text) >= 2, {"usable_text_pages": len(pages_with_text)},
                             "needs at least two clean pages with text evidence"),
        "chart_table": bucket(q_min, visual_units >= 1, {"table_figure_image_units": visual_units},
                              "no table, figure or image evidence in this parse (the fast strategy has no table "
                              "structure; hi_res is not available); rows and cells are never guessed from text"),
        "confusable": bucket(q_min, text_units >= 2, {"text_units": text_units},
                             "needs at least two distinct clean text evidence units to build distractors from"),
    }
    return {
        "plan_version": PLAN_VERSION,
        "n_target": n_target,
        "q_min": q_min,
        "pages": {"total": page_count, "usable": usable_pages,
                  "insufficient_material": {str(p): skipped_pages[p] for p in sorted(skipped_pages)}},
        "evidence_units": all_units,
        "buckets": buckets,
        "insufficient_buckets": [name for name in BUCKETS if buckets[name]["status"] == "insufficient_material"],
        "human_review": {"status": "skipped", "note": HUMAN_REVIEW_NOTE},
        "compliance_projection": "partial",
        "limits": [
            "Material checks are necessary conditions only: they do not show that verified questions can be written.",
            residual_limit,
        ],
    }
