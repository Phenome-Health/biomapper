"""Scorer tests. These pin the metrics themselves, which is where drift moves a headline.

The structure-oracle tests in particular encode the strict-vs-equivalence-set distinction and the
coverage-only rule for rows without a gold structure. If any of these change, a benchmark number
moves for a reason that has nothing to do with the engine.
"""

from __future__ import annotations

import pandas as pd
import pytest

from biomapper.benchmarks.config import HAJJAR, HGNC, METABENCH, NECS, NLMGENE
from biomapper.benchmarks.scorers.curie_scorer import (
    namespace_bare_gold,
    normalize_curie,
    predicted_curies,
    score_curie,
    split_gold_curies,
)
from biomapper.benchmarks.scorers.gold_structure import (
    assert_gold_column_present,
    has_gold_structure,
)
from biomapper.benchmarks.scorers.metabench_scorer import MetaBenchNotHeldOutError, score_metabench
from biomapper.benchmarks.scorers.nlmgene_scorer import UnscorableRunError, score_nlmgene_ambiguity
from biomapper.benchmarks.scorers.paper_metric import score_paper_metric
from biomapper.benchmarks.scorers.regression import (
    assert_capability_floor,
    capability_resolvability,
)
from biomapper.benchmarks.scorers.structure_oracle_scorer import (
    first_block,
    name_source_regime,
    neutralize_first_block,
    score_structure_oracle,
)


class _Oracle:
    """A deterministic stand-in: node id -> (keys[0] block, full block set, neutral block)."""

    def __init__(
        self,
        table: dict[str, tuple[str | None, set[str], str | None]],
        *,
        kg_only: set[str] | None = None,
    ):
        self.table = table
        self.kg_only = kg_only if kg_only is not None else set(table)

    def kg_block(self, node_id):  # noqa: ANN001, ANN201
        return self.table.get(node_id, (None, set(), None))[0] if node_id in self.kg_only else None

    def resolved_block(self, node_id):  # noqa: ANN001, ANN201
        return self.table.get(node_id, (None, set(), None))[0]

    def resolved_blocks(self, node_id):  # noqa: ANN001, ANN201
        return self.table.get(node_id, (None, set(), None))[1]

    def neutral_block(self, node_id):  # noqa: ANN001, ANN201
        return self.table.get(node_id, (None, set(), None))[2]


# --------------------------------------------------------------------------------------------------
# structure_oracle_scorer
# --------------------------------------------------------------------------------------------------


def test_first_block_treats_missing_values_as_missing():
    assert first_block("AAAAAAAAAAAAAA-BBBBBBBBBB-N") == "AAAAAAAAAAAAAA"
    for missing in (None, float("nan"), "", "   ", "nan", "NaN"):
        assert first_block(missing) is None


def test_strict_counts_keys_zero_while_equivalence_set_counts_membership():
    """The keys[0] artifact at the scorer level.

    Row 1's gold sits at a non-first position: a strict miss, an equivalence-set hit. Row 2 is a
    genuinely different structure: a miss under both. That is the distinction that makes the
    strict-vs-equivalence-set gap a representation artifact rather than a chemistry failure.
    """
    mapped = pd.DataFrame(
        {
            HAJJAR.name_column: ["shifted", "wrong"],
            HAJJAR.gold_inchikey_column: ["AAAAAAAAAAAAAA-XX-N", "GGGGGGGGGGGGGG-XX-N"],
            "chosen_kg_id": ["CHEBI:1", "CHEBI:2"],
        }
    )
    oracle = _Oracle(
        {
            "CHEBI:1": ("ZZZZZZZZZZZZZZ", {"ZZZZZZZZZZZZZZ", "AAAAAAAAAAAAAA"}, None),
            "CHEBI:2": ("HHHHHHHHHHHHHH", {"HHHHHHHHHHHHHH"}, None),
        }
    )
    result = score_structure_oracle(mapped, HAJJAR, oracle, vocab="CHEBI")
    assert result["comparable_core"]["correct"] == 0
    assert result["comparable_core"]["scored_denominator"] == 2
    assert result["comparable_core_kg_equivalence_set"]["correct"] == 1
    # Same denominator, so the two numbers stay comparable.
    assert result["comparable_core_kg_equivalence_set"]["scored_denominator"] == 2


