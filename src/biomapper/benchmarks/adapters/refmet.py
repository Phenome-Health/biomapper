"""RefMet adapter (metabolite arm — structure oracle, streamed + subsampled).

RefMet is the Metabolomics Workbench reference nomenclature (Fahy & Subramaniam 2020, Nat.
Methods, DOI 10.1038/s41592-020-01009-y). The bulk CSV (databases/refmet/refmet_download.php)
ships ``refmet_name`` + a crosswalk to ChEBI/HMDB/PubChem/KEGG/LipidMaps + ``inchi_key``.

Design combines the two established harness patterns:
  - **Structure oracle** (like ``necs_metabolon`` / ``hajjar``): the gold **InChIKey** column is
    the independent oracle — preserved verbatim, zero shared infra with the system under test. The
    crosswalk IDs (ChEBI/HMDB/PubChem/KEGG/LipidMaps) are reported as coverage only. Only
    BioMapper's *prediction* is ever resolved (in the scorer).
  - **Streaming + reservoir subsample** (like ``backbones``): >200k analytes and only ~17%
    InChIKey-annotated, so the source is streamed line-by-line, filtered to the InChIKey-bearing
    population (``require_gold_structure`` — the oracle needs a held-out structure), and
    deterministically reservoir-subsampled (``seed`` pinned on the card). The exact scored subset
    is PERSISTED beside the card (the download URL is a mutable "current release", so URL+seed+n
    alone cannot reconstruct it).

Network is isolated behind ``stream_source_lines`` (reused from ``backbones``) so the parse +
subsample + card transforms are fully unit-testable on an in-memory line iterator.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

# Reuse the generic streaming / subsample / persistence machinery (identical discipline as the
# gene/protein backbones). These are dataset-agnostic helpers.
from biomapper.benchmarks.adapters.backbones import (  # noqa: F401  (re-exported for the adapter's public surface)
    load_persisted_subsample,
    reservoir_sample,
    sha256_bytes,
    stream_source_lines,
    subsample_csv_bytes,
    subsample_filename,
)
from biomapper.benchmarks.config import REFMET, DatasetConfig

# Marks rows retained for coverage accounting but excluded from the accuracy denominator (mirrors
# hajjar/necs). Under ``require_gold_structure`` every sampled row is structure-bearing, but the
# flag is still emitted so the scorer's coverage-only rule applies uniformly across datasets.
HAS_STRUCTURE_COL = "has_gold_structure"

# Canonical query column + candidate raw headers (case-insensitive, stripped). The real bulk CSV
# header ships with a leading space (" refmet_id,..."), so every lookup is stripped+lowercased.
QUERY_CANDIDATES: tuple[str, ...] = ("refmet_name", "name")
# Canonical held-out column -> candidate raw headers. The InChIKey is the oracle (required for a
# scorable row); the rest are crosswalk coverage.
GOLD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "gold_inchikey": ("inchi_key", "inchikey"),
    "gold_chebi": ("chebi_id", "chebi"),
    "gold_hmdb": ("hmdb_id", "hmdb"),
    "gold_pubchem": ("pubchem_cid", "pubchem"),
    "gold_kegg": ("kegg_id", "kegg"),
    "gold_lipidmaps": ("lipidmaps_id", "lipidmaps"),
}


def _resolve_index(header: list[str], candidates: tuple[str, ...]) -> int | None:
    """First column index whose stripped/lowercased header matches any candidate."""
    lookup = {str(h).strip().lower(): i for i, h in enumerate(header)}
    for cand in candidates:
        hit = lookup.get(cand.strip().lower())
        if hit is not None:
            return hit
    return None


def refmet_records(lines: Iterable[str], *, require_structure: bool = True) -> Iterator[dict[str, str]]:
    """Parse the RefMet bulk CSV (header + quoted fields). refmet_name -> InChIKey + crosswalk.

    Uses ``csv.reader`` (not a naive split) because analyte names contain commas and are quoted.
    Filtering happens DURING the stream so the reservoir samples from the eligible population
    (structure-bearing rows when ``require_structure``), not the raw file. An empty query row is
    always dropped (nothing to map); a row missing the oracle InChIKey is dropped only when
    ``require_structure``.
    """
    reader = csv.reader(lines)
    try:
        header = next(reader)
    except StopIteration:
        return
    name_i = _resolve_index(header, QUERY_CANDIDATES)
    if name_i is None:
        raise KeyError(f"RefMet header missing a recognizable 'refmet_name' column; got {header[:6]!r}...")
    gold_idx = {canonical: _resolve_index(header, cands) for canonical, cands in GOLD_CANDIDATES.items()}
    ik_i = gold_idx["gold_inchikey"]

    def _get(fields: list[str], i: int | None) -> str:
        return fields[i].strip() if (i is not None and i < len(fields)) else ""

    for fields in reader:
        if not fields:
            continue
        name = _get(fields, name_i)
        if not name:
            continue
        inchikey = _get(fields, ik_i)
        if require_structure and not inchikey:
            continue
        rec = {REFMET.name_column: name}
        for canonical, i in gold_idx.items():
            rec[canonical] = _get(fields, i)
        yield rec


def build_input_df(records: list[dict[str, str]], config: DatasetConfig = REFMET) -> pd.DataFrame:
    """Build the mapper-ready input_df: name query + held-out gold columns + structure flag.

    The mapper is later called with ``name_column=config.name_column`` and
    ``provided_id_columns=[]`` — the gold columns ride along untouched and are consumed only by
    the scorer, never by BioMapper.
    """
    gold_cols = [col for _, col in config.gold_coverage_columns]
    columns = [config.name_column, *gold_cols]
    rows = [{c: rec.get(c, "") for c in columns} for rec in records]
    out = pd.DataFrame(rows, columns=columns)
    out[HAS_STRUCTURE_COL] = out[config.gold_inchikey_column].map(lambda s: bool(str(s).strip()))
    return out


def build_card(
    input_df: pd.DataFrame,
    *,
    n_scanned: int,
    source_sha: str,
    config: DatasetConfig = REFMET,
    source_version: str | None = None,
) -> dict[str, Any]:
    """Build the dataset_card: N, subsample n/seed, per-column coverage, oracle column, SHA."""
    n = len(input_df)
    coverage: dict[str, dict[str, Any]] = {}
    for namespace, column in config.gold_coverage_columns:
        col = input_df[column] if column in input_df.columns else pd.Series([""] * n)
        present = int(col.map(lambda s: bool(str(s).strip())).sum())
        coverage[namespace] = {"n": present, "fraction": (present / n) if n else 0.0}
    return {
        "dataset": config.key,
        "arm": config.arm,
        "entity_type": config.entity_type,
        "input_type": config.input_type,
        "target_vocabs": list(config.target_vocabs),
        "n_rows": n,
        "n_scanned": n_scanned,
        "subsample": {"n": config.subsample_n, "seed": config.subsample_seed, "method": "reservoir"},
        # The scored subset is drawn from the InChIKey-bearing population; recorded so a reviewer
        # sees the accuracy is over structure-bearing analytes (crosswalk IDs are coverage only).
        "require_gold_structure": config.require_gold_structure,
        "coverage": coverage,
        "structure_oracle_column": config.gold_inchikey_column,
        "source_doi": config.source_doi,
        "source_url": config.source_url,
        # Reproducibility is guaranteed by the PERSISTED subsample (``persist_subsample`` writes it
        # beside this card); ``subsample_sha256`` pins those exact bytes. ``source_version`` records
        # the resolved upstream release (None when unavailable), for provenance only.
        "subsample_sha256": source_sha,
        "subsample_filename": subsample_filename(config.key),
        "source_version": source_version,
        "license": config.license,
    }


@dataclass(frozen=True)
class RefMetBundle:
    input_df: pd.DataFrame
    card: dict[str, Any]


def persist_subsample(bundle: RefMetBundle, out_dir: Path | str) -> Path:
    """Write the exact scored subsample beside the card (byte-identical to ``subsample_sha256``)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / subsample_filename(bundle.card["dataset"])
    path.write_bytes(subsample_csv_bytes(bundle.input_df))
    return path


