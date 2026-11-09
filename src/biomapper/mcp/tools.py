"""Result shaping and error wording for the interactive tools.

Kept free of the MCP SDK: these are pydantic models and pure functions, and ``server.py`` wires
them to tools. What the model sees is deliberately compact. ``raw_response`` and
``kestrel_results`` are dropped (they would blow through tool output limits), and every result
says plainly whether it resolved, was refused, came back unresolved, or failed, because an agent
that cannot tell "no match" from "the call failed" will report a failure as a finding.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field

from biomapper._pins import KESTREL_LABEL, kg_build_label, kg_verified
from biomapper._version import resolve_version
from biomapper.exceptions import (
    BioMapperAuthError,
    BioMapperError,
    BioMapperRateLimitError,
    BioMapperServerError,
    BioMapperTimeoutError,
)
from biomapper.models import MappingResult

EntityStatus = Literal["resolved", "unresolved", "refused", "error"]

# How many kg_equivalent_ids prefixes / ids per prefix an interactive result carries. Lipids and
# common metabolites carry dozens of cross-references; the full set is in map_file's output.
MAX_EQUIVALENT_PREFIXES = 12
MAX_IDS_PER_PREFIX = 5

SERVER_INSTRUCTIONS = (
    "BioMapper resolves biological entity names (metabolites, lipids, proteins, genes, clinical "
    "labs) to knowledge-graph identifiers over a single shared public deployment. It is SLOW: "
    "about 4-5 seconds per entity. Call list_entity_types first to pick the entity_type. Use "
    "map_entity for one entity and map_batch for up to 5. For more than 5 entities, write them to "
    "a TSV/CSV file and call map_file, which runs a resumable background job; poll job_status "
    "every minute or so rather than mapping entities one by one. Results report status "
    "'resolved', 'unresolved', 'refused', or 'error'; an error is a failed call, never a "
    "confirmed no-match."
)

MAP_ENTITY_DESCRIPTION = (
    "Map ONE entity name to knowledge-graph identifiers. Takes about 4-5 s. Call "
    "list_entity_types first to choose entity_type (e.g. biolink:SmallMolecule for metabolites). "
    "Optional identifiers are hints, e.g. {\"HMDB\": \"HMDB0000177\"}. For more than 5 entities "
    "use map_file instead; repeated single calls are refused after a limit."
)
MAP_BATCH_DESCRIPTION = (
    "Map up to 5 entities of one entity_type in a single request (about 4-5 s per entity, so up "
    "to ~25 s). More than 5 is refused: write them to a TSV/CSV and use map_file."
)
DISCOVERY_DESCRIPTIONS = {
    "list_entity_types": (
        "List the Biolink entity types BioMapper accepts, with aliases and default identifier "
        "prefixes. Call this first to choose entity_type for the mapping tools."
    ),
    "list_vocabularies": "List the identifier vocabularies (CURIE prefixes) BioMapper knows.",
    "list_annotators": "List the annotators the BioMapper mapping pipeline can use.",
}


class Provenance(BaseModel):
    """Which client, API, and KG build answered. ``kg_verified`` is false unless one is named."""

    client_version: str
    api_base_url: str
    kestrel_url: str
    kg_build: str = Field(description="KG build per Kestrel /health, or 'unknown'/'pending'")
    kg_verified: bool
    kg_label: str = KESTREL_LABEL
    pins_status: Literal["pending", "ready"]


class EntityResult(BaseModel):
    query_name: str
    status: EntityStatus
    primary_curie: str | None = None
    chosen_kg_id: str | None = None
    confidence_score: float | None = None
    confidence_tier: str = Field(
        default="unknown",
        description="high/medium/low from the annotator score. not_scored = resolved, but no "
        "annotator ran (typically because identifiers were supplied), so there is no score; "
        "this is not a sign of a weak match, so check certificate_state instead. unknown = "
        "not resolved.",
    )
    certificate_state: str | None = Field(
        default=None,
        description="corroborated/uncorroborated/contradicted/...; uncorroborated deserves a "
        "human look before it is reported as a finding",
    )
    refusal_reason: str | None = None
    reason: str | None = Field(default=None, description="Why unresolved, refused, or errored")
    kg_equivalent_ids: dict[str, list[str]] = Field(default_factory=dict)
    error: str | None = None
    # 1.7.0. Present only on weak rows: a strong row serializes exactly as it did in 1.6.3.
    weak: bool | None = Field(
        default=None,
        exclude_if=lambda v: v is None,
        description="true when the result is weak (unresolved, refused, uncorroborated, "
        "contradicted, or unavailable for a small molecule). Absent on strong rows.",
    )
    ref: str | None = Field(
        default=None,
        exclude_if=lambda v: v is None,
        description="Opaque reference for a weak row; pass it to get_candidates. Absent on "
        "strong rows.",
    )


class MapEntityResult(BaseModel):
    result: EntityResult
    provenance: Provenance


class MapBatchResult(BaseModel):
    results: list[EntityResult]
    provenance: Provenance


class EntityTypeEntry(BaseModel):
    type: str
    aliases: list[str] = Field(default_factory=list)
    default_prefixes: list[str] = Field(default_factory=list)


class EntityTypesResult(BaseModel):
    entity_types: list[EntityTypeEntry]


class VocabularyEntry(BaseModel):
    prefix: str
    aliases: list[str] = Field(default_factory=list)


class VocabulariesResult(BaseModel):
    vocabularies: list[VocabularyEntry]


class AnnotatorEntry(BaseModel):
    slug: str
    name: str
    description: str | None = None


class AnnotatorsResult(BaseModel):
    annotators: list[AnnotatorEntry]


class SystemicFailure(Exception):
    """Every row of a request failed the same way: one failure, not N per-entity errors."""


# ---------------------------------------------------------------------------
# Shaping
# ---------------------------------------------------------------------------


def entity_status(result: MappingResult) -> EntityStatus:
    """The one-word verdict. An error (including an empty-string error) is never 'unresolved'."""
    if result.error is not None:
        return "error"
    if result.refusal_reason:
        return "refused"
    if result.chosen_kg_id or result.resolved:
        return "resolved"
    return "unresolved"


WEAK_STATUSES = frozenset({"unresolved", "refused"})
WEAK_CERTIFICATES = frozenset({"uncorroborated", "contradicted"})
# The Unit 0 spike (2026-11-09) found certificate ``unavailable`` hid engine errors on small
# molecules (0 of 11 engine-right), so it is weak there. For genes and proteins the certificate is
# structural and ``unavailable`` is routine, so it is not.
SMALL_MOLECULE_TYPES = frozenset({"smallmolecule", "small_molecule", "small molecule",
                                  "metabolite", "lipid"})


def is_small_molecule(entity_type: str) -> bool:
    """Whether a request's entity type is a small molecule, by Biolink name or common alias."""
    value = (entity_type or "").strip().lower()
    if value.startswith("biolink:"):
        value = value[len("biolink:"):]
    return value in SMALL_MOLECULE_TYPES


