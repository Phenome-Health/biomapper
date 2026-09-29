"""SwissLipids adapter (cross-source lipid ACCURACY arm — structure oracle, non-Kraken gold).

SwissLipids ships a TSV of curated lipids with its OWN names/abbreviations, InChIKey, SMILES, and a
PubChem CID crosswalk. It is NOT a Kraken ingest source, so it is a legal accuracy gold (LMSD/RefMet
are in Kraken -> circular). The query is SwissLipids' own name (non-LIPID-MAPS dialect); the gold
structure is resolved from the HELD-OUT PubChem CID by the INDEPENDENT PubChem resolver at scoring
time (see scorers/cross_source_gold.py), keeping the resolution-path binding (KG/RefMet) disjoint
from the gold source (PubChem). Mirrors ``lmsd.py``: streamed + reservoir-subsampled + persisted,
fully offline-testable behind ``stream_tsv_lines``.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from biomapper.benchmarks.adapters.backbones import (  # noqa: F401  (re-exported for the adapter's public surface)
    load_persisted_subsample,
    reservoir_sample,
    sha256_bytes,
    subsample_csv_bytes,
    subsample_filename,
)
from biomapper.benchmarks.config import SWISSLIPIDS, DatasetConfig

QUERY_SOURCE_COL = "query_source"
HELD_OUT_PUBCHEM_COL = "held_out_pubchem"
HAS_PUBCHEM_COL = "has_gold_pubchem"
SL_OWN_INCHIKEY_COL = "gold_inchikey_swisslipids"
GOLD_STRUCTURE_SOURCE = "PubChem"  # the gold InChIKey is resolved from the held-out PubChem CID

# Tolerant header lookup: SwissLipids column labels drift across releases, so each canonical field maps
# to an ordered list of accepted header names (first present wins).
_QUERY_HEADERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("name", ("Name",)),
    ("abbreviation", ("Abbreviation*", "Abbreviation")),
)
_INCHIKEY_HEADERS = ("InChI key (pH7.3)", "InChI key", "InChIKey")
_SMILES_HEADERS = ("SMILES (pH7.3)", "SMILES")
_HMDB_HEADERS = ("HMDB",)
_PUBCHEM_HEADERS = ("PubChem CID", "PubChem", "PubChem CID*")


def _first(tags: dict[str, str], headers: Iterable[str]) -> str:
    for h in headers:
        value = str(tags.get(h, "")).strip()
        if value:
            return value
    return ""


def tsv_records(lines: Iterable[str]) -> Iterator[dict[str, str]]:
    """Stream a headered TSV, yielding one ``{header: value}`` dict per data row (pure transform)."""
    header: list[str] | None = None
    for raw in lines:
        row = raw.rstrip("\n")
        if not row:
            continue
        fields = row.split("\t")
        if header is None:
            header = fields
            continue
        yield {header[i]: (fields[i] if i < len(fields) else "") for i in range(len(header))}


def _pick_query(tags: dict[str, str]) -> tuple[str, str]:
    for source, headers in _QUERY_HEADERS:
        value = _first(tags, headers)
        if value:
            return value, source
    return "", ""


def swisslipids_records(lines: Iterable[str], *, require_pubchem: bool = True) -> Iterator[dict[str, str]]:
    """Parse SwissLipids TSV rows into canonical rows: name query + held-out PubChem + own InChIKey.

    A row with no usable name is dropped. A row missing the PubChem CID (the gold-resolution source)
    is dropped when ``require_pubchem`` — the accuracy oracle needs an independently-resolvable gold.
    """
    for tags in tsv_records(lines):
        name, source = _pick_query(tags)
        if not name:
            continue
        pubchem = _first(tags, _PUBCHEM_HEADERS)
        if require_pubchem and not pubchem:
            continue
        yield {
            SWISSLIPIDS.name_column: name,
            QUERY_SOURCE_COL: source,
            HELD_OUT_PUBCHEM_COL: pubchem,
            SL_OWN_INCHIKEY_COL: _first(tags, _INCHIKEY_HEADERS),
            "gold_smiles": _first(tags, _SMILES_HEADERS),
            "gold_hmdb": _first(tags, _HMDB_HEADERS),
        }


def build_input_df(records: list[dict[str, str]], config: DatasetConfig = SWISSLIPIDS) -> pd.DataFrame:
    """Mapper-ready input_df: name query + held-out gold columns. ``gold_inchikey`` is filled later.

    ``gold_inchikey`` (the scored structure oracle) starts EMPTY here on purpose — it is populated at
    scoring time by the independent PubChem resolver from ``held_out_pubchem`` (Task 8), NOT copied
    from SwissLipids' own InChIKey. This is what makes the gold source (PubChem) disjoint from the
    resolution path.
    """
    columns = [
        config.name_column,
        QUERY_SOURCE_COL,
        HELD_OUT_PUBCHEM_COL,
        SL_OWN_INCHIKEY_COL,
        "gold_smiles",
        "gold_hmdb",
    ]
    rows = [{c: rec.get(c, "") for c in columns} for rec in records]
    out = pd.DataFrame(rows, columns=columns)
    out[config.gold_inchikey_column] = ""  # filled by cross_source_gold at scoring time
    out[HAS_PUBCHEM_COL] = out[HELD_OUT_PUBCHEM_COL].map(lambda s: bool(str(s).strip()))
    return out


def _name_source_breakdown(input_df: pd.DataFrame) -> dict[str, int]:
    if QUERY_SOURCE_COL not in input_df.columns:
        return {}
    counts = input_df[QUERY_SOURCE_COL].map(lambda s: str(s).strip() or "none").value_counts()
    return {str(k): int(v) for k, v in counts.items()}


def build_card(
    input_df: pd.DataFrame,
    *,
    n_scanned: int,
    source_sha: str,
    config: DatasetConfig = SWISSLIPIDS,
    source_version: str | None = None,
) -> dict[str, Any]:
    """Dataset card: N, subsample, name mix, and the independence audit (gold source vs Kraken)."""
    n = len(input_df)
    coverage: dict[str, dict[str, Any]] = {}
    for namespace, column in config.gold_coverage_columns:
        col = input_df[column] if column in input_df.columns else pd.Series([""] * n)
        present = int(col.map(lambda s: bool(str(s).strip())).sum())
        coverage[namespace] = {"n": present, "fraction": (present / n) if n else 0.0}
    return {
        "dataset": config.key,
        "arm": config.arm,
        "role": config.role,
        "entity_type": config.entity_type,
        "input_type": config.input_type,
        "target_vocabs": list(config.target_vocabs),
        "n_rows": n,
        "n_scanned": n_scanned,
        "subsample": {"n": config.subsample_n, "seed": config.subsample_seed, "method": "reservoir"},
        "require_gold_structure": config.require_gold_structure,
        "coverage": coverage,
        "name_source_breakdown": _name_source_breakdown(input_df),
        "structure_oracle_column": config.gold_inchikey_column,
        # Independence audit: the gold structure is resolved from the held-out PubChem CID (external,
        # non-KG) and SwissLipids is NOT a Kraken ingest source, so it is a legal accuracy gold.
        "gold_structure_source": GOLD_STRUCTURE_SOURCE,
        "gold_is_kraken_ingest_source": False,
        "held_out_id_column": HELD_OUT_PUBCHEM_COL,
        "source_doi": config.source_doi,
        "source_url": config.source_url,
        "subsample_sha256": source_sha,
        "subsample_filename": subsample_filename(config.key),
        "source_version": source_version,
        "license": config.license,
    }


@dataclass(frozen=True)
class SwissLipidsBundle:
    input_df: pd.DataFrame
    card: dict[str, Any]


def persist_subsample(bundle: SwissLipidsBundle, out_dir: Path | str) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / subsample_filename(bundle.card["dataset"])
    path.write_bytes(subsample_csv_bytes(bundle.input_df))
    return path


def subsample_from_lines(lines: Iterable[str], config: DatasetConfig) -> tuple[pd.DataFrame, int]:
    if config.subsample_n is None:
        raise ValueError(f"{config.key}: subsample_n is required (SwissLipids ships tens of thousands of rows).")
    counter = {"n": 0}

    def _counting(it: Iterator[dict[str, str]]) -> Iterator[dict[str, str]]:
        for rec in it:
            counter["n"] += 1
            yield rec

    sampled = reservoir_sample(
        _counting(swisslipids_records(lines, require_pubchem=config.require_gold_structure)),
        config.subsample_n,
        config.subsample_seed,
    )
    return build_input_df(sampled, config), counter["n"]


def stream_tsv_lines(url: str, *, timeout: float = 300.0) -> Iterator[str]:
    """Stream the SwissLipids TSV line-by-line (network). Isolated (not unit-tested)."""
    import requests

    with requests.get(url, timeout=timeout, stream=True) as resp:
        resp.raise_for_status()
        for raw in resp.iter_lines(decode_unicode=True):
            yield raw if raw is not None else ""


def load_swisslipids(
    source: Iterable[str] | str,
    config: DatasetConfig = SWISSLIPIDS,
    *,
    source_version: str | None = None,
) -> SwissLipidsBundle:
    """Load SwissLipids from a line iterator (tests) or a URL string (streamed download)."""
    lines: Iterable[str] = stream_tsv_lines(source) if isinstance(source, str) else source
    input_df, n_scanned = subsample_from_lines(lines, config)
    sha = sha256_bytes(subsample_csv_bytes(input_df))
    card = build_card(input_df, n_scanned=n_scanned, source_sha=sha, config=config, source_version=source_version)
    return SwissLipidsBundle(input_df=input_df, card=card)
