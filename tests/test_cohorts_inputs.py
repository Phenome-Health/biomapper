"""Unit 2: inputs become keyed records; entity aliases resolve to Biolink categories."""

from __future__ import annotations

from pathlib import Path

import pytest

from biomapper.cohorts import (
    ENTITY_ALIASES,
    clean_id,
    normalize_vocabulary,
    read_cohort,
    resolve_category,
)

# -- categories ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("alias", "category"),
    [
        ("metabolites", "biolink:SmallMolecule"),
        ("proteins", "biolink:Protein"),
        ("genes", "biolink:Gene"),
        ("labs", "biolink:ClinicalMeasurement"),
        ("LABS", "biolink:ClinicalMeasurement"),
    ],
)
def test_aliases_resolve_to_biolink_categories(alias: str, category: str) -> None:
    assert resolve_category(alias) == category


def test_raw_biolink_category_passes_through() -> None:
    assert resolve_category("biolink:ClinicalFinding") == "biolink:ClinicalFinding"


def test_category_override_wins_over_the_alias() -> None:
    assert resolve_category("labs", category="biolink:ClinicalFinding") == "biolink:ClinicalFinding"


def test_unknown_alias_raises_listing_the_valid_ones() -> None:
    with pytest.raises(ValueError) as exc:
        resolve_category("lipids")
    for alias in ENTITY_ALIASES:
        assert alias in str(exc.value)


def test_override_must_be_a_biolink_category() -> None:
    with pytest.raises(ValueError, match="biolink:"):
        resolve_category("labs", category="ClinicalFinding")


# -- vocabularies -------------------------------------------------------------


def test_vocabulary_normalization_folds_synonyms_and_case() -> None:
    assert normalize_vocabulary("KEGG.COMPOUND") == normalize_vocabulary("kegg") == "KEGG"
    assert normalize_vocabulary(" loinc ") == "LOINC"


def test_two_declared_synonyms_merge_into_one_vocabulary() -> None:
    rows = [{"name": "Glucose", "kegg_a": "C00031", "kegg_b": " C00031 "}]
    cohort = read_cohort(
        rows,
        label="a",
        name_column="name",
        vocabularies={"KEGG.COMPOUND": ["kegg_a"], "KEGG": "kegg_b"},
    )
    assert cohort.vocabularies == {"KEGG": ("kegg_a", "kegg_b")}
    # Values from both columns, cleaned and de-duplicated in column order.
    assert cohort.records[0].identifiers == {"KEGG": ("C00031",)}


def test_declared_column_missing_from_the_table_raises() -> None:
    with pytest.raises(ValueError, match="Quest LOINC ID"):
        read_cohort(
            [{"name": "Glucose"}],
            label="arivale",
            name_column="name",
            vocabularies={"LOINC": ["Quest LOINC ID"]},
        )


def test_missing_name_column_raises() -> None:
    with pytest.raises(ValueError, match="title"):
        read_cohort([{"name": "Glucose"}], label="a", name_column="title")


# -- keys ---------------------------------------------------------------------


def test_duplicate_names_get_distinct_default_keys() -> None:
    cohort = read_cohort([{"name": "Glucose"}, {"name": "Glucose"}], label="a", name_column="name")
    keys = [r.key for r in cohort.records]
    assert keys == ["0|Glucose", "1|Glucose"]


def test_default_keys_keep_original_row_positions_after_exclusions() -> None:
    cohort = read_cohort(
        [{"name": "Glucose"}, {"name": "  "}, {"name": "Urea"}], label="a", name_column="name"
    )
    assert [r.key for r in cohort.records] == ["0|Glucose", "2|Urea"]
    assert cohort.excluded == ({"row": 1, "reason": "blank name"},)


def test_explicit_key_column_is_respected() -> None:
    cohort = read_cohort(
        [{"id": "30740", "name": "Glucose"}, {"id": 30670.0, "name": "Urea"}],
        label="a",
        name_column="name",
        key_column="id",
    )
    assert [r.key for r in cohort.records] == ["30740", "30670"]


