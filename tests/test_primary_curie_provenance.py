"""``primary_curie`` from the server, per-annotator CURIE provenance, and their old-server fallback.

The response below is trimmed from a real prod call (engine v1.3.1, 2026-11-06,
``POST /api/v1/map/entity`` for ``dihomo-linolenate (20:3n3 or n6)``). ``curies`` is alphabetical,
so ``curies[0]`` is KRAKEN text search's CHEBI:88670 (dihomo-linoleate, 20:2: the wrong compound)
while the server's ``primary_curie`` is the chosen RefMet node.
"""

from __future__ import annotations

import copy
from typing import Any

from biomapper.harmonize import harmonize
from biomapper.models import MappingResult, RawApiResult

PROD_V131_DGLA: dict[str, Any] = {
    "result": {
        "name": "dihomo-linolenate (20:3n3 or n6)",
        "curies": ["CHEBI:88670", "RM:0152308", "RM:0153916"],
        "primary_curie": "RM:0153916",
        "curies_provided": [],
        "curies_assigned": {
            "metabolomics-workbench": ["RM:0153916"],
            "goslin-lipid": ["RM:0152308", "RM:0153916"],
            "kestrel-hybrid-search": ["CHEBI:88670"],
        },
        "chosen_kg_id": "RM:0153916",
        "chosen_kg_id_review": "divergent_refmet",
        "lipid_resolution": {
            "query_lipid_level_asserted": "sn_position",
            "matched_lipid_level": "molecular_species",
            "mapping_relation": "broad",
            "name_is_mixture": True,
            "query_species_from_parenthetical": True,
        },
    },
    "metadata": {"request_id": "73a7a454-51c7-4383-ade7-f53bacc13eb6"},
}


def _old_server(response: dict[str, Any]) -> dict[str, Any]:
    """The same response as a pre-v1.3.0 server would send it: no new fields."""
    old = copy.deepcopy(response)
    for field in ("primary_curie", "curies_provided", "curies_assigned"):
        old["result"].pop(field)
    for field in ("name_is_mixture", "query_species_from_parenthetical"):
        old["result"]["lipid_resolution"].pop(field)
    return old


def test_primary_curie_is_read_from_the_server() -> None:
    r = MappingResult.from_api_response(PROD_V131_DGLA, query_name="dgla")
    assert r.primary_curie == "RM:0153916" == r.chosen_kg_id
    assert r.resolved


def test_primary_curie_falls_back_to_first_curie_on_an_old_server() -> None:
    r = MappingResult.from_api_response(_old_server(PROD_V131_DGLA), query_name="x")
    assert r.primary_curie == "CHEBI:88670"
    assert r.curies_by_annotator == {} and r.curies_provided == []
    assert r.name_is_mixture is None


def test_explicit_null_primary_curie_is_kept() -> None:
    # The engine sends null only when nothing resolved; a sent null is not replaced by curies[0].
    raw = RawApiResult(curies=["CHEBI:1"], primary_curie=None)
    assert MappingResult.from_batch_entry(raw, query_name="x").primary_curie is None
    assert MappingResult.from_batch_entry(RawApiResult(), query_name="x").primary_curie is None


def test_batch_entries_read_the_same_fields() -> None:
    raw = RawApiResult.model_validate(PROD_V131_DGLA["result"])
    r = MappingResult.from_batch_entry(raw, query_name="x")
    assert r.primary_curie == "RM:0153916"
    assert r.curies_by_annotator["kestrel-hybrid-search"] == ["CHEBI:88670"]


def test_curies_by_annotator_and_provided() -> None:
    r = MappingResult.from_api_response(PROD_V131_DGLA, query_name="x")
    assert r.curies_by_annotator == {
        "metabolomics-workbench": ["RM:0153916"],
        "goslin-lipid": ["RM:0152308", "RM:0153916"],
        "kestrel-hybrid-search": ["CHEBI:88670"],
    }
    assert r.curies_provided == []
    provided = copy.deepcopy(PROD_V131_DGLA)
    provided["result"]["curies_provided"] = ["HMDB:HMDB0002925"]
    assert MappingResult.from_api_response(provided, query_name="x").curies_provided == [
        "HMDB:HMDB0002925"
    ]


def test_engine_mixture_flag_from_lipid_resolution() -> None:
    r = MappingResult.from_api_response(PROD_V131_DGLA, query_name="x")
    assert r.name_is_mixture is True
    assert r.lipid_resolution is not None
    assert r.lipid_resolution.query_species_from_parenthetical is True


def test_harmonize_ignores_primary_curie_and_provenance() -> None:
    # harmonize() links on chosen_kg_id + kg_equivalent_ids only: the new fields change nothing.
    new = MappingResult.from_api_response(PROD_V131_DGLA, query_name="dgla")
    old = MappingResult.from_api_response(_old_server(PROD_V131_DGLA), query_name="dgla")
    other = MappingResult(query_name="other", chosen_kg_id="RM:0153916")
    a = harmonize([new], [other]).summary()
    b = harmonize([old], [other]).summary()
    assert a["n_links"] == b["n_links"] == 1
