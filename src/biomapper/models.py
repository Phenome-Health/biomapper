"""Pydantic v2 models for BioMapper2 API request/response shapes."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from biomapper.exceptions import BioMapperError

# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class MapEntityRequest(BaseModel):
    """Payload for POST /api/v1/map/entity.

    The ``options`` dict supports:
        - ``annotation_mode``: "missing" | "all" | "none"
        - ``annotators``: List of annotator names to use (e.g., ["kestrel-vector-search"])

    When ``annotators`` is not specified, BioMapper2 uses all available annotators,
    which may include fuzzy matchers that return low-confidence matches for vendor codes.
    """

    name: str
    entity_type: str = "biolink:SmallMolecule"
    identifiers: dict[str, str | list[str]] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=lambda: {"annotation_mode": "missing"})

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class ApiMetadata(BaseModel):
    """Metadata block returned alongside every API result."""

    request_id: str = ""
    processing_time_ms: float = 0.0


class ResolutionCertificate(BaseModel):
    """What the graph asserts about ``chosen_kg_id``, and what independent evidence says about it.

    Mirrors the API's ``ResolutionCertificateModel``. Every field has a default so an older
    server (or a mapping that failed before the certificate was assembled) still parses.

    Notable values:
        state:   ``"corroborated"`` | ``"uncorroborated"`` | ``"contradicted"`` |
                 ``"unavailable"`` | ``"not_applicable"``. ``"contradicted"`` means a human
                 should look, never that the resolver is wrong; ``"unavailable"`` means there
                 was nothing to check against, which is unverifiable rather than wrong.
        independent_of_selection: ``False`` when the independent source is the same registry
                 that supplied the chosen node, so corroboration there would be circular.
    """

    state: str = "not_applicable"
    structure_status: str = "not_applicable"
    node_inchikey_blocks: list[str] = Field(default_factory=list)
    comparison_rule: str = ""
    equivalent_ids_lookup_ok: bool = True
    selection_conflict: str | None = None
    independent_source: str | None = None
    independent_inchikey_block: str | None = None
    independent_of_selection: bool | None = None
    tier_b_outcome: str = "off"
    lipid_resolution_level: str = "unavailable"
    refusal_reason: str | None = None
    refmet_availability: str = "not_queried"
    refmet_source: str = "not_queried"
    refmet_snapshot_version: str | None = None
    tier_b_snapshot_version: str | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)


class LipidResolution(BaseModel):
    """Lipid hierarchy-aware resolution detail for ``chosen_kg_id``; ``None`` for non-lipid rows.

    ``mapping_relation`` (``"exact"`` | ``"broad"`` | ``"narrow"`` | ``"unknown"``) is the
    canonical successor of the flat ``chosen_kg_id_lipid_hint`` field: ``"broad"`` is exactly the
    ``"lipid_generalized"`` case that hint marks.
    """

    query_lipid_level_asserted: str | None = None
    query_lipid_level_effective: str | None = None
    matched_lipid_level: str | None = None
    mapping_relation: str = "unknown"
    mapping_predicate: str | None = None
    query_transformed: str | None = None
    ambiguous: bool = False
    candidate_structure_count: int | None = None
    ambiguity_basis: str | None = None
    goslin_dialect: str | None = None
    goslin_formula: str | None = None
    goslin_mass: float | None = None


class KestrelRequestParams(BaseModel):
    """The parameters actually sent for the passthrough rows, so a caller can reproduce the call."""

    search_text: str = ""
    limit: int = 0
    category: str = ""
    prefix: list[str] | None = None


class KestrelSearchResult(BaseModel):
    """Raw Kestrel passthrough rows for one search endpoint the pipeline used.

    Present only when the request set ``kestrel_top_n``. Passthrough only: these rows never
    affect ``chosen_kg_id``, ``assigned_ids`` or the certificate. ``rows`` holds the raw dicts
    exactly as Kestrel returned them — no coercion, no null-filling, nothing added or dropped —
    which is why they are typed as plain dicts rather than a model. They are UNTRUSTED external
    data: any renderer must treat every field as unescaped and unverified.
    """

    endpoint: str = ""
    request: KestrelRequestParams = Field(default_factory=KestrelRequestParams)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    fetch_strategy: str = "separate_call"
    error: str | None = None


class RawApiResult(BaseModel):
    """Direct mapping of the ``result`` object in the API response.

    Kept intentionally permissive so the client doesn't break on API additions.
    """

    name: str | None = None
    curies: list[str] = Field(default_factory=list)
    chosen_kg_id: str | None = None
    chosen_kg_id_review: str | None = None
    chosen_kg_id_lipid_hint: str | None = None
    resolution_certificate: ResolutionCertificate | None = None
    lipid_resolution: LipidResolution | None = None
    refmet_availability: str = "not_queried"
    refmet_source: str = "not_queried"
    refmet_snapshot_version: str | None = None
    tier_b_snapshot_version: str | None = None
    kg_equivalent_ids: dict[str, list[str]] = Field(default_factory=dict)
    kg_ids: dict[str, list[str]] = Field(default_factory=dict)
    assigned_ids: dict[str, dict[str, dict[str, Any]]] = Field(default_factory=dict)
    kestrel_results: list[KestrelSearchResult] | None = None
    error: str | None = None


class RawApiResponse(BaseModel):
    """Top-level envelope returned by POST /map/entity."""

    result: RawApiResult | None = None
    metadata: ApiMetadata = Field(default_factory=ApiMetadata)


class BatchMappingResponse(BaseModel):
    """Top-level envelope returned by POST /map/batch.

    The per-entity ``results`` list uses the flat :class:`RawApiResult` shape
    (not :class:`RawApiResponse` — the batch endpoint does not wrap each entry
    in its own envelope).
    """

    results: list[RawApiResult] = Field(default_factory=list)
    metadata: ApiMetadata = Field(default_factory=ApiMetadata)
    # Values are NOT uniformly ints: the API types this as ``{str: int | {str: int}}`` and returns
    # a nested ``refmet_source_counts`` map alongside the scalar tallies. Typing this as a flat
    # ``dict[str, int]`` made the whole batch response fail validation, which the batch loop then
    # converted into a per-entity error for every record in the chunk.
    summary: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Discovery endpoint models
# ---------------------------------------------------------------------------


class EntityTypeInfo(BaseModel):
    """One Biolink entity type with aliases and default vocabulary prefixes.

    The API may return either:
    - **New shape** (v2): an array of ``{type, aliases?, defaultPrefixes?}`` objects
    - **Old shape** (v1): ``{entity_types: [...], aliases: {...}}`` dict

    The client handles both transparently. ``default_prefixes`` is empty when
    the server returns the old shape (which has no prefix data).
    """

    model_config = ConfigDict(populate_by_name=True)

    type: str
    aliases: list[str] = Field(default_factory=list)
    default_prefixes: list[str] = Field(default_factory=list, alias="defaultPrefixes")

    @model_validator(mode="before")
    @classmethod
    def _coerce_nulls(cls, data: Any) -> Any:  # noqa: ANN401
        """Coerce ``null`` values to empty lists for list fields.

        The server sends ``"aliases": null`` and ``"defaultPrefixes": null``
        for entities without these values, but the model uses non-optional
        ``list[str]`` fields with empty defaults.
        """
        if isinstance(data, dict):
            if data.get("aliases") is None:
                data["aliases"] = []
            # Handle both wire name (defaultPrefixes) and Python name (default_prefixes)
            if data.get("defaultPrefixes") is None:
                data["defaultPrefixes"] = []
            if data.get("default_prefixes") is None:
                data["default_prefixes"] = []
        return data


class AnnotatorInfo(BaseModel):
    """Metadata about one available annotator."""

    slug: str
    name: str
    description: str | None = None


class VocabularyInfo(BaseModel):
    """Metadata about one supported identifier vocabulary."""

    prefix: str
    iri: str | None = None
    aliases: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Derived / user-facing model
# ---------------------------------------------------------------------------


class MappingResult(BaseModel):
    """Clean, flat representation of a single compound mapping.

    Derived from :class:`RawApiResponse` via :meth:`from_api_response`.

    Attributes:
        query_name:       The name submitted to the API (after preprocessing).
        resolved:         Whether at least one identifier was returned.
        primary_curie:    First CURIE in the response list.
        chosen_kg_id:     The resolver-selected knowledge graph ID.
        chosen_kg_id_review: Review flag for source-weighted small-molecule ChEBI conflicts
            (``"divergent_refmet"`` or ``"conflict_no_structure"``); ``None`` when no review
            is warranted.
        confidence_score: Highest confidence score found across annotators.
        identifiers:      Vocabulary → list[id] mapping (e.g. ``{"CHEBI": ["15971"]}``).
        hmdb_hint:        HMDB hint that was passed in the request, if any.
        error:            Error message if the request or mapping failed.
        raw_response:     Full parsed API response for downstream inspection.
    """

    query_name: str
    resolved: bool = False
    primary_curie: str | None = None
    chosen_kg_id: str | None = None
    chosen_kg_id_review: str | None = None
    confidence_score: float | None = None
    identifiers: dict[str, list[str]] = Field(default_factory=dict)
    kg_equivalent_ids: dict[str, list[str]] = Field(default_factory=dict)
    certificate: ResolutionCertificate | None = None
    lipid_resolution: LipidResolution | None = None
    refmet_availability: str = "not_queried"
    refmet_source: str = "not_queried"
    refmet_snapshot_version: str | None = None
    tier_b_snapshot_version: str | None = None
    kestrel_results: list[KestrelSearchResult] | None = None
    hmdb_hint: str | None = None
    error: str | None = None
    raw_response: RawApiResponse | None = None

    @classmethod
    def from_api_response(
        cls,
        response: dict[str, Any],
        query_name: str,
        hmdb_hint: str | None = None,
    ) -> MappingResult:
        """Build a :class:`MappingResult` from a single-entity API response dict.

        Args:
            response:   Parsed JSON dict from the API (``{"result": ..., "metadata": ...}``).
            query_name: The name that was submitted.
            hmdb_hint:  Any HMDB identifier that was passed as a hint.

        Returns:
            A populated :class:`MappingResult`.
        """
        # Surface-level error (network / HTTP, captured before reaching this method)
        if "error" in response and "result" not in response:
            return cls(
                query_name=query_name,
                hmdb_hint=hmdb_hint,
                error=str(response["error"]),
            )

        parsed = RawApiResponse.model_validate(response)

        if parsed.result is None:
            return cls(
                query_name=query_name,
                hmdb_hint=hmdb_hint,
                error="Empty result from API",
                raw_response=parsed,
            )

        return cls._build_from_raw_result(
            parsed.result,
            query_name=query_name,
            hmdb_hint=hmdb_hint,
            raw_response=parsed,
        )

    @classmethod
    def from_batch_entry(
        cls,
        raw: RawApiResult,
        query_name: str,
        hmdb_hint: str | None = None,
    ) -> MappingResult:
        """Build a :class:`MappingResult` from one entry in a batch response.

        The batch endpoint (``POST /map/batch``) returns each result as a flat
        :class:`RawApiResult`, not wrapped in an envelope. ``raw_response`` is
        not populated on batch-derived results — callers needing full envelope
        data should use the single-entity endpoint.

        Args:
            raw:        One entry from ``BatchMappingResponse.results``.
            query_name: The name that was submitted for this entry.
            hmdb_hint:  Any HMDB identifier that was passed as a hint.

        Returns:
            A populated :class:`MappingResult`.
        """
        return cls._build_from_raw_result(
            raw, query_name=query_name, hmdb_hint=hmdb_hint, raw_response=None
        )

    @classmethod
    def _build_from_raw_result(
        cls,
        r: RawApiResult,
        *,
        query_name: str,
        hmdb_hint: str | None,
        raw_response: RawApiResponse | None,
    ) -> MappingResult:
        """Shared extraction from a :class:`RawApiResult` to a user-facing result.

        Used by both :meth:`from_api_response` (envelope-wrapped) and
        :meth:`from_batch_entry` (flat-shaped). The difference is upstream:
        envelope handling happens in the caller.
        """
        base: dict[str, Any] = {
            "query_name": query_name,
            "hmdb_hint": hmdb_hint,
            "raw_response": raw_response,
        }

        if r.error:
            base["error"] = r.error
            return cls(**base)

        if r.curies:
            base["resolved"] = True
            base["primary_curie"] = r.curies[0]

        base["chosen_kg_id"] = r.chosen_kg_id
        base["chosen_kg_id_review"] = r.chosen_kg_id_review
        base["kg_equivalent_ids"] = dict(r.kg_equivalent_ids)
        base["certificate"] = r.resolution_certificate
        base["lipid_resolution"] = r.lipid_resolution
        base["refmet_availability"] = r.refmet_availability
        base["refmet_source"] = r.refmet_source
        # Batch and dataset-stream results carry no raw_response, so a field parsed into
        # RawApiResult but not copied here is unreachable for those callers.
        base["refmet_snapshot_version"] = r.refmet_snapshot_version
        base["tier_b_snapshot_version"] = r.tier_b_snapshot_version
        base["kestrel_results"] = r.kestrel_results

        # Flatten assigned_ids → {vocab: [code, ...]} and extract best score
        identifiers: dict[str, list[str]] = {}
        best_score: float | None = None

        for _annotator, vocabs in r.assigned_ids.items():
            for vocab, codes in vocabs.items():
                for code, meta in codes.items():
                    identifiers.setdefault(vocab, []).append(code)
                    if isinstance(meta, dict) and "score" in meta:
                        score = float(meta["score"])
                        if best_score is None or score > best_score:
                            best_score = score

        base["identifiers"] = identifiers
        base["confidence_score"] = best_score

        return cls(**base)

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    def equivalent_ids_for(self, prefix: str) -> list[str]:
        """Return equivalent IDs for the given CURIE prefix.

        Example::

            result.equivalent_ids_for("HMDB")   # ["HMDB0000067"]
            result.equivalent_ids_for("LM")     # ["ST01010001", "ST01010093"]
        """
        return self.kg_equivalent_ids.get(prefix, [])

    def ids_for(self, vocab: str) -> list[str]:
        """Return all identifiers for the given vocabulary prefix.

        Example::

            result.ids_for("CHEBI")   # ["15971"]
            result.ids_for("refmet_id")  # ["RM0129894"]
        """
        return self.identifiers.get(vocab, [])

    @property
    def refusal_reason(self) -> str | None:
        """Why the resolver refused this entity, when it did. ``None`` without a certificate.

        Distinct from :attr:`error`: an error means the call failed, a refusal means the
        pipeline ran and declined to assert an answer.
        """
        return self.certificate.refusal_reason if self.certificate is not None else None

    @property
    def confidence_tier(self) -> str:
        """Human-readable confidence tier based on score.

        Returns one of ``"high"``, ``"medium"``, ``"low"``, or ``"unknown"``.
        """
        if self.confidence_score is None:
            return "unknown"
        if self.confidence_score >= 2.0:
            return "high"
        if self.confidence_score >= 1.0:
            return "medium"
        return "low"


class MappingSummary(BaseModel):
    """Aggregate statistics for a batch mapping run."""

    total_queried: int
    resolved: int
    unresolved: int
    errors: int
    resolution_rate: float
    vocabulary_coverage: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _compute_rate(cls, data: dict[str, Any]) -> dict[str, Any]:
        total = data.get("total_queried", 0)
        resolved = data.get("resolved", 0)
        if "resolution_rate" not in data:
            data["resolution_rate"] = resolved / total if total > 0 else 0.0
        return data

    @classmethod
    def from_results(cls, results: list[MappingResult]) -> MappingSummary:
        """Compute summary statistics from a list of mapping results."""
        resolved = sum(1 for r in results if r.resolved)
        errors = sum(1 for r in results if r.error and not r.resolved)
        vocab_coverage: dict[str, int] = {}
        for r in results:
            for vocab in r.identifiers:
                vocab_coverage[vocab] = vocab_coverage.get(vocab, 0) + 1
        total = len(results)
        return cls(
            total_queried=total,
            resolved=resolved,
            unresolved=total - resolved,
            errors=errors,
            resolution_rate=resolved / total if total > 0 else 0.0,
            vocabulary_coverage=vocab_coverage,
        )


# ---------------------------------------------------------------------------
# Dataset mapping result (for map_dataset_file_sync / map_dataset_file_iter)
# ---------------------------------------------------------------------------


class DatasetMappingResult(BaseModel):
    """Aggregated outcome of a dataset-file mapping run.

    Returned by :func:`biomapper.map_dataset_file_sync` and hand-assembled by
    callers of :meth:`biomapper.BioMapperClient.map_dataset_file_iter` that
    want a single return object. Captures the full result list, any summary
    statistics the server emitted, request metadata, and — if the stream was
    truncated by a transport-level failure — the error text so callers can
    distinguish clean runs from partial runs.

    The ``stats`` field is populated **only** if the NDJSON stream emits a
    terminal summary line. If the server contract omits summary lines,
    ``stats`` stays ``{}`` — this is expected, not a failure signal.

    ``metadata`` remains the default :class:`ApiMetadata` on mid-stream
    failures because no completion envelope ever arrived. Callers correlating
    partial results with server logs rely on other channels (e.g. enabling
    httpx request logging).

    Attributes:
        results:  Per-record mapping outcomes in the order the server emitted
                  them. May be empty (zero results, or initial-request error).
        stats:    Server-provided summary statistics, if any. Otherwise ``{}``.
        metadata: Server-provided request metadata. Empty defaults if the run
                  was truncated before the server emitted completion data.
        error:    Populated on mid-stream transport failure; holds the
                  exception's ``str(exc)``. ``None`` on clean runs. Partial
                  ``results`` are retained alongside the error.

    Usage:
        Two patterns, mirroring :meth:`httpx.Response.raise_for_status`:

        Exception semantics (notebooks, scripts)::

            result = map_dataset_file_sync(path, ...)
            result.raise_for_error()
            summary = summarize(result.results)

        Partial-results semantics (UIs, resumable workflows)::

            result = map_dataset_file_sync(path, ...)
            if result.error:
                log_and_render_partial(result.results, result.error)
            else:
                render_complete(result.results)
    """

    results: list[MappingResult] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)
    metadata: ApiMetadata = Field(default_factory=ApiMetadata)
    error: str | None = None

    def raise_for_error(self) -> None:
        """Raise :class:`BioMapperError` if :attr:`error` is set; otherwise do nothing.

        Mirrors :meth:`httpx.Response.raise_for_status`. The stored error text
        becomes the exception message, so the caller sees a typed exception
        carrying the original failure reason. Partial ``results`` are **not**
        cleared — they are still accessible on the instance after catching the
        exception::

            try:
                result.raise_for_error()
            except BioMapperError:
                salvage_partial(result.results)  # result.results still intact
                raise
        """
        if self.error is not None:
            raise BioMapperError(self.error)