def weak_trigger(result: MappingResult, entity_type: str) -> str | None:
    """Why a result is weak (the trigger name), or ``None`` for a strong row or a failed call.

    An error is a failed call, not a weak answer, so it is never weak.
    """
    status = entity_status(result)
    if status == "error":
        return None
    if status in WEAK_STATUSES:
        return status
    certificate = result.certificate.state if result.certificate is not None else None
    if certificate in WEAK_CERTIFICATES:
        return certificate
    if certificate == "unavailable" and is_small_molecule(entity_type):
        return "unavailable"
    return None


def is_weak(result: MappingResult, entity_type: str) -> bool:
    return weak_trigger(result, entity_type) is not None


def _trim_equivalents(ids: dict[str, list[str]]) -> dict[str, list[str]]:
    return {
        prefix: list(values[:MAX_IDS_PER_PREFIX])
        for prefix, values in sorted(ids.items())[:MAX_EQUIVALENT_PREFIXES]
    }


def error_text(result: MappingResult) -> str:
    """A non-empty description of a row error (``str(exc)`` can be empty for some timeouts)."""
    if result.error:
        if result.error == "Empty result from API":
            return (
                "Empty result from API. This is an unknown failure, not a confirmed no-match; "
                "retry later."
            )
        return result.error
    return "Unknown error (the request failed with an empty message)."


NOT_SCORED = "not_scored"


def mcp_confidence_tier(result: MappingResult, status: EntityStatus) -> str:
    """The client's tier, except a resolved row without a score reads ``not_scored``.

    The engine scores only annotator matches. With identifiers supplied (annotation_mode
    ``missing``) no annotator runs, so a correct, ID-anchored resolution has no score. The
    client reports that as ``unknown``, which agents in the 2026-11-04 live trials read as
    "low quality" and flagged on every row.
    """
    if status == "resolved" and result.confidence_score is None:
        return NOT_SCORED
    return result.confidence_tier


