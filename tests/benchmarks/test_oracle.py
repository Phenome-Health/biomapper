"""Oracle tests: the API-derived oracle must reproduce StructureResolver's semantics exactly.

This is the module whose drift would move a headline number for reasons unrelated to the engine,
so the tests pin the distinction between the strict (``keys[0]``) and equivalence-set (union)
metrics rather than just checking that something is returned.
"""

from __future__ import annotations

from biomapper.benchmarks.oracle import ApiStructureOracle


class _FakeNames:
    """Stands in for Kestrel /get-nodes."""

    def __init__(self, mapping: dict[str, str | None]) -> None:
        self.mapping = mapping
        self.primed: list[str] = []

    def prime(self, node_ids):  # noqa: ANN001, ANN201
        self.primed.extend(node_ids)

    def name(self, node_id):  # noqa: ANN001, ANN201
        return self.mapping.get(node_id)

    def stats(self):  # noqa: ANN201
        return {"primed": len(self.primed)}


class _FakeNameStructures:
    """Stands in for the MW -> PubChem name fallback."""

    def __init__(self, mapping: dict[str, str | None]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def block(self, name):  # noqa: ANN001, ANN201
        self.calls.append(name)
        full = self.mapping.get(name)
        return full.split("-")[0] if full else None

    def stats(self):  # noqa: ANN201
        return {"calls": len(self.calls)}


def _rows(node_id: str, inchikeys: list[str], *, smiles: list[str] | None = None, cert=None):
    equiv: dict[str, list[str]] = {}
    if inchikeys:
        equiv["INCHIKEY"] = inchikeys
    if smiles:
        equiv["SMILES"] = smiles
    row = {"chosen_kg_id": node_id, "kg_equivalent_ids": equiv}
    if cert is not None:
        row["certificate"] = cert
    return [row]


def test_kg_block_is_keys_zero_not_the_sorted_set():
    """Strict accuracy is defined on ``keys[0]``, so ordering must be preserved.

    The certificate's ``node_inchikey_blocks`` is sorted, so deriving the strict metric from it
    would silently pick a different representation and move the number.
    """
    rows = _rows("CHEBI:1", ["ZZZZZZZZZZZZZZ-AAAAAAAAAA-N", "AAAAAAAAAAAAAA-BBBBBBBBBB-N"])
    oracle = ApiStructureOracle.from_rows(rows)
    assert oracle.kg_block("CHEBI:1") == "ZZZZZZZZZZZZZZ"
    assert oracle.resolved_blocks("CHEBI:1") == {"ZZZZZZZZZZZZZZ", "AAAAAAAAAAAAAA"}


def test_equivalence_set_recovers_a_non_first_gold_but_strict_does_not():
    """The keys[0] artifact, in one assertion.

    A gold structure sitting at a non-first position is a miss under strict and a hit under the
    equivalence set. That gap is a representation artifact, not a resolution error, which is why
    both numbers are reported and strict alone is never presented as a chemistry failure.
    """
    gold = "AAAAAAAAAAAAAA"
    rows = _rows("CHEBI:1", ["ZZZZZZZZZZZZZZ-AAAAAAAAAA-N", f"{gold}-BBBBBBBBBB-N"])
    oracle = ApiStructureOracle.from_rows(rows)
    assert oracle.kg_block("CHEBI:1") != gold
    assert gold in oracle.resolved_blocks("CHEBI:1")


def test_name_fallback_fires_only_when_the_graph_asserts_no_structure():
    names = _FakeNames({"RM:1": "trans-4-Hydroxyproline"})
    structures = _FakeNameStructures({"trans-4-Hydroxyproline": "PMMYEEVYMWASQN-DMTCNVIQSA-N"})
    rows = _rows("RM:1", [])
    oracle = ApiStructureOracle.from_rows(rows, name_fallback=structures, node_names=names)
    assert oracle.kg_block("RM:1") is None
    assert oracle.resolved_block("RM:1") == "PMMYEEVYMWASQN"
    assert oracle.resolved_blocks("RM:1") == {"PMMYEEVYMWASQN"}
    # Primed in one batch rather than per row.
    assert names.primed == ["RM:1"]

    # And it must NOT fire when the graph does assert a structure.
    with_structure = ApiStructureOracle.from_rows(
        _rows("CHEBI:9", ["QQQQQQQQQQQQQQ-AAAAAAAAAA-N"]),
        name_fallback=structures,
        node_names=names,
    )
    before = len(structures.calls)
    assert with_structure.resolved_block("CHEBI:9") == "QQQQQQQQQQQQQQ"
    assert len(structures.calls) == before


def test_no_fallback_reports_unverifiable_rather_than_guessing():
    oracle = ApiStructureOracle.from_rows(_rows("RM:1", []))
    assert oracle.resolved_block("RM:1") is None
    assert oracle.resolved_blocks("RM:1") == set()


def test_unresolvable_name_stays_none_and_is_reported():
    names = _FakeNames({"RM:2": "ST 27:1;O"})
    structures = _FakeNameStructures({})  # a lipid shorthand MW/PubChem cannot resolve
    oracle = ApiStructureOracle.from_rows(
        _rows("RM:2", []), name_fallback=structures, node_names=names
    )
    assert oracle.resolved_block("RM:2") is None
    report = oracle.fallback_report()
    assert report["nodes_needing_fallback"] == 1
    assert report["nodes_resolved_by_fallback"] == 0
    assert report["unresolved_nodes"] == ["RM:2"]


def test_neutral_block_uses_the_graph_smiles_and_falls_back_to_strict():
    # Acetate anion vs acetic acid share connectivity once neutralized.
    rows = _rows("CHEBI:1", ["QTBSBXVTEAMEQO-UHFFFAOYSA-N"], smiles=["CC(=O)[O-]"])
    oracle = ApiStructureOracle.from_rows(rows)
    assert oracle.neutral_block("CHEBI:1") == "QTBSBXVTEAMEQO"

    # No SMILES to neutralize -> the strict block, because a hash cannot be neutralized.
    no_smiles = ApiStructureOracle.from_rows(_rows("CHEBI:2", ["WWWWWWWWWWWWWW-AAAAAAAAAA-N"]))
    assert no_smiles.neutral_block("CHEBI:2") == "WWWWWWWWWWWWWW"


def test_integrity_check_detects_certificate_disagreement():
    """The premise that StructureResolver is redundant rests on this agreeing. So it is checked."""
    agreeing = _rows(
        "CHEBI:1",
        ["AAAAAAAAAAAAAA-BBBBBBBBBB-N", "ZZZZZZZZZZZZZZ-BBBBBBBBBB-N"],
        cert={"node_inchikey_blocks": ["AAAAAAAAAAAAAA", "ZZZZZZZZZZZZZZ"]},
    )
    report = ApiStructureOracle.from_rows(agreeing).integrity()
    assert report["certificate_agrees_with_kg_equivalent_ids"] == 1
    assert report["disagreements"] == []

    disagreeing = _rows(
        "CHEBI:2",
        ["AAAAAAAAAAAAAA-BBBBBBBBBB-N"],
        cert={"node_inchikey_blocks": ["SOMETHINGELSEX"]},
    )
    report = ApiStructureOracle.from_rows(disagreeing).integrity()
    assert report["certificate_agrees_with_kg_equivalent_ids"] == 0
    assert report["disagreements"][0]["node"] == "CHEBI:2"