def test_rows_without_a_gold_structure_are_coverage_only():
    """Excluded from the accuracy denominator, still counted in coverage.

    Otherwise a sparsely-annotated source's unscorable rows would silently become misses.
    """
    mapped = pd.DataFrame(
        {
            HAJJAR.name_column: ["has gold", "no gold"],
            HAJJAR.gold_inchikey_column: ["AAAAAAAAAAAAAA-XX-N", ""],
            "chosen_kg_id": ["CHEBI:1", "CHEBI:2"],
        }
    )
    oracle = _Oracle(
        {
            "CHEBI:1": ("AAAAAAAAAAAAAA", {"AAAAAAAAAAAAAA"}, None),
            "CHEBI:2": ("BBBBBBBBBBBBBB", {"BBBBBBBBBBBBBB"}, None),
        }
    )
    result = score_structure_oracle(mapped, HAJJAR, oracle, vocab="CHEBI")
    assert result["comparable_core"]["scored_denominator"] == 1
    assert result["comparable_core"]["top1_accuracy"] == 1.0
    assert result["coverage"] == {"n_predicted": 2, "total": 2, "fraction": 1.0}


def test_fallback_bucket_segregates_predictions_that_leaned_on_the_name_hop():
    """A correct answer whose structure came from the fallback is flagged, not hidden."""
    mapped = pd.DataFrame(
        {
            HAJJAR.name_column: ["fallback row"],
            HAJJAR.gold_inchikey_column: ["PMMYEEVYMWASQN-XX-N"],
            "chosen_kg_id": ["RM:1"],
        }
    )
    # kg_only is empty, so kg_block returns None while resolved_block still answers: the exact
    # shape of a node the graph asserts no structure for.
    oracle = _Oracle({"RM:1": ("PMMYEEVYMWASQN", {"PMMYEEVYMWASQN"}, None)}, kg_only=set())
    result = score_structure_oracle(mapped, HAJJAR, oracle, vocab="CHEBI")
    assert result["comparable_core"]["correct"] == 1
    assert result["fallback_bucket"] == {"count": 1, "rows": ["RM:1"]}


def test_charge_normalized_shares_the_strict_denominator():
    """A parseable gold SMILES without a gold InChIKey must not inflate the cn denominator."""
    mapped = pd.DataFrame(
        {
            NECS.name_column: ["with gold key", "smiles only"],
            NECS.gold_inchikey_column: ["QTBSBXVTEAMEQO-XX-N", ""],
            NECS.gold_smiles_column: ["CC(=O)[O-]", "CCO"],
            "chosen_kg_id": ["CHEBI:1", "CHEBI:2"],
        }
    )
    oracle = _Oracle(
        {
            "CHEBI:1": ("QTBSBXVTEAMEQO", {"QTBSBXVTEAMEQO"}, "QTBSBXVTEAMEQO"),
            "CHEBI:2": ("XXXXXXXXXXXXXX", {"XXXXXXXXXXXXXX"}, "XXXXXXXXXXXXXX"),
        }
    )
    result = score_structure_oracle(
        mapped, NECS, oracle, vocab="CHEBI", gold_smiles_normalizer=neutralize_first_block
    )
    assert result["comparable_core"]["scored_denominator"] == 1
    assert result["comparable_core_charge_normalized"]["scored_denominator"] == 1


def test_charge_normalized_is_none_without_a_gold_normalizer():
    """Hajjar ships no SMILES, so the variant must report absent rather than be computed."""
    mapped = pd.DataFrame(
        {
            HAJJAR.name_column: ["x"],
            HAJJAR.gold_inchikey_column: ["AAAAAAAAAAAAAA-XX-N"],
            "chosen_kg_id": ["CHEBI:1"],
        }
    )
    oracle = _Oracle({"CHEBI:1": ("AAAAAAAAAAAAAA", {"AAAAAAAAAAAAAA"}, None)})
    result = score_structure_oracle(mapped, HAJJAR, oracle, vocab="CHEBI")
    assert result["comparable_core_charge_normalized"] is None


def test_name_source_regime_splits_shorthand_from_common_systematic():
    assert name_source_regime("abbreviation") == "shorthand"
    assert name_source_regime("ABBREVIATION") == "shorthand"
    for other in ("common_name", "systematic_name", "", None, "unexpected"):
        assert name_source_regime(other) == "common_systematic"


def test_neutralize_collapses_a_carboxylate_onto_its_acid():
    assert neutralize_first_block("CC(=O)[O-]") == neutralize_first_block("CC(=O)O")
    for missing in (None, float("nan"), "", "nan", "not a smiles at all!!!"):
        assert neutralize_first_block(missing) is None


# --------------------------------------------------------------------------------------------------
# paper_metric
# --------------------------------------------------------------------------------------------------


