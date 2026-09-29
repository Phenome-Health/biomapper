"""Engine API surface the wrapper must expose.

Every shape asserted here was read from the deployed API's own OpenAPI document
(``GET /api/v1/openapi.json`` at ``biomapper.expertintheloop.io``), not from a guess.
No test in this file touches the network; requests are intercepted with respx.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from biomapper.client import BioMapperClient
from biomapper.models import (
    BatchMappingResponse,
    MapEntityRequest,
    MappingResult,
    RawApiResult,
)
from tests.conftest import make_batch_entry, make_ndjson_body

BASE_URL = "https://biomapper.expertintheloop.io/api/v1"


@pytest.fixture()
def client() -> BioMapperClient:
    return BioMapperClient(api_key="test-key", timeout=5.0)


# A certificate exactly as the deployed API emits it.
CERTIFICATE: dict[str, Any] = {
    "state": "corroborated",
    "structure_status": "structure_present",
    "node_inchikey_blocks": ["WQZGKKKJIJFFOK"],
    "comparison_rule": "inchikey_block1",
    "equivalent_ids_lookup_ok": True,
    "selection_conflict": None,
    "independent_source": "pubchem",
    "independent_inchikey_block": "WQZGKKKJIJFFOK",
    "independent_of_selection": True,
    "tier_b_outcome": "resolved",
    "lipid_resolution_level": "unavailable",
    "refusal_reason": None,
    "refmet_availability": "voted",
    "refmet_source": "local_snapshot",
    "refmet_snapshot_version": "2026-08-07",
    "tier_b_snapshot_version": None,
    "provenance": {"tier_b": "live"},
}

LIPID_RESOLUTION: dict[str, Any] = {
    "query_lipid_level_asserted": "sn_position",
    "query_lipid_level_effective": "species",
    "matched_lipid_level": "species",
    "mapping_relation": "broad",
    "mapping_predicate": "skos:broadMatch",
    "query_transformed": "goslin_species_canonical",
    "ambiguous": True,
    "candidate_structure_count": 4,
    "ambiguity_basis": "lipidmaps_abbrev_chains",
    "goslin_dialect": "SHORTHAND2020",
    "goslin_formula": "C42H82NO8P",
    "goslin_mass": 759.5778,
}


# ---------------------------------------------------------------------------
# Batch summary — the deployed API returns a NESTED value in `summary`
# ---------------------------------------------------------------------------


def test_batch_summary_accepts_the_nested_refmet_source_counts_the_api_returns():
    # OpenAPI types summary as {str: int | {str: int}}. A flat dict[str, int] model
    # rejects the whole response and every entity in the chunk turns into an error.
    parsed = BatchMappingResponse.model_validate(
        {
            "results": [],
            "metadata": {"request_id": "r", "processing_time_ms": 1.0},
            "summary": {
                "total": 2,
                "successful": 2,
                "failed": 0,
                "refmet_unavailable": 0,
                "refmet_source_counts": {"local_snapshot": 2},
            },
        }
    )
    assert parsed.summary["refmet_source_counts"] == {"local_snapshot": 2}


@respx.mock
@pytest.mark.asyncio()
async def test_map_entities_survives_a_response_carrying_refmet_source_counts(client):
    respx.post(f"{BASE_URL}/map/batch").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [make_batch_entry("Glucose")],
                "metadata": {"request_id": "r", "processing_time_ms": 1.0},
                "summary": {"total": 1, "refmet_source_counts": {"live_api": 1}},
            },
        )
    )
    async with client as c:
        results = await c.map_entities([{"name": "Glucose"}])
    assert results[0].error is None
    assert results[0].resolved is True


# ---------------------------------------------------------------------------
# Response fields the wrapper did not parse
# ---------------------------------------------------------------------------


def test_raw_result_parses_the_resolution_certificate():
    raw = RawApiResult.model_validate(
        {"name": "Glucose", "chosen_kg_id": "CHEBI:17234", "resolution_certificate": CERTIFICATE}
    )
    assert raw.resolution_certificate is not None
    assert raw.resolution_certificate.state == "corroborated"
    assert raw.resolution_certificate.node_inchikey_blocks == ["WQZGKKKJIJFFOK"]
    assert raw.resolution_certificate.independent_of_selection is True


def test_raw_result_parses_lipid_resolution_and_refmet_provenance():
    raw = RawApiResult.model_validate(
        {
            "name": "PC 34:1",
            "lipid_resolution": LIPID_RESOLUTION,
            "chosen_kg_id_lipid_hint": "lipid_generalized",
            "refmet_availability": "unavailable",
            "refmet_source": "live_api",
            "refmet_snapshot_version": "2026-08-07",
            "tier_b_snapshot_version": "tb-1",
        }
    )
    assert raw.lipid_resolution is not None
    assert raw.lipid_resolution.mapping_relation == "broad"
    assert raw.lipid_resolution.goslin_mass == pytest.approx(759.5778)
    assert raw.chosen_kg_id_lipid_hint == "lipid_generalized"
    assert raw.refmet_availability == "unavailable"
    assert raw.refmet_source == "live_api"
    assert raw.tier_b_snapshot_version == "tb-1"


def test_raw_result_parses_kestrel_passthrough_rows_verbatim():
    raw = RawApiResult.model_validate(
        {
            "name": "Glucose",
            "kestrel_results": [
                {
                    "endpoint": "hybrid-search",
                    "request": {
                        "search_text": "Glucose",
                        "limit": 2,
                        "category": "biolink:SmallMolecule",
                        "prefix": ["CHEBI"],
                    },
                    "rows": [{"id": "CHEBI:17234", "score": 2.4, "vendor_extra": "kept"}],
                    "fetch_strategy": "separate_call",
                    "error": None,
                }
            ],
        }
    )
    assert raw.kestrel_results is not None
    # Rows are carried through unchanged: no coercion, no null-filling, nothing dropped.
    assert raw.kestrel_results[0].rows[0]["vendor_extra"] == "kept"
    assert raw.kestrel_results[0].request.limit == 2


def test_raw_result_still_parses_a_response_without_any_of_the_new_fields():
    raw = RawApiResult.model_validate({"name": "Glucose", "chosen_kg_id": "CHEBI:17234"})
    assert raw.resolution_certificate is None
    assert raw.lipid_resolution is None
    assert raw.kestrel_results is None
    assert raw.refmet_availability == "not_queried"


def test_mapping_result_surfaces_the_certificate_and_refmet_availability():
    result = MappingResult.from_api_response(
        {
            "result": {
                "name": "Glucose",
                "curies": ["CHEBI:17234"],
                "chosen_kg_id": "CHEBI:17234",
                "resolution_certificate": CERTIFICATE,
                "refmet_availability": "voted",
            },
            "metadata": {"request_id": "r", "processing_time_ms": 1.0},
        },
        query_name="Glucose",
    )
    assert result.certificate is not None
    assert result.certificate.state == "corroborated"
    assert result.refmet_availability == "voted"


def test_mapping_result_exposes_the_refusal_reason_from_the_certificate():
    refused = {**CERTIFICATE, "state": "unavailable", "refusal_reason": "off_category"}
    result = MappingResult.from_api_response(
        {"result": {"name": "X", "curies": [], "resolution_certificate": refused}},
        query_name="X",
    )
    assert result.refusal_reason == "off_category"


def test_mapping_result_refusal_reason_is_none_without_a_certificate():
    result = MappingResult.from_api_response(
        {"result": {"name": "X", "curies": []}}, query_name="X"
    )
    assert result.refusal_reason is None


# ---------------------------------------------------------------------------
# Request options the wrapper did not send
# ---------------------------------------------------------------------------


def test_map_entity_request_accepts_list_valued_identifiers():
    # The API types identifiers as dict[str, str | list[str]].
    req = MapEntityRequest(name="Glucose", identifiers={"KEGG": ["C00031", "C00267"]})
    assert req.identifiers["KEGG"] == ["C00031", "C00267"]


@respx.mock
@pytest.mark.asyncio()
async def test_map_entity_sends_every_mapping_option(client):
    route = respx.post(f"{BASE_URL}/map/entity").mock(
        return_value=httpx.Response(200, json={"result": {"name": "Glucose", "curies": []}})
    )
    async with client as c:
        await c.map_entity(
            "Glucose",
            vocab="chebi",
            array_delimiters=["|"],
            prefer_human=False,
            prefer_canonical=False,
            candidate_limit=20,
            kestrel_top_n=5,
        )
    options = route.calls[0].request.read()
    import json as _json

    sent = _json.loads(options)["options"]
    assert sent["vocab"] == "chebi"
    assert sent["array_delimiters"] == ["|"]
    assert sent["prefer_human"] is False
    assert sent["prefer_canonical"] is False
    assert sent["candidate_limit"] == 20
    assert sent["kestrel_top_n"] == 5


@respx.mock
@pytest.mark.asyncio()
async def test_map_entity_omits_options_the_caller_did_not_set(client):
    # An unset option must not be sent, so the server's own default governs.
    route = respx.post(f"{BASE_URL}/map/entity").mock(
        return_value=httpx.Response(200, json={"result": {"name": "Glucose", "curies": []}})
    )
    async with client as c:
        await c.map_entity("Glucose")
    import json as _json

    sent = _json.loads(route.calls[0].request.read())["options"]
    assert sent == {"annotation_mode": "missing"}


@respx.mock
@pytest.mark.asyncio()
async def test_map_entities_sends_every_mapping_option(client):
    route = respx.post(f"{BASE_URL}/map/batch").mock(
        return_value=httpx.Response(
            200,
            json={"results": [make_batch_entry("Glucose")], "metadata": {}, "summary": {}},
        )
    )
    async with client as c:
        await c.map_entities(
            [{"name": "Glucose"}], vocab=["chebi", "refmet"], prefer_human=False, kestrel_top_n=3
        )
    import json as _json

    sent = _json.loads(route.calls[0].request.read())["entities"][0]["options"]
    assert sent["vocab"] == ["chebi", "refmet"]
    assert sent["prefer_human"] is False
    assert sent["kestrel_top_n"] == 3


@pytest.mark.parametrize("bad", [0, 101])
@pytest.mark.asyncio()
async def test_candidate_limit_out_of_range_is_rejected_before_the_request(client, bad):
    # The API bounds this to 1..100 and 422s; failing locally saves the round trip.
    async with client as c:
        with pytest.raises(ValueError, match="candidate_limit"):
            await c.map_entity("Glucose", candidate_limit=bad)


@pytest.mark.parametrize("bad", [0, 101])
@pytest.mark.asyncio()
async def test_kestrel_top_n_out_of_range_is_rejected_before_the_request(client, bad):
    async with client as c:
        with pytest.raises(ValueError, match="kestrel_top_n"):
            await c.map_entity("Glucose", kestrel_top_n=bad)


@respx.mock
@pytest.mark.asyncio()
async def test_dataset_stream_sends_the_new_query_params(client, tmp_path: Path):
    path = tmp_path / "d.tsv"
    path.write_text("name\tid\nGlucose\tX\n")
    route = respx.post(f"{BASE_URL}/map/dataset/stream").mock(
        return_value=httpx.Response(200, content=make_ndjson_body([{"name": "Glucose"}]))
    )
    async with client as c:
        async for _ in c.map_dataset_file_iter(
            path,
            name_column="name",
            provided_id_columns=["id"],
            prefer_human=False,
            prefer_canonical=False,
            candidate_limit=10,
            kestrel_top_n=2,
        ):
            pass
    params = route.calls[0].request.url.params
    assert params["prefer_human"] == "false"
    assert params["prefer_canonical"] == "false"
    assert params["candidate_limit"] == "10"
    assert params["kestrel_top_n"] == "2"


@respx.mock
@pytest.mark.asyncio()
async def test_dataset_stream_omits_unset_params(client, tmp_path: Path):
    path = tmp_path / "d.tsv"
    path.write_text("name\tid\nGlucose\tX\n")
    route = respx.post(f"{BASE_URL}/map/dataset/stream").mock(
        return_value=httpx.Response(200, content=make_ndjson_body([{"name": "Glucose"}]))
    )
    async with client as c:
        async for _ in c.map_dataset_file_iter(
            path, name_column="name", provided_id_columns=["id"]
        ):
            pass
    params = route.calls[0].request.url.params
    assert "candidate_limit" not in params
    assert "kestrel_top_n" not in params
    assert "prefer_human" not in params


# ---------------------------------------------------------------------------
# Snapshot provenance reaches the user-facing result (Greptile review, PR #2)
# ---------------------------------------------------------------------------


def test_mapping_result_carries_the_snapshot_versions():
    # Batch and dataset results keep no raw_response, so a field parsed into RawApiResult but
    # never copied across is unreachable for those callers.
    raw = RawApiResult.model_validate(
        {
            "name": "Glucose",
            "curies": ["CHEBI:17234"],
            "refmet_snapshot_version": "2026-08-07",
            "tier_b_snapshot_version": "tb-1",
        }
    )
    result = MappingResult.from_batch_entry(raw, query_name="Glucose")
    assert result.refmet_snapshot_version == "2026-08-07"
    assert result.tier_b_snapshot_version == "tb-1"


def test_mapping_result_snapshot_versions_default_to_none():
    result = MappingResult.from_batch_entry(
        RawApiResult.model_validate({"name": "Glucose"}), query_name="Glucose"
    )
    assert result.refmet_snapshot_version is None
    assert result.tier_b_snapshot_version is None