def test_duplicate_explicit_keys_raise_naming_the_column() -> None:
    with pytest.raises(ValueError, match="field_id"):
        read_cohort(
            [{"field_id": "1", "name": "A"}, {"field_id": "1", "name": "B"}],
            label="a",
            name_column="name",
            key_column="field_id",
        )


def test_blank_explicit_key_raises() -> None:
    with pytest.raises(ValueError, match="field_id"):
        read_cohort(
            [{"field_id": "", "name": "A"}], label="a", name_column="name", key_column="field_id"
        )


def test_records_are_deterministic_across_calls() -> None:
    rows = [{"name": "Glucose", "loinc": "2345-7"}, {"name": "Urea", "loinc": None}]
    one = read_cohort(rows, label="a", name_column="name", vocabularies={"LOINC": "loinc"})
    two = read_cohort(rows, label="a", name_column="name", vocabularies={"LOINC": "loinc"})
    assert one.records == two.records


# -- readers ------------------------------------------------------------------


def test_tsv_path_skips_comment_lines(tmp_path: Path) -> None:
    path = tmp_path / "panel.tsv"
    path.write_text(
        "# exported 2026-10-08\n# panel metadata only\nName\tLOINC\nGlucose\t2345-7\n"
        "# trailing note\nUrea\t\n"
    )
    cohort = read_cohort(path, label="a", name_column="Name", vocabularies={"LOINC": "LOINC"})
    assert [r.name for r in cohort.records] == ["Glucose", "Urea"]
    assert cohort.records[0].identifiers == {"LOINC": ("2345-7",)}
    assert cohort.records[1].identifiers == {}
    assert cohort.source["kind"] == "file"
    assert cohort.source["file"] == "panel.tsv"  # basename only, never an absolute path
    assert len(cohort.source["sha256"]) == 64


def test_csv_path_uses_commas(tmp_path: Path) -> None:
    path = tmp_path / "panel.csv"
    path.write_text("name,hmdb\nGlucose,HMDB0000122\n")
    cohort = read_cohort(path, label="a", name_column="name", vocabularies={"HMDB": "hmdb"})
    assert cohort.records[0].identifiers == {"HMDB": ("HMDB0000122",)}


def test_dataframe_is_accepted_by_duck_typing() -> None:
    pd = pytest.importorskip("pandas")
    frame = pd.DataFrame({"name": ["Glucose", None], "loinc": ["2345-7", float("nan")]})
    cohort = read_cohort(frame, label="a", name_column="name", vocabularies={"LOINC": "loinc"})
    assert [r.name for r in cohort.records] == ["Glucose"]
    assert cohort.source["kind"] == "dataframe"
    assert cohort.excluded == ({"row": 1, "reason": "blank name"},)


def test_multi_value_cells_are_split() -> None:
    rows = [{"name": "IL6R", "uniprot": "P29459_P29460", "hmdb": "HMDB1; HMDB2|HMDB1"}]
    cohort = read_cohort(
        rows, label="a", name_column="name", vocabularies={"UniProtKB": "uniprot", "HMDB": "hmdb"}
    )
    assert cohort.records[0].identifiers == {
        "UNIPROTKB": ("P29459", "P29460"),
        "HMDB": ("HMDB1", "HMDB2"),
    }


def test_underscores_split_only_protein_accessions() -> None:
    rows = [{"name": "x", "code": "AB_12"}]
    cohort = read_cohort(rows, label="a", name_column="name", vocabularies={"LOCAL": "code"})
    assert cohort.records[0].identifiers == {"LOCAL": ("AB_12",)}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (float("nan"), None),
        ("nan", None),
        ("  ", None),
        ("30740.0", "30740"),
        (30740.0, "30740"),
        ("2345-7", "2345-7"),
        ("1.5", "1.5"),
    ],
)
def test_clean_id(value: object, expected: str | None) -> None:
    assert clean_id(value) == expected


def test_unsupported_input_type_raises() -> None:
    with pytest.raises(TypeError):
        read_cohort(42, label="a", name_column="name")  # type: ignore[arg-type]