def test_paper_metric_is_a_match_rate_labelled_by_input_type():
    mapped = pd.DataFrame({"chosen_kg_id": ["CHEBI:1", None, "CHEBI:3"]})
    result = score_paper_metric(mapped, HAJJAR, vocab="CHEBI")
    assert result["metric"] == "match_rate"
    assert result["matched"] == 2
    assert result["total"] == 3
    # Labelled so it is never mistaken for the structure-oracle accuracy.
    assert result["input_type"] == "name"


# --------------------------------------------------------------------------------------------------
# curie_scorer
# --------------------------------------------------------------------------------------------------


def test_prefix_synonyms_fold_but_genuinely_different_spaces_do_not():
    """KEGG.COMPOUND == KEGG; KEGG.GLYCAN is a different identifier space and must stay distinct."""
    assert normalize_curie("KEGG.COMPOUND:C00031") == normalize_curie("KEGG:C00031")
    assert normalize_curie("PUBCHEM.COMPOUND:5793") == normalize_curie("PUBCHEM:5793")
    assert normalize_curie("KEGG.GLYCAN:G00001") != normalize_curie("KEGG:G00001")


def test_bare_gold_is_prefixed_to_its_declared_namespace():
    assert split_gold_curies("KDXKERNSBIXSRK-YFKPBYRVSA-N", "INCHIKEY") == {
        "INCHIKEY:KDXKERNSBIXSRK-YFKPBYRVSA-N"
    }
    # An already-prefixed value keeps its own prefix.
    assert split_gold_curies("CHEBI:18019", "INCHIKEY") == {"CHEBI:18019"}


def test_maf_bare_gold_drops_non_chemical_tokens():
    """Feature labels and placeholders must never pad the id-concordance denominator."""
    assert namespace_bare_gold("M123T456") == set()
    assert namespace_bare_gold("--") == set()
    assert namespace_bare_gold("HMDB0000122") == {"HMDB:HMDB0000122"}
    assert namespace_bare_gold("C00031") == {"KEGG:C00031"}


def test_predicted_curies_spans_chosen_node_and_equivalence_set():
    row = pd.Series(
        {
            "chosen_kg_id": "CHEBI:4167",
            "kg_equivalent_ids": '{"HMDB": ["HMDB0000122"], "KEGG": ["C00031"]}',
        }
    )
    predicted = predicted_curies(row)
    assert "CHEBI:4167" in predicted
    assert "HMDB:HMDB0000122" in predicted
    assert "KEGG:C00031" in predicted


def test_gene_accuracy_is_per_namespace_and_the_rollup_is_flagged_non_quotable():
    """The guardrail, asserted.

    One row matches Ensembl only. The roll-up therefore says 100% while Ensembl is 100%, NCBIGene
    0% and UniProtKB 0% — which is exactly why the roll-up must not be quoted as the arm's
    accuracy.
    """
    mapped = pd.DataFrame(
        {
            HGNC.name_column: ["TP53"],
            "gold_ensembl": ["ENSEMBL:ENSG00000141510"],
            "gold_entrez": ["NCBIGene:7157"],
            "gold_uniprot": ["UniProtKB:P04637"],
            "chosen_kg_id": ["ENSEMBL:ENSG00000141510"],
            "kg_equivalent_ids": ["{}"],
        }
    )
    result = score_curie(mapped, HGNC, vocab="ENSEMBL")
    assert result["reportable_metric"] == "per_namespace_accuracy"
    assert result["comparable_core"]["quotable"] is False
    assert result["comparable_core"]["metric"] == "top1_accuracy_any_namespace"
    assert result["comparable_core"]["top1_accuracy"] == 1.0
    per_namespace = result["per_namespace_accuracy"]
    assert per_namespace["ENSEMBL"]["top1_accuracy"] == 1.0
    assert per_namespace["NCBIGene"]["top1_accuracy"] == 0.0
    assert per_namespace["UniProtKB"]["top1_accuracy"] == 0.0


# --------------------------------------------------------------------------------------------------
# nlmgene_scorer
# --------------------------------------------------------------------------------------------------


def test_ambiguous_partition_scores_flag_rate_and_silent_over_commit():
    """Abstaining on an ambiguous form is correct; committing wrongly is the danger."""
    mapped = pd.DataFrame(
        {
            NLMGENE.name_column: ["abstained", "landed on a referent", "silently wrong"],
            "gold_ncbigene": ["NCBIGene:1|NCBIGene:2"] * 3,
            "chosen_kg_id": [None, "NCBIGene:1", "NCBIGene:999"],
            "kg_equivalent_ids": ["{}"] * 3,
        }
    )
    result = score_nlmgene_ambiguity(mapped, NLMGENE, vocab="NCBIGene")
    assert result["comparable_core"]["metric"] == "flag_rate"
    assert result["comparable_core"]["flagged"] == 1
    assert result["silent_over_commit_rate"] == pytest.approx(1 / 3)
    assert result["member_when_committed"] == pytest.approx(0.5)