def shape_result(result: MappingResult) -> EntityResult:
    status = entity_status(result)
    reason: str | None = None
    if status == "error":
        reason = error_text(result)
    elif status == "refused":
        reason = result.refusal_reason
    elif status == "unresolved":
        reason = "BioMapper returned no candidate identifier for this name and entity_type."
    certificate = result.certificate.state if result.certificate is not None else None
    return EntityResult(
        query_name=result.query_name,
        status=status,
        primary_curie=None if status == "error" else result.primary_curie,
        chosen_kg_id=None if status == "error" else result.chosen_kg_id,
        confidence_score=result.confidence_score,
        confidence_tier=mcp_confidence_tier(result, status),
        certificate_state=certificate,
        refusal_reason=result.refusal_reason,
        reason=reason,
        kg_equivalent_ids=_trim_equivalents(result.kg_equivalent_ids),
        error=error_text(result) if status == "error" else None,
    )


def systemic_error(results: Sequence[MappingResult], n_sent: int) -> str | None:
    """Why a batch failed as a whole, or ``None`` when its rows are individually meaningful.

    ``map_entities`` turns a 429/5xx/timeout/malformed response into one identical ``error`` per
    row, and only warns on a length mismatch. Either shape is one failure.
    """
    if len(results) != n_sent:
        return (
            f"BioMapper returned {len(results)} results for {n_sent} entities, so results cannot "
            f"be aligned to requests. Nothing from this batch was used."
        )
    if not results:
        return None
    errors = [r.error for r in results]
    if all(e is not None for e in errors) and len(set(errors)) == 1:
        return error_text(results[0])
    return None


# Prefixes of the ``str(exc)`` that ``map_entities`` writes into every row of a chunk that failed
# as a whole (429, 5xx, timeouts, transport errors, a response that would not parse or align).
_SYSTEMIC_MARKERS = (
    "Rate limit exceeded",
    "Server error (HTTP",
    "timed out",
    "Timeout",
    "zip()",
    "validation error",
    "All connection attempts failed",
    "Connection",
    "Server disconnected",
)


def looks_systemic(message: str) -> bool:
    """Whether a lone row's error reads like a whole-request failure rather than its own answer.

    Only needed for a chunk of one row, where "every row shares one error" says nothing.
    """
    return not message.strip() or any(m in message for m in _SYSTEMIC_MARKERS) or (
        message.startswith("Unknown error")
    )


def explain_client_error(exc: BaseException) -> str:
    """Readable wording for the client's typed exceptions. Never includes the API key."""
    if isinstance(exc, BioMapperRateLimitError):
        wait = f" Retry after {exc.retry_after:g} s." if exc.retry_after else " Retry later."
        return f"BioMapper is rate limiting requests (HTTP 429).{wait}"
    if isinstance(exc, BioMapperServerError):
        return (
            f"BioMapper server error (HTTP {exc.status_code}). The shared deployment may be "
            f"overloaded; retry later, or use map_file for many entities."
        )
    if isinstance(exc, BioMapperTimeoutError):
        return (
            "BioMapper did not answer in time; prod is slow right now. Retry later, or use "
            "map_file, which waits as long as it takes."
        )
    if isinstance(exc, BioMapperAuthError):
        return str(exc)
    if isinstance(exc, BioMapperError):
        return f"BioMapper request failed: {exc}"
    return f"BioMapper request failed: {type(exc).__name__}"


def rate_limit_hint(message: str) -> str:
    if message.startswith("Rate limit exceeded"):
        return "BioMapper is rate limiting requests (HTTP 429). Retry later."
    return message


def build_provenance(
    *, pins: dict[str, Any] | None, base_url: str, kestrel_url: str
) -> Provenance:
    if pins is None:
        return Provenance(
            client_version=resolve_version(),
            api_base_url=base_url,
            kestrel_url=kestrel_url,
            kg_build="pending",
            kg_verified=False,
            pins_status="pending",
        )
    return Provenance(
        client_version=resolve_version(),
        api_base_url=base_url,
        kestrel_url=kestrel_url,
        kg_build=kg_build_label(pins),
        kg_verified=kg_verified(pins),
        pins_status="ready",
    )
