"""NIST SRM 1950 / SRM1950-DB adapter (metabolite arm — structure oracle).

SRM1950-DB (Mandal et al. 2025, Anal. Chem., DOI 10.1021/acs.analchem.4c05018) is the certified
NIST SRM 1950 human-plasma reference set — 1,058 metabolites at srm1950-data.wishartlab.com. The
CSV delivery (metabolites.csv) ships ``HMDB_ID``, ``NAME``, ``SMILES`` and ``INCHIKEY`` columns,
but at acquisition the **INCHIKEY column is empty** while SMILES is ~95% populated.

Design mirrors ``necs_metabolon`` (small enough to load in full) with ONE acquisition-driven
difference: the independent structure-oracle InChIKey is **derived from the certified SMILES** via
RDKit (deterministic, standard cheminformatics; zero shared infra with BioMapper's resolver) when
the delivery's INCHIKEY column is empty. An explicit INCHIKEY (should a future delivery populate
it) is preferred verbatim. Rows whose SMILES fails to parse (or is absent) yield no gold structure
and are retained as coverage-only — excluded from the accuracy denominator by the scorer.

Network is isolated behind ``fetch_supplement`` so the transform is fully unit-testable offline.
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from typing import Any

import pandas as pd

from biomapper.benchmarks.config import SRM1950, DatasetConfig

HAS_STRUCTURE_COL = "has_gold_structure"

_ACCESSION_DIGITS = re.compile(r"(\d+)")


class RowIndexGoldColumnError(ValueError):
    """A CONFIGURED GOLD column whose values are really the row number.

    Raised only when the synthetic column would actually feed a reported figure, because then any
    score computed from the delivery is a score against invented values. A synthetic column that
    feeds nothing is dropped instead: see :func:`screen_row_index_columns`. Refusing an arm over an
    unused column discards a working benchmark, which is a worse outcome than the one this guard
    exists to prevent.
    """


# Canonical held-out column -> candidate raw headers (case-insensitive, exact after strip).
QUERY_CANDIDATES: tuple[str, ...] = ("NAME", "Name", "metabolite_name", "Metabolite")
SMILES_CANDIDATES: tuple[str, ...] = ("SMILES", "Smiles", "Canonical SMILES")
INCHIKEY_CANDIDATES: tuple[str, ...] = ("INCHIKEY", "InChIKey", "INCHI_KEY", "InChI Key")
HMDB_CANDIDATES: tuple[str, ...] = ("HMDB_ID", "HMDB", "HMDBID", "HMDB ID")


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def fetch_supplement(url: str, *, timeout: float = 60.0) -> bytes:
    """Fetch the SRM1950-DB metabolites.csv bytes (network). Isolated so tests never hit it."""
    import requests

    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content


#: Literal strings the delivery uses to mean "no value". ``#N/A`` is an Excel error escaping into
#: the export, and it appears in the SMILES column on 24 rows. Pandas would silently coerce it to
#: NaN under its default ``na_values``, which is the right outcome but an invisible one: the row
#: count would drop with no record of why. These are recognised explicitly so a missing-data
#: sentinel is reported as a sentinel and not as chemistry that failed to parse. Same class of
#: defect as the NECS ``4000`` gold sentinel.
MISSING_SENTINELS: frozenset[str] = frozenset(
    {"#N/A", "#NA", "N/A", "NA", "NULL", "NONE", "-", "."}
)


def parse_csv(raw: bytes) -> pd.DataFrame:
    """Parse the delivery bytes into a raw DataFrame (all cells as literal strings).

    ``keep_default_na=False`` on purpose: pandas' default NA coercion would turn the delivery's
    ``#N/A`` sentinels into NaN before anything could count them, so a reader of the dataset card
    would see a smaller denominator with no explanation. Read everything literally, then classify
    explicitly in :func:`gold_structure_exclusions`.
    """
    text = raw.decode("utf-8-sig")
    return pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False).fillna("")


def is_missing(value: Any) -> bool:
    """True when a cell is blank or one of the delivery's missing-data sentinels."""
    text = _norm(value)
    return not text or text.upper() in MISSING_SENTINELS


