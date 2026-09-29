"""Cross-cohort panel adapter (Deliverable 2 — harmonization arms).

Ported verbatim from the biomapper2 engine at ``origin/dev``
(``studies/external_benchmarks/adapters/cohort_panel.py``, commit
``1ffb571e54fe028ef0ae4e748fc2e7ec093ee603``). Only the module path changed: this adapter has no
engine imports, so the panel construction, the exclusion accounting, and the
``certifiable`` determination are the same code that produced the previously circulated panel
sizes (necs 1213, xuetal 821, llfs 364, blsa 468, arivale 659).

A *cohort panel* is a much simpler object than a benchmark dataset with a gold oracle: it is a
list of metabolite names plus whatever vendor cross-reference IDs the cohort published. Unlike
``necs_metabolon`` there is **no gold structure column on the cohort side** — the structure used
to certify a cross-cohort link comes from an oracle that is INDEPENDENT of the KG resolution
(``scorers/independent_inchikey``), resolved from these vendor IDs. A cohort with no vendor IDs
(BLSA: names only) is therefore ``certifiable=False``: its links can be counted but never
structurally certified (this is also the sum-composition-lipid reality for BLSA per Tian 2023).

Design mirrors ``necs_metabolon.py`` discipline:
  - Network isolated behind ``fetch_bytes`` so ``build_panel`` is unit-testable on in-memory data.
  - Source bytes' SHA is pinned on the panel card.
  - Every row dropped (blank name, unnamed ``X-`` feature, de-dup) is COUNTED and reported —
    never a silent exclusion (the SwissLipids zero-byte and NECS gold-column lessons).

The panel exposes exactly two things the arm harness needs:
  - ``name_column`` — the query handed to the mapper (Arm M: name-only).
  - ``id_columns`` — vendor namespace -> column, fed to the mapper as ``provided_id_columns``
    (Arm M+ID) and to the independent structure oracle (certificate).
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

# Unnamed vendor features (Metabolon "X - 12345" / Biocrates placeholders). Excluded from the
# panel but the count is reported, never silently dropped.
_UNIDENTIFIED = re.compile(r"^x\s*-\s*\d+$", re.IGNORECASE)
_NULL = frozenset({"", "-", "na", "nan", "none", "null"})


@dataclass(frozen=True)
class CohortPanelConfig:
    """Registry entry for one cohort's published metabolite panel."""

    key: str  # e.g. "arivale"
    name_column: str  # output query column (mapper.name_column)
    # vendor namespace -> raw source header. Empty dict => names only => not structurally
    # certifiable.
    id_columns: dict[str, str] = field(default_factory=dict)
    source_doi: str = ""
    license: str = ""
    # Monti's published panel size for this cohort (Table 2); when it differs from the loaded N,
    # the delta is surfaced on the card as an unresolved provenance gap, not silently reconciled.
    monti_panel_size: int | None = None


@dataclass(frozen=True)
class CohortPanel:
    """A loaded cohort panel: query names + vendor IDs + provenance."""

    key: str
    frame: pd.DataFrame  # columns: name_column + id_columns.keys()
    name_column: str
    id_columns: tuple[str, ...]  # namespaces with a resolved column (subset of config.id_columns)
    certifiable: (
        bool  # True iff at least one structural vendor ID (HMDB/PubChem/CAS/KEGG) is present
    )
    card: dict[str, Any]

    @property
    def names(self) -> list[str]:
        return self.frame[self.name_column].tolist()


# Vendor namespaces from which an INDEPENDENT structure can be resolved (PubChem PUG-REST).
# KEGG alone is not structure-resolvable via the PubChem oracle, so it does not confer
# certifiability.
_STRUCTURE_ID_NAMESPACES = frozenset({"hmdb", "pubchem", "cas"})


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def fetch_bytes(url: str, *, timeout: float = 60.0) -> bytes:
    """Fetch source bytes (network). Isolated so tests never hit it."""
    import requests

    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def _clean(value: Any) -> str:  # noqa: ANN401 - a source cell is genuinely untyped
    s = "" if value is None else str(value).strip()
    return "" if s.lower() in _NULL else s


