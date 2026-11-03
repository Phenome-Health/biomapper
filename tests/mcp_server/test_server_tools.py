"""Unit 3: interactive tools through the in-memory MCP client, against respx-mocked HTTP.

No test touches the network. The one prod-shaped payload is a real ``/map/batch`` response
captured on 2026-11-03 (``tests/fixtures/mcp/``), because hand-built fixtures have hidden live
bugs before.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
import respx
from mcp import Client

from biomapper.mcp.server import build_server
from tests.conftest import SAMPLE_API_RESPONSE
from tests.mcp_server.mcp_helpers import (
    API,
    KESTREL,
    InFlight,
    batch_echo,
    batch_response,
    healthy_routes,
    make_config,
    prod_batch_payload,
    resolved_entry,
)


@pytest.fixture()
def router():
    with respx.mock(assert_all_called=False) as r:
        healthy_routes(r)
        yield r


def _payload(result) -> dict:
    assert result.structured_content is not None, result.content
    return result.structured_content


async def _wait_for_pins(client: Client) -> None:
    for _ in range(200):
        status = _payload(await client.call_tool("server_info", {}))
        if status["provenance"]["pins_status"] == "ready":
            return
        await asyncio.sleep(0.01)
    raise AssertionError("pins never landed")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


async def test_discovery_tools_return_parsed_lists(router, tmp_path) -> None:
    router.get(f"{API}/entity-types").mock(
        return_value=httpx.Response(
            200, json=[{"type": "biolink:SmallMolecule", "aliases": ["metabolite"],
                        "defaultPrefixes": ["CHEBI"]}]
        )
    )
    router.get(f"{API}/vocabularies").mock(
        return_value=httpx.Response(200, json={"vocabularies": [{"prefix": "CHEBI",
                                                                 "aliases": ["chebi"]}]})
    )
    router.get(f"{API}/annotators").mock(
        return_value=httpx.Response(200, json={"annotators": [{"slug": "kestrel-hybrid-search",
                                                               "name": "Kestrel hybrid"}]})
    )
    async with Client(build_server(make_config(tmp_path))) as client:
        types_ = _payload(await client.call_tool("list_entity_types", {}))
        vocabs = _payload(await client.call_tool("list_vocabularies", {}))
        annotators = _payload(await client.call_tool("list_annotators", {}))
    assert types_["entity_types"] == [
        {"type": "biolink:SmallMolecule", "aliases": ["metabolite"], "default_prefixes": ["CHEBI"]}
    ]
    assert vocabs["vocabularies"] == [{"prefix": "CHEBI", "aliases": ["chebi"]}]
    assert annotators["annotators"][0]["slug"] == "kestrel-hybrid-search"


async def test_tool_descriptions_carry_throughput_guidance(router, tmp_path) -> None:
    async with Client(build_server(make_config(tmp_path))) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert {"list_entity_types", "list_vocabularies", "list_annotators", "map_entity",
            "map_batch", "server_info"} <= set(tools)
    assert "4-5 s" in tools["map_entity"].description
    assert "list_entity_types" in tools["map_entity"].description
    assert "map_file" in tools["map_batch"].description
    assert "up to 5" in tools["map_batch"].description


# ---------------------------------------------------------------------------
# map_entity
# ---------------------------------------------------------------------------


async def test_map_entity_resolved_carries_curie_tier_and_provenance(router, tmp_path) -> None:
    router.post(f"{API}/map/entity").mock(return_value=httpx.Response(200,
                                                                      json=SAMPLE_API_RESPONSE))
    async with Client(build_server(make_config(tmp_path))) as client:
        await _wait_for_pins(client)
        out = _payload(await client.call_tool(
            "map_entity", {"name": "L-Histidine", "entity_type": "biolink:SmallMolecule"}
        ))
    r = out["result"]
    assert r["status"] == "resolved"
    assert r["primary_curie"] == "RM:0129894"
    assert r["chosen_kg_id"] == "CHEBI:15971"
    assert r["confidence_tier"] == "high"
    prov = out["provenance"]
    assert prov["client_version"]
    assert prov["kg_build"].startswith("2.3.0")
    assert prov["kg_verified"] is True
    assert "assumed, not verified" in prov["kg_label"]
    assert prov["api_base_url"] == API


async def test_map_entity_sends_identifiers_and_annotation_mode(router, tmp_path) -> None:
    route = router.post(f"{API}/map/entity").mock(
        return_value=httpx.Response(200, json=SAMPLE_API_RESPONSE)
    )
    async with Client(build_server(make_config(tmp_path))) as client:
        await client.call_tool("map_entity", {
            "name": "L-Histidine", "entity_type": "biolink:SmallMolecule",
            "identifiers": {"HMDB": "HMDB0000177"}, "annotation_mode": "all",
        })
    body = json.loads(route.calls.last.request.content)
    assert body["identifiers"] == {"HMDB": "HMDB0000177"}
    assert body["options"]["annotation_mode"] == "all"


async def test_map_entity_unresolved_body_is_unresolved_never_a_fabricated_curie(
    router, tmp_path
) -> None:
    router.post(f"{API}/map/entity").mock(return_value=httpx.Response(200, json={
        "result": {"name": "zzz", "curies": [], "chosen_kg_id": None}, "metadata": {}
    }))
    async with Client(build_server(make_config(tmp_path))) as client:
        r = _payload(await client.call_tool(
            "map_entity", {"name": "zzz", "entity_type": "biolink:SmallMolecule"}
        ))["result"]
    assert r["status"] == "unresolved"
    assert r["primary_curie"] is None and r["chosen_kg_id"] is None
    assert r["reason"]


async def test_map_entity_empty_body_is_an_unknown_error_not_a_no_match(router, tmp_path) -> None:
    router.post(f"{API}/map/entity").mock(return_value=httpx.Response(200, json={}))
    async with Client(build_server(make_config(tmp_path))) as client:
        r = _payload(await client.call_tool(
            "map_entity", {"name": "zzz", "entity_type": "biolink:SmallMolecule"}
        ))["result"]
    assert r["status"] == "error"
    assert "not a confirmed no-match" in r["error"]
    assert r["chosen_kg_id"] is None


async def test_map_entity_429_is_a_tool_error_with_retry_after(router, tmp_path) -> None:
    router.post(f"{API}/map/entity").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "7"})
    )
    async with Client(build_server(make_config(tmp_path))) as client:
        res = await client.call_tool(
            "map_entity", {"name": "x", "entity_type": "biolink:SmallMolecule"}
        )
    assert res.is_error
    assert "429" in res.content[0].text and "7 s" in res.content[0].text


async def test_map_entity_5xx_and_timeout_are_readable_tool_errors(router, tmp_path) -> None:
    route = router.post(f"{API}/map/entity")
    route.side_effect = [httpx.Response(503, text="down"), httpx.ReadTimeout("slow")]
    async with Client(build_server(make_config(tmp_path))) as client:
        first = await client.call_tool("map_entity", {"name": "x",
                                                      "entity_type": "biolink:SmallMolecule"})
        second = await client.call_tool("map_entity", {"name": "x",
                                                       "entity_type": "biolink:SmallMolecule"})
    assert first.is_error and "HTTP 503" in first.content[0].text
    assert second.is_error and "map_file" in second.content[0].text


# ---------------------------------------------------------------------------
# map_batch
# ---------------------------------------------------------------------------


async def test_map_batch_of_five_succeeds(router, tmp_path) -> None:
    inflight = InFlight()
    router.post(f"{API}/map/batch").mock(side_effect=batch_echo(inflight))
    entities = [{"name": f"m{i}"} for i in range(5)]
    async with Client(build_server(make_config(tmp_path))) as client:
        out = _payload(await client.call_tool(
            "map_batch", {"entities": entities, "entity_type": "biolink:SmallMolecule"}
        ))
    assert [r["query_name"] for r in out["results"]] == [f"m{i}" for i in range(5)]
    assert all(r["status"] == "resolved" for r in out["results"])
    assert inflight.calls == 1


async def test_map_batch_of_six_is_refused_without_any_http_call(router, tmp_path) -> None:
    route = router.post(f"{API}/map/batch")
    async with Client(build_server(make_config(tmp_path))) as client:
        res = await client.call_tool("map_batch", {
            "entities": [{"name": f"m{i}"} for i in range(6)],
            "entity_type": "biolink:SmallMolecule",
        })
    assert res.is_error
    assert "map_file" in res.content[0].text
    assert route.call_count == 0


async def test_map_batch_with_one_shared_error_is_one_systemic_failure(router, tmp_path) -> None:
    router.post(f"{API}/map/batch").mock(return_value=httpx.Response(200, json=batch_response(
        [{"name": f"m{i}", "error": "annotator backend down"} for i in range(5)]
    )))
    async with Client(build_server(make_config(tmp_path))) as client:
        res = await client.call_tool("map_batch", {
            "entities": [{"name": f"m{i}"} for i in range(5)],
            "entity_type": "biolink:SmallMolecule",
        })
    assert res.is_error
    text = res.content[0].text
    assert text.count("annotator backend down") == 1


async def test_map_batch_429_is_one_systemic_error_and_never_sleeps(
    router, tmp_path, monkeypatch
) -> None:
    router.post(f"{API}/map/batch").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "30"})
    )
    async with Client(build_server(make_config(tmp_path))) as client:
        start = time.monotonic()
        res = await client.call_tool("map_batch", {
            "entities": [{"name": "a"}, {"name": "b"}], "entity_type": "biolink:SmallMolecule",
        })
        assert time.monotonic() - start < 5
    assert res.is_error and "429" in res.content[0].text


async def test_map_batch_length_mismatch_is_an_error_not_misaligned_results(
    router, tmp_path
) -> None:
    router.post(f"{API}/map/batch").mock(return_value=httpx.Response(200, json=batch_response(
        [resolved_entry("a")]
    )))
    async with Client(build_server(make_config(tmp_path))) as client:
        res = await client.call_tool("map_batch", {
            "entities": [{"name": "a"}, {"name": "b"}], "entity_type": "biolink:SmallMolecule",
        })
    assert res.is_error


async def test_map_batch_on_a_captured_prod_payload(router, tmp_path) -> None:
    """The real shape, including a nonsense name that prod resolves 'uncorroborated'."""
    router.post(f"{API}/map/batch").mock(
        return_value=httpx.Response(200, json=prod_batch_payload())
    )
    async with Client(build_server(make_config(tmp_path))) as client:
        res = await client.call_tool("map_batch", {
            "entities": [{"name": "L-Histidine"}, {"name": "Z1005800534 not a metabolite"}],
            "entity_type": "biolink:SmallMolecule",
        })
    out = _payload(res)
    hist, junk = out["results"]
    assert hist["status"] == "resolved" and hist["chosen_kg_id"] == "RM:0129894"
    assert hist["certificate_state"] == "corroborated"
    assert junk["certificate_state"] == "uncorroborated"
    assert "raw_response" not in json.dumps(out) and "kestrel_results" not in json.dumps(out)
    assert len(res.content[0].text) < 20_000


# ---------------------------------------------------------------------------
# Loop guard, gate, deadline
# ---------------------------------------------------------------------------


async def test_loop_guard_refuses_the_21st_call_and_ignores_discovery(router, tmp_path) -> None:
    router.post(f"{API}/map/entity").mock(return_value=httpx.Response(200,
                                                                      json=SAMPLE_API_RESPONSE))
    router.get(f"{API}/vocabularies").mock(return_value=httpx.Response(200,
                                                                       json={"vocabularies": []}))
    args = {"name": "x", "entity_type": "biolink:SmallMolecule"}
    async with Client(build_server(make_config(tmp_path))) as client:
        for _ in range(20):
            assert not (await client.call_tool("map_entity", args)).is_error
            await client.call_tool("list_vocabularies", {})
        refused = await client.call_tool("map_entity", args)
        assert not (await client.call_tool("list_vocabularies", {})).is_error
    assert refused.is_error and "map_file" in refused.content[0].text


async def test_loop_guard_window_slides(router, tmp_path) -> None:
    router.post(f"{API}/map/entity").mock(return_value=httpx.Response(200,
                                                                      json=SAMPLE_API_RESPONSE))
    now = [0.0]
    config = make_config(tmp_path, loop_limit=2, loop_window_s=60.0, monotonic=lambda: now[0])
    args = {"name": "x", "entity_type": "biolink:SmallMolecule"}
    async with Client(build_server(config)) as client:
        await client.call_tool("map_entity", args)
        await client.call_tool("map_entity", args)
        assert (await client.call_tool("map_entity", args)).is_error
        now[0] += 61
        assert not (await client.call_tool("map_entity", args)).is_error


async def test_concurrent_calls_never_overlap_on_the_wire(router, tmp_path) -> None:
    inflight = InFlight()

    async def slow_entity(request: httpx.Request) -> httpx.Response:
        await inflight.enter(request, delay=0.1)
        return httpx.Response(200, json=SAMPLE_API_RESPONSE)

    router.post(f"{API}/map/entity").mock(side_effect=slow_entity)
    args = {"name": "x", "entity_type": "biolink:SmallMolecule"}
    async with Client(build_server(make_config(tmp_path))) as client:
        results = await asyncio.gather(*(client.call_tool("map_entity", args) for _ in range(3)))
    assert not any(r.is_error for r in results)
    assert inflight.calls == 3
    assert inflight.peak == 1


async def test_a_held_gate_returns_busy_within_the_budget(router, tmp_path) -> None:
    inflight = InFlight()

    async def slow_entity(request: httpx.Request) -> httpx.Response:
        await inflight.enter(request, delay=1.0)
        return httpx.Response(200, json=SAMPLE_API_RESPONSE)

    router.post(f"{API}/map/entity").mock(side_effect=slow_entity)
    args = {"name": "x", "entity_type": "biolink:SmallMolecule"}
    config = make_config(tmp_path, gate_wait_s=0.1, loop_limit=2)
    async with Client(build_server(config)) as client:
        first = asyncio.ensure_future(client.call_tool("map_entity", args))
        await asyncio.sleep(0.2)
        start = time.monotonic()
        busy = await client.call_tool("map_entity", args)
        assert time.monotonic() - start < 0.8
        assert not (await first).is_error
        # Limit 2: the first call counted, the busy refusal must not have, so this one fits.
        third = await client.call_tool("map_entity", args)
    assert busy.is_error and "busy" in busy.content[0].text.lower()
    assert not third.is_error, third.content[0].text


async def test_a_hung_request_returns_a_tool_error_within_the_deadline(router, tmp_path) -> None:
    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return httpx.Response(200, json=SAMPLE_API_RESPONSE)  # pragma: no cover

    router.post(f"{API}/map/entity").mock(side_effect=hang)
    config = make_config(tmp_path, tool_deadline_s=0.3)
    async with Client(build_server(config)) as client:
        start = time.monotonic()
        res = await client.call_tool("map_entity", {"name": "x",
                                                    "entity_type": "biolink:SmallMolecule"})
        assert time.monotonic() - start < 3
    assert res.is_error and "map_file" in res.content[0].text


async def test_initialize_does_not_wait_for_slow_health_probes(tmp_path) -> None:
    with respx.mock(assert_all_called=False) as r:
        def hang(request: httpx.Request) -> httpx.Response:
            time.sleep(2)
            return httpx.Response(200, json={"status": "healthy"})

        r.get(f"{API}/health").mock(side_effect=hang)
        r.get(f"{KESTREL}/health").mock(side_effect=hang)
        start = time.monotonic()
        async with Client(build_server(make_config(tmp_path))) as client:
            info = _payload(await client.call_tool("server_info", {}))
            assert time.monotonic() - start < 1.5
        assert info["provenance"]["pins_status"] == "pending"
        assert info["provenance"]["kg_verified"] is False


async def test_env_overrides_reach_requests_and_pins(tmp_path) -> None:
    from biomapper.mcp.config import ServerConfig

    other_api = "https://staging.test/api/v1"
    other_kestrel = "https://kestrel-staging.test/api"
    config = ServerConfig.from_env({
        "BIOMAPPER_BASE_URL": other_api,
        "BIOMAPPER_KESTREL_URL": other_kestrel,
        "BIOMAPPER_MCP_HOME": str(tmp_path / "h"),
    })
    with respx.mock(assert_all_called=False) as r:
        r.get(f"{other_api}/health").mock(return_value=httpx.Response(200, json={"status": "ok"}))
        kestrel = r.get(f"{other_kestrel}/health").mock(
            return_value=httpx.Response(200, json={"kestrel_version": "1", "kg_build": {
                "kg_version": "9.9.9"}})
        )
        entity = r.post(f"{other_api}/map/entity").mock(
            return_value=httpx.Response(200, json=SAMPLE_API_RESPONSE)
        )
        async with Client(build_server(config)) as client:
            await _wait_for_pins(client)
            out = _payload(await client.call_tool(
                "map_entity", {"name": "x", "entity_type": "biolink:SmallMolecule"}
            ))
    assert entity.call_count == 1 and kestrel.call_count >= 1
    assert out["provenance"]["api_base_url"] == other_api
    assert out["provenance"]["kestrel_url"] == other_kestrel
    assert out["provenance"]["kg_build"] == "9.9.9"


def test_from_env_reads_key_home_and_roots(tmp_path) -> None:
    import os

    from biomapper.mcp.config import ServerConfig

    config = ServerConfig.from_env({
        "BIOMAPPER_API_KEY": "k",
        "BIOMAPPER_MCP_HOME": str(tmp_path),
        "BIOMAPPER_MCP_ROOTS": os.pathsep.join([str(tmp_path / "a"), str(tmp_path / "b")]),
    })
    assert config.api_key == "k"
    assert config.home == tmp_path
    assert config.env_roots == [tmp_path / "a", tmp_path / "b"]
    assert ServerConfig.from_env({}).env_roots == []


async def test_api_key_is_sent_as_header_but_never_returned(tmp_path) -> None:
    with respx.mock(assert_all_called=False) as r:
        healthy_routes(r)
        route = r.post(f"{API}/map/entity").mock(
            return_value=httpx.Response(200, json=SAMPLE_API_RESPONSE)
        )
        async with Client(build_server(make_config(tmp_path, api_key="s3cret-key"))) as client:
            res = await client.call_tool("map_entity", {"name": "x",
                                                        "entity_type": "biolink:SmallMolecule"})
            info = await client.call_tool("server_info", {})
    assert route.calls.last.request.headers["X-API-Key"] == "s3cret-key"
    assert "s3cret-key" not in res.content[0].text
    assert "s3cret-key" not in info.content[0].text
