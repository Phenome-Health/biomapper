"""NECS Metabolon adapter (metabolite arm — structure oracle).

Turns the Monti et al. 2026 NECS supplement (GeroScience, MOESM5 xlsx) into a mapper-ready
``input_df`` (Metabolon ``CHEMICAL_NAME`` query + held-out gold columns) and a ``dataset_card``
recording N=1,495, input_type=name, per-column coverage (the source's *partial* external-ID
annotation: InChIKey ~53%, HMDB ~57%, KEGG ~32%, ...), source SHA, and license.

Design mirrors ``hajjar.py`` exactly:
  - The gold **InChIKey** column is the independent structure oracle — preserved verbatim, no
    resolver, zero shared infra with the system under test. Only BioMapper's *prediction* is
    ever resolved (in the scorer).
  - Network is isolated behind ``fetch_supplement`` so the transform (``build_input_df`` /
    ``build_card``) is fully unit-testable on an in-memory fixture.
  - Rows lacking a gold InChIKey are *retained* (they still count toward per-column coverage)
    but marked no-structure, so the structure-oracle scorer excludes them from the accuracy
    denominator — a coverage-only row, never a silent accuracy inflation/deflation.

The exact supplement column names are resolved against the fetched workbook at acquisition via
the candidate lists below (Metabolon delivery headers vary); the resolved header is a one-line
edit, and the fetched bytes' SHA is pinned on the card.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from typing import Any

import pandas as pd

from biomapper.benchmarks.config import NECS, DatasetConfig

# Marks rows retained for coverage accounting but excluded from the accuracy denominator.
HAS_STRUCTURE_COL = "has_gold_structure"

# Canonical held-out column -> candidate raw headers (case-insensitive, first match wins).
# The query column (chemical_name) is REQUIRED; every gold column is optional (a missing one
# yields an empty held-out column and 0% coverage — honest, not fabricated).
QUERY_CANDIDATES: tuple[str, ...] = (
    "CHEMICAL_NAME",
    "Chemical Name",
    "BIOCHEMICAL",
    "Biochemical Name",
    "metabolite_name",
    "Metabolite",
)
GOLD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "gold_inchikey": ("INCHIKEY", "InChIKey", "INCHI_KEY", "InChI Key"),
    "gold_smiles": ("SMILES", "Smiles", "Canonical SMILES"),
    "gold_hmdb": ("HMDB", "HMDB_ID", "HMDBID", "HMDB ID"),
    "gold_kegg": ("KEGG", "KEGG_ID", "KEGGID", "KEGG ID"),
    "gold_pubchem": ("PUBCHEM", "PubChem", "PUBCHEM_CID", "CID", "PubChem CID"),
    "gold_cas": ("CAS", "CAS_ID", "CAS Registry", "CAS_REGISTRY"),
    "gold_chemspider": ("CHEMSPIDER", "ChemSpider", "CSID"),
    "gold_refmet": ("REFMET", "RefMet", "REFMET_NAME", "RefMet Name"),
    # Second annotation vintage + molecular fields, needed by the gold-repair classifier
    # (Unit 4). Optional: a delivery without them yields honest-empty columns.
    "gold_inchikey_standard": ("inchi_key",),
    "gold_smiles_standard": ("smiles",),
    "gold_formula": ("formula", "FORMULA", "Formula"),
    "gold_exactmass": ("exactmass", "EXACTMASS", "exact_mass", "Exact Mass"),
}

# Modern-vintage columns are bound by EXACT header only: they must be honestly empty when the
# second vintage is absent, never silently fall back to the legacy column via a case-fold.
_VINTAGE_EXACT: frozenset[str] = frozenset(
    {"gold_inchikey_standard", "gold_smiles_standard", "gold_formula", "gold_exactmass"}
)


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def fetch_supplement(url: str, *, timeout: float = 60.0) -> bytes:
    """Fetch the NECS MOESM5 xlsx bytes (network). Isolated so tests never hit it."""
    import requests

    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def parse_xlsx(raw: bytes, sheet: str | int = 0) -> pd.DataFrame:
    """Parse xlsx supplement bytes into a raw DataFrame (all cells as strings)."""
    df = pd.read_excel(io.BytesIO(raw), sheet_name=sheet, dtype=str, engine="openpyxl")
    return df.fillna("")


def _norm(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _resolve_column(raw_df: pd.DataFrame, candidates: tuple[str, ...], *, exact_only: bool = False) -> str | None:
    """First raw header matching any candidate.

    Exact (case-sensitive) match is tried first, so vintage columns that differ ONLY by case
    -- ``SMILES`` (legacy) vs ``smiles`` (modern), ``INCHIKEY`` vs ``inchi_key`` -- bind to the
    intended column instead of colliding. A case-insensitive fallback (FIRST column wins, not
    last) then handles delivery-header variants for datasets that ship only one vintage.
    """
    cols = list(raw_df.columns)
    for cand in candidates:  # exact, case-sensitive
        if cand in cols:
            return cand
    if exact_only:
        # Vintage-specific columns (modern inchi_key/smiles/formula) must NOT fall back to the
        # legacy column on a case-fold, or a single-vintage delivery would bind both roles to it.
        return None
    lower: dict[str, str] = {}
    for c in cols:  # case-insensitive fallback, first occurrence wins (deterministic)
        lower.setdefault(str(c).strip().lower(), c)
    for cand in candidates:
        hit = lower.get(cand.strip().lower())
        if hit is not None:
            return hit
    return None


def build_input_df(raw_df: pd.DataFrame, config: DatasetConfig = NECS) -> pd.DataFrame:
    """Build the mapper-ready input_df: name query + held-out gold columns.

    The mapper is later called with ``name_column=config.name_column`` and
    ``provided_id_columns=[]`` — the gold columns ride along untouched and are consumed only by
    the scorer, never by BioMapper.
    """
    query_raw = _resolve_column(raw_df, QUERY_CANDIDATES)
    if query_raw is None:
        raise KeyError(
            f"NECS supplement is missing a recognizable query column; tried {QUERY_CANDIDATES!r} "
            f"against {list(raw_df.columns)!r}"
        )
    out = pd.DataFrame()
    out[config.name_column] = raw_df[query_raw].map(_norm)

    for canonical, candidates in GOLD_CANDIDATES.items():
        raw_col = _resolve_column(raw_df, candidates, exact_only=canonical in _VINTAGE_EXACT)
        out[canonical] = raw_df[raw_col].map(_norm) if raw_col is not None else ""

    # A row missing gold InChIKey is retained (still counted in coverage) but marked
    # no-structure: excluded from the accuracy denominator by the structure-oracle scorer.
    out[HAS_STRUCTURE_COL] = out[config.gold_inchikey_column].map(lambda s: bool(_norm(s)))
    return out


def build_card(
    raw_df: pd.DataFrame,
    source_sha: str,
    config: DatasetConfig = NECS,
) -> dict[str, Any]:
    """Build the dataset_card: N, input_type, per-column coverage, pinned source SHA, license."""
    input_df = build_input_df(raw_df, config)
    n = len(input_df)
    coverage: dict[str, dict[str, Any]] = {}
    for namespace, column in config.gold_coverage_columns:
        present = int((input_df.get(column, pd.Series([""] * n)).map(_norm) != "").sum())
        coverage[namespace] = {"n": present, "fraction": (present / n) if n else 0.0}
    return {
        "dataset": config.key,
        "arm": config.arm,
        "entity_type": config.entity_type,
        "input_type": config.input_type,
        "target_vocabs": list(config.target_vocabs),
        "n_rows": n,
        # Per-column coverage IS the Phase-2 characterization: the source's external-ID
        # annotation is partial, and the structure-oracle accuracy is reported only over the
        # InChIKey-bearing subset (coverage-only rows are excluded from the denominator).
        "coverage": coverage,
        "structure_oracle_column": config.gold_inchikey_column,
        "source_doi": config.source_doi,
        "source_sha256": source_sha,
        "license": config.license,
    }


@dataclass(frozen=True)
class NECSBundle:
    input_df: pd.DataFrame
    card: dict[str, Any]


def load_necs(source: bytes | str | pd.DataFrame, config: DatasetConfig = NECS) -> NECSBundle:
    """Load NECS from raw xlsx bytes (SHA pinned), a URL (fetched), or a raw DataFrame (tests).

    When ``source`` is a DataFrame the card's ``source_sha256`` is computed over its canonical
    CSV bytes so the pin is deterministic for tests.
    """
    if isinstance(source, pd.DataFrame):
        raw_df = source
        raw_bytes = raw_df.to_csv(index=False).encode("utf-8")
    elif isinstance(source, bytes):
        raw_bytes = source
        raw_df = parse_xlsx(raw_bytes)
    elif isinstance(source, str):
        raw_bytes = fetch_supplement(source)
        raw_df = parse_xlsx(raw_bytes)
    else:
        raise TypeError(f"unsupported source type {type(source)!r}")

    sha = sha256_bytes(raw_bytes)
    return NECSBundle(input_df=build_input_df(raw_df, config), card=build_card(raw_df, sha, config))