def subsample_from_lines(lines: Iterable[str], config: DatasetConfig) -> tuple[pd.DataFrame, int]:
    """Stream -> filter -> reservoir-subsample -> input_df. Returns (input_df, n_scanned).

    ``n_scanned`` counts eligible records seen (post structure-bearing filter), for card
    transparency. Fails loud when ``subsample_n`` is unset — RefMet is too large to load in full.
    """
    if config.subsample_n is None:
        raise ValueError(
            f"{config.key}: subsample_n is required (RefMet is >200k analytes and must be "
            f"reservoir-subsampled, not loaded in full)."
        )
    counter = {"n": 0}

    def _counting(it: Iterator[dict[str, str]]) -> Iterator[dict[str, str]]:
        for rec in it:
            counter["n"] += 1
            yield rec

    sampled = reservoir_sample(
        _counting(refmet_records(lines, require_structure=config.require_gold_structure)),
        config.subsample_n,
        config.subsample_seed,
    )
    return build_input_df(sampled, config), counter["n"]


def load_refmet(
    source: Iterable[str] | str,
    config: DatasetConfig = REFMET,
    *,
    source_version: str | None = None,
) -> RefMetBundle:
    """Load RefMet from a line iterator (tests) or a URL string (streamed, network)."""
    lines: Iterable[str] = stream_source_lines(source) if isinstance(source, str) else source
    input_df, n_scanned = subsample_from_lines(lines, config)
    sha = sha256_bytes(subsample_csv_bytes(input_df))
    card = build_card(input_df, n_scanned=n_scanned, source_sha=sha, config=config, source_version=source_version)
    return RefMetBundle(input_df=input_df, card=card)
