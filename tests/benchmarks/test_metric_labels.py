"""One word, "strict", must not mean two numbers.

On Hajjar-100 the KG-record-only figure is 92/100 and the name-fallback figure is 95/100. The
suite historically reported the latter as ``comparable_core``, its headline, while the independence
audit defined the published strict figure as the former. Quoting the headline as "strict" silently
changes the metric's definition between documents, and nothing in the artifact told a reader which
one they were holding.

Decided 2026-09-23: published strict is KG-record-only. These tests pin that the artifact says so
in a way that survives being read by someone who was not in the decision.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from biomapper.benchmarks.scorers.structure_oracle_scorer import (
    CHOSEN_COL,
    score_structure_oracle,
)


class _Oracle:
    """Minimal structure oracle.

    ``kg_records`` are structures the node itself asserts; ``fallback`` are ones obtainable only
    by an external lookup on the node's name. The gap between them is the whole point.
    """

    def __init__(self, kg_records: dict[str, str], fallback: dict[str, str]) -> None:
        self._kg = kg_records
        self._fallback = fallback

    def kg_block(self, node_id: str) -> str | None:
        return self._kg.get(node_id)

    def resolved_block(self, node_id: str) -> str | None:
        return self._kg.get(node_id) or self._fallback.get(node_id)


class _Config:
    key = "unit"
    input_type = "name"
    name_column = "name"
    gold_inchikey_column = "gold_inchikey"
    gold_smiles_column = None
    role = "accuracy"


@pytest.fixture
def scored() -> dict[str, Any]:
    """Four rows: two resolved by the graph, two only by the external name fallback."""
    df = pd.DataFrame(
        {
            "name": ["a", "b", "c", "d"],
            "gold_inchikey": [
                "AAAAAAAAAAAAAA-UHFFFAOYSA-N",
                "BBBBBBBBBBBBBB-UHFFFAOYSA-N",
                "CCCCCCCCCCCCCC-UHFFFAOYSA-N",
                "DDDDDDDDDDDDDD-UHFFFAOYSA-N",
            ],
            CHOSEN_COL: ["KG:a", "KG:b", "KG:c", "KG:d"],
        }
    )
    oracle = _Oracle(
        kg_records={"KG:a": "AAAAAAAAAAAAAA", "KG:b": "BBBBBBBBBBBBBB"},
        fallback={"KG:c": "CCCCCCCCCCCCCC", "KG:d": "DDDDDDDDDDDDDD"},
    )
    return score_structure_oracle(df, _Config(), oracle, vocab="CHEBI")


def test_strict_kg_only_excludes_fallback_rows(scored: dict[str, Any]) -> None:
    """The two rows the graph could not answer are misses under the published definition."""
    strict = scored["comparable_core_strict_kg_only"]
    assert strict["correct"] == 2
    assert strict["scored_denominator"] == 4
    assert strict["top1_accuracy"] == 0.5


def test_name_fallback_variant_includes_them(scored: dict[str, Any]) -> None:
    """And the fallback variant counts all four, which is the 92-against-95 gap in miniature."""
    assert scored["comparable_core"]["correct"] == 4
    assert scored["comparable_core"]["top1_accuracy"] == 1.0


def test_the_two_figures_share_a_denominator(scored: dict[str, Any]) -> None:
    """They must be comparable. A different denominator would make the gap uninterpretable."""
    assert (
        scored["comparable_core_strict_kg_only"]["scored_denominator"]
        == scored["comparable_core"]["scored_denominator"]
    )


def test_exactly_one_figure_claims_to_be_published_strict(scored: dict[str, Any]) -> None:
    """A reader must be able to find THE strict number without prior knowledge."""
    claiming = [
        key
        for key, value in scored.items()
        if isinstance(value, dict) and value.get("is_published_strict")
    ]
    assert claiming == ["comparable_core_strict_kg_only"]


def test_the_fallback_variant_is_explicitly_not_strict(scored: dict[str, Any]) -> None:
    """Silence would be read as 'this is the headline', which is how the confusion started."""
    assert scored["comparable_core"]["is_published_strict"] is False
    assert "never as 'strict'" in scored["comparable_core"]["definition"]


def test_every_reported_variant_carries_a_definition(scored: dict[str, Any]) -> None:
    """A metric name alone did not stop two numbers sharing the word 'strict'."""
    for key in ("comparable_core_strict_kg_only", "comparable_core"):
        assert scored[key]["definition"].strip()


def test_metric_names_are_distinct(scored: dict[str, Any]) -> None:
    """Both were called ``top1_accuracy``, which is what made them look interchangeable."""
    assert scored["comparable_core_strict_kg_only"]["metric"] != scored["comparable_core"]["metric"]
    assert scored["comparable_core_strict_kg_only"]["metric"] == "top1_accuracy_strict_kg_only"


def test_strict_equals_fallback_when_the_graph_answers_everything() -> None:
    """No fallback fired, so the two definitions must agree. Guards against an off-by-one."""
    df = pd.DataFrame(
        {
            "name": ["a", "b"],
            "gold_inchikey": ["AAAAAAAAAAAAAA-UHFFFAOYSA-N", "BBBBBBBBBBBBBB-UHFFFAOYSA-N"],
            CHOSEN_COL: ["KG:a", "KG:b"],
        }
    )
    oracle = _Oracle(kg_records={"KG:a": "AAAAAAAAAAAAAA", "KG:b": "BBBBBBBBBBBBBB"}, fallback={})
    result = score_structure_oracle(df, _Config(), oracle, vocab="CHEBI")
    assert (
        result["comparable_core_strict_kg_only"]["correct"] == result["comparable_core"]["correct"]
    )


def test_a_wrong_fallback_answer_is_a_miss_under_both() -> None:
    """The fallback cannot manufacture a hit: it is judged against the independent gold."""
    df = pd.DataFrame(
        {
            "name": ["a"],
            "gold_inchikey": ["AAAAAAAAAAAAAA-UHFFFAOYSA-N"],
            CHOSEN_COL: ["KG:a"],
        }
    )
    oracle = _Oracle(kg_records={}, fallback={"KG:a": "ZZZZZZZZZZZZZZ"})
    result = score_structure_oracle(df, _Config(), oracle, vocab="CHEBI")
    assert result["comparable_core_strict_kg_only"]["correct"] == 0
    assert result["comparable_core"]["correct"] == 0


# ------------------------------------------------------------------------------------------------
# Greptile round 1 on PR #14: four ways the labelling was still incomplete.
# ------------------------------------------------------------------------------------------------


def test_every_emitted_variant_carries_the_metadata() -> None:
    """The README promises `definition` and `is_published_strict` on each figure. Check them ALL.

    The first pass only added them to the two principal figures, so the charge-normalized and
    per-regime variants silently lacked the fields the report told readers to rely on.
    """
    df = pd.DataFrame(
        {
            "name": ["a", "b"],
            "gold_inchikey": ["AAAAAAAAAAAAAA-UHFFFAOYSA-N", "BBBBBBBBBBBBBB-UHFFFAOYSA-N"],
            "gold_smiles": ["CCO", "CC(=O)O"],
            "query_source": ["abbreviation", "common_name"],
            CHOSEN_COL: ["KG:a", "KG:b"],
        }
    )

    class _CnOracle(_Oracle):
        def neutral_block(self, node_id: str) -> str | None:
            return self._kg.get(node_id)

    class _CnConfig(_Config):
        gold_smiles_column = "gold_smiles"

    result = score_structure_oracle(
        df,
        _CnConfig(),
        _CnOracle(kg_records={"KG:a": "AAAAAAAAAAAAAA", "KG:b": "BBBBBBBBBBBBBB"}, fallback={}),
        vocab="CHEBI",
        gold_smiles_normalizer=lambda s: {"CCO": "AAAAAAAAAAAAAA", "CC(=O)O": "BBBBBBBBBBBBBB"}.get(
            str(s)
        ),
        name_source_column="query_source",
    )

    checked = 0
    for key in (
        "comparable_core_strict_kg_only",
        "comparable_core",
        "comparable_core_charge_normalized",
        "comparable_core_kg_equivalence_set",
    ):
        figure = result.get(key)
        if figure is None:
            continue
        assert figure.get("definition", "").strip(), f"{key} has no definition"
        assert "is_published_strict" in figure, f"{key} does not say whether it is strict"
        checked += 1
    assert checked >= 3

    for regime, block in (result["by_name_source_regime"] or {}).items():
        for key in ("comparable_core_strict_kg_only", "comparable_core"):
            figure = block.get(key)
            assert figure is not None, f"{regime} missing {key}"
            assert figure.get("definition", "").strip(), f"{regime}.{key} has no definition"
            assert "is_published_strict" in figure, f"{regime}.{key} does not say"


def test_per_regime_blocks_expose_the_strict_figure() -> None:
    """A per-regime breakout that only carries the fallback figure repeats the original bug."""
    df = pd.DataFrame(
        {
            "name": ["a", "b"],
            "gold_inchikey": ["AAAAAAAAAAAAAA-UHFFFAOYSA-N", "BBBBBBBBBBBBBB-UHFFFAOYSA-N"],
            "query_source": ["abbreviation", "abbreviation"],
            CHOSEN_COL: ["KG:a", "KG:b"],
        }
    )
    oracle = _Oracle(kg_records={"KG:a": "AAAAAAAAAAAAAA"}, fallback={"KG:b": "BBBBBBBBBBBBBB"})
    result = score_structure_oracle(
        df, _Config(), oracle, vocab="CHEBI", name_source_column="query_source"
    )
    block = (result["by_name_source_regime"] or {})["shorthand"]
    assert block["comparable_core_strict_kg_only"]["correct"] == 1
    assert block["comparable_core"]["correct"] == 2
