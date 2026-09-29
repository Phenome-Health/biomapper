"""LMSD (LIPID MAPS Structure Database) adapter (metabolite/lipid arm — structure oracle).

LMSD is the LIPID MAPS Structure Database, governed by Liebisch et al. 2020 (J. Lipid Res.,
DOI 10.1194/jlr.S120001025 — the LIPID MAPS shorthand nomenclature). The bulk SDF download
(``lipidmaps.org/files/?file=LMSD&ext=sdf.zip``, CC BY 4.0, ~50k curated records) ships per
record a lipid **shorthand ``ABBREVIATION``** (e.g. ``PC 16:0/18:1``), a common ``NAME``, a
``SYSTEMATIC_NAME``, the ``INCHI_KEY`` + ``SMILES``, and a crosswalk to PubChem/HMDB/KEGG/ChEBI/
SwissLipids. This targets BioMapper's KNOWN lipid weakness (NIST SRM 1950 lipids scored only
40.3%) with an honest gap-characterization on lipid NAME inputs.

Design mirrors ``refmet`` (a large, streamed, reservoir-subsampled structure-oracle arm), with the
LMSD-specific transforms:

  - **Structure oracle = LMSD's own ``INCHI_KEY``** (first block, charge-normalized via the record's
    SMILES). Preserved verbatim; the crosswalk IDs are coverage only. Only BioMapper's *prediction*
    is ever resolved (in the scorer) — the gold InChIKey never touches the resolver.
  - **NAME input, LM_ID held out (contamination control).** The query handed to BioMapper is a lipid
    *name* whose structure must be inferred — the per-row best-available of ``ABBREVIATION`` (lipid
    shorthand) -> ``NAME`` (common) -> ``SYSTEMATIC_NAME``. The ``LM_ID`` is NEVER a query and NEVER
    the oracle; it is carried only as a held-out provenance column (``held_out_lm_id``). Because the
    Kestrel KG recognizes the LIPIDMAPS namespace, scoring on LM_IDs would be circular — so we score
    on names, whose structure BioMapper must resolve independently.
  - **Streaming + reservoir subsample** (like ``refmet``): the SDF is streamed record-by-record,
    filtered to the InChIKey-bearing population (``require_gold_structure`` — the oracle needs a
    held-out structure), deterministically reservoir-subsampled (``seed`` pinned), and the exact
    scored subset is PERSISTED beside the card (the download is a mutable "current release", so
    URL+seed+n alone cannot reconstruct it).

Network is isolated behind ``stream_sdf_lines`` so the SDF parse + subsample + card transforms are
fully unit-testable on an in-memory line iterator.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

# Reuse the generic streaming / subsample / persistence machinery (identical discipline as RefMet
# and the gene/protein backbones). These are dataset-agnostic helpers.
from biomapper.benchmarks.adapters.backbones import (  # noqa: F401  (re-exported for the adapter's public surface)
    load_persisted_subsample,
    reservoir_sample,
    sha256_bytes,
    subsample_csv_bytes,
    subsample_filename,
)
from biomapper.benchmarks.config import LMSD, DatasetConfig

# Marks rows retained for coverage accounting but excluded from the accuracy denominator (mirrors
# refmet/srm1950). Under ``require_gold_structure`` every sampled row is structure-bearing, but the
# flag is still emitted so the scorer's coverage-only rule applies uniformly across datasets.
HAS_STRUCTURE_COL = "has_gold_structure"

# Column recording WHICH SDF name field supplied the query (for card transparency; the name mix is
# load-bearing — a reviewer must see the shorthand/common/systematic composition of the scored set).
QUERY_SOURCE_COL = "query_source"
# Held-out LIPID MAPS id — carried for provenance ONLY; never a query, never the oracle (the
# contamination control: the KG recognizes the LIPIDMAPS namespace, so an LM_ID input would be
# circular). It rides alongside the name into the mapper output but ``provided_id_columns=[]``, so
# BioMapper never sees it.
HELD_OUT_LM_ID_COL = "held_out_lm_id"

# Canonical query field -> the SDF tag it comes from, in preference order (shorthand first: the lipid
# ABBREVIATION is the metabolomics-standard name and the hardest name->structure inference; then the
# common NAME; then the full SYSTEMATIC_NAME). The chosen source is recorded per row.
QUERY_PREFERENCE: tuple[tuple[str, str], ...] = (
    ("abbreviation", "ABBREVIATION"),
    ("common_name", "NAME"),
    ("systematic_name", "SYSTEMATIC_NAME"),
)

# Canonical held-out column -> SDF tag. ``INCHI_KEY`` is the oracle; ``SMILES`` powers the charge-
# normalized gold; the rest are crosswalk coverage. ``LM_ID`` is held out (never scored/provided).
SDF_GOLD_TAGS: dict[str, str] = {
    "gold_inchikey": "INCHI_KEY",
    "gold_smiles": "SMILES",
    "gold_chebi": "CHEBI_ID",
    "gold_hmdb": "HMDB_ID",
    "gold_pubchem": "PUBCHEM_CID",
    "gold_kegg": "KEGG_ID",
    "gold_swisslipids": "SWISSLIPIDS_ID",
}
LM_ID_TAG = "LM_ID"


def sdf_records(lines: Iterable[str]) -> Iterator[dict[str, str]]:
    """Stream an SDF, yielding one ``{TAG: value}`` dict per record (the connection table is skipped).

    SDF records are ``$$$$``-delimited. Each tagged datum is a ``> <TAG>`` line followed by one or
    more value lines terminated by a blank line. Only the tag section is parsed (the molblock/coords
    are irrelevant to a name->structure benchmark). Multi-line values are ``\\n``-joined, but every
    LMSD field this adapter reads (names, InChIKey, SMILES, crosswalk ids) is single-line. Parsing is
    a pure line transform so it is fully unit-testable on an in-memory iterator (no network, no RDKit).
    """
    record: dict[str, str] = {}
    current_tag: str | None = None
    value_lines: list[str] = []

    def _flush_tag() -> None:
        nonlocal current_tag, value_lines
        if current_tag is not None:
            record[current_tag] = "\n".join(value_lines).strip()
        current_tag = None
        value_lines = []

    for raw in lines:
        line = raw.rstrip("\n")
        if line.strip() == "$$$$":
            _flush_tag()
            if record:
                yield record
            record = {}
            continue
        if line.startswith("> <") and line.rstrip().endswith(">"):
            _flush_tag()
            current_tag = line.strip()[3:-1]  # between "> <" and ">"
            continue
        if current_tag is not None:
            if line.strip() == "":
                _flush_tag()  # blank line terminates the current tag value
            else:
                value_lines.append(line)
    # Emit a trailing record if the SDF did not end with a delimiter.
    _flush_tag()
    if record:
        yield record


def _pick_query(tags: dict[str, str]) -> tuple[str, str]:
    """Best-available lipid name + its source field, per ``QUERY_PREFERENCE``. ("", "") if none."""
    for source, tag in QUERY_PREFERENCE:
        value = str(tags.get(tag, "")).strip()
        if value:
            return value, source
    return "", ""


def lmsd_records(lines: Iterable[str], *, require_structure: bool = True) -> Iterator[dict[str, str]]:
    """Parse LMSD SDF records into canonical rows: lipid NAME query + gold InChIKey + crosswalk.

    Filtering happens DURING the stream so the reservoir samples from the eligible population
    (structure-bearing rows with a usable name) rather than the raw file. A record with no usable
    name is always dropped (nothing to map); a record missing the oracle InChIKey is dropped only
    when ``require_structure`` (the structure oracle needs a held-out structure). The ``LM_ID`` is
    carried as held-out provenance only — never as the query, never as the oracle.
    """
    for tags in sdf_records(lines):
        name, source = _pick_query(tags)
        if not name:
            continue
        inchikey = str(tags.get(SDF_GOLD_TAGS["gold_inchikey"], "")).strip()
        if require_structure and not inchikey:
            continue
        rec: dict[str, str] = {
            LMSD.name_column: name,
            QUERY_SOURCE_COL: source,
            HELD_OUT_LM_ID_COL: str(tags.get(LM_ID_TAG, "")).strip(),
        }
        for canonical, tag in SDF_GOLD_TAGS.items():
            rec[canonical] = str(tags.get(tag, "")).strip()
        yield rec


def build_input_df(records: list[dict[str, str]], config: DatasetConfig = LMSD) -> pd.DataFrame:
    """Build the mapper-ready input_df: name query + query-source + held-out gold/provenance columns.

    The mapper is later called with ``name_column=config.name_column`` and ``provided_id_columns=[]``,
    so the gold columns AND the held-out LM_ID ride along untouched and are consumed only by the
    scorer (gold InChIKey/SMILES) or reported for provenance (LM_ID / query source) — never by
    BioMapper. A defensive anti-contamination guard asserts the query is never an LM_ID.
    """
    gold_cols = [col for _, col in config.gold_coverage_columns]
    columns = [config.name_column, QUERY_SOURCE_COL, HELD_OUT_LM_ID_COL, *gold_cols]
    rows = [{c: rec.get(c, "") for c in columns} for rec in records]
    out = pd.DataFrame(rows, columns=columns)
    # Contamination guard (fail-loud): the query must be a lipid NAME, never the held-out LM_ID.
    leaked = out[config.name_column].astype(str).str.strip() == out[HELD_OUT_LM_ID_COL].astype(str).str.strip()
    if bool(leaked.any()) and bool((out[HELD_OUT_LM_ID_COL].astype(str).str.strip() != "").any()):
        n = int((leaked & (out[HELD_OUT_LM_ID_COL].astype(str).str.strip() != "")).sum())
        raise ValueError(
            f"LMSD contamination guard: {n} query value(s) equal the held-out LM_ID — the LIPID MAPS "
            f"id must never be handed to BioMapper as the name (the KG recognizes the LIPIDMAPS "
            f"namespace, so scoring on LM_IDs would be circular). Refusing to build the input."
        )
    out[HAS_STRUCTURE_COL] = out[config.gold_inchikey_column].map(lambda s: bool(str(s).strip()))
    return out


def _name_source_breakdown(input_df: pd.DataFrame) -> dict[str, int]:
    """Per-source counts of which SDF field supplied each query (shorthand/common/systematic mix)."""
    if QUERY_SOURCE_COL not in input_df.columns:
        return {}
    counts = input_df[QUERY_SOURCE_COL].map(lambda s: str(s).strip() or "none").value_counts()
    return {str(k): int(v) for k, v in counts.items()}


def build_card(
    input_df: pd.DataFrame,
    *,
    n_scanned: int,
    source_sha: str,
    config: DatasetConfig = LMSD,
    source_version: str | None = None,
) -> dict[str, Any]:
    """Build the dataset_card: N, subsample n/seed, per-column coverage, oracle column, name mix, SHA."""
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
        # The scored subset is drawn from the InChIKey-bearing population; recorded so a reviewer sees
        # the accuracy is over structure-bearing lipids (crosswalk IDs are coverage only).
        "require_gold_structure": config.require_gold_structure,
        "coverage": coverage,
        # Load-bearing composition: which lipid-name field supplied each query (shorthand vs common vs
        # systematic). A reviewer must be able to see the name mix behind a single accuracy number.
        "name_source_breakdown": _name_source_breakdown(input_df),
        "structure_oracle_column": config.gold_inchikey_column,
        # Contamination control recorded on the card: the LM_ID is HELD OUT — never a query, never the
        # oracle — so a reviewer can confirm the score is not a LIPIDMAPS-namespace self-lookup.
        "held_out_id_column": HELD_OUT_LM_ID_COL,
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
class LMSDBundle:
    input_df: pd.DataFrame
    card: dict[str, Any]


def persist_subsample(bundle: LMSDBundle, out_dir: Path | str) -> Path:
    """Write the exact scored subsample beside the card (byte-identical to ``subsample_sha256``)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / subsample_filename(bundle.card["dataset"])
    path.write_bytes(subsample_csv_bytes(bundle.input_df))
    return path


