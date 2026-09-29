"""Hajjar-100 adapter — the paper's curated 100-metabolite supplement.

Turns the supplement into a mapper-ready ``input_df`` (a name query plus held-out gold ChEBI +
gold InChIKey columns) and a ``dataset_card`` recording N, input_type, coverage, source SHA and
license.

Network is isolated behind ``biomapper.benchmarks.sources`` so the transform is fully
unit-testable on an in-memory fixture. The gold InChIKey column is preserved **verbatim** — it
is the independent structure oracle and must share no infrastructure with the system under test.

Two things changed relative to the engine adapter, both deliberate:

**The source is a .docx, not a CSV.** The engine's ``parse_raw`` auto-detected CSV vs TSV, which
never worked against the real supplement; the gold was passed in by hand instead, which is why
this arm sat in ``SUITE_SKIPPED``. The parse now goes through
:mod:`biomapper.benchmarks.adapters.docx_tables`, deterministically, from the SHA-pinned bytes.
The regenerated gold's own SHA is recorded on the card so the parse is pinned as well as the
source.

**The column mapping is resolved, not assumed.** The supplement's table exposes ``ChEBI Name`` /
``ChEBI Identifier`` / ``InChIKey`` / ``Monoisotopic mass`` / ``Polarity`` / ``Chemical class``.
The engine adapter expected ``Metabolite name`` / ``ChEBI ID`` / ``InChIKey`` / ``SMILES``, so
three of four names were wrong and the fourth — SMILES — **does not exist in this supplement at
all**. The config's ``gold_smiles_column`` is therefore ``None`` and the charge-normalized
variant reports as unavailable with a reason, rather than being computed against an empty column
and reported as a real number.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from typing import Any

import pandas as pd

from biomapper.benchmarks.adapters.docx_tables import table_with_header
from biomapper.benchmarks.config import HAJJAR, DatasetConfig

# Supplement column names -> our canonical held-out column names. Resolved against the pinned
# supplement on 2026-09-23; kept here so a format change is a one-line edit with a visible diff.
RAW_NAME_COL = "ChEBI Name"
RAW_CHEBI_COL = "ChEBI Identifier"
RAW_INCHIKEY_COL = "InChIKey"
RAW_MASS_COL = "Monoisotopic mass"

# The supplement ships NO SMILES column. Recorded as a constant rather than a comment so the
# absence is assertable in a test: if a future supplement gains one, the test fails loudly
# instead of the column quietly reappearing.
SUPPLEMENT_HAS_SMILES = False

REQUIRED_COLUMNS: tuple[str, ...] = (RAW_NAME_COL, RAW_CHEBI_COL, RAW_INCHIKEY_COL)

# Marks rows retained for coverage accounting but excluded from the accuracy denominator.
HAS_STRUCTURE_COL = "has_gold_structure"


class HajjarSmilesColumnError(ValueError):
    """Raised when a config asks for a Hajjar gold SMILES column.

    The supplement has none. Silently resolving it to empty is what produced a
    ``gold_smiles_column="gold_smiles"`` config whose charge-normalized denominator was built
    on a column that was never populated.
    """


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def parse_raw(raw: bytes) -> pd.DataFrame:
    """Parse the supplement's gold table from .docx bytes into a raw DataFrame.

    Selected by header content, not position: the supplement holds three tables and the other
    two are the paper's competitor comparisons. Matching on the header means a reordered
    document fails loudly instead of scoring a competitor table as the gold.
    """
    table = table_with_header(raw, REQUIRED_COLUMNS)
    header, rows = table[0], table[1:]
    frame = pd.DataFrame(rows, columns=header, dtype=str)
    return frame.fillna("")


def canonical_gold_csv(raw_df: pd.DataFrame) -> str:
    """The parsed table as canonical LF-terminated CSV, for SHA-pinning the parse itself.

    Pinning the source bytes proves which document was read; pinning this proves which table was
    extracted from it. Both matter: the original 81/100 run recorded a parse SHA nobody could
    later reproduce, which is exactly the gap this closes.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(list(raw_df.columns))
    writer.writerows(raw_df.astype(str).itertuples(index=False, name=None))
    return buffer.getvalue()


def _norm(value: Any) -> str:  # noqa: ANN401 - a parsed docx cell is genuinely untyped
    return "" if value is None else str(value).strip()


