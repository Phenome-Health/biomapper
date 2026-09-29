"""Tests for the two multi-pass scorers, plus the name->structure fallback.

Both of these fold several mapper passes into one row before scoring. The folding is where a
double-count or a lost row would silently move the denominator, so that is what these pin.
"""

from __future__ import annotations

import json

import httpx
import pandas as pd
import pytest

from biomapper.benchmarks.config import METABOLITEANNOTATOR_POS, METLINKR
from biomapper.benchmarks.scorers.metlinkr_scorer import (
    CuratorLeakError,
    assert_curator_held_out,
    score_metlinkr,
)
from biomapper.benchmarks.scorers.metlinkr_scorer import (
    UnscorableRunError as MetLinkRUnscorableError,
)
from biomapper.benchmarks.scorers.metlinkr_scorer import merge_vocab_runs as merge_metlinkr
from biomapper.benchmarks.scorers.name_hit_scorer import (
    SOURCE_ACCESSION_COL,
    UnscorableRunError,
    merge_vocab_runs,
    resolves_to_target_vocab,
    score_name_hit,
)
from biomapper.benchmarks.structure import NameStructureResolver, inchikey_from_smiles

CONFIG = METABOLITEANNOTATOR_POS


class _NeutralOracle:
    def __init__(self, table: dict[str, str | None]) -> None:
        self.table = table

    def neutral_block(self, node_id):  # noqa: ANN001, ANN201
        return self.table.get(node_id)

    def resolved_block(self, node_id):  # noqa: ANN001, ANN201
        return self.table.get(node_id)


# --------------------------------------------------------------------------------------------------
# name_hit_scorer
# --------------------------------------------------------------------------------------------------


def _pass(name: str, chosen: str | None, equiv: dict, *, accession="MTBLS1", gold="CHEBI:17234"):
    return pd.DataFrame(
        {
            CONFIG.name_column: [name],
            "chosen_kg_id": [chosen],
            "kg_equivalent_ids": [json.dumps(equiv)],
            CONFIG.gold_id_column: [gold],
            CONFIG.gold_smiles_column: [""],
            SOURCE_ACCESSION_COL: [accession],
        }
    )


def test_a_hit_in_any_vocab_pass_counts_exactly_once():
    """The union rule.

    Scoring one pass would under-count; double-counting inflates the numerator.
    """
    chebi_pass = _pass("glucose", None, {})
    hmdb_pass = _pass("glucose", "HMDB:HMDB0000122", {"HMDB": ["HMDB0000122"]})
    merged = merge_vocab_runs([chebi_pass, hmdb_pass], CONFIG)
    assert len(merged) == 1  # one input row, not two
    result = score_name_hit(merged, CONFIG, vocab="CHEBI")
    assert result["comparable_core"]["matched"] == 1
    assert result["comparable_core"]["total"] == 1


def test_same_name_in_two_accessions_stays_two_input_rows():
    """Otherwise the per-input denominator silently shrinks and the rate inflates."""
    merged = merge_vocab_runs(
        [
            _pass("glucose", "CHEBI:4167", {}, accession="MTBLS1"),
            _pass("glucose", "CHEBI:4167", {}, accession="MTBLS2"),
        ],
        CONFIG,
    )
    assert len(merged) == 2


def test_hit_is_read_from_the_prediction_never_the_gold():
    row = pd.Series({"chosen_kg_id": "HMDB:HMDB0000122", "kg_equivalent_ids": "{}"})
    assert resolves_to_target_vocab(row, CONFIG.target_vocabs)
    # A node outside every target vocab is not a hit, even though it resolved.
    off_target = pd.Series({"chosen_kg_id": "UMLS:C0017725", "kg_equivalent_ids": "{}"})
    assert not resolves_to_target_vocab(off_target, CONFIG.target_vocabs)


def test_non_chemical_gold_tokens_are_excluded_not_counted_as_discordant():
    """A spectral feature label can never concord, so it must not pad the denominator."""
    merged = merge_vocab_runs(
        [_pass("feature", "CHEBI:4167", {"CHEBI": ["4167"]}, gold="M123T456")], CONFIG
    )
    result = score_name_hit(merged, CONFIG)
    assert result["id_concordance"]["scored"] == 0
    assert result["id_concordance"]["excluded_nonchemical"] == 1


def test_zero_names_refuses_a_hollow_rate():
    with pytest.raises(UnscorableRunError, match="measured nothing"):
        score_name_hit(pd.DataFrame(columns=[CONFIG.name_column]), CONFIG)


