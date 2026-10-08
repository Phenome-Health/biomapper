"""``harmonize_cohorts()``: the cohort harmonization protocol as one call.

Harmonizing two cohorts takes several steps (map each cohort, link the results, decide what to do
with each cohort's identifiers), and the obvious path can quietly make the result worse. When only
one cohort supplies an identifier type, supplying it moves that cohort onto code-specific entries
the other cohort never reaches by name: on the UK Biobank x Arivale clinical labs, supplying
Arivale's LOINC codes cut matched pairs from 31 to 2, and no ``annotation_mode`` recovered the
names-only result. This module builds the safe protocol in:

1. **Names-only arm.** Both cohorts are mapped by name with the resolved Biolink category and no
   identifiers.
2. **Identifier arm.** Identifier vocabularies are declared by the caller per cohort. Only the
   vocabularies BOTH cohorts declare (shared) are supplied as mapping input, and only rows carrying
   a shared-vocabulary value are re-mapped; every other row reuses its names-only result. When
   nothing is shared the arm is skipped and the report says why.
3. **Review queue.** Each one-sided code is resolved on its own (``annotation_mode="none"``, one
   request per row, vocabulary and value) and compared with the row's names-only entry, so a
   disagreement such as "Glucose" mapped by name to a glucagon-challenge entry while its LOINC code
   says serum glucose is listed for a person to judge rather than silently fed into matching.
4. Both arms are linked with the existing pure :func:`biomapper.harmonize.harmonize`, and the
   report carries both results, their diff, the review queue, warnings and version pins. By default
   it writes everything to a timestamped directory, each arm as soon as it completes.

Mapping is injected (``mapper=``) so the protocol can be replayed offline; the default mapper calls
the hosted API in small batches with a long timeout. ``harmonize()`` and the
``biomapper.harmonize`` subpackage are unchanged and remain offline. This module imports no pandas:
a DataFrame input is detected by duck typing.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import re
import sys
import warnings
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from biomapper._provenance import DEFAULT_KESTREL_URL, fetch_kg_build_info
from biomapper._version import resolve_version
from biomapper.client import DEFAULT_BASE_URL
from biomapper.harmonize.curies import canonical_prefix, curie_set
from biomapper.harmonize.linking import (
    HarmonizationResult,
    Link,
    _require_distinct_labels,
    harmonize,
)
from biomapper.mapper import map_entities
from biomapper.models import MappingResult

__all__ = [
    "ENTITY_ALIASES",
    "REVIEW_STATUSES",
    "ArmDiff",
    "BatchedApiMapper",
    "CohortHarmonizationReport",
    "CohortRecord",
    "Mapper",
    "ReviewItem",
    "harmonize_cohorts",
    "one_to_one",
]

# Friendly entity names -> Biolink categories. "labs" is ClinicalMeasurement, not ClinicalFinding:
# lab tests live under ClinicalMeasurement in Biolink, and on the UK Biobank x Arivale labs the
# names-only ClinicalMeasurement run matched 37 pairs (21 one-to-one) against 31 for
# ClinicalFinding. Raw ``biolink:`` categories are always accepted, so this table never has to
# track the server's own alias list.
ENTITY_ALIASES: dict[str, str] = {
    "metabolites": "biolink:SmallMolecule",
    "proteins": "biolink:Protein",
    "genes": "biolink:Gene",
    "labs": "biolink:ClinicalMeasurement",
}

# Name linking (``harmonize(link_by_name=True)``) stays metabolite-only, as it is today.
NAME_LINKING_CATEGORIES: frozenset[str] = frozenset({"biolink:SmallMolecule"})

REVIEW_STATUSES: tuple[str, ...] = (
    "agree",
    "disagree",
    "code_unresolved",
    "name_unresolved_code_resolved",
    "errored",
)

DEFAULT_BATCH_SIZE = 10  # larger batches slow the shared deployment down sharply
DEFAULT_TIMEOUT_S = 300.0
DEFAULT_RUNS_DIR = "biomapper_runs"
API_HEALTH_TIMEOUT_S = 15.0

# Accession lists are written several ways ("P29460,P29459", "HMDB1; HMDB2"). Underscores join
# multi-protein assay accessions in UK Biobank ("P29459_P29460") but are legitimate inside other
# identifiers, so they split only for protein accession vocabularies.
_SEPARATORS = re.compile(r"[,;|]")
_UNDERSCORE_SPLIT_VOCABS: frozenset[str] = frozenset({"UNIPROTKB", "UNIPROT"})

_ARM_NAMES_ONLY = "names_only"
_ARM_IDENTIFIER = "identifier"
_ARM_REVIEW = "review"


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


def _blank(value: Any) -> bool:  # noqa: ANN401 — any table cell
    return (
        value is None
        or (isinstance(value, float) and value != value)
        or str(value).strip() in ("", "nan", "NaN", "None")
    )


def clean_id(value: Any) -> str | None:  # noqa: ANN401 — any table cell
    """Normalize one identifier cell: blank/NaN -> None; ``"30740.0"`` -> ``"30740"``.

    Spreadsheet round trips turn integer codes into floats; the trailing ``.0`` is dropped only
    when what precedes it is all digits, so a real decimal identifier is left alone.
    """
    if _blank(value):
        return None
    s = str(value).strip()
    if s.endswith(".0") and s[:-2].isdigit():
        return s[:-2]
    return s or None


def _split_codes(value: Any, vocabulary: str) -> list[str]:  # noqa: ANN401 — any table cell
    cleaned = clean_id(value)
    if cleaned is None:
        return []
    parts = _SEPARATORS.split(cleaned)
    if vocabulary in _UNDERSCORE_SPLIT_VOCABS:
        parts = [q for p in parts for q in p.split("_")]
    return [c for c in (clean_id(p) for p in parts) if c]


def normalize_vocabulary(vocabulary: str) -> str:
    """The vocabulary key that is compared across cohorts AND sent to the API.

    Upper-cased and folded with :func:`biomapper.harmonize.curies.canonical_prefix`, so
    ``KEGG.COMPOUND`` and ``kegg`` are one vocabulary.
    """
    return canonical_prefix(vocabulary.strip().upper())


def resolve_category(entity: str, category: str | None = None) -> str:
    """Resolve an entity alias (or a raw ``biolink:`` category) to the category that is sent.

    ``category`` overrides the alias's default and must itself be a ``biolink:`` category.

    Raises:
        ValueError: For an unknown alias, or an override that is not a ``biolink:`` category.
    """
    if category is not None:
        if not category.startswith("biolink:"):
            raise ValueError(
                f"category={category!r} must be a Biolink category such as "
                "'biolink:ClinicalMeasurement'."
            )
        return category
    if entity.startswith("biolink:"):
        return entity
    resolved = ENTITY_ALIASES.get(entity.strip().lower())
    if resolved is None:
        valid = ", ".join(f"{k!r} ({v})" for k, v in ENTITY_ALIASES.items())
        raise ValueError(
            f"unknown entity {entity!r}. Use one of {valid}, or a raw Biolink category such as "
            "'biolink:ClinicalFinding'."
        )
    return resolved


def one_to_one(links: Iterable[Link]) -> list[Link]:
    """Links whose A key and B key each appear in exactly one link: the conservative pair count.

    Ported from the UK Biobank x Arivale SOP notebook unchanged.
    """
    materialized = list(links)
    a_counts = Counter(lk.a_key for lk in materialized)
    b_counts = Counter(lk.b_key for lk in materialized)
    return [lk for lk in materialized if a_counts[lk.a_key] == 1 and b_counts[lk.b_key] == 1]


def review_status(name_result: MappingResult, code_result: MappingResult) -> str:
    """Compare a row's names-only entry with the entry one of its codes resolves to.

    Agreement is an identifier-set intersection (``curie_set`` of the chosen entry plus its
    equivalents), not raw entry equality, so two different entries describing the same thing
    agree.
    """
    if name_result.error or code_result.error:
        return "errored"
    code_set = curie_set(code_result.chosen_kg_id, code_result.kg_equivalent_ids)
    if not code_set:
        return "code_unresolved"
    name_set = curie_set(name_result.chosen_kg_id, name_result.kg_equivalent_ids)
    if not name_set:
        return "name_unresolved_code_resolved"
    return "agree" if name_set & code_set else "disagree"


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CohortRecord:
    """One row to map: a stable key, the name searched, and its declared identifiers."""

    key: str
    name: str
    row: int  # position in the input table (0-based, data rows only)
    identifiers: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class Cohort:
    """A cohort table normalized into keyed records."""

    label: str
    records: tuple[CohortRecord, ...]
    vocabularies: Mapping[str, tuple[str, ...]]  # normalized vocabulary -> source columns
    excluded: tuple[dict[str, Any], ...]
    source: Mapping[str, Any]
    name_column: str
    key_column: str | None


CohortInput = Any  # a DataFrame, a list of dicts, or a TSV/CSV path


def _read_delimited(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    delimiter = "," if path.suffix.lower() == ".csv" else "\t"
    with path.open(newline="", encoding="utf-8-sig") as fh:
        lines = [line for line in fh if not line.startswith("#")]
    reader = csv.DictReader(lines, delimiter=delimiter)
    rows = [dict(r) for r in reader]
    return rows, list(reader.fieldnames or [])


def _table_rows(table: CohortInput) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    """Rows, column names, and a description of the source that holds no absolute path."""
    if isinstance(table, str | os.PathLike):
        path = Path(table)
        rows, columns = _read_delimited(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return rows, columns, {"kind": "file", "file": path.name, "sha256": digest}
    if hasattr(table, "to_dict") and hasattr(table, "columns"):  # a DataFrame, by duck typing
        columns = [str(c) for c in table.columns]
        return list(table.to_dict(orient="records")), columns, {"kind": "dataframe"}
    if isinstance(table, Sequence) and all(isinstance(r, Mapping) for r in table):
        seen: list[str] = []
        for r in table:
            seen.extend(str(c) for c in r if c not in seen)
        return [dict(r) for r in table], seen, {"kind": "records"}
    raise TypeError(
        f"unsupported cohort input {type(table).__name__}: pass a pandas DataFrame, a list of "
        "dicts, or a path to a TSV/CSV file."
    )


def _normalize_declaration(
    vocabularies: Mapping[str, str | Sequence[str]] | None,
) -> dict[str, tuple[str, ...]]:
    out: dict[str, list[str]] = {}
    for vocab, columns in (vocabularies or {}).items():
        cols = [columns] if isinstance(columns, str) else list(columns)
        if not cols:
            raise ValueError(f"vocabulary {vocab!r} is declared with no columns")
        merged = out.setdefault(normalize_vocabulary(vocab), [])
        merged.extend(c for c in cols if c not in merged)
    return {k: tuple(v) for k, v in out.items()}


def read_cohort(
    table: CohortInput,
    *,
    label: str,
    name_column: str = "name",
    vocabularies: Mapping[str, str | Sequence[str]] | None = None,
    key_column: str | None = None,
) -> Cohort:
    """Normalize one cohort table into keyed records.

    Keys come from ``key_column`` when given (blank or duplicate keys raise, naming the column);
    otherwise ``"{row}|{name}"``, so repeated names never collide. Rows with a blank name are
    excluded and reported. Identifier cells are cleaned (``clean_id``), split on ``,``, ``;`` and
    ``|`` (and ``_`` for UniProt accessions), and de-duplicated across a vocabulary's columns.
    """
    rows, columns, source = _table_rows(table)
    declared = _normalize_declaration(vocabularies)
    needed = [("name column", name_column)]
    if key_column is not None:
        needed.append(("key column", key_column))
    needed += [(f"{v} column", c) for v, cols in declared.items() for c in cols]
    for role, column in needed:
        if column not in columns:
            raise ValueError(
                f"{label}: {role} {column!r} is not in the table (columns: {columns})"
            )

    records: list[CohortRecord] = []
    excluded: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, row in enumerate(rows):
        raw_name = row.get(name_column)
        if _blank(raw_name):
            excluded.append({"row": i, "reason": "blank name"})
            continue
        name = str(raw_name).strip()
        if key_column is not None:
            key = clean_id(row.get(key_column))
            if key is None:
                raise ValueError(f"{label}: blank key in key column {key_column!r} at row {i}")
            if key in seen:
                raise ValueError(
                    f"{label}: duplicate key {key!r} in key column {key_column!r}; keys must be "
                    "unique or two rows collapse onto one"
                )
        else:
            key = f"{i}|{name}"
        seen.add(key)
        ids: dict[str, tuple[str, ...]] = {}
        for vocab, cols in declared.items():
            values: list[str] = []
            for col in cols:
                values.extend(v for v in _split_codes(row.get(col), vocab) if v not in values)
            if values:
                ids[vocab] = tuple(values)
        records.append(CohortRecord(key=key, name=name, row=i, identifiers=ids))

    source = {**source, "n_rows": len(rows), "n_records": len(records)}
    return Cohort(
        label=label,
        records=tuple(records),
        vocabularies=declared,
        excluded=tuple(excluded),
        source=source,
        name_column=name_column,
        key_column=key_column,
    )


def classify_vocabularies(
    a_vocabularies: Mapping[str, Any],
    b_vocabularies: Mapping[str, Any],
    a_label: str,
    b_label: str,
) -> tuple[tuple[str, ...], dict[str, str]]:
    """Return ``(shared, one_sided)``: vocabularies both cohorts declare, and the rest by owner."""
    shared = tuple(v for v in a_vocabularies if v in b_vocabularies)
    one_sided = {v: a_label for v in a_vocabularies if v not in b_vocabularies}
    one_sided.update({v: b_label for v in b_vocabularies if v not in a_vocabularies})
    return shared, one_sided


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


class Mapper(Protocol):
    """Maps request dicts (``{"name", "identifiers"}``) to results, one per request, in order."""

    def __call__(
        self, records: list[dict[str, Any]], *, entity_type: str, annotation_mode: str
    ) -> list[MappingResult]: ...


class BatchedApiMapper:
    """The default mapper: the hosted API in small batches with a long timeout.

    Each batch goes through :func:`biomapper.map_entities`, so a failed chunk becomes per-row
    error results rather than an exception, and an auth failure still aborts.
    """

    def __init__(
        self,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        timeout: float = DEFAULT_TIMEOUT_S,
        base_url: str | None = None,
        api_key: str | None = None,
        progress: bool = False,
    ) -> None:
        if batch_size < 1:
            raise ValueError(f"batch_size must be at least 1, got {batch_size}")
        self.batch_size = batch_size
        self.timeout = timeout
        self.base_url = base_url
        self.api_key = api_key
        self.progress = progress

    def __call__(
        self, records: list[dict[str, Any]], *, entity_type: str, annotation_mode: str
    ) -> list[MappingResult]:
        out: list[MappingResult] = []
        total = len(records)
        for start in range(0, total, self.batch_size):
            batch = records[start : start + self.batch_size]
            out.extend(
                map_entities(
                    batch,
                    api_key=self.api_key,
                    base_url=self.base_url,
                    entity_type=entity_type,
                    annotation_mode=annotation_mode,
                    timeout=self.timeout,
                )
            )
            if self.progress:
                print(
                    f"  mapped {len(out)}/{total} ({entity_type}, annotation_mode="
                    f"{annotation_mode})",
                    file=sys.stderr,
                    flush=True,
                )
        return out


def _call_mapper(
    mapper: Mapper,
    requests: list[dict[str, Any]],
    *,
    entity_type: str,
    annotation_mode: str,
) -> list[MappingResult]:
    if not requests:
        return []
    results = list(mapper(requests, entity_type=entity_type, annotation_mode=annotation_mode))
    if len(results) != len(requests):
        raise RuntimeError(
            f"mapper returned {len(results)} results for {len(requests)} requests; a mapper must "
            "return one result per request, in order"
        )
    return results


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewItem:
    """One one-sided code compared with its row's names-only entry."""

    cohort: str
    key: str
    name: str
    vocabulary: str
    code: str
    name_entry: str | None
    code_entry: str | None
    status: str


