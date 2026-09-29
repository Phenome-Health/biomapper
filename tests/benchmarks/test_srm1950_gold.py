"""SRM 1950's delivery is partly synthetic, and the adapter has to survive that without lying.

Two separate defects in the pinned delivery
(``~/external_benchmark_runs/_pinned_sources/srm1950_metabolites.csv``, sha256 ``c4662210…``):

1. ``HMDB_ID`` runs ``HMDB0000001..HMDB0001058`` in file order against chemically unrelated names.
   Cholic acid is listed as ``HMDB0000001``; its real accession is ``HMDB0000619``. The column is a
   row counter wearing an accession's format.
2. ``INCHIKEY`` is empty on all 1,058 rows, so the gold structure must be derived from the
   certified ``SMILES``. 51 rows are blank and a further 24 carry an ``#N/A`` Excel sentinel.

The arm previously refused outright on (1), which discarded a scoreable independent accuracy
benchmark over a column that feeds no reported figure. These tests pin the current contract: drop
the synthetic column, derive the gold from SMILES, and account for every excluded row by cause.
"""

from __future__ import annotations

import dataclasses

import pandas as pd
import pytest

from biomapper.benchmarks.adapters.srm1950 import (
    RowIndexGoldColumnError,
    build_card,
    build_input_df,
    gold_structure_exclusions,
    is_missing,
    is_row_index_column,
    parse_csv,
    parsed_gold_sha256,
    screen_row_index_columns,
)
from biomapper.benchmarks.config import SRM1950

# Ethanol, acetic acid, benzene: real SMILES that RDKit parses. The last two rows exercise the two
# missing-data shapes the delivery actually contains.
DELIVERY = (
    b"HMDB_ID,NAME,SMILES,INCHIKEY,CHEMICAL_FORMULA,AVERAGE_MASS,MONO_MASS\n"
    b"HMDB0000001,Ethanol,CCO,,,46.07,46.04\n"
    b"HMDB0000002,Acetic acid,CC(=O)O,,,60.05,60.02\n"
    b"HMDB0000003,Benzene,c1ccccc1,,,78.11,78.05\n"
    b"HMDB0000004,Blank entry,,,,0.0,0.0\n"
    b"HMDB0000005,Sentinel entry,#N/A,,,0.0,0.0\n"
)


@pytest.fixture
def raw_df() -> pd.DataFrame:
    return parse_csv(DELIVERY)


def test_sentinels_survive_parsing(raw_df: pd.DataFrame) -> None:
    """``#N/A`` must reach the classifier as text, not as a pandas NaN.

    Pandas' default ``na_values`` would coerce it before anything could count it, so the row would
    vanish from the denominator with no record of why. Reading literally is what makes the
    exclusion reportable.
    """
    assert raw_df.loc[4, "SMILES"] == "#N/A"


def test_row_index_column_is_detected(raw_df: pd.DataFrame) -> None:
    assert is_row_index_column(raw_df["HMDB_ID"].tolist()) is True


def test_genuine_accessions_are_not_flagged() -> None:
    """The guard must not fire on real accessions, which are unique but not consecutive."""
    assert is_row_index_column(["HMDB0000619", "HMDB0000626", "HMDB0000042"]) is False


def test_synthetic_column_is_dropped_not_refused(raw_df: pd.DataFrame) -> None:
    """The arm runs. The synthetic column is removed and the removal is recorded."""
    report = screen_row_index_columns(raw_df, SRM1950)
    assert report["dropped_columns"] == ["HMDB_ID"]
    assert "row index" in report["reason"]

    built = build_input_df(raw_df, SRM1950)
    assert "HMDB_ID" not in built.columns
    assert not [c for c in built.columns if "hmdb" in c.lower()]


def test_refusal_is_kept_when_the_synthetic_column_is_a_configured_gold() -> None:
    """The fatal path survives for the case that actually warrants it.

    Dropping is right when the column feeds nothing. If it IS a gold column, scoring would run
    against invented values, and the run must stop instead.
    """
    config = dataclasses.replace(SRM1950, gold_inchikey_column="HMDB_ID")
    with pytest.raises(RowIndexGoldColumnError, match="Refusing the run"):
        screen_row_index_columns(parse_csv(DELIVERY), config)


