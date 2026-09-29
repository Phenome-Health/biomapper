"""CURIE normalization and identifier-only CURIE sets (offline, no network)."""

from __future__ import annotations

import pytest

from biomapper.harmonize import canonical_prefix, curie_set, normalize_curie

# ---------------------------------------------------------------------------
# normalize_curie
# ---------------------------------------------------------------------------


def test_normalize_uppercases_prefix_and_preserves_local_part():
    # Local parts of gene/protein ids are case-significant; only the prefix is folded.
    assert normalize_curie("Ensembl:ENSG00000141510") == "ENSEMBL:ENSG00000141510"
    assert normalize_curie("  chebi:17234  ") == "CHEBI:17234"


def test_normalize_returns_none_for_blank_and_nan():
    assert normalize_curie(None) is None
    assert normalize_curie("") is None
    assert normalize_curie("   ") is None
    assert normalize_curie("nan") is None
    assert normalize_curie(float("nan")) is None


def test_normalize_bare_value_has_no_colon_and_is_uppercased():
    assert normalize_curie("chebi") == "CHEBI"


def test_kegg_compound_folds_to_kegg_but_kegg_glycan_does_not():
    # Prefix SYNONYMS for one id space fold; genuinely different spaces stay distinct.
    assert canonical_prefix("KEGG.COMPOUND") == "KEGG"
    assert canonical_prefix("PUBCHEM.COMPOUND") == "PUBCHEM"
    assert canonical_prefix("KEGG.GLYCAN") == "KEGG.GLYCAN"
    assert canonical_prefix("KEGG.DRUG") == "KEGG.DRUG"
    assert normalize_curie("KEGG.COMPOUND:C00031") == normalize_curie("KEGG:C00031")
    assert normalize_curie("KEGG.GLYCAN:G00031") != normalize_curie("KEGG:G00031")


# ---------------------------------------------------------------------------
# curie_set
# ---------------------------------------------------------------------------


def test_curie_set_unions_chosen_and_equivalents():
    assert curie_set("CHEBI:17234", {"KEGG": ["C00031"]}) == frozenset(
        {"CHEBI:17234", "KEGG:C00031"}
    )


def test_curie_set_excludes_structure_encoding_namespaces():
    # INCHIKEY/INCHI/SMILES are structure hashes; admitting them would make the linker
    # structural and the downstream certificate circular.
    got = curie_set(
        "CHEBI:17234",
        {
            "KEGG": ["C00031"],
            "INCHIKEY": ["WQZGKKKJIJFFOK-GASJEMHNSA-N"],
            "INCHI": ["InChI=1S/C6H12O6"],
            "SMILES": ["OCC1OC(O)C(O)C(O)C1O"],
        },
    )
    assert got == frozenset({"CHEBI:17234", "KEGG:C00031"})


def test_curie_set_parses_dict_repr_string_from_a_tsv():
    assert curie_set("CHEBI:17234", "{'KEGG': ['C00031'], 'PUBCHEM.COMPOUND': ['5793']}") == (
        frozenset({"CHEBI:17234", "KEGG:C00031", "PUBCHEM:5793"})
    )


def test_curie_set_ignores_unparseable_equivalents_cell():
    assert curie_set("CHEBI:17234", "not a dict at all") == frozenset({"CHEBI:17234"})


def test_curie_set_skips_blank_equivalent_values():
    assert curie_set("CHEBI:17234", {"PUBCHEM.COMPOUND": ["5793", "", "  "]}) == frozenset(
        {"CHEBI:17234", "PUBCHEM:5793"}
    )


def test_curie_set_keeps_an_already_prefixed_equivalent_value_as_is():
    assert curie_set(None, {"KEGG": ["KEGG.COMPOUND:C00031"]}) == frozenset({"KEGG:C00031"})


def test_curie_set_accepts_a_scalar_equivalent_value():
    assert curie_set(None, {"CHEBI": "17234"}) == frozenset({"CHEBI:17234"})


def test_curie_set_is_empty_when_nothing_resolved():
    # An empty set means the entity did not resolve. It is a refusal candidate, not a link.
    assert curie_set(None, None) == frozenset()
    assert curie_set("", "") == frozenset()


@pytest.mark.parametrize("structural", ["INCHIKEY", "INCHI", "SMILES"])
def test_curie_set_is_empty_when_only_structure_is_known(structural):
    assert curie_set(None, {structural: ["ABCDEFGHIJKLMN-OPQRSTUVWX-Y"]}) == frozenset()