def build_input_df(raw_df: pd.DataFrame, config: DatasetConfig = HAJJAR) -> pd.DataFrame:
    """Build the mapper-ready ``input_df``: the name query plus held-out gold columns.

    The mapper is later called with ``name_column=config.name_column`` and
    ``provided_id_columns=[]``, so the gold columns ride along untouched into the output and are
    consumed only by the scorers, never by BioMapper.
    """
    if config.gold_smiles_column:
        raise HajjarSmilesColumnError(
            f"{config.key}: gold_smiles_column is set to {config.gold_smiles_column!r}, but the "
            f"Hajjar supplement ships no SMILES column. Leave it None so the charge-normalized "
            f"variant reports as unavailable instead of scoring against an empty column."
        )
    missing = [c for c in REQUIRED_COLUMNS if c not in raw_df.columns]
    if missing:
        raise KeyError(
            f"{config.key}: supplement table is missing {missing}; columns: {list(raw_df.columns)}"
        )

    out = pd.DataFrame()
    out[config.name_column] = raw_df[RAW_NAME_COL].map(_norm)
    out[config.gold_chebi_column] = raw_df[RAW_CHEBI_COL].map(_norm)
    out[config.gold_inchikey_column] = raw_df[RAW_INCHIKEY_COL].map(_norm)  # verbatim
    # A row missing a gold InChIKey is retained but marked no-structure: excluded from the
    # accuracy denominator later, still counted in coverage.
    out[HAS_STRUCTURE_COL] = out[config.gold_inchikey_column].map(lambda s: bool(_norm(s)))
    return out


def build_card(
    raw_df: pd.DataFrame,
    source_sha: str,
    config: DatasetConfig = HAJJAR,
    *,
    parse_sha: str | None = None,
    source_provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the dataset card: N, input_type, coverage, pinned SHAs, license."""
    input_df = build_input_df(raw_df, config)
    n = len(input_df)
    n_with_inchikey = int(input_df[HAS_STRUCTURE_COL].sum())
    n_with_chebi = int((input_df[config.gold_chebi_column].map(_norm) != "").sum())
    return {
        "dataset": config.key,
        "arm": config.arm,
        "entity_type": config.entity_type,
        "input_type": config.input_type,
        "target_vocabs": list(config.target_vocabs),
        "n_rows": n,
        "coverage": {
            "gold_inchikey": {
                "n": n_with_inchikey,
                "fraction": (n_with_inchikey / n) if n else 0.0,
            },
            "gold_chebi": {"n": n_with_chebi, "fraction": (n_with_chebi / n) if n else 0.0},
        },
        "source_doi": config.source_doi,
        "source_url": config.source_url,
        "source_sha256": source_sha,
        # The SHA of the extracted table, not just of the document. Pins the parse as well as
        # the source, so a changed parser is detectable without re-reading the docx.
        "parsed_gold_sha256": parse_sha,
        "gold_smiles_available": SUPPLEMENT_HAS_SMILES,
        "charge_normalized_available": False,
        "charge_normalized_unavailable_reason": (
            "the Hajjar supplement ships no SMILES column, so the gold side cannot be "
            "neutralized. Per the 2026-09-23 decision the SMILES path is deprecated; this is "
            "reported as unavailable rather than computed against an absent column."
        ),
        "role": config.role,
        "source_provenance": source_provenance or {},
        "license": config.license,
    }


@dataclass(frozen=True)
class HajjarBundle:
    input_df: pd.DataFrame
    card: dict[str, Any]


def load_hajjar(
    source: bytes | pd.DataFrame,
    config: DatasetConfig = HAJJAR,
    *,
    source_provenance: dict[str, Any] | None = None,
) -> HajjarBundle:
    """Load Hajjar from pinned .docx bytes, or from an already-parsed DataFrame (tests).

    A URL string is deliberately NOT accepted: acquisition belongs to
    :func:`biomapper.benchmarks.sources.acquire`, which asserts a non-empty body and the pinned
    SHA. Letting an adapter fetch its own bytes is how a source integrity check gets bypassed.
    """
    if isinstance(source, pd.DataFrame):
        raw_df = source
        parse_sha = sha256_bytes(canonical_gold_csv(raw_df).encode())
        source_sha = parse_sha  # no document to hash; the parse IS the pin for a fixture
    elif isinstance(source, bytes):
        source_sha = sha256_bytes(source)
        raw_df = parse_raw(source)
        parse_sha = sha256_bytes(canonical_gold_csv(raw_df).encode())
    else:
        raise TypeError(
            f"unsupported source type {type(source)!r}; pass pinned bytes from sources.acquire() "
            f"or a parsed DataFrame"
        )

    return HajjarBundle(
        input_df=build_input_df(raw_df, config),
        card=build_card(
            raw_df,
            source_sha,
            config,
            parse_sha=parse_sha,
            source_provenance=source_provenance,
        ),
    )