def test_merge_requires_at_least_one_pass():
    with pytest.raises(ValueError, match="no vocab runs to merge"):
        merge_vocab_runs([], CONFIG)


def test_charge_normalized_qualifier_only_when_an_oracle_supplies_it():
    merged = merge_vocab_runs([_pass("glucose", "CHEBI:4167", {"CHEBI": ["4167"]})], CONFIG)
    without = score_name_hit(merged, CONFIG)
    with_oracle = score_name_hit(
        merged, CONFIG, oracle=_NeutralOracle({"CHEBI:4167": "WQZGKKKJIJFFOK"})
    )
    assert without["structure_concordance_charge_normalized"] is None
    assert with_oracle["structure_concordance_charge_normalized"] is not None


# --------------------------------------------------------------------------------------------------
# metlinkr_scorer
# --------------------------------------------------------------------------------------------------


def _metlinkr_frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            METLINKR.name_column: [r["name"] for r in rows],
            METLINKR.group_label_column: [r["group"] for r in rows],
            METLINKR.gold_hmdb_column: [r.get("hmdb", "") for r in rows],
            METLINKR.gold_pubchem_column: [r.get("pubchem", "") for r in rows],
            METLINKR.source_file_column: [r["source"] for r in rows],
            "chosen_kg_id": [r.get("chosen") for r in rows],
            "kg_equivalent_ids": [json.dumps(r.get("equiv", {})) for r in rows],
        }
    )


def test_curator_agreement_counts_a_cross_dataset_pair_as_linked_when_sets_intersect():
    """Two rows the curators call the same compound, in two datasets.

    Sharing a canonical id links them.
    """
    frame = _metlinkr_frame(
        [
            {
                "name": "glucose",
                "group": "G1",
                "source": "cohortA",
                "chosen": "CHEBI:4167",
                "equiv": {"HMDB": ["HMDB0000122"]},
            },
            {
                "name": "D-glucose",
                "group": "G1",
                "source": "cohortB",
                "chosen": "CHEBI:4167",
                "equiv": {"HMDB": ["HMDB0000122"]},
            },
        ]
    )
    result = score_metlinkr(frame, METLINKR)
    assert result["curator_agreement"]["linked"] == 1
    assert result["curator_agreement"]["curator_cross_pairs"] == 1
    assert result["curator_agreement"]["curator_agreement_rate"] == 1.0


def test_a_pair_sharing_no_canonical_id_is_not_linked():
    frame = _metlinkr_frame(
        [
            {"name": "glucose", "group": "G1", "source": "cohortA", "chosen": "CHEBI:4167"},
            {"name": "D-glucose", "group": "G1", "source": "cohortB", "chosen": "CHEBI:99999"},
        ]
    )
    result = score_metlinkr(frame, METLINKR)
    assert result["curator_agreement"]["linked"] == 0


def test_same_dataset_rows_are_not_a_cross_dataset_pair():
    """The benchmark is cross-dataset linking; a within-dataset pair is a different claim.

    A frame holding only same-dataset rows therefore yields no pairs, and with nothing
    structurally resolvable either the scorer refuses rather than reporting a hollow 0/0 rate.
    That refusal IS the assertion: a within-dataset pair must not be quietly counted to make the
    denominator non-empty.
    """
    frame = _metlinkr_frame(
        [
            {"name": "glucose", "group": "G1", "source": "cohortA", "chosen": "CHEBI:4167"},
            {"name": "D-glucose", "group": "G1", "source": "cohortA", "chosen": "CHEBI:4167"},
        ]
    )
    with pytest.raises(MetLinkRUnscorableError, match="no curator cross-dataset pairs"):
        score_metlinkr(frame, METLINKR)


def test_cross_dataset_pairing_ignores_rows_the_curators_did_not_group_together():
    """Two datasets but two different curator groups is still zero pairs."""
    frame = _metlinkr_frame(
        [
            {"name": "glucose", "group": "G1", "source": "cohortA", "chosen": "CHEBI:4167"},
            {"name": "alanine", "group": "G2", "source": "cohortB", "chosen": "CHEBI:16977"},
        ]
    )
    with pytest.raises(MetLinkRUnscorableError):
        score_metlinkr(frame, METLINKR)


def test_metlinkr_refuses_a_frame_missing_the_held_out_curator_grouping():
    frame = _metlinkr_frame(
        [{"name": "x", "group": "G1", "source": "a", "chosen": "CHEBI:1"}]
    ).drop(columns=[METLINKR.group_label_column])
    with pytest.raises(CuratorLeakError, match="held-out curator columns"):
        assert_curator_held_out(frame, METLINKR)