def test_empty_ambiguous_partition_refuses_a_hollow_flag_rate():
    with pytest.raises(UnscorableRunError):
        score_nlmgene_ambiguity(pd.DataFrame(columns=[NLMGENE.name_column]), NLMGENE)


# --------------------------------------------------------------------------------------------------
# metabench_scorer
# --------------------------------------------------------------------------------------------------


def _metabench_frame(**overrides) -> pd.DataFrame:
    base = {
        METABENCH.name_column: ["glucose"],
        METABENCH.source_id_column: [""],
        METABENCH.source_namespace_column: [""],
        METABENCH.gold_target_column: ["C00031"],
        METABENCH.target_namespace_column: ["KEGG"],
        METABENCH.pair_type_column: ["name2id"],
        "chosen_kg_id": ["CHEBI:4167"],
        "kg_equivalent_ids": ['{"KEGG": ["C00031"]}'],
    }
    base.update(overrides)
    return pd.DataFrame(base)


def test_metabench_scores_bare_gold_against_the_equivalence_set():
    result = score_metabench(_metabench_frame(), METABENCH)
    assert result["comparable_core"]["correct"] == 1
    assert result["per_namespace"]["KEGG"] == {"correct": 1, "scored": 1}


def test_metabench_refuses_a_same_namespace_round_trip():
    """A source namespace equal to the target namespace self-matches the gold."""
    frame = _metabench_frame(
        **{
            METABENCH.source_namespace_column: ["KEGG"],
            METABENCH.pair_type_column: ["id2id"],
            METABENCH.source_id_column: ["C00031"],
        }
    )
    with pytest.raises(MetaBenchNotHeldOutError, match="same-namespace round-trip"):
        score_metabench(frame, METABENCH)


def test_metabench_refuses_a_frame_missing_the_held_out_columns():
    frame = _metabench_frame().drop(columns=[METABENCH.gold_target_column])
    with pytest.raises(MetaBenchNotHeldOutError, match="held-out scoring column"):
        score_metabench(frame, METABENCH)


# --------------------------------------------------------------------------------------------------
# gold_structure + regression gate
# --------------------------------------------------------------------------------------------------


def test_gold_structure_accepts_both_inchikey_vintages_and_rejects_the_corrupt_sentinel():
    assert has_gold_structure("AAAAAAAAAAAAAA-BBBBBBBBBB-N")  # standard three-block
    assert has_gold_structure("AAAAAAAAAAAAAA-BBBBBBBBBB")  # legacy two-block
    assert not has_gold_structure("4000")  # the corrupt placeholder
    assert not has_gold_structure("")
    assert not has_gold_structure("TOOSHORT-BB-N")


def test_absent_gold_column_refuses_a_degenerate_all_missing_verdict():
    """The project's canonical 'guard reports clean via a blind spot' failure."""
    rows = [{"gold_inchikey": "AAAAAAAAAAAAAA-BBBBBBBBBB-N"}]
    assert_gold_column_present(rows, "gold_inchikey")  # present -> fine
    with pytest.raises(ValueError, match="absent or empty on all"):
        assert_gold_column_present(rows, "repaired_inchikey")


def test_capability_floor_fails_closed_when_the_target_regime_is_absent():
    """A blended number must never stand in for an absent shorthand measurement."""
    blended_only = {
        "by_name_source_regime": {"common_systematic": {"coverage": {"total": 10, "fraction": 1.0}}}
    }
    assert capability_resolvability(blended_only, regime="shorthand") is None
    with pytest.raises(ValueError, match="measured nothing in its target class"):
        assert_capability_floor(blended_only, 0.90, regime="shorthand")


def test_capability_floor_fails_when_resolvability_regressed():
    regressed = {
        "by_name_source_regime": {"shorthand": {"coverage": {"total": 100, "fraction": 0.056}}}
    }
    assert capability_resolvability(regressed, regime="shorthand") == pytest.approx(0.056)
    with pytest.raises(ValueError, match="regression floor"):
        assert_capability_floor(regressed, 0.90, regime="shorthand")

    passing = {
        "by_name_source_regime": {"shorthand": {"coverage": {"total": 100, "fraction": 0.97}}}
    }
    assert_capability_floor(passing, 0.90, regime="shorthand")
