"""The API-backed stand-in for ``biomapper2.mapper.Mapper``.

The whole migration turns on this one seam. The engine harness called
``Mapper.map_dataset_to_kg(dataset, entity_type, name_column, provided_id_columns, vocab,
annotation_mode, output_dir, output_prefix) -> (output_tsv, stats)`` from a single runner, so
reproducing that signature against the REST API ports every arm without rewriting any of them.

What this measures is deliberately different from what the in-process harness measured:
running against a deployment measures the service the paper describes, and pins provenance to
the backend that actually served the answers rather than to a client checkout.

Differences from the engine path, each of which is a recorded fact rather than a silent one:

* **Column set.** The engine's TSV also carries intermediate annotation/normalization/linking
  columns. Only the scored surface is reproduced here (``chosen_kg_id``,
  ``kg_equivalent_ids``, the ``certificate_*`` flat columns, the ``lipid_*`` flat columns,
  ``curies``, ``assigned_ids``). No scorer in the ported suite reads an intermediate column.
* **Dict-valued columns** are written as JSON. ``ast.literal_eval`` — what the ported
  ``curie_scorer`` uses — parses JSON objects, so this is compatible with the engine's
  ``repr``-style cells while being readable by anything else.
* **Server options are the client's typed keyword arguments**, not a generic ``options`` dict.
  ``MappingOptions`` sets ``extra: "ignore"`` server-side, so a passthrough dict would work — but
  offering two ways to set ``vocab`` is worse than either one, and the suite needs nothing the
  client does not already model. If a future arm needs an unmodelled option, add it to the client
  rather than reintroducing a bypass here.
* **The assigned/provided split** is derived from whether a row carries ``assigned_ids``
  rather than from the engine's internal ``kg_ids_assigned``. Under name-only input
  (``provided_id_columns=[]``, ``annotation_mode='all'``) a mapping can only come through the
  annotate path, so this reproduces the anti-trivial guard exactly: if the gold leaked in as a
  provided id, ``assigned_ids`` is empty for those rows and the guard fires.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from biomapper.benchmarks.checkpoint import EntityCache, cache_key
from biomapper.benchmarks.pacing import Pacer
from biomapper.client import BioMapperClient
from biomapper.exceptions import BioMapperRateLimitError, BioMapperServerError
from biomapper.models import MappingResult

DEFAULT_BATCH_SIZE = 20
DEFAULT_MAX_RETRIES = 4
DEFAULT_BACKOFF_S = 2.0

# Certificate fields flattened onto the mapped TSV, mirroring the engine's
# ``ResolutionCertificate.to_flat_columns`` naming so a ported consumer reads the same names.
# `resolution_level` / `resolution_level_worst` are absent: the API does not expose them, and
# no arm in this suite scores off them. Listed here rather than inferred so the omission is
# visible instead of looking like an oversight.
CERTIFICATE_FIELDS: tuple[str, ...] = (
    "state",
    "structure_status",
    "node_inchikey_blocks",
    "comparison_rule",
    "equivalent_ids_lookup_ok",
    "selection_conflict",
    "independent_source",
    "independent_inchikey_block",
    "independent_of_selection",
    "tier_b_outcome",
    "lipid_resolution_level",
    "refusal_reason",
    "refmet_availability",
    "refmet_source",
    "refmet_snapshot_version",
    "tier_b_snapshot_version",
)

LIPID_FIELDS: tuple[str, ...] = (
    "query_lipid_level_asserted",
    "query_lipid_level_effective",
    "matched_lipid_level",
    "mapping_relation",
    "mapping_predicate",
    "query_transformed",
    "ambiguous",
    "candidate_structure_count",
    "ambiguity_basis",
    "goslin_dialect",
    "goslin_formula",
    "goslin_mass",
)

logger = logging.getLogger(__name__)


class TrivialMappingError(RuntimeError):
    """Assigned mappings are zero under name-only input — the gold-as-provided trap.

    Name-only input with ``annotation_mode='all'`` must resolve through the annotate path.
    Zero assigned mappings means the gold column reached the mapper as a provided id, which
    would score a trivial 100%. Refuse the run.
    """


class BatchOrderMismatchError(RuntimeError):
    """The API returned batch results in a different order than they were sent.

    Fatal here, where it is merely a warning in the client. Predictions are joined to the
    held-out gold columns BY POSITION, so a reordered response scores each prediction against a
    different entity's gold — silently, and in a direction that could go either way. There is no
    stable per-row identity on the wire to realign by (the response echoes a name, which is not
    unique across a dataset: the MetaboliteAnnotator arms legitimately carry the same name in
    several accessions). So the only safe response is to refuse the arm.
    """


class NoProvidedMappingError(RuntimeError):
    """A provided-ID run produced zero KG mappings via the provided path.

    The name-input guard is inverted here: provided-ID mode runs with
    ``annotation_mode='none'``, so zero *assigned* mappings is expected and zero *provided*
    mappings is the failure. The source id never linked, which is a broken run rather than a
    scorable zero.
    """


class EmptyDatasetError(RuntimeError):
    """An adapter handed the mapper zero rows.

    A source that yields nothing is a broken run, not a score of zero. Without this guard an
    empty frame travels onward and surfaces much later as a confusing pandas join error that
    names schema columns and points at the wrong problem entirely — which is what happened to
    SwissLipids on 2026-08-05, when its source began returning HTTP 200 with a zero-byte body.
    """


@dataclass
class RequestCounters:
    """Per-arm request accounting. A reader can tell a clean run from one that limped.

    Recorded because the counts that previously circulated were read off a log by hand: a
    5xx-heavy arm and a clean arm produced numbers that looked equally trustworthy.
    """

    batches: int = 0
    entities: int = 0
    retries: int = 0
    server_errors: int = 0
    rate_limited: int = 0
    failed_entities: int = 0
    # Entities answered from a resume cache. Kept out of ``entities`` and ``seconds`` so the
    # throughput figure describes the deployment, not how much of the work was already on disk.
    cache_hits: int = 0
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def snapshot(self) -> dict[str, Any]:
        return {
            "batches": self.batches,
            "entities": self.entities,
            "retries": self.retries,
            "server_errors": self.server_errors,
            "rate_limited": self.rate_limited,
            "failed_entities": self.failed_entities,
            "cache_hits": self.cache_hits,
            "seconds": round(self.seconds, 2),
            "entities_per_second": round(self.entities / self.seconds, 3) if self.seconds else None,
            "errors": self.errors[:20],
        }


class ApiMapper:
    """Drives the mapping API over a DataFrame and writes the engine's scored column surface.

    Args:
        endpoint: API root, e.g. ``https://biomapper.expertintheloop.io/api/v1``.
        api_key: Optional key. Omit for a deployment with auth disabled.
        batch_size: Entities per ``/map/batch`` request.
        max_retries: Attempts per batch before its rows are recorded as errors. The public
            KRAKEN host returned 5xx under load during the 2026-08-05 suite run, so a batch
            retries with backoff; an arm that still fails is reported, never dropped.
        pacer: Optional :class:`~biomapper.benchmarks.pacing.Pacer`. Waited on immediately before
            every outgoing ``/map/batch`` request, including retries, and NEVER for an entity
            answered from ``cache`` (PR #9's rule: pace the request path, not cache hits).
        cache: Optional :class:`~biomapper.benchmarks.checkpoint.EntityCache`. Entities already in
            it are not sent; every batch's successful results are committed to it durably before
            the next request, so a killed run resumes from the last completed batch.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        api_key: str | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_s: float = DEFAULT_BACKOFF_S,
        timeout: float = 900.0,
        pacer: Pacer | None = None,
        cache: EntityCache | None = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self.batch_size = batch_size
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.timeout = timeout
        self.counters = RequestCounters()
        self.pacer = pacer
        self.cache = cache

    # ------------------------------------------------------------------
    # The Mapper-compatible entry point
    # ------------------------------------------------------------------

    def map_dataset_to_kg(
        self,
        dataset: pd.DataFrame,
        entity_type: str,
        name_column: str,
        provided_id_columns: list[str],
        vocab: str | list[str] | None = None,
        output_prefix: str | None = None,
        output_dir: str | Path = ".",
        annotation_mode: str = "missing",
        annotators: list[str] | None = None,
        prefer_canonical: bool | None = None,
        prefer_human: bool | None = None,
        candidate_limit: int | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Map every row and write ``{output_prefix}_MAPPED.tsv``. Returns ``(path, stats)``.

        Signature-compatible with ``biomapper2.mapper.Mapper.map_dataset_to_kg`` for the
        arguments the harness actually passes, so the ported runner and orchestrators call it
        unchanged.
        """
        if not isinstance(dataset, pd.DataFrame):
            raise TypeError(f"dataset must be a DataFrame for the API suite, got {type(dataset)!r}")
        if len(dataset) == 0:
            raise EmptyDatasetError(
                "the adapter produced 0 rows, so there is nothing to map. This is a broken run, "
                "not a score of zero. An HTTP 200 with an empty body reads as success to a "
                "streaming adapter and yields exactly this."
            )
        if name_column not in dataset.columns:
            raise KeyError(
                f"name_column {name_column!r} is not in the input frame: {list(dataset.columns)}"
            )

        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{output_prefix or 'input_df'}_MAPPED.tsv"

        records = self._build_records(dataset, name_column, provided_id_columns)
        results = asyncio.run(
            self._map_all(
                records,
                entity_type=entity_type,
                annotation_mode=annotation_mode,
                annotators=annotators,
                vocab=vocab,
                prefer_canonical=prefer_canonical,
                prefer_human=prefer_human,
                candidate_limit=candidate_limit,
            )
        )

        mapped_df = self._assemble_frame(dataset, results)
        mapped_df.to_csv(out_path, sep="\t", index=False)
        stats = self._build_stats(results, annotation_mode=annotation_mode)
        return str(out_path), stats

    def rows(self, mapped_df: pd.DataFrame) -> list[dict[str, Any]]:
        """Re-read the JSON-valued columns of a mapped frame into row dicts.

        Used to build the oracle from a frame that has been round-tripped through TSV, so the
        scoring path is identical whether it runs in-process or from a persisted artifact.
        """
        out: list[dict[str, Any]] = []
        for _, row in mapped_df.iterrows():
            out.append(
                {
                    "chosen_kg_id": None
                    if pd.isna(row.get("chosen_kg_id"))
                    else str(row.get("chosen_kg_id")),
                    "kg_equivalent_ids": _load_json_cell(row.get("kg_equivalent_ids")),
                    "certificate": {
                        "node_inchikey_blocks": _split_pipe(
                            row.get("certificate_node_inchikey_blocks")
                        ),
                        "structure_status": row.get("certificate_structure_status"),
                        "state": row.get("certificate_state"),
                    },
                }
            )
        return out

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _build_records(
        dataset: pd.DataFrame, name_column: str, provided_id_columns: list[str]
    ) -> list[dict[str, Any]]:
        """One API record per row: the name, plus any PROVIDED identifier columns.

        Columns outside ``provided_id_columns`` are never sent. That is the held-out-gold
        invariant: the gold columns ride along in the frame for the scorer and are invisible to
        the mapper.
        """
        records: list[dict[str, Any]] = []
        for _, row in dataset.iterrows():
            identifiers: dict[str, str] = {}
            for column in provided_id_columns:
                value = row.get(column)
                if value is None or (isinstance(value, float) and pd.isna(value)):
                    continue
                text = str(value).strip()
                # "-" and "NO_MATCH" are the sources' missing-value sentinels; the engine
                # replaces both with NaN before mapping, so they must not become real ids.
                if not text or text.lower() == "nan" or text in ("-", "NO_MATCH"):
                    continue
                identifiers[column] = text
            name = row.get(name_column)
            records.append(
                {
                    "name": ""
                    if name is None or (isinstance(name, float) and pd.isna(name))
                    else str(name),
                    "identifiers": identifiers,
                }
            )
        return records

    async def _map_all(
        self,
        records: list[dict[str, Any]],
        *,
        entity_type: str,
        annotation_mode: str,
        annotators: list[str] | None,
        vocab: str | list[str] | None,
        prefer_canonical: bool | None,
        prefer_human: bool | None,
        candidate_limit: int | None,
    ) -> list[MappingResult]:
        client_kwargs: dict[str, Any] = {"base_url": self.endpoint, "timeout": self.timeout}
        if self._api_key:
            client_kwargs["api_key"] = self._api_key
        else:
            client_kwargs["anonymous"] = True

        # Positional slots, so cache hits and fresh answers reassemble in input order. The gold is
        # joined BY POSITION downstream, so order is load-bearing here.
        results: list[MappingResult | None] = [None] * len(records)
        options = {
            "entity_type": entity_type,
            "annotation_mode": annotation_mode,
            "annotators": annotators,
            "candidate_limit": candidate_limit,
            "prefer_canonical": prefer_canonical,
            "prefer_human": prefer_human,
        }
        keys = [cache_key(r["name"], vocab, r["identifiers"], options) for r in records]
        pending: list[int] = []
        for index, key in enumerate(keys):
            hit = self.cache.get(key) if self.cache is not None else None
            if hit is not None:
                results[index] = hit
            else:
                pending.append(index)
        self.counters.cache_hits += len(records) - len(pending)

        n_batches = -(-len(pending) // self.batch_size) if pending else 0
        started = time.monotonic()
        if pending:
            async with BioMapperClient(**client_kwargs) as client:
                for batch_index, i in enumerate(range(0, len(pending), self.batch_size)):
                    slots = pending[i : i + self.batch_size]
                    answered = await self._map_chunk(
                        client,
                        [records[s] for s in slots],
                        entity_type=entity_type,
                        annotation_mode=annotation_mode,
                        annotators=annotators,
                        vocab=vocab,
                        prefer_canonical=prefer_canonical,
                        prefer_human=prefer_human,
                        candidate_limit=candidate_limit,
                    )
                    if len(answered) != len(slots):  # pragma: no cover - invariant guard
                        raise RuntimeError(
                            f"batch returned {len(answered)} results for {len(slots)} records; "
                            f"refusing to align gold columns against misaligned predictions."
                        )
                    for slot, result in zip(slots, answered, strict=True):
                        results[slot] = result
                    if self.cache is not None:
                        self.cache.commit_batch(
                            vocab=vocab,
                            batch_index=batch_index,
                            keyed_results=[
                                (keys[s], r) for s, r in zip(slots, answered, strict=True)
                            ],
                        )
                        self.cache.record_progress(
                            vocab,
                            batches_attempted=batch_index + 1,
                            n_batches=n_batches,
                            total=len(records),
                            entities_cached=self._cached_count(keys),
                        )
        elif self.cache is not None:
            self.cache.record_progress(
                vocab,
                batches_attempted=0,
                n_batches=0,
                total=len(records),
                entities_cached=self._cached_count(keys),
            )
        self.counters.seconds += time.monotonic() - started
        self.counters.entities += len(pending)
        if any(r is None for r in results):  # pragma: no cover - invariant guard
            raise RuntimeError(
                "a record was neither answered nor cached; refusing to align gold columns against "
                "misaligned predictions."
            )
        return [r for r in results if r is not None]

    def _cached_count(self, keys: list[str]) -> int:
        cache = self.cache
        return sum(1 for key in keys if cache is not None and cache.get(key) is not None)

    async def _map_chunk(
        self,
        client: BioMapperClient,
        chunk: list[dict[str, Any]],
        *,
        entity_type: str,
        annotation_mode: str,
        annotators: list[str] | None,
        vocab: str | list[str] | None,
        prefer_canonical: bool | None,
        prefer_human: bool | None,
        candidate_limit: int | None,
    ) -> list[MappingResult]:
        """Map one chunk, retrying transient server/rate-limit failures with backoff.

        ``map_entities`` already converts a failed chunk into per-record error results rather
        than raising, so a retry here is driven by inspecting those results: if every row in
        the chunk carries an error mentioning a 5xx or a rate limit, the chunk is retried.
        Partial failures are not retried — re-sending rows that succeeded would double-count
        them against the deployment.
        """
        delay = self.backoff_s
        last: list[MappingResult] = []
        for attempt in range(self.max_retries):
            self.counters.batches += 1
            if self.pacer is not None:
                # The request path only: cache hits never reach _map_chunk.
                self.pacer.wait()
            try:
                # The client warns (RuntimeWarning) when a returned entry's name does not match
                # the one sent at that position, then carries on matching positionally. For a
                # benchmark that is not a warning-level event, so the warning is captured and
                # escalated. Safe to use catch_warnings here because chunks are mapped
                # sequentially within this loop; it would not be safe under asyncio.gather.
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always")
                    last = await client.map_entities(
                        chunk,
                        entity_type=entity_type,
                        annotation_mode=annotation_mode,
                        annotators=annotators,
                        vocab=vocab,
                        prefer_canonical=prefer_canonical,
                        prefer_human=prefer_human,
                        candidate_limit=candidate_limit,
                    )
                self._assert_batch_order(caught, chunk)
            except BatchOrderMismatchError:
                raise  # never retried, never downgraded: the batch is unscorable
            except (BioMapperServerError, BioMapperRateLimitError) as exc:
                # map_entities normally absorbs these; a raise here is from the client layer.
                self._record_transport_error(exc)
                last = [MappingResult(query_name=r["name"], error=str(exc)) for r in chunk]

            if not self._chunk_wholly_transient(last):
                return last
            self.counters.retries += 1
            if attempt < self.max_retries - 1:
                logger.warning(
                    "Batch of %d failed transiently (attempt %d/%d); retrying in %.1fs",
                    len(chunk),
                    attempt + 1,
                    self.max_retries,
                    delay,
                )
                await asyncio.sleep(delay)
                delay *= 2
        return last

    @staticmethod
    def _assert_batch_order(
        caught: list[warnings.WarningMessage], chunk: list[dict[str, Any]]
    ) -> None:
        """Abort when the client reported a batch-order mismatch.

        Raised rather than logged: see :class:`BatchOrderMismatchError`. The exception escapes
        ``_map_chunk`` so the whole arm fails and the suite records it, instead of the affected
        rows quietly becoming unresolved predictions that still get scored.
        """
        mismatches = [str(w.message) for w in caught if "Batch order mismatch" in str(w.message)]
        if not mismatches:
            return
        raise BatchOrderMismatchError(
            f"the API returned {len(mismatches)} of {len(chunk)} batch result(s) out of order,"
            f" and predictions are joined to the held-out gold BY POSITION — so scoring this"
            f" batch would compare each prediction against another entity's gold. Refusing the"
            f" arm. First mismatch: {mismatches[0]}"
        )

    def _record_transport_error(self, exc: Exception) -> None:
        if isinstance(exc, BioMapperRateLimitError):
            self.counters.rate_limited += 1
        else:
            self.counters.server_errors += 1
        self.counters.errors.append(f"{type(exc).__name__}: {exc}")

    def _chunk_wholly_transient(self, results: list[MappingResult]) -> bool:
        """Whether every row failed for a reason worth retrying.

        Scoped to 5xx / 429 / timeout wording. A 422 or a per-record mapping error is a real
        answer and retrying it would just burn the deployment.
        """
        if not results:
            return False
        transient_markers = (
            "HTTP 5",
            "Server error",
            "Rate limit",
            "timed out",
            "ReadTimeout",
            "ConnectError",
        )
        wholly = all(r.error and any(m in r.error for m in transient_markers) for r in results)
        if wholly:
            for r in results:
                if r.error and "Rate limit" in r.error:
                    self.counters.rate_limited += 1
                else:
                    self.counters.server_errors += 1
                self.counters.errors.append(r.error or "")
        return wholly

    def _assemble_frame(self, dataset: pd.DataFrame, results: list[MappingResult]) -> pd.DataFrame:
        """Input frame (gold columns intact) joined to the scored prediction columns."""
        out = dataset.reset_index(drop=True).copy()
        prediction_rows: list[dict[str, Any]] = []
        for result in results:
            certificate = result.certificate
            lipid = result.lipid_resolution
            row: dict[str, Any] = {
                "chosen_kg_id": result.chosen_kg_id,
                "chosen_kg_id_review": result.chosen_kg_id_review,
                "curies": json.dumps([result.primary_curie] if result.primary_curie else []),
                "kg_equivalent_ids": json.dumps(result.kg_equivalent_ids or {}),
                "assigned_ids": json.dumps(result.identifiers or {}),
                "confidence_score": result.confidence_score,
                "mapping_error": result.error,
            }
            for name in CERTIFICATE_FIELDS:
                value = getattr(certificate, name, None) if certificate is not None else None
                # node_inchikey_blocks is a list; the engine writes it pipe-joined.
                row[f"certificate_{name}"] = "|".join(value) if isinstance(value, list) else value
            for name in LIPID_FIELDS:
                row[f"lipid_{name}"] = getattr(lipid, name, None) if lipid is not None else None
            prediction_rows.append(row)

        # Backstop, not a currently-reachable path: the client emits the mismatch as a WARNING and
        # still appends a normal result, so today it is always caught in `_map_chunk`.
        #
        # Reaching this check needs BOTH of two things at once, not either alone. Parallelizing
        # chunk mapping alone is not enough: under ordinary warning settings the client still
        # appends a normal result and never populates `error`. A global warnings-as-errors setting
        # alone is not enough either: the `simplefilter("always")` inside our own
        # `catch_warnings` block overrides it. It takes parallel mapping AND warnings raised as
        # errors — then one task's filter state can leak into another's `map_entities` call, the
        # warning raises inside the client's broad `except`, and it lands as a per-record error.
        # Narrow, but cheap to guard, and the alternative is a silently mis-scored arm.
        order_errors = [
            r.error for r in results if r.error and "Batch order mismatch" in r.error
        ]
        if order_errors:
            raise BatchOrderMismatchError(
                f"a batch-order mismatch reached result assembly ({len(order_errors)} row(s)); "
                f"predictions are joined to the held-out gold by position, so this frame cannot be "
                f"scored. First: {order_errors[0]}"
            )

        predictions = pd.DataFrame(prediction_rows, index=out.index)
        # Refuse to silently shadow an input column: an adapter emitting its own
        # `chosen_kg_id` would make the scorer read the input as the prediction.
        collisions = sorted(set(out.columns) & set(predictions.columns))
        if collisions:
            raise ValueError(
                f"input frame already carries prediction column(s) {collisions}; refusing to "
                f"overwrite them, since the scorer could not then tell input from prediction."
            )
        return out.join(predictions)

    def _build_stats(self, results: list[MappingResult], *, annotation_mode: str) -> dict[str, Any]:
        """Mapping stats, including the two counts the anti-trivial guards read."""
        mapped = sum(1 for r in results if r.chosen_kg_id)
        # Under name-only input a chosen node can only have come from the annotate path, so a
        # row carrying assigned identifiers is an assigned mapping. Under
        # annotation_mode='none' nothing is annotated, so a chosen node came from the
        # provided id.
        assigned = sum(1 for r in results if r.chosen_kg_id and r.identifiers)
        provided = sum(1 for r in results if r.chosen_kg_id and not r.identifiers)
        errors = sum(1 for r in results if r.error)
        self.counters.failed_entities += errors
        return {
            "n_rows": len(results),
            "mapped_to_kg": mapped,
            "mapped_to_kg_assigned": assigned if annotation_mode != "none" else 0,
            "mapped_to_kg_provided": provided if annotation_mode == "none" else 0,
            "errors": errors,
            "annotation_mode": annotation_mode,
            "request_counters": self.counters.snapshot(),
        }


def assigned_stats_nonnull(stats: dict[str, Any]) -> bool:
    """True iff the run produced at least one *assigned* KG mapping."""
    return int(stats.get("mapped_to_kg_assigned", 0) or 0) > 0


def mapped_provided_nonnull(stats: dict[str, Any]) -> bool:
    """True iff a provided-ID run produced at least one mapping via the *provided* path."""
    return int(stats.get("mapped_to_kg_provided", 0) or 0) > 0


def _load_json_cell(value: Any) -> dict[str, Any]:  # noqa: ANN401 - a pandas cell is genuinely untyped (str | float NaN | dict)
    """Parse a dict-valued cell written as JSON (or a Python repr, defensively)."""
    if isinstance(value, dict):
        return value
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return {}
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        import ast

        try:
            parsed = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            return {}
    return parsed if isinstance(parsed, dict) else {}


def _split_pipe(value: Any) -> list[str]:  # noqa: ANN401 - a pandas cell is genuinely untyped (str | float NaN | dict)
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return []
    return [p for p in text.split("|") if p]