def test_gold_is_derived_from_smiles(raw_df: pd.DataFrame) -> None:
    """The INCHIKEY column is empty, so the oracle comes from the certified SMILES."""
    built = build_input_df(raw_df, SRM1950)
    gold = built[SRM1950.gold_inchikey_column].tolist()
    assert gold[0].startswith("LFQSCWFLJHTTHZ")  # ethanol
    assert gold[1].startswith("QTBSBXVTEAMEQO")  # acetic acid
    assert gold[2].startswith("UHOVQNZJYSORNB")  # benzene
    assert gold[3] == "" and gold[4] == ""


def test_exclusions_separate_blank_from_sentinel(raw_df: pd.DataFrame) -> None:
    """A blank cell and an ``#N/A`` cell are different data-quality facts and are counted apart.

    Collapsing them is how the real delivery's exclusion count reads as 51 when it is 75.
    """
    ex = gold_structure_exclusions(raw_df, SRM1950)
    assert ex["n_excluded_blank_smiles"] == 1
    assert ex["n_excluded_sentinel_smiles"] == 1
    assert ex["n_excluded_unparseable_smiles"] == 0
    assert ex["n_excluded_total"] == 2
    assert ex["n_scorable"] == 3
    assert ex["excluded_sentinel_smiles"] == ["Sentinel entry"]


def test_unparseable_smiles_is_its_own_bucket() -> None:
    """Text that is neither blank nor a sentinel but will not parse is a third, distinct cause."""
    delivery = DELIVERY + b"HMDB0000006,Broken,not-a-smiles-((,,,0.0,0.0\n"
    ex = gold_structure_exclusions(parse_csv(delivery), SRM1950)
    assert ex["n_excluded_unparseable_smiles"] == 1
    assert ex["excluded_unparseable_smiles"] == ["Broken"]


def test_structureless_rows_are_excluded_not_counted_as_misses(raw_df: pd.DataFrame) -> None:
    """The accuracy denominator is the scorable rows, never the row count.

    Counting a row with no gold structure as a miss reports a data gap as a resolver error.
    """
    built = build_input_df(raw_df, SRM1950)
    assert built["has_gold_structure"].sum() == 3
    assert len(built) == 5


def test_parsed_gold_sha_is_stable_and_order_independent(raw_df: pd.DataFrame) -> None:
    """The digest pins what was scored, so it must not move with incidental row order."""
    first = parsed_gold_sha256(build_input_df(raw_df, SRM1950), SRM1950)
    shuffled = raw_df.iloc[::-1].reset_index(drop=True)
    second = parsed_gold_sha256(build_input_df(shuffled, SRM1950), SRM1950)
    assert first == second


def test_parsed_gold_sha_is_order_independent_for_duplicate_names() -> None:
    """Rows sharing a name must not make the digest depend on delivery order.

    Greptile on PR #11: a name-only sort leaves same-name rows in file order, so reordering two
    rows with the same name and different gold moved the SHA even though the gold set was
    identical. Sorting by every hashed column fixes it.
    """
    a = (
        b"HMDB_ID,NAME,SMILES,INCHIKEY,CHEMICAL_FORMULA,AVERAGE_MASS,MONO_MASS\n"
        b"HMDB0000001,Same name,CCO,,,46.07,46.04\n"
        b"HMDB0000002,Same name,CC(=O)O,,,60.05,60.02\n"
    )
    b = (
        b"HMDB_ID,NAME,SMILES,INCHIKEY,CHEMICAL_FORMULA,AVERAGE_MASS,MONO_MASS\n"
        b"HMDB0000001,Same name,CC(=O)O,,,60.05,60.02\n"
        b"HMDB0000002,Same name,CCO,,,46.07,46.04\n"
    )
    assert parsed_gold_sha256(build_input_df(parse_csv(a), SRM1950), SRM1950) == parsed_gold_sha256(
        build_input_df(parse_csv(b), SRM1950), SRM1950
    )