def _norm(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _resolve_column(raw_df: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    lookup = {str(c).strip().lower(): c for c in raw_df.columns}
    for cand in candidates:
        hit = lookup.get(cand.strip().lower())
        if hit is not None:
            return hit
    return None


def inchikey_from_smiles(smiles: Any) -> str:
    """Standard InChIKey from a SMILES via RDKit, or "" when absent/unparseable.

    The certified structure is the dataset's own SMILES; RDKit's conversion is deterministic and
    shares no infrastructure with BioMapper's resolver, preserving oracle independence.
    """
    s = _norm(smiles)
    if not s:
        return ""
    from rdkit import Chem, RDLogger

    RDLogger.DisableLog("rdApp.*")  # type: ignore[attr-defined]
    mol = Chem.MolFromSmiles(s)
    if mol is None:
        return ""
    return Chem.MolToInchiKey(mol)


def is_row_index_column(values: Any) -> bool:
    """True when a column of accession-shaped strings is really the row number in disguise.

    The rule is deliberately narrow, because the cost of a false positive is a refused run. All
    four must hold over the FULL column: every value populated, numeric parts unique, monotonically
    increasing, and exactly the consecutive sequence starting at one. A genuine accession set is
    unique but not consecutive; a sorted genuine set has gaps; a filtered subset of a corrupt
    column also has gaps, which is why this must be evaluated on the raw delivery and never on a
    downstream slice.

    A single row satisfies "the consecutive sequence starting at one" trivially and is not
    evidence, so the guard needs more than one row before it will fire.
    """
    parsed: list[int] = []
    for value in list(values):
        text = _norm(value)
        if not text:
            return False
        match = _ACCESSION_DIGITS.search(text)
        if match is None:
            return False
        parsed.append(int(match.group(1)))
    if len(parsed) < 2:
        return False
    return parsed == list(range(1, len(parsed) + 1))


def screen_row_index_columns(
    raw_df: pd.DataFrame, config: DatasetConfig = SRM1950
) -> dict[str, Any]:
    """Drop a synthetic accession column, or refuse the run if it would feed a reported figure.

    The delivery's ``HMDB_ID`` runs ``HMDB0000001..HMDB0001058``, one value per row in file order,
    against chemically unrelated names: cholic acid is listed as ``HMDB0000001`` when its real
    accession is ``HMDB0000619``. It has an accession's exact format, so it reads as gold to
    anything that greps for one.

    Dropping is the right response here, not refusing. This column feeds nothing that is reported:
    SRM1950's ``gold_coverage_columns`` are INCHIKEY and SMILES, and the structure oracle is the
    InChIKey derived from the certified SMILES, which never read it. Refusing the whole arm over an
    unused column threw away a scoreable independent accuracy benchmark, which is a worse outcome
    than the one the guard existed to prevent.

    The refusal is kept for the case that actually warrants it: a row-index column that IS a
    configured gold column, where scoring would silently run against synthetic values. That is the
    failure the guard was written for, and it stays fatal.

    Returns a report for the dataset card, so a reader can see the column was found and dropped
    rather than silently absent.
    """
    report: dict[str, Any] = {"dropped_columns": [], "reason": None}
    hmdb_raw = _resolve_column(raw_df, HMDB_CANDIDATES)
    if hmdb_raw is None:
        return report
    if not is_row_index_column(raw_df[hmdb_raw].tolist()):
        return report

    reason = (
        f"{hmdb_raw!r} is a row index wearing an accession's format: its numeric parts are "
        f"unique, monotonic, and exactly the consecutive sequence starting at one over all rows. "
        f"Dropped at acquisition; it is never emitted as a query, a gold value, or a coverage "
        f"column, so no reported figure is computed from it."
    )

    gold_columns = {column for _namespace, column in config.gold_coverage_columns}
    gold_columns.update({config.gold_inchikey_column, config.gold_smiles_column or ""})
    if hmdb_raw in gold_columns:
        raise RowIndexGoldColumnError(
            f"{reason} It is ALSO a configured gold column for {config.key}, so a score computed "
            f"from this delivery would be a score against synthetic values. Refusing the run."
        )

    report["dropped_columns"] = [hmdb_raw]
    report["reason"] = reason
    return report


def gold_structure_exclusions(
    raw_df: pd.DataFrame, config: DatasetConfig = SRM1950
) -> dict[str, Any]:
    """Account for every row that yields no gold structure, with the reason it does not.

    Two distinct causes, kept apart because they mean different things: a row that ships no SMILES
    at all (the delivery is incomplete for it) and a row whose SMILES is present but will not parse
    (the delivery is wrong for it). Collapsing them into one "missing" count hides a data-quality
    signal, and reporting neither would let the accuracy denominator shrink silently.
    """
    smiles_raw = _resolve_column(raw_df, SMILES_CANDIDATES)
    inchikey_raw = _resolve_column(raw_df, INCHIKEY_CANDIDATES)
    n = len(raw_df)
    smiles = raw_df[smiles_raw].map(_norm) if smiles_raw is not None else pd.Series([""] * n)
    explicit = raw_df[inchikey_raw].map(_norm) if inchikey_raw is not None else pd.Series([""] * n)
    names = raw_df[_resolve_column(raw_df, QUERY_CANDIDATES) or raw_df.columns[0]].map(_norm)

    blank, sentinel, unparseable = [], [], []
    for name, ik, sm in zip(names.values, explicit.values, smiles.values):
        if not is_missing(ik):
            continue
        text = _norm(sm)
        if not text:
            blank.append(name)
        elif text.upper() in MISSING_SENTINELS:
            sentinel.append(name)
        elif not inchikey_from_smiles(text):
            unparseable.append(name)
    excluded = len(blank) + len(sentinel) + len(unparseable)
    return {
        "n_rows": n,
        # Split deliberately. A naive "empty string" check sees only the blanks and reports 51
        # exclusions; the true figure is 75, because 24 further rows carry an ``#N/A`` sentinel
        # that is text, not a structure. Reporting one number would understate the gap by a third.
        "n_excluded_blank_smiles": len(blank),
        "n_excluded_sentinel_smiles": len(sentinel),
        "n_excluded_unparseable_smiles": len(unparseable),
        "n_excluded_total": excluded,
        "n_scorable": n - excluded,
        "excluded_blank_smiles": blank,
        "excluded_sentinel_smiles": sentinel,
        "excluded_unparseable_smiles": unparseable,
        "reason": (
            "the delivery's INCHIKEY column is empty on every row, so the gold structure is "
            "derived from the certified SMILES with RDKit. A row whose SMILES is blank, is a "
            "missing-data sentinel such as '#N/A', or does not parse, yields no gold structure "
            "and is excluded from the accuracy denominator rather than counted as a miss. "
            "Counting a structureless row as a miss would report a data gap as a resolver error."
        ),
    }


def build_input_df(raw_df: pd.DataFrame, config: DatasetConfig = SRM1950) -> pd.DataFrame:
    """Build the mapper-ready input_df: name query + held-out gold columns + structure flag.

    Gold InChIKey = the delivery's INCHIKEY when present, else derived from the certified SMILES.
    The gold columns ride along untouched into the mapper output (``provided_id_columns=[]``) and
    are consumed only by the scorer.
    """
    query_raw = _resolve_column(raw_df, QUERY_CANDIDATES)
    if query_raw is None:
        raise KeyError(
            f"SRM1950 delivery is missing a recognizable NAME column; tried {QUERY_CANDIDATES!r} "
            f"against {list(raw_df.columns)!r}"
        )
    smiles_raw = _resolve_column(raw_df, SMILES_CANDIDATES)
    inchikey_raw = _resolve_column(raw_df, INCHIKEY_CANDIDATES)
    screen_row_index_columns(raw_df, config)

    out = pd.DataFrame()
    out[config.name_column] = raw_df[query_raw].map(_norm)
    smiles = (
        raw_df[smiles_raw].map(_norm) if smiles_raw is not None else pd.Series([""] * len(raw_df))
    )
    explicit_ik = (
        raw_df[inchikey_raw].map(_norm)
        if inchikey_raw is not None
        else pd.Series([""] * len(raw_df))
    )
    assert config.gold_smiles_column is not None  # SRM1950 config carries a gold SMILES column
    out[config.gold_smiles_column] = smiles.values
    # Prefer an explicit delivery InChIKey; otherwise derive from the certified SMILES.
    out[config.gold_inchikey_column] = [
        ""
        if is_missing(ik) and is_missing(sm)
        else (ik if not is_missing(ik) else inchikey_from_smiles(sm))
        for ik, sm in zip(explicit_ik.values, smiles.values)
    ]
    # The delivery's identifier column is NOT emitted. See ``RowIndexGoldColumnError``: it was a row
    # index in accession clothing, and a quarantined-but-present gold column is a trap for the next
    # person who greps for a gold identifier. The structure oracle is the certified SMILES-derived
    # InChIKey, which never read this column, so accuracy is unaffected by the drop.
    out[HAS_STRUCTURE_COL] = out[config.gold_inchikey_column].map(lambda s: bool(_norm(s)))
    return out


def parsed_gold_sha256(input_df: pd.DataFrame, config: DatasetConfig = SRM1950) -> str:
    """SHA of the parsed gold values, so the scored subset is reproducible.

    Hashes every gold input that scoring reads: the name, the derived gold InChIKey, and the gold
    SMILES. The SMILES is not redundant. When a delivery supplies an explicit InChIKey the SMILES
    no longer determines it, but the charge-normalized variant still neutralizes the SMILES, so a
    SMILES-only change would move a reported score while leaving a name-plus-InChIKey digest
    untouched.

    Sorted by ALL hashed columns, not by name alone: two retained rows can share a name and differ
    in gold, and a name-only sort leaves those ties in delivery order, which makes the digest move
    when rows are merely reordered.

    Separate from the delivery SHA: the delivery pins what arrived, this pins what was scored, and
    an RDKit or adapter change moves this one while leaving the delivery digest untouched.
    """
    columns = [config.name_column, config.gold_inchikey_column]
    if config.gold_smiles_column:
        columns.append(config.gold_smiles_column)
    frame = input_df[columns].copy()
    # Normalize missing markers to blank before hashing, so a delivery that switches between an
    # empty cell and an '#N/A' sentinel for the same absent structure does not move the digest.
    # The digest describes the gold that was scored, and both spellings score identically.
    for column in columns[1:]:
        frame[column] = frame[column].map(lambda v: "" if is_missing(v) else _norm(v))
    frame = frame.sort_values(columns, kind="stable")
    return hashlib.sha256(frame.to_csv(index=False).encode()).hexdigest()


def build_card(
    raw_df: pd.DataFrame,
    source_sha: str,
    config: DatasetConfig = SRM1950,
) -> dict[str, Any]:
    """Build the dataset_card: N, input_type, per-column coverage, oracle provenance, SHA, license."""
    input_df = build_input_df(raw_df, config)
    n = len(input_df)
    coverage: dict[str, dict[str, Any]] = {}
    for namespace, column in config.gold_coverage_columns:
        # A missing-data sentinel is NOT coverage. Reading the delivery literally keeps '#N/A' in
        # the SMILES column, so a plain non-empty test would report those 24 rows as covered while
        # the exclusion report next to it calls them missing. One card cannot say both.
        values = input_df.get(column, pd.Series([""] * n))
        present = int(sum(0 if is_missing(v) else 1 for v in values))
        coverage[namespace] = {"n": present, "fraction": (present / n) if n else 0.0}
    return {
        "dataset": config.key,
        "arm": config.arm,
        "entity_type": config.entity_type,
        "input_type": config.input_type,
        "target_vocabs": list(config.target_vocabs),
        "n_rows": n,
        "coverage": coverage,
        "structure_oracle_column": config.gold_inchikey_column,
        # Load-bearing provenance: the delivery's InChIKey column is empty, so the oracle InChIKey
        # is derived from the certified SMILES (recorded so a reviewer knows the oracle's origin).
        "structure_oracle_source": "derived_from_certified_smiles",
        # The SHA of the PARSED gold, not of the delivery. The delivery's SHA pins the bytes that
        # arrived; this pins the gold structures actually scored against, which is what another
        # run has to reproduce. They are different artifacts and a reader needs both.
        "parsed_gold_sha256": parsed_gold_sha256(input_df, config),
        # Which delivery columns were dropped as synthetic, and why. Recorded rather than silently
        # omitted so the absence of an identifier column is visibly a decision, not an oversight.
        "screened_columns": screen_row_index_columns(raw_df, config),
        # Rows carrying no derivable gold structure, split by cause. The accuracy denominator is
        # n_scorable, not n_rows; stating only the former would make the exclusions invisible.
        "gold_structure_exclusions": gold_structure_exclusions(raw_df, config),
        "source_doi": config.source_doi,
        "source_url": config.source_url,
        "source_sha256": source_sha,
        "license": config.license,
    }


@dataclass(frozen=True)
class SRM1950Bundle:
    input_df: pd.DataFrame
    card: dict[str, Any]


def load_srm1950(
    source: bytes | str | pd.DataFrame, config: DatasetConfig = SRM1950
) -> SRM1950Bundle:
    """Load SRM1950 from raw CSV bytes (SHA pinned), a URL (fetched), or a DataFrame (tests).

    When ``source`` is a DataFrame the card's ``source_sha256`` is computed over its canonical CSV
    bytes so the pin is deterministic for tests.
    """
    if isinstance(source, pd.DataFrame):
        raw_df = source
        raw_bytes = raw_df.to_csv(index=False).encode("utf-8")
    elif isinstance(source, bytes):
        raw_bytes = source
        raw_df = parse_csv(raw_bytes)
    elif isinstance(source, str):
        raw_bytes = fetch_supplement(source)
        raw_df = parse_csv(raw_bytes)
    else:
        raise TypeError(f"unsupported source type {type(source)!r}")

    sha = sha256_bytes(raw_bytes)
    return SRM1950Bundle(
        input_df=build_input_df(raw_df, config), card=build_card(raw_df, sha, config)
    )
