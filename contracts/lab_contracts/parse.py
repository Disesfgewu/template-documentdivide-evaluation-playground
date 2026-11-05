"""Internal parser contract (SPEC 19.2). Upstream parser JSON (Unstructured,
MinerU) is never exposed as a LAB core contract: adapters (#8) translate into
these models and keep the raw upstream output as referenced artifacts."""
from __future__ import annotations

from typing import Annotated, Literal, Optional

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from .base import Contract, FiniteFloat, ResourceId, Sha256
from .base import check_unique as _unique

UNKNOWN = "unknown"
# Values that do not pin anything; matched case-insensitively.
_UNPINNED = {"unknown", "latest", "unpinned", "main", "master", "head"}


def _unpinned(v: str) -> bool:
    v = v.strip().lower()
    return v in _UNPINNED or v.endswith(("@unknown", "@latest"))
# A version string, or the literal "unknown" when upstream does not disclose
# it. Never invent one.
Version = Annotated[str, StringConstraints(min_length=1, max_length=128)]
Label = Annotated[str, StringConstraints(min_length=1, max_length=256)]
PageIndex = Annotated[int, Field(ge=0)]

CAPABILITIES = ("layout", "ocr", "table_structure", "cell_geometry", "image_crops",
                "chart_reading", "cross_page_table", "full_document_postprocess")


class Capability(Contract):
    state: Literal["supported", "unsupported", "unknown"]
    limitations: list[Label]
    # Fixture IDs that demonstrate the claim; "supported" without evidence is
    # only an assertion and is rejected.
    fixture_evidence: list[ResourceId]

    @model_validator(mode="after")
    def _evidence(self):
        if self.state == "supported" and not self.fixture_evidence:
            raise ValueError("supported requires at least one fixture_evidence")
        return self


class CapabilityManifest(Contract):
    """Each capability is declared on its own: OCR support says nothing about
    chart reading."""

    parser_name: ResourceId
    adapter_revision: ResourceId
    layout: Capability
    ocr: Capability
    table_structure: Capability
    cell_geometry: Capability
    image_crops: Capability
    chart_reading: Capability
    cross_page_table: Capability
    full_document_postprocess: Capability


class ModelRevision(Contract):
    name: Label
    revision: Version          # "unknown" if not disclosed
    checksum: Optional[Sha256]  # null if not obtainable


class PageMapEntry(Contract):
    parser_page: Annotated[int, Field(ge=0)]  # the parser's own page numbering
    page_index: PageIndex                      # physical page (SPEC 4.1)


class RenderSettings(Contract):
    dpi: Annotated[int, Field(ge=72, le=600)]
    max_megapixels: Annotated[FiniteFloat, Field(gt=0, le=16)]


class ParseManifest(Contract):
    parser_name: ResourceId
    package_or_service_version: Version
    adapter_revision: ResourceId
    output_schema: Version
    model_revisions: list[ModelRevision]
    requested_profile: ResourceId
    effective_profile: ResourceId
    fallback_reason: Optional[Label]
    ocr_languages: list[Annotated[str, StringConstraints(pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")]]
    page_selection: Annotated[list[PageIndex], Field(min_length=1)]
    page_index_map: Annotated[list[PageMapEntry], Field(min_length=1)]
    render_settings: RenderSettings
    postprocess_flags: list[Label]
    endpoint_deployment_id: Optional[ResourceId]
    started_at: AwareDatetime
    finished_at: AwareDatetime
    cost_usd: Optional[Annotated[FiniteFloat, Field(ge=0)]]  # null = unknown, 0 = confirmed free
    warnings: list[Label]
    # True only when every version is pinned; such manifests may be used by a
    # reproducible formal profile.
    reproducible: bool

    @model_validator(mode="after")
    def _consistency(self):
        if self.finished_at < self.started_at:
            raise ValueError("finished_at is before started_at")
        if (self.requested_profile != self.effective_profile) != (self.fallback_reason is not None):
            raise ValueError("fallback_reason is required exactly when effective_profile differs from requested")
        _unique(self.page_selection, "page_selection index")
        _unique([e.parser_page for e in self.page_index_map], "parser_page")
        mapped = [e.page_index for e in self.page_index_map]
        _unique(mapped, "mapped page_index")
        if set(mapped) != set(self.page_selection):
            raise ValueError("page_index_map must cover exactly the selected pages")
        _unique(self.ocr_languages, "ocr_language")
        _unique(self.postprocess_flags, "postprocess_flag")
        _unique([m.name for m in self.model_revisions], "model_revisions name")
        if self.reproducible:
            versions = [self.package_or_service_version, self.output_schema]
            versions += [m.revision for m in self.model_revisions]
            if any(_unpinned(v) for v in versions):
                raise ValueError("reproducible manifests must not contain unknown or floating (latest) versions")
        return self


class ParseRequest(Contract):
    document_artifact_ref: ResourceId
    page_indices: Annotated[list[PageIndex], Field(min_length=1)]
    profile_revision: ResourceId
    budget_ref: ResourceId
    operation_id: ResourceId

    @model_validator(mode="after")
    def _distinct(self):
        _unique(self.page_indices, "page_index")
        return self


class PageStatus(Contract):
    page_index: PageIndex
    status: Literal["ok", "partial", "needs_review", "failed"]
    diagnostics: list[Label]

    @model_validator(mode="after")
    def _explain(self):
        if self.status != "ok" and not self.diagnostics:
            raise ValueError("non-ok page status requires diagnostics")
        return self


class ParseResult(Contract):
    operation_id: ResourceId
    manifest_ref: ResourceId
    raw_result_refs: Annotated[list[ResourceId], Field(min_length=1)]
    normalized_evidence_refs: list[ResourceId]
    pages: Annotated[list[PageStatus], Field(min_length=1)]

    @model_validator(mode="after")
    def _distinct(self):
        _unique([p.page_index for p in self.pages], "page_index")
        _unique(self.raw_result_refs, "raw_result_ref")
        _unique(self.normalized_evidence_refs, "normalized_evidence_ref")
        return self

    def check_against_request(self, req: ParseRequest) -> list[str]:
        """Every requested page must be reported; failures may not vanish
        from coverage (SPEC 4.2)."""
        reasons = []
        if self.operation_id != req.operation_id:
            reasons.append("operation_id_mismatch")
        got = {p.page_index for p in self.pages}
        if got - set(req.page_indices):
            reasons.append("unrequested_pages_reported")
        if set(req.page_indices) - got:
            reasons.append("requested_pages_missing")
        return reasons
