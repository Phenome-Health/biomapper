"""ApiMapper tests: the held-out-gold invariant, the anti-trivial guards, frame assembly."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from biomapper.benchmarks.api_mapper import (
    ApiMapper,
    EmptyDatasetError,
    _load_json_cell,
    assigned_stats_nonnull,
    mapped_provided_nonnull,
)
from biomapper.models import MappingResult, ResolutionCertificate


def _mapper() -> ApiMapper:
    return ApiMapper("https://example.invalid/api/v1")


def test_only_provided_id_columns_reach_the_api():
    """The held-out-gold invariant. Gold columns ride along in the frame and are never sent."""
    frame = pd.DataFrame(
        {
            "metabolite_name": ["glucose"],
            "gold_inchikey": ["WQZGKKKJIJFFOK-GASJEMHNSA-N"],
            "gold_chebi": ["CHEBI:4167"],
        }
    )
    records = ApiMapper._build_records(frame, "metabolite_name", [])
    assert records == [{"name": "glucose", "identifiers": {}}]

    # And when a column IS provided, only that one goes.
    records = ApiMapper._build_records(frame, "metabolite_name", ["gold_chebi"])
    assert records[0]["identifiers"] == {"gold_chebi": "CHEBI:4167"}
    assert "gold_inchikey" not in records[0]["identifiers"]


@pytest.mark.parametrize("sentinel", ["-", "NO_MATCH", "", "nan", "NaN"])
def test_missing_value_sentinels_never_become_real_identifiers(sentinel):
    """The engine replaces these with NaN before mapping; a sentinel sent as an id is a wrong id."""
    frame = pd.DataFrame({"name": ["x"], "src": [sentinel]})
    records = ApiMapper._build_records(frame, "name", ["src"])
    assert records[0]["identifiers"] == {}


def test_empty_frame_is_refused_as_broken_not_scored_as_zero(tmp_path):
    with pytest.raises(EmptyDatasetError, match="broken run, not a score of zero"):
        _mapper().map_dataset_to_kg(
            dataset=pd.DataFrame({"name": []}),
            entity_type="metabolite",
            name_column="name",
            provided_id_columns=[],
            output_dir=tmp_path,
        )


def test_missing_name_column_fails_loudly(tmp_path):
    with pytest.raises(KeyError, match="not in the input frame"):
        _mapper().map_dataset_to_kg(
            dataset=pd.DataFrame({"other": ["a"]}),
            entity_type="metabolite",
            name_column="name",
            provided_id_columns=[],
            output_dir=tmp_path,
        )


def test_frame_assembly_keeps_gold_and_flattens_the_certificate():
    frame = pd.DataFrame({"metabolite_name": ["glucose"], "gold_inchikey": ["WQZGKKKJIJFFOK-X-N"]})
    result = MappingResult(
        query_name="glucose",
        resolved=True,
        primary_curie="CHEBI:4167",
        chosen_kg_id="CHEBI:4167",
        kg_equivalent_ids={"INCHIKEY": ["WQZGKKKJIJFFOK-GASJEMHNSA-N"]},
        identifiers={"CHEBI": ["4167"]},
        certificate=ResolutionCertificate(
            state="corroborated",
            structure_status="structure_present",
            node_inchikey_blocks=["WQZGKKKJIJFFOK"],
            comparison_rule="inchikey_first_block_set_intersection/v1",
        ),
    )
    assembled = _mapper()._assemble_frame(frame, [result])
    row = assembled.iloc[0]
    # The gold column survives untouched — the scorer reads it.
    assert row["gold_inchikey"] == "WQZGKKKJIJFFOK-X-N"
    assert row["chosen_kg_id"] == "CHEBI:4167"
    assert row["certificate_state"] == "corroborated"
    # List-valued certificate fields are pipe-joined, matching the engine's flat TSV form.
    assert row["certificate_node_inchikey_blocks"] == "WQZGKKKJIJFFOK"
    assert json.loads(row["kg_equivalent_ids"]) == {"INCHIKEY": ["WQZGKKKJIJFFOK-GASJEMHNSA-N"]}


def test_a_prediction_column_in_the_input_is_refused_not_overwritten():
    """Otherwise the scorer could read the adapter's own column as the prediction."""
    frame = pd.DataFrame({"name": ["x"], "chosen_kg_id": ["CHEBI:999"]})
    with pytest.raises(ValueError, match="already carries prediction column"):
        _mapper()._assemble_frame(frame, [MappingResult(query_name="x")])