def parse_xlsx(raw: bytes, sheet: str | int = 0) -> pd.DataFrame:
    return pd.read_excel(io.BytesIO(raw), sheet_name=sheet, dtype=str, engine="openpyxl").fillna("")


def parse_name_list(raw: bytes) -> pd.DataFrame:
    """Parse a newline-delimited name list (BLSA) into a single-column raw frame."""
    text = raw.decode("utf-8-sig")
    names = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return pd.DataFrame({"name": names})


def build_panel(raw_df: pd.DataFrame, config: CohortPanelConfig, source_sha: str) -> CohortPanel:
    """Build a CohortPanel from a raw frame: resolve columns, exclude+count, pin provenance."""
    cols = {str(c).strip(): c for c in raw_df.columns}
    lower = {str(c).strip().lower(): c for c in raw_df.columns}

    # Query column: exact then case-insensitive.
    name_raw = cols.get(config.name_column) or lower.get(config.name_column.lower())
    if name_raw is None:
        raise KeyError(
            f"{config.key} panel is missing query column {config.name_column!r}; "
            f"have {list(raw_df.columns)!r}"
        )

    resolved_ids: dict[str, str] = {}
    for ns, header in config.id_columns.items():
        hit = cols.get(header) or lower.get(header.lower())
        if hit is not None:
            resolved_ids[ns] = hit

    out = pd.DataFrame()
    out[config.name_column] = raw_df[name_raw].map(_clean)
    for ns, raw_col in resolved_ids.items():
        out[ns] = raw_df[raw_col].map(_clean)

    n_raw = len(out)
    blank = int((out[config.name_column] == "").sum())
    out = out[out[config.name_column] != ""]
    unidentified = int(out[config.name_column].map(lambda s: bool(_UNIDENTIFIED.match(s))).sum())
    out = out[~out[config.name_column].map(lambda s: bool(_UNIDENTIFIED.match(s)))]
    n_prededup = len(out)
    out = out.drop_duplicates(subset=[config.name_column]).reset_index(drop=True)
    duplicates = n_prededup - len(out)

    certifiable = bool(_STRUCTURE_ID_NAMESPACES & set(resolved_ids))

    exclusions = {"blank_name": blank, "unidentified_x": unidentified, "duplicate_name": duplicates}
    card: dict[str, Any] = {
        "cohort": config.key,
        "n_rows": len(out),
        "n_raw": n_raw,
        "exclusions": exclusions,
        "id_namespaces": sorted(resolved_ids),
        "certifiable": certifiable,
        "source_doi": config.source_doi,
        "source_sha256": source_sha,
        "license": config.license,
    }
    # Surface — never silently reconcile — a gap between the loaded panel and Monti's Table 2 size.
    if config.monti_panel_size is not None and config.monti_panel_size != len(out):
        card["monti_panel_size"] = config.monti_panel_size
        card["monti_size_gap"] = len(out) - config.monti_panel_size

    return CohortPanel(
        key=config.key,
        frame=out,
        name_column=config.name_column,
        id_columns=tuple(sorted(resolved_ids)),
        certifiable=certifiable,
        card=card,
    )


def load_cohort_panel(
    source: bytes | pd.DataFrame,
    config: CohortPanelConfig,
    *,
    name_list: bool = False,
) -> CohortPanel:
    """Load a cohort panel from raw bytes (xlsx or newline list) or an in-memory frame (tests)."""
    if isinstance(source, pd.DataFrame):
        raw_df = source
        raw_bytes = raw_df.to_csv(index=False).encode("utf-8")
    elif isinstance(source, bytes):
        raw_bytes = source
        raw_df = parse_name_list(raw_bytes) if name_list else parse_xlsx(raw_bytes)
    else:
        raise TypeError(f"unsupported source type {type(source)!r}")
    return build_panel(raw_df, config, sha256_bytes(raw_bytes))


