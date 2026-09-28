"""HGNC per-namespace accuracy must be scored from each namespace's OWN target-vocab run.

The mapper runs once per target vocab and node selection is steered by that vocab, so the three
runs can pick different nodes for the same symbol. Scoring every namespace off the Ensembl run
(the pre-fix behaviour) reports NCBIGene cross-refs of an Ensembl-steered resolution, which is
how the 2026-09 "NCBIGene fell" inversion was manufactured.

The fixture below is built so the two scoring rules disagree: the tRNA row resolves correctly in
the NCBIGene run and to an unrelated gene in the Ensembl run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from biomapper.benchmarks import arms
from biomapper.benchmarks.adapters import backbones
from biomapper.benchmarks.api_mapper import ApiMapper
from biomapper.benchmarks.config import HGNC
from biomapper.benchmarks.runner import VocabRun
from biomapper.benchmarks.scorers.curie_scorer import (
    MissingTargetRunError,
    score_curie_per_target_run,
)

GOLD = {
    "PROTCODE": ("ENSEMBL:ENSG01", "NCBIGene:1", "UniProtKB:P00001"),
    "PSEUDO1P": ("ENSEMBL:ENSG02", "NCBIGene:2", ""),
    "TRX-ABC1-1": ("", "NCBIGene:3", ""),
}

# Node each target-vocab run chose, as (chosen_kg_id, kg_equivalent_ids).
NODE_PROTCODE = ("HGNC:1", {"ENSEMBL": ["ENSG01"], "NCBIGene": ["1"], "UniProtKB": ["P00001"]})
NODE_PSEUDO = ("HGNC:2", {"ENSEMBL": ["ENSG02"], "NCBIGene": ["2"]})
NODE_TRNA = ("HGNC:3", {"NCBIGene": ["3"]})
NODE_UNRELATED = ("HGNC:9", {"ENSEMBL": ["ENSG09"], "NCBIGene": ["9"], "UniProtKB": ["P00009"]})

CHOICES = {
    # Ensembl run: the tRNA has no Ensembl id, so the Ensembl-steered pass lands on another gene.
    "ENSEMBL": {"PROTCODE": NODE_PROTCODE, "PSEUDO1P": NODE_PSEUDO, "TRX-ABC1-1": NODE_UNRELATED},
    "NCBIGene": {"PROTCODE": NODE_PROTCODE, "PSEUDO1P": NODE_PSEUDO, "TRX-ABC1-1": NODE_TRNA},
    # UniProt run: neither non-coding row has a UniProt id; both land on protein-coding genes.
    "UniProtKB": {
        "PROTCODE": NODE_PROTCODE,
        "PSEUDO1P": NODE_PROTCODE,
        "TRX-ABC1-1": NODE_UNRELATED,
    },
}


def _mapped(vocab: str) -> pd.DataFrame:
    rows = []
    for symbol, (ens, ncbi, uni) in GOLD.items():
        chosen, equiv = CHOICES[vocab][symbol]
        rows.append(
            {
                HGNC.name_column: symbol,
                "gold_ensembl": ens,
                "gold_entrez": ncbi,
                "gold_uniprot": uni,
                "chosen_kg_id": chosen,
                "kg_equivalent_ids": json.dumps(equiv),
            }
        )
    return pd.DataFrame(rows)


def _acc(result: dict, ns: str) -> tuple[int, int]:
    entry = result["per_namespace_accuracy"][ns]
    return entry["correct"], entry["scored_denominator"]


def test_each_namespace_is_scored_from_its_own_run():
    result = score_curie_per_target_run({v: _mapped(v) for v in HGNC.target_vocabs}, HGNC)
    assert _acc(result, "ENSEMBL") == (2, 2)
    assert _acc(result, "NCBIGene") == (3, 3)
    assert _acc(result, "UniProtKB") == (1, 1)
    for ns in HGNC.target_vocabs:
        assert result["per_namespace_accuracy"][ns]["source_vocab_run"] == ns
        assert result["per_namespace_accuracy"][ns]["n_rows"] == 3


def test_forced_hits_count_target_ids_returned_without_target_gold():
    result = score_curie_per_target_run({v: _mapped(v) for v in HGNC.target_vocabs}, HGNC)
    forced = result["forced_hit_diagnostic"]["per_namespace"]
    assert result["forced_hit_diagnostic"]["diagnostic_only"] is True
    assert forced["ENSEMBL"] == {
        "rows_without_target_gold": 1,
        "returned_target_id_anyway": 1,
        "checkable_against_other_gold": 1,
        "wrong_gene_where_checkable": 1,
        "source_vocab_run": "ENSEMBL",
    }
    assert forced["NCBIGene"]["rows_without_target_gold"] == 0
    assert forced["UniProtKB"]["rows_without_target_gold"] == 2
    assert forced["UniProtKB"]["returned_target_id_anyway"] == 2
    assert forced["UniProtKB"]["wrong_gene_where_checkable"] == 2


def test_a_missing_target_run_is_refused_not_borrowed():
    with pytest.raises(MissingTargetRunError):
        score_curie_per_target_run({"ENSEMBL": _mapped("ENSEMBL")}, HGNC)


def test_run_hgnc_reports_per_namespace_figures_from_each_run(tmp_path: Path, monkeypatch):
    """End to end through the arm, which is where the cross-run scoring bug lived.

    Scoring every namespace from the Ensembl run would give NCBIGene 2/3 here; the correct
    answer is 3/3 from the NCBIGene run.
    """
    paths = {}
    for vocab in HGNC.target_vocabs:
        path = tmp_path / f"{vocab}_MAPPED.tsv"
        _mapped(vocab).to_csv(path, sep="\t", index=False)
        paths[vocab] = path

    def fake_run_all(*_args, **_kwargs):
        return {
            v: VocabRun(vocab=v, ok=True, output_tsv=str(p), stats=None, manifest=None)
            for v, p in paths.items()
        }

    bundle = backbones.BackboneBundle(
        input_df=pd.DataFrame({HGNC.name_column: list(GOLD)}),
        card={"dataset": HGNC.key, "subsample_sha256": "0" * 64},
    )
    monkeypatch.setattr(arms, "run_all", fake_run_all)
    monkeypatch.setattr(backbones, "resolve_source_version", lambda _url: "test")
    monkeypatch.setattr(backbones, "load_backbone", lambda *_a, **_k: bundle)
    monkeypatch.setattr(backbones, "persist_subsample", lambda *_a, **_k: None)

    out = arms.run_hgnc(
        mapper=ApiMapper("https://example.invalid/api/v1"),
        out_dir=tmp_path / "hgnc",
        provenance=None,
        kestrel_url="https://example.invalid/kestrel",
    )
    result = out["results"]
    assert _acc(result, "ENSEMBL") == (2, 2)
    assert _acc(result, "NCBIGene") == (3, 3)
    assert _acc(result, "UniProtKB") == (1, 1)
    assert result["per_namespace_scoring"] == "own_target_run"
    assert result["rollup_source_vocab_run"] == "ENSEMBL"
    assert result["per_row_source_vocab_run"] == "ENSEMBL"
    for vocab, path in paths.items():
        entry = result["per_namespace_accuracy"][vocab]
        assert entry["source_tsv"] == str(path)
        assert len(entry["source_tsv_sha256"]) == 64
    assert (
        result["forced_hit_diagnostic"]["per_namespace"]["UniProtKB"]["wrong_gene_where_checkable"]
        == 2
    )
    written = json.loads((tmp_path / "hgnc" / "ENSEMBL_results.json").read_text())
    assert written["per_namespace"]["NCBIGene"] == {"correct": 3, "scored": 3}


def test_campaign_report_prints_per_namespace_rows_not_the_rollup():
    """The report must show each namespace's own-run figure, never the non-quotable roll-up."""
    from biomapper.benchmarks.report.campaign import _curie_row

    result = score_curie_per_target_run({v: _mapped(v) for v in HGNC.target_vocabs}, HGNC)
    result["coverage"] = {"n_predicted": 3, "total": 3}
    result["comparable_core"] = {"top1_accuracy": 1.0, "scored_denominator": 3}
    lines = _curie_row({"key": HGNC.key, "arm": "gene", "result": result}).split("\n")
    assert lines == [
        f"| {HGNC.key} (ENSEMBL) | gene | 100.0% | 2 | 3/3 | n/a | n/a | n/a |",
        f"| {HGNC.key} (NCBIGene) | gene | 100.0% | 3 | 3/3 | n/a | n/a | n/a |",
        f"| {HGNC.key} (UniProtKB) | gene | 100.0% | 1 | 3/3 | n/a | n/a | n/a |",
    ]