def test_metlinkr_merge_folds_passes_without_losing_held_out_columns():
    first = _metlinkr_frame([{"name": "glucose", "group": "G1", "source": "a", "chosen": None}])
    second = _metlinkr_frame(
        [
            {
                "name": "glucose",
                "group": "G1",
                "source": "a",
                "chosen": "CHEBI:4167",
                "equiv": {"HMDB": ["HMDB0000122"]},
            }
        ]
    )
    merged = merge_metlinkr([first, second], METLINKR)
    assert len(merged) == 1
    assert merged.iloc[0][METLINKR.group_label_column] == "G1"
    assert merged.iloc[0]["chosen_kg_id"] == "CHEBI:4167"


# --------------------------------------------------------------------------------------------------
# structure.py — the name fallback
# --------------------------------------------------------------------------------------------------


def test_mw_answers_first_and_pubchem_is_not_consulted(monkeypatch):
    calls: list[str] = []

    def fake_get(self, url, **kwargs):
        calls.append(url)
        return httpx.Response(
            200,
            json={"inchi_key": "PMMYEEVYMWASQN-DMTCNVIQSA-N"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    resolver = NameStructureResolver()
    assert resolver.block("trans-4-Hydroxyproline") == "PMMYEEVYMWASQN"
    assert len(calls) == 1
    assert "metabolomicsworkbench" in calls[0]
    assert resolver.stats()["hits_by_source"]["metabolomics_workbench"] == 1


def test_pubchem_is_the_second_hop(monkeypatch):
    def fake_get(self, url, **kwargs):
        if "metabolomicsworkbench" in url:
            return httpx.Response(404, request=httpx.Request("GET", url))
        return httpx.Response(
            200,
            json={"PropertyTable": {"Properties": [{"InChIKey": "WQZGKKKJIJFFOK-GASJEMHNSA-N"}]}},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    resolver = NameStructureResolver()
    assert resolver.block("glucose") == "WQZGKKKJIJFFOK"
    assert resolver.stats()["hits_by_source"]["pubchem"] == 1


def test_the_missing_value_sentinel_is_never_accepted_as_a_structure(monkeypatch):
    def fake_get(self, url, **kwargs):
        if "metabolomicsworkbench" in url:
            return httpx.Response(200, json={"inchi_key": "-"}, request=httpx.Request("GET", url))
        return httpx.Response(404, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    resolver = NameStructureResolver()
    assert resolver.block("nothing") is None
    assert resolver.stats()["no_match"] == 1


def test_a_throttled_service_is_counted_apart_from_a_real_no_match(monkeypatch):
    """Collapsing these is how a degraded service gets reported as name difficulty."""

    def fake_get(self, url, **kwargs):
        return httpx.Response(503, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    resolver = NameStructureResolver()
    assert resolver.block("anything") is None
    stats = resolver.stats()
    assert stats["lookup_failed"] == 1
    assert stats["no_match"] == 0


def test_lookups_are_memoized_per_name(monkeypatch):
    calls: list[str] = []

    def fake_get(self, url, **kwargs):
        calls.append(url)
        return httpx.Response(
            200, json={"inchi_key": "AAAAAAAAAAAAAA-BB-N"}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    resolver = NameStructureResolver()
    resolver.block("x")
    resolver.block("x")
    assert len(calls) == 1


def test_blank_name_never_reaches_the_network(monkeypatch):
    def explode(self, url, **kwargs):  # pragma: no cover
        raise AssertionError("should not be called")

    monkeypatch.setattr(httpx.Client, "get", explode)
    assert NameStructureResolver().block(None) is None
    assert NameStructureResolver().block("") is None


def test_the_freeze_gap_is_stated_rather_than_hidden():
    """The deployment's pinned RefMet freeze has no API surface; the report must say so."""
    stats = NameStructureResolver().stats()
    assert stats["refmet_freeze_consulted"] is False
    assert "no API surface" in stats["refmet_freeze_note"]


def test_srm1950_gold_is_derived_deterministically_from_certified_smiles():
    """The delivery's InChIKey column is empty, so the oracle structure comes from RDKit."""
    key = inchikey_from_smiles("C(C(=O)O)N")  # glycine
    assert key is not None and key.startswith("DHMQDGOQFOQNFH")
    assert inchikey_from_smiles("not a smiles!!!") is None
    assert inchikey_from_smiles("") is None