# --- Registry -------------------------------------------------------------------------------

# Watanabe 2023 (Nat Med, PMC10115644, CC BY) Supplementary Data 2, sheet Arivale_Metabolomics.
# 766 metabolites with CAS/KEGG/HMDB/PubChem IDs — independent structure available on this side.
ARIVALE = CohortPanelConfig(
    key="arivale",
    name_column="BiochemicalName",
    id_columns={"cas": "CAS_ID", "kegg": "KEGG_ID", "hmdb": "HMDB_ID", "pubchem": "PubChem_ID"},
    source_doi="10.1038/s41591-023-02248-0",
    license="CC BY (Watanabe et al. 2023, Nature Medicine).",
    # Monti used a 626 subset of the 766 public panel; the gap is surfaced on the card.
    monti_panel_size=626,
)

# --- Spreadsheet-sourced cohorts (the 4-cohort metabolite spreadsheet: one sheet, columns
# blsa/llfs/necs/xuetal, NAMES ONLY). These are the canonical source for BLSA/LLFS/NECS/Xu; each
# config's name_column IS the cohort's column, so the generic loader picks it straight out of the
# spreadsheet frame. No vendor IDs => certifiable=False for all four => structural certification is
# DEFERRED (coverage-first); the certificate (Unit 5) is built but not wired to these until an
# independent structure source is chosen. Arivale is NOT in this spreadsheet (see ARIVALE above).

# BLSA (Biocrates) — spreadsheet column = 468 names, matches Monti Table 2. ~81% sum-composition
# lipid species (Tian 2023): counts-only context, never a structural-precision claim.
BLSA = CohortPanelConfig(
    key="blsa",
    name_column="blsa",
    id_columns={},
    source_doi="10.1038/s43587-023-00514-x",
    license="See Tian et al. 2023 supplement terms.",
    monti_panel_size=468,  # Monti Table 2 — spreadsheet column matches
)

# LLFS (MS) — spreadsheet column = 364, the RefMet-standardizable SUBSET. The published panel is
# 408 (Sebastiani 2024); the 364-vs-408 gap is the pre-filtering and is surfaced, so a later switch
# to the full panel (needed to show recovery on the non-standardizing names) is an explicit choice.
LLFS = CohortPanelConfig(
    key="llfs",
    name_column="llfs",
    id_columns={},
    source_doi="10.1016/j.xgen.2024.100601",
    license="See Sebastiani et al. 2024 supplement terms.",
    monti_panel_size=408,  # published panel; spreadsheet ships the 364 RefMet subset — gap surfaced
)

# NECS (Metabolon) — spreadsheet column = 1495 (incl. unnamed X- features, excluded here → 1213
# named, matching Monti's "1213 named"). The structure-oracle NECS work uses the MOESM5 supplement.
NECS = CohortPanelConfig(
    key="necs",
    name_column="necs",
    id_columns={},
    source_doi="10.1007/s11357-026-02174-2",
    license="See Monti et al. 2026 supplement terms.",
    monti_panel_size=1213,  # named metabolites (Table 2); 282 unnamed X- excluded at load
)

# Xu et al. 2022 (Metabolon) — spreadsheet column = 821 names.
XU = CohortPanelConfig(
    key="xuetal",
    name_column="xuetal",
    id_columns={},
    source_doi="10.1038/s42003-022-03323-x",
    license="See Xu et al. 2022 supplement terms.",
    monti_panel_size=821,
)

# The 4-cohort spreadsheet's columns, for the spreadsheet loader.
SPREADSHEET_COHORTS: dict[str, CohortPanelConfig] = {c.key: c for c in (BLSA, LLFS, NECS, XU)}
