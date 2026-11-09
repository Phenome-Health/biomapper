"""``harmonize_many()``: harmonize two or more cohorts in one pinned run.

:func:`biomapper.harmonize_cohorts` handles exactly two cohorts and re-maps both on every call, and
its pairwise links do not chain into consistent groups. ``harmonize_many`` maps each cohort once,
saves the mappings for later runs, and groups rows across all cohorts by the chosen KRAKEN node:

1. **Read and validate everything first.** Labels, entity, columns and keys are checked, and the
   KG build is read, before any request is sent. A saved-mapping store made on a different build
   is refused before any request too (see :mod:`biomapper._mapping_store`).
2. **Names-only arm** (primary). Every cohort is mapped by name, no identifiers, through the store:
   an entity already saved on this build is reused, and each new entity is mapped once.
3. **Identifier arm.** Only vocabularies EVERY cohort declares are supplied, and only rows carrying
   such a code are re-mapped; the others reuse their names-only result. When nothing is shared
   the arm is skipped and the report says why. Its groups are diffed against names-only by node.
4. **Grouping** (:mod:`biomapper.harmonize.nway`, offline): node-anchored groups, flags for
   multiplicity and class-level ("broad") nodes, a cross-node review queue aggregated by node
   pair, and unresolved / errored / no-node rows listed per cohort. Links come from the unchanged
   :func:`biomapper.harmonize.harmonize`, called once per cohort pair, so every pair reconciles:
   same-node row pairs + cross-node link pairs = ``harmonize()``'s links.
5. **Identifier check** (:mod:`biomapper.harmonize.idcheck`, offline): each grouped row's declared
   identifiers are compared with its node. In the identifier arm those codes were sent to the
   mapper, so its statuses are labelled not independent.

The report is saved by default to ``biomapper_runs/harmonize_many_<UTC stamp>/``, each arm as soon
as it completes, and the absolute paths are printed. A run that reused any saved mapping is
``iteration-grade, not for publication``: the API exposes no engine fingerprint, so reuse cannot
detect engine drift on one KG build. Only a ``remap_all=True`` run on a verified build is
``publishable``. This module imports no pandas.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import sys
import uuid
import warnings
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from biomapper._mapping_store import ARMS, MappingStore, map_requests, pin_known
from biomapper._pins import collect_pins, kg_build_label, kg_verified
from biomapper._provenance import DEFAULT_KESTREL_URL
from biomapper.client import DEFAULT_BASE_URL
from biomapper.cohorts import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_RUNS_DIR,
    DEFAULT_TIMEOUT_S,
    ENTITY_ALIASES,
    NAME_LINKING_CATEGORIES,
    BatchedApiMapper,
    Cohort,
    CohortInput,
    CohortRecord,
    Mapper,
    _new_run_dir,
    _safe,
    _write_mapping,
    _write_tsv,
    read_cohort,
    request_checksum,
    resolve_category,
)
from biomapper.harmonize.idcheck import COMPARISON_RULE, ID_CHECK_STATUSES, IdCheck
from biomapper.harmonize.idcheck import check_identifiers as _check_identifiers
from biomapper.harmonize.linking import _require_distinct_labels
from biomapper.harmonize.nway import (
    BUCKETS,
    NodeGroup,
    NwayCohort,
    NwayResult,
    RowPair,
    group_cohorts,
)
from biomapper.models import MappingResult

__all__ = ["GroupDiff", "ManyCohortReport", "harmonize_many"]

ARM_NAMES_ONLY, ARM_IDENTIFIER = ARMS
STORE_DIRNAME = "harmonize_many_store"
# The matrix report's fixed columns; cohort labels may not reuse them (one column per label).
MATRIX_FIXED_HEADER: tuple[str, ...] = ("node", "n_cohorts", "clean", "multiplicity", "broad")
_RESERVED_LABELS = frozenset(h.casefold() for h in MATRIX_FIXED_HEADER)
RUN_PREFIX = "harmonize_many"
GRADE_PUBLISHABLE = "publishable"
GRADE_ITERATION = "iteration-grade, not for publication"
ID_CHECK_NOTE = (
    "not_confirmed means none of the row's declared identifiers is among its node's identifiers. "
    "It does NOT mean contradicted: the node may simply lack that cross-reference."
)
DRILLDOWN_HEADER = ["a_cohort", "a_key", "a_name", "a_node", "b_cohort", "b_key", "b_name",
                    "b_node", "basis", "bases"]


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewCheck:
    """A grouped row whose declared identifiers did not confirm its node."""

    cohort: str
    key: str
    name: str
    node: str | None
    check: IdCheck


@dataclass
class ArmReport:
    """One arm: what was sent and returned per cohort, the grouping, and the identifier checks."""

    arm: str
    records: dict[str, tuple[CohortRecord, ...]]
    sent: dict[str, list[dict[str, list[str]]]]
    results: dict[str, list[MappingResult]]
    nway: NwayResult
    id_checks: dict[tuple[str, str], IdCheck]
    id_check_independent: bool
    drilldown_file: Path | None = None

    @property
    def id_check_review(self) -> tuple[ReviewCheck, ...]:
        out = []
        for row in self.nway.rows:
            check = self.id_checks.get((row.cohort, row.key))
            if check is not None and check.status == "not_confirmed":
                out.append(ReviewCheck(row.cohort, row.key, row.name, row.node, check))
        return tuple(out)

    def summary(self) -> dict[str, Any]:
        groups = self.nway.groups
        per_cohort = self.nway.counts_by_cohort()
        statuses = Counter(c.status for c in self.id_checks.values())
        return {
            "groups": len(groups),
            "multi_cohort_groups": sum(1 for g in groups if len(g.cohorts) > 1),
            "clean_groups": sum(1 for g in groups if g.clean),
            "flagged_groups": sum(1 for g in groups if not g.clean),
            "multiplicity_groups": sum(1 for g in groups if g.multiplicity),
            "broad_groups": sum(1 for g in groups if g.broad),
            "broad_only_groups": sum(1 for g in groups if g.broad and not g.multiplicity),
            "cross_node_entries": len(self.nway.cross_node),
            "cross_node_row_pairs": sum(e.n_row_pairs for e in self.nway.cross_node),
            "withheld_name_pairs": len(self.nway.withheld),
            "per_cohort": per_cohort,
            "per_cohort_grouped_total": sum(c["grouped"] for c in per_cohort.values()),
            "pairs": [
                {"a": p.a, "b": p.b, "overlap_groups": p.overlap_groups,
                 "same_node_row_pairs": p.same_node_row_pairs,
                 "cross_node_link_pairs": p.cross_node_link_pairs,
                 "harmonize_links": p.harmonize_links, "reconciles": p.reconciles}
                for p in self.nway.pair_counts
            ],
            "id_check": {
                "independent": self.id_check_independent,
                "label": (
                    "independent of mapping" if self.id_check_independent
                    else "ID supplied to mapper, not independent"
                ),
                "comparison_rule": COMPARISON_RULE,
                "by_status": {s: statuses.get(s, 0) for s in ID_CHECK_STATUSES},
                "n_review": statuses.get("not_confirmed", 0),
                "note": ID_CHECK_NOTE,
            },
        }


@dataclass(frozen=True)
class GroupDiff:
    """Group nodes by which arm produced them, and shared nodes whose members changed."""

    names_only_only: tuple[str, ...]
    identifier_only: tuple[str, ...]
    both: tuple[str, ...]
    membership_changed: tuple[str, ...]

    def counts(self) -> dict[str, int]:
        return {"names_only_only": len(self.names_only_only),
                "identifier_only": len(self.identifier_only), "both": len(self.both),
                "membership_changed": len(self.membership_changed)}


def _members(group: NodeGroup) -> frozenset[tuple[str, str]]:
    return frozenset((m.cohort, m.key) for m in group.members)


def compute_group_diff(names_only: NwayResult, identifier: NwayResult) -> GroupDiff:
    a = {g.node: _members(g) for g in names_only.groups}
    b = {g.node: _members(g) for g in identifier.groups}
    both = sorted(set(a) & set(b))
    return GroupDiff(
        names_only_only=tuple(sorted(set(a) - set(b))),
        identifier_only=tuple(sorted(set(b) - set(a))),
        both=tuple(both),
        membership_changed=tuple(n for n in both if a[n] != b[n]),
    )


@dataclass
class ManyCohortReport:
    """Everything one :func:`harmonize_many` run produced.

    ``arms["names_only"]`` is always present; ``arms["identifier"]`` only when every cohort
    declared a shared vocabulary (``identifier_skipped_reason`` says why otherwise).
    """

    labels: tuple[str, ...]
    category: str
    settings: dict[str, Any]
    arms: dict[str, ArmReport]
    identifier_skipped_reason: str | None
    diff: GroupDiff | None
    pins: dict[str, Any]
    status: str
    grade: str
    store: dict[str, Any]
    mapping: dict[str, dict[str, dict[str, Any]]]
    warnings: tuple[str, ...]
    excluded: dict[str, tuple[dict[str, Any], ...]]
    session_id: str
    output_dir: Path | None = None
    _store_root: Path | None = field(default=None, repr=False)

    @property
    def names_only(self) -> ArmReport:
        return self.arms[ARM_NAMES_ONLY]

    @property
    def identifier(self) -> ArmReport | None:
        return self.arms.get(ARM_IDENTIFIER)

    def summary(self) -> dict[str, Any]:
        """Counts-only summary: JSON-safe, with no absolute paths."""
        return {
            "entity": self.settings["entity"],
            "category": self.category,
            "link_by_name": self.settings["link_by_name"],
            "labels": list(self.labels),
            "n_cohorts": len(self.labels),
            "status": self.status,
            "publication_grade": self.grade,
            "kg_build": kg_build_label(self.pins),
            "kg_verified": kg_verified(self.pins),
            "vocabularies": {
                "declared": dict(self.settings["vocabularies"]["declared"]),
                "shared": list(self.settings["vocabularies"]["shared"]),
            },
            "arms": {
                ARM_NAMES_ONLY: self.names_only.summary(),
                ARM_IDENTIFIER: self.identifier.summary() if self.identifier else None,
            },
            "identifier_arm_skipped": self.identifier_skipped_reason,
            "diff": self.diff.counts() if self.diff is not None else None,
            "store": dict(self.store),
            "mapping": self.mapping,
            "excluded": {label: len(rows) for label, rows in self.excluded.items()},
            "warnings": list(self.warnings),
            "session_id": self.session_id,
            "pins": self.pins,
        }

    # -- writing ---------------------------------------------------------

    def write(self, path: str | os.PathLike[str] | None = None) -> Path:
        """Write every table and the summary; return the directory.

        ``path`` defaults to this report's ``output_dir``, or a new
        ``biomapper_runs/harmonize_many_<UTC timestamp>/`` under the current directory.
        """
        if path is not None:
            directory = Path(path).resolve()
        elif self.output_dir is not None:
            directory = self.output_dir
        else:
            directory = _new_run_dir(Path(DEFAULT_RUNS_DIR).resolve(), RUN_PREFIX)
        directory.mkdir(parents=True, exist_ok=True)
        for arm in self.arms.values():
            _write_arm(directory, arm, self.labels)
        if self.diff is not None:
            _write_tsv(
                directory / "diff.tsv", ["node", "found_by"],
                [*([n, "names_only_only"] for n in self.diff.names_only_only),
                 *([n, "identifier_only"] for n in self.diff.identifier_only),
                 *([n, "both_membership_changed" if n in self.diff.membership_changed
                    else "both"] for n in self.diff.both)],
            )
        _write_json(directory / "settings.json", {**self.settings, "pins": self.pins})
        _write_json(directory / "summary.json", self.summary())
        self.output_dir = directory
        print(f"harmonize_many: wrote results to {directory}")
        if self._store_root is not None:
            print(f"harmonize_many: saved mappings in {self._store_root}")
        return directory


def _write_json(path: Path, payload: Any) -> None:  # noqa: ANN401 — any JSON-safe value
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")


def _pair_row(p: RowPair) -> list[Any]:
    return [p.a_cohort, p.a_key, p.a_name, p.a_node, p.b_cohort, p.b_key, p.b_name, p.b_node,
            p.basis, "|".join(sorted(p.bases))]


def _write_arm(directory: Path, arm: ArmReport, labels: tuple[str, ...]) -> None:
    name = arm.arm
    nway = arm.nway
    for label in labels:
        _write_mapping(directory, name, label, [
            (rec, sent, res) for rec, sent, res in
            zip(arm.records[label], arm.sent[label], arm.results[label], strict=True)
        ])
    group_of = {(m.cohort, m.key): g for g in nway.groups for m in g.members}
    records = {(label, r.key): r for label in labels for r in arm.records[label]}

    def membership_row(row: Any) -> list[Any]:  # noqa: ANN401 — a RowRef
        g = group_of.get((row.cohort, row.key))
        check = arm.id_checks.get((row.cohort, row.key))
        declared = records[(row.cohort, row.key)].identifiers
        return [
            row.cohort, row.key, row.name, row.bucket, row.node, row.chosen_kg_id,
            len(g.cohorts) if g else "", g.clean if g else "", g.multiplicity if g else "",
            g.broad if g else "", row.broad, row.error,
            check.status if check else "", check.reason if check else "",
            json.dumps({v: list(c) for v, c in declared.items()}, sort_keys=True) if declared
            else "",
            "|".join(check.matched) if check else "",
        ]

    _write_tsv(
        directory / f"membership_{name}.tsv",
        ["cohort", "key", "name", "bucket", "node", "chosen_kg_id", "group_n_cohorts",
         "group_clean", "group_multiplicity", "group_broad", "row_broad", "error", "id_check",
         "id_check_reason", "declared_ids", "id_check_matched"],
        (membership_row(r) for r in nway.rows),
    )
    _write_tsv(
        directory / f"matrix_{name}.tsv",
        [*MATRIX_FIXED_HEADER, *labels],
        (
            [g.node, len(g.cohorts), g.clean, g.multiplicity, g.broad,
             *(";".join(m.key for m in g.members if m.cohort == label) for label in labels)]
            for g in nway.groups
        ),
    )
    _write_tsv(
        directory / f"overlap_{name}.tsv",
        ["cohort_a", "cohort_b", "overlap_groups", "same_node_row_pairs",
         "cross_node_link_pairs", "harmonize_links", "reconciles"],
        ([p.a, p.b, p.overlap_groups, p.same_node_row_pairs, p.cross_node_link_pairs,
          p.harmonize_links, p.reconciles] for p in nway.pair_counts),
    )
    _write_tsv(
        directory / f"cross_node_{name}.tsv",
        ["node_a", "node_b", "bases", "cohorts", "n_row_pairs"],
        ([e.node_a, e.node_b, "|".join(sorted(e.bases)), "|".join(e.cohorts), e.n_row_pairs]
         for e in nway.cross_node),
    )
    drill = directory / f"cross_node_pairs_{name}.tsv"
    if nway.drilldown is not None:
        _write_tsv(drill, DRILLDOWN_HEADER, (_pair_row(p) for p in nway.drilldown))
    elif arm.drilldown_file is not None and arm.drilldown_file.resolve() != drill.resolve():
        shutil.copyfile(arm.drilldown_file, drill)
    _write_tsv(
        directory / f"withheld_{name}.tsv", ["a_cohort", "a_key", "b_cohort", "b_key"],
        ([w.a_cohort, w.a_key, w.b_cohort, w.b_key] for w in nway.withheld),
    )
    _write_tsv(
        directory / f"flagged_groups_{name}.tsv",
        ["node", "multiplicity", "broad", "cohorts", "members"],
        ([g.node, g.multiplicity, g.broad, "|".join(g.cohorts),
          "|".join(f"{m.cohort}:{m.key}" for m in g.members)] for g in nway.flagged_groups),
    )
    _write_tsv(
        directory / f"not_grouped_{name}.tsv",
        ["cohort", "key", "name", "bucket", "chosen_kg_id", "error"],
        ([r.cohort, r.key, r.name, r.bucket, r.chosen_kg_id, r.error]
         for r in nway.rows if r.bucket != "grouped"),
    )
    _write_tsv(
        directory / f"id_check_review_{name}.tsv",
        ["cohort", "key", "name", "node", "status", "declared", "reason", "independent"],
        ([i.cohort, i.key, i.name, i.node, i.check.status, "|".join(i.check.declared),
          i.check.reason, arm.id_check_independent] for i in arm.id_check_review),
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _per_cohort(
    value: Any,  # noqa: ANN401 — one setting for all cohorts, or a per-label mapping
    labels: Sequence[str],
    what: str,
    default: Any,  # noqa: ANN401
) -> dict[str, Any]:
    if value is None:
        return dict.fromkeys(labels, default)
    if isinstance(value, Mapping):
        unknown = [k for k in value if k not in labels]
        if unknown:
            raise ValueError(
                f"{what} names {unknown}, which is not a cohort label (labels: {list(labels)})"
            )
        return {label: value.get(label, default) for label in labels}
    return dict.fromkeys(labels, value)


def _require_labels(labels: Sequence[str]) -> None:
    if len(labels) < 2:
        raise ValueError(f"harmonize_many needs at least two cohorts, got {len(labels)}")
    for label in labels:
        if not isinstance(label, str) or not label.strip():
            raise ValueError(f"cohort labels must be non-empty strings, got {label!r}")
        if label.strip().casefold() in _RESERVED_LABELS:
            raise ValueError(
                f"cohort label {label!r} is reserved: the matrix report has a fixed "
                f"{label.strip()!r} column, which a cohort column of that name would overwrite. "
                f"Reserved: {sorted(MATRIX_FIXED_HEADER)}"
            )
    for i, a in enumerate(labels):
        for b in labels[i + 1 :]:
            # Each pair is linked by harmonize(), whose summary keys on the labels.
            _require_distinct_labels(a, b)
            if _safe(a) == _safe(b):
                raise ValueError(
                    f"cohort labels {a!r} and {b!r} both save as {_safe(a)!r}, so one cohort's "
                    "files would overwrite the other's; choose labels that differ in letters, "
                    "digits, '.', '-' or '_'."
                )


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def _mapper_identity(mapper: Any) -> str:  # noqa: ANN401 — any mapper callable
    """A stable name for an injected mapper: ``store_id`` if it has one, else module.qualname."""
    explicit = getattr(mapper, "store_id", None)
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    target = mapper if hasattr(mapper, "__qualname__") else type(mapper)
    module = getattr(target, "__module__", None) or "unknown"
    return f"{module}.{getattr(target, '__qualname__', type(mapper).__name__)}"


def _drilldown_writer(stack: ExitStack, directory: Path | None, arm: str) -> tuple[
    Any, Path | None
]:
    if directory is None:
        return None, None
    path = directory / f"cross_node_pairs_{arm}.tsv"
    fh: IO[str] = stack.enter_context(path.open("w", newline="", encoding="utf-8"))
    writer = csv.writer(fh, delimiter="\t", lineterminator="\n")
    writer.writerow(DRILLDOWN_HEADER)

    def sink(pair: RowPair) -> None:
        writer.writerow(["" if v is None else v for v in _pair_row(pair)])

    return sink, path


def harmonize_many(
    cohorts: Mapping[str, CohortInput],
    *,
    entity: str,
    name_columns: str | Mapping[str, str] = "name",
    vocabularies: Mapping[str, Mapping[str, str | Sequence[str]]] | None = None,
    key_columns: Mapping[str, str] | None = None,
    category: str | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    timeout: float = DEFAULT_TIMEOUT_S,
    progress: bool = False,
    mapper: Mapper | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    kestrel_url: str = DEFAULT_KESTREL_URL,
    probe_pins: bool = True,
    pins: Mapping[str, Any] | None = None,
    store_dir: str | os.PathLike[str] | None = None,
    output_dir: str | os.PathLike[str] | None = None,
    save: bool = True,
    remap_all: bool = False,
) -> ManyCohortReport:
    """Harmonize two or more cohort tables, grouping rows across all of them by chosen node.

    Args:
        cohorts: ``{label: table}``, each table a pandas DataFrame, a list of dicts, or a path to
            a TSV/CSV file. Labels key every output and must be distinct (also after filename
            sanitizing) and not a reserved ``harmonize()`` summary field.
        entity: ``"metabolites"``, ``"proteins"``, ``"genes"``, ``"labs"`` or a raw Biolink
            category. One entity type per run. Name links are on for small molecules only.
        name_columns: The name column for every cohort, or ``{label: column}`` (default
            ``"name"``).
        vocabularies: ``{label: {vocabulary: column(s)}}``: each cohort's declared identifier
            columns. Only vocabularies every cohort declares are supplied as mapping input (the
            identifier arm); all are compared offline with each row's node (the identifier check).
        key_columns: ``{label: column}`` of unique row keys; the default is ``"{row}|{name}"``.
        category: Override the category the alias resolves to.
        batch_size, timeout, progress: Default-mapper request policy (10 per batch, 300 s);
            ``batch_size`` is also how often mappings are saved.
        mapper: A ``(records, *, entity_type, annotation_mode) -> list[MappingResult]`` callable
            to use instead of the hosted API (a replay in tests). Its saved mappings are kept in
            their own part of the store (``custom_mappers/<id>/``, the id being the mapper's
            ``store_id`` attribute or its module and qualified name), never mixed with the API's.
        base_url, api_key: Passed to the default mapper and the API health pin.
        kestrel_url: The Kestrel root whose ``/health`` names the KG build.
        probe_pins: Read the API and Kestrel ``/health`` endpoints (default True).
        pins: An already-taken pin block (``{"kestrel": {"kg_build": ...}, ...}``) used instead of
            probing; for replays and offline fixtures.
        store_dir: The saved-mapping store. Default ``biomapper_runs/harmonize_many_store/`` when
            ``save`` is on, otherwise no store. Keep it outside throwaway directories.
        output_dir: Where to write; default ``biomapper_runs/harmonize_many_<UTC stamp>/``.
        save: Write results (default True).
        remap_all: Map every entity again on the current build instead of reusing saved mappings
            (the old ones are kept aside). Use it for numbers you will publish.

    Returns:
        A :class:`ManyCohortReport`.

    Raises:
        ValueError: Invalid labels, entity, columns or keys, before any request.
        biomapper._mapping_store.StoreBuildMismatch: The store holds mappings from a different KG
            build, before any request.
        biomapper._mapping_store.StoreLocked: Another run is writing the store.
    """
    labels = list(cohorts)
    _require_labels(labels)
    resolved_category = resolve_category(entity, category)
    link_by_name = resolved_category in NAME_LINKING_CATEGORIES
    names_by = _per_cohort(name_columns, labels, "name_columns", "name")
    vocabs_by = _per_cohort(vocabularies, labels, "vocabularies", None)
    keys_by = _per_cohort(key_columns, labels, "key_columns", None)
    read: list[Cohort] = [
        read_cohort(cohorts[label], label=label, name_column=names_by[label],
                    vocabularies=vocabs_by[label], key_column=keys_by[label])
        for label in labels
    ]

    api_base = base_url or DEFAULT_BASE_URL
    the_mapper: Mapper = mapper or BatchedApiMapper(
        batch_size=batch_size, timeout=timeout, base_url=base_url, api_key=api_key,
        progress=progress,
    )
    # The KG build is read before any mapping request, so a mismatch can be refused up front.
    pin_block = (
        dict(pins) if pins is not None
        else collect_pins(probe=probe_pins, base_url=api_base, api_key=api_key,
                          kestrel_url=kestrel_url)
    )
    # A custom mapper's saved answers are kept apart from the hosted API's (and from each other),
    # so a toy or test mapper can never be replayed as API output by a later run.
    mapper_id = None if isinstance(the_mapper, BatchedApiMapper) else _mapper_identity(the_mapper)
    run_pins = {**pin_block, "mapper": type(the_mapper).__name__,
                "mapper_pins": getattr(the_mapper, "pins", None)}
    session_id = uuid.uuid4().hex

    store_root: Path | None
    if store_dir is not None:
        store_root = Path(store_dir).resolve()
    elif save:
        store_root = Path(DEFAULT_RUNS_DIR).resolve() / STORE_DIRNAME
    else:
        store_root = None

    run_warnings: list[str] = []

    def say(message: str) -> None:
        if progress:
            print(f"harmonize_many: {message}", file=sys.stderr, flush=True)

    with MappingStore(store_root, pins=pin_block, base_url=api_base,
                      entity_type=resolved_category, annotation_mode="missing",
                      remap_all=remap_all, batch_size=batch_size,
                      session_id=session_id, mapper_id=mapper_id) as store, \
            ExitStack() as files:
        if store_root is not None and not pin_known(store.pin):
            run_warnings.append(f"{store.reason}.")
            warnings.warn(store.reason or "", UserWarning, stacklevel=2)

        directory: Path | None = None
        if save:
            directory = (
                Path(output_dir).resolve() if output_dir is not None
                else _new_run_dir(Path(DEFAULT_RUNS_DIR).resolve(), RUN_PREFIX)
            )
            directory.mkdir(parents=True, exist_ok=True)
            print(f"harmonize_many: run directory {directory}")
            if store.saving and store.root is not None:
                print(f"harmonize_many: saved-mapping store {store.root}")

        mapping: dict[str, dict[str, dict[str, Any]]] = {}

        def run_arm(arm: str, sent: dict[str, list[dict[str, list[str]]]],
                    base: dict[str, list[MappingResult]] | None) -> ArmReport:
            # Rows with nothing to send in the identifier arm keep their names-only result.
            index: list[tuple[str, int]] = []
            requests: list[dict[str, Any]] = []
            for c in read:
                for i, (rec, ids) in enumerate(zip(c.records, sent[c.label], strict=True)):
                    if base is None or ids:
                        index.append((c.label, i))
                        requests.append({"name": rec.name, "identifiers": ids})
            say(f"{arm} arm: {len(requests)} rows to map")
            outcome = map_requests(the_mapper, requests, store=store.arm(arm),
                                   entity_type=resolved_category, annotation_mode="missing")
            results = {c.label: list(base[c.label]) if base is not None
                       else [MappingResult(query_name=r.name) for r in c.records] for c in read}
            reused: dict[str, list[bool]] = {c.label: [] for c in read}
            for (label, i), res, was_reused in zip(index, outcome.results, outcome.reused,
                                                   strict=True):
                results[label][i] = res
                reused[label].append(was_reused)
            mapping[arm] = {}
            for c in read:
                provenance = {
                    "arm": arm,
                    "session_id": session_id,
                    "request_checksum": request_checksum(
                        [(r.key, r.name, ids) for r, ids in zip(c.records, sent[c.label],
                                                               strict=True)],
                        arm=arm, category=resolved_category, annotation_mode="missing",
                        name_column=c.name_column, key_column=c.key_column,
                        vocabularies={v: list(cols) for v, cols in c.vocabularies.items()},
                    ),
                    "source": dict(c.source),
                    "n_rows": len(c.records),
                    "n_sent_rows": len(reused[c.label]),
                    "n_reused": sum(reused[c.label]),
                    "n_fresh": len(reused[c.label]) - sum(reused[c.label]),
                    "n_errored": sum(1 for r in results[c.label] if r.error),
                }
                mapping[arm][c.label] = provenance
                if directory is not None:
                    _write_json(directory / f"provenance_{arm}_{_safe(c.label)}.json", provenance)
            sink, drill_path = _drilldown_writer(files, directory, arm)
            nway = group_cohorts(
                [NwayCohort(c.label, tuple(r.key for r in c.records), tuple(results[c.label]))
                 for c in read],
                link_by_name=link_by_name, drilldown_sink=sink,
            )
            records = {c.label: c.records for c in read}
            by_key = {(c.label, r.key): (r, res) for c in read
                      for r, res in zip(c.records, results[c.label], strict=True)}
            id_checks = {
                (row.cohort, row.key): _check_identifiers(
                    by_key[(row.cohort, row.key)][0].identifiers, by_key[(row.cohort, row.key)][1]
                )
                for row in nway.rows if row.bucket == "grouped"
            }
            report_arm = ArmReport(
                arm=arm, records=records, sent=sent, results=results, nway=nway,
                id_checks=id_checks, id_check_independent=arm == ARM_NAMES_ONLY,
                drilldown_file=drill_path,
            )
            if directory is not None:
                if drill_path is not None:
                    files.close()  # flush the streamed drill-down before anything reads it
                _write_arm(directory, report_arm, tuple(labels))
            return report_arm

        arms: dict[str, ArmReport] = {}
        arms[ARM_NAMES_ONLY] = run_arm(
            ARM_NAMES_ONLY, {c.label: [{} for _ in c.records] for c in read}, None
        )

        declared = {c.label: list(c.vocabularies) for c in read}
        shared = [v for v in declared[labels[0]] if all(v in declared[x] for x in labels)]
        skipped: str | None = None
        diff: GroupDiff | None = None
        if not any(declared.values()):
            skipped = "no identifier columns declared; names-only arm only"
        elif not shared:
            missing = {
                v: [x for x in labels if v not in declared[x]]
                for v in dict.fromkeys(v for vs in declared.values() for v in vs)
            }
            skipped = (
                "no vocabulary is declared by every cohort ("
                + "; ".join(f"{v} not declared by every cohort, missing for "
                            + ", ".join(repr(x) for x in miss) for v, miss in missing.items())
                + "); vocabularies not declared by every cohort are never supplied as mapping "
                "input, only compared offline in the identifier check"
            )
        else:
            sent = {c.label: [{v: list(r.identifiers[v]) for v in shared if v in r.identifiers}
                              for r in c.records] for c in read}
            arms[ARM_IDENTIFIER] = run_arm(
                ARM_IDENTIFIER, sent, arms[ARM_NAMES_ONLY].results
            )
            diff = compute_group_diff(arms[ARM_NAMES_ONLY].nway, arms[ARM_IDENTIFIER].nway)

        reused_any = any(p["n_reused"] for by in mapping.values() for p in by.values())
        all_rows = sum(len(c.records) for c in read)
        errored = sum(p["n_errored"] for p in mapping[ARM_NAMES_ONLY].values())
        status = "failed" if all_rows and errored == all_rows else "partial" if any(
            p["n_errored"] for by in mapping.values() for p in by.values()) else "ok"
        grade = (GRADE_PUBLISHABLE if remap_all and not reused_any and kg_verified(pin_block)
                 else GRADE_ITERATION)

        settings: dict[str, Any] = {
            "entity": entity,
            "category": resolved_category,
            "category_overridden": category is not None,
            "aliases": dict(ENTITY_ALIASES),
            "link_by_name": link_by_name,
            "labels": list(labels),
            "cohorts": {
                c.label: {
                    "name_column": c.name_column,
                    "key_column": c.key_column,
                    "vocabularies": {v: list(cols) for v, cols in c.vocabularies.items()},
                    "source": dict(c.source),
                    "n_excluded": len(c.excluded),
                }
                for c in read
            },
            "vocabularies": {"declared": declared, "shared": shared},
            "annotation_mode": {ARM_NAMES_ONLY: "missing", ARM_IDENTIFIER: "missing"},
            "id_check_comparison_rule": COMPARISON_RULE,
            "buckets": list(BUCKETS),
            "batch_size": batch_size,
            "timeout_s": timeout,
            "mapper": type(the_mapper).__name__,
            "base_url": api_base,
            "kestrel_url": kestrel_url,
            "remap_all": remap_all,
        }
        report = ManyCohortReport(
            labels=tuple(labels),
            category=resolved_category,
            settings=settings,
            arms=arms,
            identifier_skipped_reason=skipped,
            diff=diff,
            pins=run_pins,
            status=status,
            grade=grade,
            store={
                "used": store.root is not None,
                "dir_name": store.root.name if store.root is not None else None,
                "reuse_enabled": store.reuse_enabled,
                "saving": store.saving,
                "reason": store.reason,
                "n_superseded_files": len(store.superseded),
                "pin": dict(store.pin),
            },
            mapping=mapping,
            warnings=tuple(run_warnings),
            excluded={c.label: c.excluded for c in read},
            session_id=session_id,
            output_dir=directory,
            _store_root=store.root if store.saving else None,
        )
        if directory is not None:
            report.write(directory)
        return report