@dataclass(frozen=True)
class ArmDiff:
    """Linked (a_key, b_key) pairs by which arm found them."""

    names_only_only: tuple[tuple[str, str], ...]
    identifier_only: tuple[tuple[str, str], ...]
    both: tuple[tuple[str, str], ...]

    def counts(self) -> dict[str, int]:
        return {
            "names_only_only": len(self.names_only_only),
            "identifier_only": len(self.identifier_only),
            "both": len(self.both),
        }


def compute_diff(names_only_links: Iterable[Link], identifier_links: Iterable[Link]) -> ArmDiff:
    names = {(lk.a_key, lk.b_key) for lk in names_only_links}
    ident = {(lk.a_key, lk.b_key) for lk in identifier_links}
    return ArmDiff(
        names_only_only=tuple(sorted(names - ident)),
        identifier_only=tuple(sorted(ident - names)),
        both=tuple(sorted(names & ident)),
    )


def _safe(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", label) or "cohort"


def _write_tsv(path: Path, header: Sequence[str], rows: Iterable[Sequence[Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, delimiter="\t", lineterminator="\n")
        writer.writerow(header)
        for row in rows:
            writer.writerow(["" if v is None else v for v in row])


def _write_mapping(
    directory: Path,
    arm: str,
    label: str,
    rows: Sequence[tuple[CohortRecord, dict[str, list[str]], MappingResult]],
) -> None:
    _write_tsv(
        directory / f"mapping_{arm}_{_safe(label)}.tsv",
        ["key", "name", "identifiers_sent", "chosen_kg_id", "resolved", "error",
         "kg_equivalent_ids"],
        (
            [
                rec.key,
                rec.name,
                json.dumps(sent, sort_keys=True) if sent else "",
                res.chosen_kg_id,
                bool(curie_set(res.chosen_kg_id, res.kg_equivalent_ids)),
                res.error,
                json.dumps(res.kg_equivalent_ids, sort_keys=True) if res.kg_equivalent_ids else "",
            ]
            for rec, sent, res in rows
        ),
    )


def _new_run_dir(base: Path) -> Path:
    """Create and return a fresh, timestamped run directory under ``base``.

    The directory is created exclusively (``exist_ok=False``) so two runs starting in the same
    second can never both claim it: the loser of the race gets ``FileExistsError`` and moves on
    to the next suffix.
    """
    base.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    candidate = base / f"harmonize_cohorts_{stamp}"
    n = 2
    while True:
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            candidate = base / f"harmonize_cohorts_{stamp}_{n}"
            n += 1


# Fields CohortHarmonizationReport adds to each arm's per-cohort summary; a cohort label equal to
# one of these would have its counts overwritten.
_RESERVED_COHORT_LABELS = frozenset({"n_one_to_one"})


def _require_cohort_labels(a_label: str, b_label: str) -> None:
    """Reject labels that collide in the report's summary or in its saved filenames."""
    _require_distinct_labels(a_label, b_label)
    for side, label in (("a_label", a_label), ("b_label", b_label)):
        if label in _RESERVED_COHORT_LABELS:
            raise ValueError(
                f"{side}={label!r} is reserved: the report summary emits it as a scalar field, "
                "so a cohort under that label would lose its counts."
            )
    if _safe(a_label) == _safe(b_label):
        raise ValueError(
            f"a_label={a_label!r} and b_label={b_label!r} both save as {_safe(a_label)!r}, so "
            "one cohort's mapping files would overwrite the other's; choose labels that differ "
            "in letters, digits, '.', '-' or '_'."
        )


@dataclass
class CohortHarmonizationReport:
    """Everything one :func:`harmonize_cohorts` run produced.

    ``names_only`` and ``identifier`` are :class:`~biomapper.harmonize.HarmonizationResult`s
    (``identifier`` is ``None`` when the arm was skipped; ``identifier_skipped_reason`` says why).
    ``review`` holds every one-sided code with its status; ``review_queue`` is the part a person
    should look at (everything but ``agree``).
    """

    a_label: str
    b_label: str
    category: str
    settings: dict[str, Any]
    names_only: HarmonizationResult
    identifier: HarmonizationResult | None
    identifier_skipped_reason: str | None
    diff: ArmDiff | None
    review: tuple[ReviewItem, ...]
    warnings: tuple[str, ...]
    pins: dict[str, Any]
    excluded: dict[str, tuple[dict[str, Any], ...]]
    mappings: dict[str, dict[str, list[tuple[CohortRecord, dict[str, list[str]], MappingResult]]]]
    output_dir: Path | None = None

    @property
    def review_queue(self) -> tuple[ReviewItem, ...]:
        return tuple(i for i in self.review if i.status != "agree")

    @staticmethod
    def _arm_summary(result: HarmonizationResult) -> dict[str, Any]:
        return {**result.summary(), "n_one_to_one": len(one_to_one(result.links))}

    def summary(self) -> dict[str, Any]:
        """Counts-only summary: safe to log or serialize (round-trips through JSON)."""
        status_counts = Counter(i.status for i in self.review)
        return {
            "entity": self.settings["entity"],
            "category": self.category,
            "link_by_name": self.settings["link_by_name"],
            "labels": {"a": self.a_label, "b": self.b_label},
            "vocabularies": {
                "shared": list(self.settings["vocabularies"]["shared"]),
                "one_sided": dict(self.settings["vocabularies"]["one_sided"]),
            },
            "arms": {
                _ARM_NAMES_ONLY: self._arm_summary(self.names_only),
                _ARM_IDENTIFIER: (
                    self._arm_summary(self.identifier) if self.identifier is not None else None
                ),
            },
            "identifier_arm_skipped": self.identifier_skipped_reason,
            "diff": self.diff.counts() if self.diff is not None else None,
            "review": {
                "by_status": {s: status_counts.get(s, 0) for s in REVIEW_STATUSES},
                "n_listed": len(self.review_queue),
            },
            "excluded": {label: len(rows) for label, rows in self.excluded.items()},
            "warnings": list(self.warnings),
            "pins": self.pins,
        }

    def _write_links(self, directory: Path, arm: str, result: HarmonizationResult) -> None:
        exclusive = {(lk.a_key, lk.b_key) for lk in one_to_one(result.links)}
        _write_tsv(
            directory / f"links_{arm}.tsv",
            [f"{self.a_label}_key", f"{self.b_label}_key", "basis", "bases", "shared_identifiers",
             "one_to_one"],
            (
                [
                    lk.a_key,
                    lk.b_key,
                    lk.basis,
                    "|".join(sorted(lk.bases)),
                    "|".join(sorted(lk.shared)),
                    (lk.a_key, lk.b_key) in exclusive,
                ]
                for lk in result.links
            ),
        )

    def write(self, path: str | os.PathLike[str] | None = None) -> Path:
        """Write every table and the summary; return the directory.

        ``path`` defaults to this report's ``output_dir``, or a new
        ``biomapper_runs/harmonize_cohorts_<UTC timestamp>/`` under the current directory.
        """
        if path is not None:
            directory = Path(path).resolve()
        elif self.output_dir is not None:
            directory = self.output_dir
        else:
            directory = _new_run_dir(Path(DEFAULT_RUNS_DIR).resolve())
        directory.mkdir(parents=True, exist_ok=True)

        for arm, by_label in self.mappings.items():
            for label, rows in by_label.items():
                _write_mapping(directory, arm, label, rows)
        self._write_links(directory, _ARM_NAMES_ONLY, self.names_only)
        if self.identifier is not None:
            self._write_links(directory, _ARM_IDENTIFIER, self.identifier)
        if self.diff is not None:
            _write_tsv(
                directory / "diff.tsv",
                [f"{self.a_label}_key", f"{self.b_label}_key", "found_by"],
                [
                    *([a, b, "names_only_only"] for a, b in self.diff.names_only_only),
                    *([a, b, "identifier_only"] for a, b in self.diff.identifier_only),
                    *([a, b, "both"] for a, b in self.diff.both),
                ],
            )
        _write_tsv(
            directory / "review_queue.tsv",
            ["cohort", "key", "name", "vocabulary", "code", "name_entry", "code_entry", "status"],
            (
                [i.cohort, i.key, i.name, i.vocabulary, i.code, i.name_entry, i.code_entry,
                 i.status]
                for i in self.review_queue
            ),
        )
        (directory / "settings.json").write_text(
            json.dumps({**self.settings, "pins": self.pins}, indent=1, sort_keys=True) + "\n"
        )
        (directory / "summary.json").write_text(
            json.dumps(self.summary(), indent=1, sort_keys=True) + "\n"
        )
        self.output_dir = directory
        print(f"harmonize_cohorts: wrote results to {directory}")
        return directory


# ---------------------------------------------------------------------------
# Pins
# ---------------------------------------------------------------------------


def _api_health(base_url: str, api_key: str | None) -> dict[str, Any]:
    key = api_key or os.getenv("BIOMAPPER_API_KEY")
    headers = {"X-API-Key": key} if key else {}
    try:
        with httpx.Client(timeout=API_HEALTH_TIMEOUT_S, headers=headers) as client:
            response = client.get(f"{base_url.rstrip('/')}/health")
            response.raise_for_status()
            data = response.json()
        return {"status": data.get("status"), "self_reported_version": data.get("version"),
                "error": None}
    except Exception as exc:  # noqa: BLE001 — a pin must never abort a run
        return {"status": "unreachable", "self_reported_version": None,
                "error": f"{type(exc).__name__}: {exc}"}


def _collect_pins(
    *,
    probe: bool,
    base_url: str,
    api_key: str | None,
    kestrel_url: str,
    mapper: Mapper,
) -> dict[str, Any]:
    if probe:
        api = _api_health(base_url, api_key)
        kestrel_version, kg_build, kestrel_error = fetch_kg_build_info(kestrel_url)
        kg: dict[str, Any] | None = kg_build.model_dump()
    else:
        api = {"status": "not probed", "self_reported_version": None, "error": None}
        kestrel_version, kg, kestrel_error = "not probed", None, None
    return {
        "timestamp_utc": dt.datetime.now(dt.UTC).isoformat(),
        "biomapper_version": resolve_version(),
        "api": {
            "base_url": base_url,
            **api,
            "self_reported_version_label": "self-reported, known stale",
            # The API does not expose the engine release it runs.
            "engine_release": "unavailable",
        },
        "kestrel": {
            "url": kestrel_url,
            "kestrel_version": kestrel_version,
            "kg_build": kg,
            "error": kestrel_error,
            "label": (
                "Kestrel's own /health; the graph the API used is assumed, not verified, to be "
                "this build"
            ),
        },
        "mapper": type(mapper).__name__,
        "mapper_pins": getattr(mapper, "pins", None),
    }


# ---------------------------------------------------------------------------
# The protocol
# ---------------------------------------------------------------------------


def harmonize_cohorts(
    a: CohortInput,
    b: CohortInput,
    *,
    entity: str,
    a_name_column: str = "name",
    b_name_column: str = "name",
    a_vocabularies: Mapping[str, str | Sequence[str]] | None = None,
    b_vocabularies: Mapping[str, str | Sequence[str]] | None = None,
    a_key_column: str | None = None,
    b_key_column: str | None = None,
    a_label: str = "a",
    b_label: str = "b",
    category: str | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    timeout: float = DEFAULT_TIMEOUT_S,
    progress: bool = False,
    mapper: Mapper | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    kestrel_url: str = DEFAULT_KESTREL_URL,
    probe_pins: bool = True,
    output_dir: str | os.PathLike[str] | None = None,
    save: bool = True,
) -> CohortHarmonizationReport:
    """Harmonize two cohort tables with the names-first protocol, and report both arms.

    Args:
        a, b: Each cohort as a pandas DataFrame, a list of dicts, or a path to a TSV/CSV file
            (``#`` comment lines are skipped). Only panel metadata is needed.
        entity: ``"metabolites"``, ``"proteins"``, ``"genes"``, ``"labs"`` or a raw Biolink
            category. ``labs`` resolves to ``biolink:ClinicalMeasurement``.
        a_name_column, b_name_column: Column holding the name to search.
        a_vocabularies, b_vocabularies: Identifier columns per vocabulary, e.g.
            ``{"LOINC": ["Labcorp LOINC ID", "Quest LOINC ID"]}``. Vocabulary names are normalized
            (``KEGG.COMPOUND`` equals ``KEGG``); the normalized name is what is sent. Only
            vocabularies both cohorts declare are supplied as mapping input; the others go to the
            review queue, with a warning.
        a_key_column, b_key_column: Optional unique row keys; the default is ``"{row}|{name}"``.
        a_label, b_label: Cohort labels for the report (distinct, not a reserved summary field).
        category: Override the category the alias resolves to.
        batch_size, timeout, progress: Default-mapper request policy (10 per batch, 300 s).
        mapper: A callable ``(records, *, entity_type, annotation_mode) -> list[MappingResult]``
            to use instead of the hosted API (a replay in tests). An attribute ``pins`` on it is
            recorded in the report.
        base_url, api_key: Passed to the default mapper and the API health pin.
        kestrel_url: The Kestrel root whose ``/health`` pins the KG build (recorded).
        probe_pins: Read the API and Kestrel ``/health`` endpoints for the pins (default True).
        output_dir: Where to write; default ``biomapper_runs/harmonize_cohorts_<UTC stamp>/``.
        save: Write results (default True). Each arm is written as soon as it completes, so a
            failure keeps finished work; ``report.write()`` can always be called later.

    Returns:
        A :class:`CohortHarmonizationReport`.

    Raises:
        ValueError: Invalid labels, entity, columns or keys. Raised before any request is sent.
    """
    _require_cohort_labels(a_label, b_label)
    resolved_category = resolve_category(entity, category)
    link_by_name = resolved_category in NAME_LINKING_CATEGORIES
    cohort_a = read_cohort(
        a, label=a_label, name_column=a_name_column, vocabularies=a_vocabularies,
        key_column=a_key_column,
    )
    cohort_b = read_cohort(
        b, label=b_label, name_column=b_name_column, vocabularies=b_vocabularies,
        key_column=b_key_column,
    )
    cohorts = (cohort_a, cohort_b)
    shared, one_sided = classify_vocabularies(
        cohort_a.vocabularies, cohort_b.vocabularies, a_label, b_label
    )

    api_base = base_url or DEFAULT_BASE_URL
    the_mapper: Mapper = mapper or BatchedApiMapper(
        batch_size=batch_size, timeout=timeout, base_url=base_url, api_key=api_key,
        progress=progress,
    )

    run_warnings: list[str] = []
    for vocab, owner in one_sided.items():
        other = b_label if owner == a_label else a_label
        message = (
            f"{vocab} is declared only for {owner!r}, not for {other!r}, so it is not supplied as "
            f"mapping input: supplying one cohort's codes moves that cohort onto code-specific "
            f"entries the other cohort cannot reach by name, and lowers matches. Its codes are "
            f"compared with the name results in the review queue instead."
        )
        run_warnings.append(message)
        warnings.warn(message, UserWarning, stacklevel=2)

    directory: Path | None = None
    if save:
        directory = (
            Path(output_dir).resolve() if output_dir is not None
            else _new_run_dir(Path(DEFAULT_RUNS_DIR).resolve())
        )
        directory.mkdir(parents=True, exist_ok=True)

    mappings: dict[str, dict[str, list[tuple[CohortRecord, dict[str, list[str]], MappingResult]]]]
    mappings = {}

    def persist(arm: str, label: str, rows: list[Any]) -> None:
        mappings.setdefault(arm, {})[label] = rows
        if directory is not None:
            _write_mapping(directory, arm, label, rows)

    def say(message: str) -> None:
        if progress:
            print(f"harmonize_cohorts: {message}", file=sys.stderr, flush=True)

    # 1. Names-only arm.
    names_results: dict[str, list[MappingResult]] = {}
    for cohort in cohorts:
        say(f"names-only arm, {cohort.label} ({len(cohort.records)} rows)")
        results = _call_mapper(
            the_mapper,
            [{"name": r.name, "identifiers": {}} for r in cohort.records],
            entity_type=resolved_category,
            annotation_mode="missing",
        )
        names_results[cohort.label] = results
        persist(_ARM_NAMES_ONLY, cohort.label,
                [(r, {}, res) for r, res in zip(cohort.records, results, strict=True)])

    def link(results_by_label: dict[str, list[MappingResult]]) -> HarmonizationResult:
        # harmonize()'s key callable sees only (result, index-within-side), so rows are keyed by
        # object identity. Copy every result first: a caching mapper may return ONE object for
        # several rows (or for both cohorts), and identity would then collapse their keys.
        copies = {
            c.label: [res.model_copy() for res in results_by_label[c.label]] for c in cohorts
        }
        keymap = {
            id(res): rec.key
            for c in cohorts
            for res, rec in zip(copies[c.label], c.records, strict=True)
        }
        return harmonize(
            copies[a_label],
            copies[b_label],
            a_label=a_label,
            b_label=b_label,
            key=lambda res, _i: keymap[id(res)],
            link_by_name=link_by_name,
        )

    names_only = link(names_results)

    # 2. Identifier arm: shared vocabularies only, coded rows only.
    identifier: HarmonizationResult | None = None
    skipped: str | None = None
    if not cohort_a.vocabularies and not cohort_b.vocabularies:
        skipped = "no identifier columns declared; names-only arm only"
    elif not shared:
        skipped = (
            "no vocabulary is shared by both cohorts (one-sided: "
            + ", ".join(f"{v} ({owner})" for v, owner in one_sided.items())
            + "); one-sided identifiers are never supplied as mapping input"
        )
    else:
        id_results: dict[str, list[MappingResult]] = {}
        for cohort in cohorts:
            sent_ids = [
                {v: list(r.identifiers[v]) for v in shared if v in r.identifiers}
                for r in cohort.records
            ]
            coded = [i for i, ids in enumerate(sent_ids) if ids]
            say(f"identifier arm, {cohort.label} ({len(coded)} rows carry {', '.join(shared)})")
            remapped = _call_mapper(
                the_mapper,
                [{"name": cohort.records[i].name, "identifiers": sent_ids[i]} for i in coded],
                entity_type=resolved_category,
                annotation_mode="missing",
            )
            merged = list(names_results[cohort.label])
            for i, res in zip(coded, remapped, strict=True):
                merged[i] = res
            id_results[cohort.label] = merged
            persist(_ARM_IDENTIFIER, cohort.label,
                    [(r, ids, res)
                     for r, ids, res in zip(cohort.records, sent_ids, merged, strict=True)])
        identifier = link(id_results)

    # 3. Review queue: one-sided codes, resolved on their own, against the names-only entry.
    review: list[ReviewItem] = []
    for cohort in cohorts:
        vocabs = [v for v, owner in one_sided.items() if owner == cohort.label]
        if not vocabs:
            continue
        jobs = [
            (i, v, code)
            for i, r in enumerate(cohort.records)
            for v in vocabs
            for code in r.identifiers.get(v, ())
        ]
        if not jobs:
            continue
        say(f"review pass, {cohort.label} ({len(jobs)} one-sided codes)")
        code_results = _call_mapper(
            the_mapper,
            [{"name": cohort.records[i].name, "identifiers": {v: [code]}} for i, v, code in jobs],
            entity_type=resolved_category,
            annotation_mode="none",
        )
        rows = []
        for (i, v, code), res in zip(jobs, code_results, strict=True):
            rec = cohort.records[i]
            name_res = names_results[cohort.label][i]
            review.append(ReviewItem(
                cohort=cohort.label, key=rec.key, name=rec.name, vocabulary=v, code=code,
                name_entry=name_res.chosen_kg_id, code_entry=res.chosen_kg_id,
                status=review_status(name_res, res),
            ))
            rows.append((rec, {v: [code]}, res))
        persist(_ARM_REVIEW, cohort.label, rows)

    settings: dict[str, Any] = {
        "entity": entity,
        "category": resolved_category,
        "category_overridden": category is not None,
        "aliases": dict(ENTITY_ALIASES),
        "link_by_name": link_by_name,
        "labels": {"a": a_label, "b": b_label},
        "cohorts": {
            c.label: {
                "name_column": c.name_column,
                "key_column": c.key_column,
                "vocabularies": {v: list(cols) for v, cols in c.vocabularies.items()},
                "source": dict(c.source),
                "n_excluded": len(c.excluded),
            }
            for c in cohorts
        },
        "vocabularies": {"shared": list(shared), "one_sided": dict(one_sided)},
        "annotation_mode": {_ARM_NAMES_ONLY: "missing", _ARM_IDENTIFIER: "missing",
                            _ARM_REVIEW: "none"},
        "batch_size": batch_size,
        "timeout_s": timeout,
        "mapper": type(the_mapper).__name__,
        "base_url": api_base,
        "kestrel_url": kestrel_url,
    }
    report = CohortHarmonizationReport(
        a_label=a_label,
        b_label=b_label,
        category=resolved_category,
        settings=settings,
        names_only=names_only,
        identifier=identifier,
        identifier_skipped_reason=skipped,
        diff=compute_diff(names_only.links, identifier.links) if identifier is not None else None,
        review=tuple(review),
        warnings=tuple(run_warnings),
        pins=_collect_pins(
            probe=probe_pins, base_url=api_base, api_key=api_key, kestrel_url=kestrel_url,
            mapper=the_mapper,
        ),
        excluded={c.label: c.excluded for c in cohorts},
        mappings=mappings,
        output_dir=directory,
    )
    if directory is not None:
        report.write(directory)
    return report
