"""MetaboliteAnnotator adapter (metabolite arm — NAME-input, name-hit-rate).

Turns the six MetaboLights sets MetaboliteAnnotator benchmarked on into a mapper-ready
``input_df`` (the MAF ``metabolite_identification`` name query + held-out gold columns) and a
``dataset_card`` recording N names, ion mode, per-accession breakdown, gold coverage, pinned
source SHA, and license. Mirrors ``necs_metabolon.py`` / ``hajjar.py``.

Design:
  - The comparable metric is a per-input NAME-HIT-RATE, so the unit is the input NAME. Rows with a
    blank ``metabolite_identification`` (MAF features with no identification) are DROPPED — they are
    not queryable input names and must not dilute the denominator. A name present but lacking a
    ``database_identifier`` is RETAINED (it is still an input name) with an empty gold — an honest
    unmatched-in-source row, never fabricated.
  - The held-out gold columns (``database_identifier`` -> gold CURIEs, ``smiles`` -> structure) are
    preserved verbatim and consumed only by the scorer's ID-concordance / charge-normalized
    qualifiers; ``provided_id_columns=[]`` keeps them out of BioMapper's sight.
  - Network is isolated behind ``fetch_maf_set`` so the transform is fully unit-testable on an
    in-memory fixture. ``fetch_maf_set`` FAILS LOUD on a needs-fetching placeholder accession, so an
    unresolved accession can never be silently scored.

ACCESSION STATUS: RESOLVED 2026-07-14 — the six MTBLS accessions are in ``config.accessions`` (from
the paper's Methods + Table 1). The needs-fetching sentinel + fail-loud fetch guard remain so any
future unresolved accession still refuses to score.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from typing import Any

import pandas as pd

from biomapper.benchmarks.config import (
    METABOLITEANNOTATOR_POS,
    NEEDS_FETCHING_SENTINEL,
    NameHitDatasetConfig,
)

# Passthrough column tagging which of the six MetaboLights sets a row came from (per-accession
# coverage / traceability — never the headline).
SOURCE_ACCESSION_COL = "source_accession"

# Stable per-input-row identity (accession-scoped), carried into the mapper output so the name-hit
# scorer can union a single input row's per-vocab passes WITHOUT collapsing distinct inputs that
# happen to share a metabolite name (e.g. "glucose" in two different MetaboLights studies, or twice
# within one study). It rides along untouched like the gold columns; BioMapper never reads it.
INPUT_ROW_ID_COL = "input_row_id"

# Canonical held-out column -> candidate raw MAF headers (case-insensitive, first match wins).
QUERY_CANDIDATES: tuple[str, ...] = (
    "metabolite_identification",
    "Metabolite name",
    "metabolite_name",
    "BIOCHEMICAL",
)
GOLD_ID_CANDIDATES: tuple[str, ...] = ("database_identifier", "Database Identifier", "database_id")
GOLD_SMILES_CANDIDATES: tuple[str, ...] = ("smiles", "SMILES", "Canonical SMILES")


class AccessionNotResolvedError(RuntimeError):
    """Raised when a live fetch is attempted against an unresolved needs-fetching placeholder."""


class NoMafError(RuntimeError):
    """Raised when a study exposes no unambiguous ``m_*.tsv`` MAF table to score.

    Fail-loud rather than silently parsing the wrong file: zero MAFs (nothing to score) or an
    ambiguous set of MAFs that ``config.mode`` cannot disambiguate both raise here, so a resolved
    run can never score the study bundle / the wrong assay's table.
    """


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def parse_maf(raw: bytes) -> pd.DataFrame:
    """Parse MetaboLights MAF bytes (tab-separated) into a raw DataFrame (all cells as strings)."""
    text = raw.decode("utf-8-sig")
    sep = "\t" if (text.splitlines() and "\t" in text.splitlines()[0]) else ","
    return pd.read_csv(io.StringIO(text), sep=sep, dtype=str).fillna("")


def _is_maf_filename(name: str) -> bool:
    """A MetaboLights Metabolite Assignment File is the ISA-Tab ``m_*.tsv`` table."""
    base = name.rsplit("/", 1)[-1].strip().lower()
    return base.startswith("m_") and base.endswith(".tsv")


# Ion-mode tokens as they appear in MAF/assay filenames (ISA-Tab convention), used ONLY to
# disambiguate when a study ships more than one MAF.
_MODE_TOKENS: dict[str, tuple[str, ...]] = {
    "positive": ("pos", "positive"),
    "negative": ("neg", "negative"),
}


def select_maf_filename(filenames: list[str], mode: str) -> str:
    """Pick the single ``m_*.tsv`` MAF to score; fail loud on none or unresolved ambiguity.

    Selection rule (documented + deterministic):
      1. Keep only ``m_*.tsv`` files — the study bundle, ISA ``a_*``/``s_*``/``i_*`` descriptors and
         raw data files are never the MAF.
      2. Exactly one MAF -> use it.
      3. More than one MAF -> disambiguate by ion ``mode`` (a filename token ``pos``/``positive`` or
         ``neg``/``negative``). Exactly one mode-matching MAF -> use it; otherwise FAIL LOUD (an
         ambiguous multi-assay study must not be silently reduced to an arbitrary table).
      4. Zero MAFs -> FAIL LOUD.
    """
    mafs = [f for f in filenames if _is_maf_filename(f)]
    if not mafs:
        raise NoMafError(
            f"no m_*.tsv MAF table found among study files {sorted(filenames)!r}; refusing to parse a "
            f"non-MAF file (e.g. the study bundle or an assay descriptor)."
        )
    if len(mafs) == 1:
        return mafs[0]
    tokens = _MODE_TOKENS.get(mode.lower(), ())
    mode_matches = [f for f in mafs if any(t in f.rsplit("/", 1)[-1].lower() for t in tokens)]
    if len(mode_matches) == 1:
        return mode_matches[0]
    raise NoMafError(
        f"study exposes {len(mafs)} MAF tables {sorted(mafs)!r} and ion mode {mode!r} does not select "
        f"exactly one ({len(mode_matches)} matched); refusing to guess. Pin the assay-matching MAF."
    )


def list_study_files(accession: str, config: NameHitDatasetConfig, *, timeout: float = 60.0) -> list[str]:
    """List the filenames in a MetaboLights study (network). Isolated so tests never hit it."""
    import requests

    base = config.source_url_template.format(accession=accession)
    resp = requests.get(f"{base}/files", timeout=timeout)
    resp.raise_for_status()
    return _extract_filenames(resp.json())


def _extract_filenames(payload: Any) -> list[str]:
    """Collect every ``file`` field from a MetaboLights ``/files`` JSON payload (shape-tolerant)."""
    names: list[str] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            f = obj.get("file")
            if isinstance(f, str) and f:
                names.append(f)
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(payload)
    return names


def _download_study_file(
    accession: str, filename: str, config: NameHitDatasetConfig, *, timeout: float = 60.0
) -> bytes:
    """Download one named file from the MetaboLights public FTP mirror (network). Isolated so tests
    never hit it. The web-service ``/download`` route returns HTTP 400 for these studies, so the MAF
    bytes come from the FTP mirror (``maf_download_url_template``), which serves per-study files
    directly."""
    import requests

    url = config.maf_download_url_template.format(accession=accession, filename=filename)
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def fetch_maf_set(accession: str, config: NameHitDatasetConfig, *, timeout: float = 60.0) -> pd.DataFrame:
    """Fetch + parse the study's MAF table for one accession, tagged with ``source_accession``.

    Resolves the actual ``m_*.tsv`` Metabolite Assignment File: (1) list the study's files, (2)
    ``select_maf_filename`` picks the mode-matching MAF (fail loud on none/ambiguity), (3) download
    and ``parse_maf`` THAT file — never the study bundle. FAILS LOUD on a needs-fetching placeholder
    before any network call. Network is isolated in ``list_study_files`` / ``_download_study_file`` so
    the transform is unit-testable without it.
    """
    if accession.startswith(NEEDS_FETCHING_SENTINEL):
        raise AccessionNotResolvedError(
            f"accession {accession!r} is a needs-fetching placeholder — the six real MTBLS accessions "
            f"for MetaboliteAnnotator (PMID {config.source_pmid}, DOI {config.source_doi}) were not "
            f"obtainable (ACS full text/SI blocked). Fill config.accessions with the real ids first."
        )
    filenames = list_study_files(accession, config, timeout=timeout)
    maf_name = select_maf_filename(filenames, config.mode)  # fail-loud on no/ambiguous MAF
    raw = _download_study_file(accession, maf_name, config, timeout=timeout)
    df = parse_maf(raw)
    df[SOURCE_ACCESSION_COL] = accession
    return df


def _norm(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _resolve_column(raw_df: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    lookup = {str(c).strip().lower(): c for c in raw_df.columns}
    for cand in candidates:
        hit = lookup.get(cand.strip().lower())
        if hit is not None:
            return hit
    return None


def build_input_df(raw_df: pd.DataFrame, config: NameHitDatasetConfig = METABOLITEANNOTATOR_POS) -> pd.DataFrame:
    """Build the mapper-ready input_df: name query + held-out gold columns (blank names dropped)."""
    query_raw = _resolve_column(raw_df, QUERY_CANDIDATES)
    if query_raw is None:
        raise KeyError(
            f"MAF is missing a recognizable metabolite-name column; tried {QUERY_CANDIDATES!r} "
            f"against {list(raw_df.columns)!r}"
        )
    gold_id_raw = _resolve_column(raw_df, GOLD_ID_CANDIDATES)
    smiles_raw = _resolve_column(raw_df, GOLD_SMILES_CANDIDATES)

    out = pd.DataFrame()
    out[config.name_column] = raw_df[query_raw].map(_norm)
    out[config.gold_id_column] = raw_df[gold_id_raw].map(_norm) if gold_id_raw is not None else ""
    if config.gold_smiles_column:
        out[config.gold_smiles_column] = raw_df[smiles_raw].map(_norm) if smiles_raw is not None else ""
    if SOURCE_ACCESSION_COL in raw_df.columns:
        out[SOURCE_ACCESSION_COL] = raw_df[SOURCE_ACCESSION_COL].map(_norm)

    # A blank name is a MAF feature with no identification: not a queryable input name -> drop it so
    # it never dilutes the name-hit denominator (the metric is per-INPUT-NAME).
    out = out[out[config.name_column] != ""].reset_index(drop=True)

    # Collapse duplicate names to UNIQUE names per study. MetaboliteAnnotator's name-hit denominator
    # is the count of unique metabolite names per MetaboLights set, not per MAF feature — the same
    # metabolite named identically on several features is ONE input name. Deduping per (accession,
    # name) reproduces the paper's published per-study totals exactly (e.g. 4314 positive) so the
    # hit-rate lands apples-to-apples beside the 93.2%/93.5% baselines; leaving features un-collapsed
    # inflates the denominator (MTBLS12764/MTBLS12636 carry repeated names). Held-out gold CURIEs are
    # UNIONed across the collapsed features so no reference identifier is lost, and the first non-blank
    # SMILES is kept. Dedup is case-sensitive on the normalized name (the exact-string match that
    # reproduces the paper's counts). Group order is first-appearance (``sort=False``) for determinism.
    group_keys = [config.name_column]
    if SOURCE_ACCESSION_COL in out.columns:
        group_keys = [SOURCE_ACCESSION_COL, config.name_column]

    def _union_gold(series: pd.Series) -> str:
        curies: list[str] = []
        for value in series:
            curies.extend(c for c in str(value).split("|") if c)
        return "|".join(dict.fromkeys(curies))  # order-preserving dedup

    agg: dict[str, Any] = {config.gold_id_column: _union_gold}
    if config.gold_smiles_column:
        agg[config.gold_smiles_column] = lambda s: next((x for x in s if str(x).strip()), "")
    out = out.groupby(group_keys, as_index=False, sort=False).agg(agg).reset_index(drop=True)

    # Stable per-input-row id, one per unique name per accession. Scoped by accession where available
    # (``{acc}:{n}``); the scorer keys on this id when it unions a name's per-vocab passes.
    if SOURCE_ACCESSION_COL in out.columns:
        seq = out.groupby(SOURCE_ACCESSION_COL).cumcount()
        out[INPUT_ROW_ID_COL] = out[SOURCE_ACCESSION_COL].str.cat(seq.astype(str), sep=":")
    else:
        out[INPUT_ROW_ID_COL] = out.index.map(lambda i: f"row:{i}")
    return out


def build_card(
    raw_df: pd.DataFrame,
    source_sha: str,
    config: NameHitDatasetConfig = METABOLITEANNOTATOR_POS,
) -> dict[str, Any]:
    """Build the dataset_card: N names, mode, per-accession breakdown, gold coverage, SHA, license."""
    input_df = build_input_df(raw_df, config)
    n = len(input_df)
    gold_present = int((input_df[config.gold_id_column].map(_norm) != "").sum())
    smiles_present = (
        int((input_df.get(config.gold_smiles_column, pd.Series([""] * n)).map(_norm) != "").sum())
        if config.gold_smiles_column
        else 0
    )

    per_accession: dict[str, dict[str, Any]] = {}
    if SOURCE_ACCESSION_COL in input_df.columns:
        for acc, grp in input_df.groupby(SOURCE_ACCESSION_COL):
            per_accession[str(acc)] = {
                "n_names": int(len(grp)),
                "gold_id_n": int((grp[config.gold_id_column].map(_norm) != "").sum()),
            }

    return {
        "dataset": config.key,
        "arm": config.arm,
        "entity_type": config.entity_type,
        "mode": config.mode,
        "input_type": config.input_type,
        "target_vocabs": list(config.target_vocabs),
        "n_names": n,
        "gold_id_coverage": {"n": gold_present, "fraction": (gold_present / n) if n else 0.0},
        "gold_smiles_coverage": {"n": smiles_present, "fraction": (smiles_present / n) if n else 0.0},
        "per_accession": per_accession,
        "accessions": list(config.accessions),
        "accessions_status": config.accessions_status,
        "gold_id_column": config.gold_id_column,
        "source_doi": config.source_doi,
        "source_pmid": config.source_pmid,
        "source_sha256": source_sha,
        "license": config.license,
    }


@dataclass(frozen=True)
class MetaboliteAnnotatorBundle:
    input_df: pd.DataFrame
    card: dict[str, Any]


def load_metaboliteannotator(
    source: bytes | str | pd.DataFrame | tuple[str, ...] | list[str],
    config: NameHitDatasetConfig = METABOLITEANNOTATOR_POS,
) -> MetaboliteAnnotatorBundle:
    """Load from a raw DataFrame (tests), MAF bytes, a single accession/URL, or an accessions tuple.

    For an accessions tuple/list each set is fetched via ``fetch_maf_set`` (fails loud on a
    placeholder) and concatenated; the card's SHA is pinned over the concatenated CSV bytes so the
    scored set is reproducible. Network stays isolated in ``fetch_maf_set``.
    """
    if isinstance(source, pd.DataFrame):
        raw_df = source
        raw_bytes = raw_df.to_csv(index=False).encode("utf-8")
    elif isinstance(source, (tuple, list)):
        frames = [fetch_maf_set(acc, config) for acc in source]
        raw_df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        raw_bytes = raw_df.to_csv(index=False).encode("utf-8")
    elif isinstance(source, bytes):
        raw_bytes = source
        raw_df = parse_maf(raw_bytes)
    elif isinstance(source, str):
        raw_df = fetch_maf_set(source, config)
        raw_bytes = raw_df.to_csv(index=False).encode("utf-8")
    else:
        raise TypeError(f"unsupported source type {type(source)!r}")

    sha = sha256_bytes(raw_bytes)
    return MetaboliteAnnotatorBundle(input_df=build_input_df(raw_df, config), card=build_card(raw_df, sha, config))