def test_parsed_gold_sha_covers_the_gold_smiles() -> None:
    """A SMILES-only change must move the digest.

    Greptile on PR #11: the charge-normalized variant neutralizes ``gold_smiles``, so when a
    delivery supplies an explicit InChIKey the SMILES can still change a reported score. A digest
    over name and InChIKey alone would not notice.
    """
    base = (
        b"HMDB_ID,NAME,SMILES,INCHIKEY,CHEMICAL_FORMULA,AVERAGE_MASS,MONO_MASS\n"
        b"HMDB0000001,Ethanol,CCO,LFQSCWFLJHTTHZ-UHFFFAOYSA-N,,46.07,46.04\n"
        b"HMDB0000002,Acetic acid,CC(=O)O,QTBSBXVTEAMEQO-UHFFFAOYSA-N,,60.05,60.02\n"
    )
    # SMILES changes; the explicit InChIKey does not.
    moved = base.replace(b"CCO,LFQSCWFLJHTTHZ", b"CCCO,LFQSCWFLJHTTHZ")
    before = parsed_gold_sha256(build_input_df(parse_csv(base), SRM1950), SRM1950)
    after = parsed_gold_sha256(build_input_df(parse_csv(moved), SRM1950), SRM1950)
    assert before != after


def test_sentinel_and_blank_hash_identically() -> None:
    """Two spellings of "no structure" must not move the digest, because they score the same."""
    blank = (
        b"HMDB_ID,NAME,SMILES,INCHIKEY,CHEMICAL_FORMULA,AVERAGE_MASS,MONO_MASS\n"
        b"HMDB0000001,Ethanol,CCO,,,46.07,46.04\n"
        b"HMDB0000002,Missing,,,,0.0,0.0\n"
    )
    sentinel = blank.replace(b"HMDB0000002,Missing,,", b"HMDB0000002,Missing,#N/A,")
    assert parsed_gold_sha256(
        build_input_df(parse_csv(blank), SRM1950), SRM1950
    ) == parsed_gold_sha256(build_input_df(parse_csv(sentinel), SRM1950), SRM1950)


def test_coverage_does_not_count_sentinels_as_present(raw_df: pd.DataFrame) -> None:
    """The card cannot report a row as covered and excluded at the same time.

    Greptile on PR #11: reading the delivery literally leaves ``#N/A`` in ``gold_smiles``, and a
    plain non-empty test counted it as coverage. On the real delivery that reported 1,007 SMILES
    present while the exclusion block called 24 of them missing.
    """
    card = build_card(raw_df, "deadbeef", SRM1950)
    ex = card["gold_structure_exclusions"]
    # Three real SMILES (ethanol, acetic acid, benzene); one blank and one '#N/A' are not coverage.
    assert card["coverage"]["SMILES"]["n"] == 3
    # The card must be internally consistent: what it calls covered plus what it calls excluded
    # accounts for every row exactly once.
    assert card["coverage"]["SMILES"]["n"] + ex["n_excluded_total"] == card["n_rows"]


def test_parsed_gold_sha_moves_when_the_gold_changes(raw_df: pd.DataFrame) -> None:
    """It must actually be sensitive to the thing it claims to pin."""
    before = parsed_gold_sha256(build_input_df(raw_df, SRM1950), SRM1950)
    changed = parse_csv(DELIVERY.replace(b"CCO,", b"CCCO,"))  # ethanol -> propanol
    after = parsed_gold_sha256(build_input_df(changed, SRM1950), SRM1950)
    assert before != after


def test_card_records_the_drop_the_exclusions_and_the_parsed_sha(raw_df: pd.DataFrame) -> None:
    """A reader of the card can see every decision without reading the adapter."""
    card = build_card(raw_df, "deadbeef", SRM1950)
    assert card["screened_columns"]["dropped_columns"] == ["HMDB_ID"]
    assert card["gold_structure_exclusions"]["n_scorable"] == 3
    assert card["parsed_gold_sha256"]
    assert card["parsed_gold_sha256"] != card["source_sha256"]
    assert card["structure_oracle_source"] == "derived_from_certified_smiles"


def test_is_missing_covers_blank_and_sentinels() -> None:
    assert is_missing("") and is_missing("   ") and is_missing(None)
    assert is_missing("#N/A") and is_missing("n/a") and is_missing("NULL")
    assert not is_missing("CCO")