def test_kg_equivalent_ids_cell_parses_as_json_and_as_a_python_repr():
    """The ported curie_scorer uses ast.literal_eval, which reads both. Both must round-trip."""
    assert _load_json_cell('{"CHEBI": ["4167"]}') == {"CHEBI": ["4167"]}
    assert _load_json_cell("{'CHEBI': ['4167']}") == {"CHEBI": ["4167"]}
    assert _load_json_cell(None) == {}
    assert _load_json_cell("nan") == {}
    assert _load_json_cell("not a dict at all") == {}


def test_assigned_and_provided_counts_drive_the_two_anti_trivial_guards():
    mapper = _mapper()
    resolved_by_annotation = MappingResult(
        query_name="glucose", chosen_kg_id="CHEBI:4167", identifiers={"CHEBI": ["4167"]}
    )
    resolved_from_provided = MappingResult(query_name="x", chosen_kg_id="CHEBI:1", identifiers={})

    name_mode = mapper._build_stats([resolved_by_annotation], annotation_mode="all")
    assert assigned_stats_nonnull(name_mode)
    assert not mapped_provided_nonnull(name_mode)

    # The gold-as-provided trap: a chosen node with NO assigned ids under name-only input.
    leaked = mapper._build_stats([resolved_from_provided], annotation_mode="all")
    assert not assigned_stats_nonnull(leaked)

    provided_mode = mapper._build_stats([resolved_from_provided], annotation_mode="none")
    assert mapped_provided_nonnull(provided_mode)
    assert not assigned_stats_nonnull(provided_mode)


def test_transient_only_chunks_are_retried_but_real_answers_are_not():
    mapper = _mapper()
    transient = [MappingResult(query_name="a", error="Server error (HTTP 503): upstream")]
    assert mapper._chunk_wholly_transient(transient)

    # A 422 is a real answer. Retrying it just burns the deployment.
    real = [MappingResult(query_name="a", error="HTTP 422 validation error")]
    assert not mapper._chunk_wholly_transient(real)

    # A partial failure is not retried: re-sending the rows that succeeded would double-count.
    partial = [
        MappingResult(query_name="a", error="Server error (HTTP 503): upstream"),
        MappingResult(query_name="b", chosen_kg_id="CHEBI:1"),
    ]
    assert not mapper._chunk_wholly_transient(partial)


def test_request_counters_report_throughput():
    mapper = _mapper()
    mapper.counters.entities = 100
    mapper.counters.seconds = 50.0
    snapshot = mapper.counters.snapshot()
    assert snapshot["entities_per_second"] == 2.0


def test_rows_round_trips_a_persisted_frame(tmp_path: Path):
    """Scoring must work identically from an in-memory frame or a persisted artifact."""
    frame = pd.DataFrame(
        {
            "chosen_kg_id": ["CHEBI:4167"],
            "kg_equivalent_ids": [json.dumps({"INCHIKEY": ["WQZGKKKJIJFFOK-GASJEMHNSA-N"]})],
            "certificate_node_inchikey_blocks": ["WQZGKKKJIJFFOK"],
            "certificate_structure_status": ["structure_present"],
            "certificate_state": ["corroborated"],
        }
    )
    path = tmp_path / "m.tsv"
    frame.to_csv(path, sep="\t", index=False)
    rows = _mapper().rows(pd.read_csv(path, sep="\t"))
    assert rows[0]["kg_equivalent_ids"]["INCHIKEY"] == ["WQZGKKKJIJFFOK-GASJEMHNSA-N"]
    assert rows[0]["certificate"]["node_inchikey_blocks"] == ["WQZGKKKJIJFFOK"]