def subsample_from_lines(lines: Iterable[str], config: DatasetConfig) -> tuple[pd.DataFrame, int]:
    """Stream -> filter -> reservoir-subsample -> input_df. Returns (input_df, n_scanned).

    ``n_scanned`` counts eligible records seen (post structure-bearing + usable-name filter), for
    card transparency. Fails loud when ``subsample_n`` is unset — LMSD is ~50k records and must be
    reservoir-subsampled, not loaded in full.
    """
    if config.subsample_n is None:
        raise ValueError(
            f"{config.key}: subsample_n is required (LMSD ships ~50k curated records and must be "
            f"reservoir-subsampled, not loaded in full)."
        )
    counter = {"n": 0}

    def _counting(it: Iterator[dict[str, str]]) -> Iterator[dict[str, str]]:
        for rec in it:
            counter["n"] += 1
            yield rec

    sampled = reservoir_sample(
        _counting(lmsd_records(lines, require_structure=config.require_gold_structure)),
        config.subsample_n,
        config.subsample_seed,
    )
    return build_input_df(sampled, config), counter["n"]


def stream_sdf_lines(url: str, *, timeout: float = 300.0) -> Iterator[str]:
    """Stream the LMSD SDF line-by-line from the ``.sdf.zip`` bulk download (network). Isolated.

    The download is a ZIP containing a single ``.sdf`` member. ZIP needs the central directory, so
    the archive bytes are fetched to memory (the compressed download is ~22MB), then the inner SDF
    member is read line-by-line WITHOUT materializing the ~290MB uncompressed text — only the
    reservoir of ``subsample_n`` rows is ever held. Not unit-tested (network); the parse it feeds is.
    """
    import io
    import zipfile

    import requests

    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    sdf_names = [n for n in zf.namelist() if n.lower().endswith(".sdf")]
    if not sdf_names:
        raise ValueError(f"LMSD download {url!r} contained no .sdf member; members were {zf.namelist()!r}")
    with zf.open(sdf_names[0]) as member:
        for raw in io.TextIOWrapper(member, encoding="utf-8", errors="replace"):
            yield raw.rstrip("\n")


def load_lmsd(
    source: Iterable[str] | str,
    config: DatasetConfig = LMSD,
    *,
    source_version: str | None = None,
) -> LMSDBundle:
    """Load LMSD from a line iterator (tests) or a URL string (the ``.sdf.zip`` download, streamed)."""
    lines: Iterable[str] = stream_sdf_lines(source) if isinstance(source, str) else source
    input_df, n_scanned = subsample_from_lines(lines, config)
    sha = sha256_bytes(subsample_csv_bytes(input_df))
    card = build_card(input_df, n_scanned=n_scanned, source_sha=sha, config=config, source_version=source_version)
    return LMSDBundle(input_df=input_df, card=card)
