"""The public package surface: harmonization exports and sync-helper option passthrough."""

from __future__ import annotations

import httpx
import pytest
import respx

import biomapper
from tests.conftest import make_batch_entry

BASE_URL = "https://biomapper.expertintheloop.io/api/v1"


def test_biomapper_harmonize_resolves_to_the_submodule_not_a_function():
    # The linking entry point is deliberately NOT re-exported at the package root: binding the
    # name `harmonize` there shadows this submodule, and `biomapper.harmonize.curie_set` then
    # raises AttributeError for anyone who reaches for it the obvious way.
    import biomapper.harmonize

    assert biomapper.harmonize.curie_set("CHEBI:1") == frozenset({"CHEBI:1"})
    assert "harmonize" not in biomapper.__all__


def test_harmonization_types_are_importable_from_the_submodule():
    from biomapper.harmonize import HarmonizationResult, Link, OverlapResult

    assert (HarmonizationResult, Link, OverlapResult) is not None


def test_certificate_models_are_exported_from_the_package_root():
    from biomapper import KestrelSearchResult, LipidResolution, ResolutionCertificate

    assert (ResolutionCertificate, LipidResolution, KestrelSearchResult) is not None


def test_the_documented_downstream_import_surface_is_intact():
    # biomapper-ui/artifacts/python-api/services/mapper.py imports exactly these.
    from biomapper import (  # noqa: F401
        BioMapperAuthError,
        BioMapperClient,
        BioMapperConfigError,
        BioMapperError,
        BioMapperRateLimitError,
    )


def test_harmonize_round_trips_over_mapping_results():
    from biomapper import MappingResult
    from biomapper.harmonize import harmonize

    report = harmonize(
        [MappingResult(query_name="Glucose", chosen_kg_id="CHEBI:17234")],
        [MappingResult(query_name="D-glucose", chosen_kg_id="CHEBI:17234")],
    )
    assert report.n_links == 1


@respx.mock
def test_sync_map_entity_forwards_the_new_options(monkeypatch):
    monkeypatch.setenv("BIOMAPPER_API_KEY", "k")
    # The sync single-entity helper delegates to the batch endpoint.
    route = respx.post(f"{BASE_URL}/map/batch").mock(
        return_value=httpx.Response(
            200,
            json={"results": [make_batch_entry("Glucose")], "metadata": {}, "summary": {}},
        )
    )
    biomapper.map_entity("Glucose", vocab="chebi", kestrel_top_n=4, prefer_canonical=False)
    import json

    sent = json.loads(route.calls[0].request.read())["entities"][0]["options"]
    assert sent["vocab"] == "chebi"
    assert sent["kestrel_top_n"] == 4
    assert sent["prefer_canonical"] is False


@respx.mock
def test_sync_map_entities_forwards_the_new_options(monkeypatch):
    monkeypatch.setenv("BIOMAPPER_API_KEY", "k")
    route = respx.post(f"{BASE_URL}/map/batch").mock(
        return_value=httpx.Response(
            200,
            json={"results": [make_batch_entry("Glucose")], "metadata": {}, "summary": {}},
        )
    )
    biomapper.map_entities([{"name": "Glucose"}], candidate_limit=25)
    import json

    sent = json.loads(route.calls[0].request.read())["entities"][0]["options"]
    assert sent["candidate_limit"] == 25


def test_sync_map_entity_rejects_an_out_of_range_bound_without_a_request(monkeypatch):
    monkeypatch.setenv("BIOMAPPER_API_KEY", "k")
    with pytest.raises(ValueError, match="kestrel_top_n"):
        biomapper.map_entity("Glucose", kestrel_top_n=999)


def test_every_new_response_model_is_exported_from_the_package_root():
    # KestrelRequestParams is reachable as KestrelSearchResult.request, so it is public whether
    # or not the export list says so.
    from biomapper import (  # noqa: F401
        KestrelRequestParams,
        KestrelSearchResult,
        LipidResolution,
        ResolutionCertificate,
    )

    for name in (
        "ResolutionCertificate",
        "LipidResolution",
        "KestrelSearchResult",
        "KestrelRequestParams",
    ):
        assert name in biomapper.__all__, name


def test_harmonize_many_is_exported_and_its_module_is_not_shadowed():
    # The function is exported at the root; its module is `biomapper.many_cohorts`, so the root
    # name never shadows a same-named submodule (the trap `harmonize` avoids above).
    import biomapper.many_cohorts

    assert "harmonize_many" in biomapper.__all__
    assert "ManyCohortReport" in biomapper.__all__
    assert biomapper.harmonize_many is biomapper.many_cohorts.harmonize_many


def test_the_readme_harmonize_many_example_runs_as_documented(tmp_path, monkeypatch):
    import re
    from pathlib import Path

    import biomapper.many_cohorts as many
    from biomapper._pins import collect_pins

    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    section = readme.split("### Harmonizing more than two cohorts: `harmonize_many()`", 1)[1]
    code = re.search(r"```python\n(.*?)```", section, re.S).group(1)
    monkeypatch.chdir(tmp_path)
    # offline: no /health probes
    monkeypatch.setattr(many, "collect_pins", lambda **kw: collect_pins(**{**kw, "probe": False}))
    namespace: dict = {}
    with pytest.warns(UserWarning, match="KG build unknown"):
        exec(compile(code, "README.md", "exec"), namespace)
    s = namespace["s"]
    arm = s["arms"]["names_only"]
    assert arm["groups"] == 3
    assert arm["cross_node_entries"] == 1
    assert arm["id_check"]["by_status"]["confirmed"] == 2
    assert "tartu" in s["identifier_arm_skipped"]
    assert s["publication_grade"] == "iteration-grade, not for publication"
