"""1.7.0 Unit 1: weak-row detection and server-issued references.

Weak rows (unresolved, refused, uncorroborated, contradicted, and ``unavailable`` for small
molecules) get ``weak: true`` and an opaque ``ref``; strong rows serialize exactly as in 1.6.3,
on both MCP content channels. Refs are one private file per record under the MCP home.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest
import respx
from mcp import Client

from biomapper.mcp import tools
from biomapper.mcp.references import RefError, RefStore, parse_ref
from biomapper.mcp.server import build_server
from biomapper.models import MappingResult
from tests.mcp_server.mcp_helpers import (
    API,
    FIXTURES,
    entity_response,
    entity_result,
    healthy_routes,
    make_config,
)

ST = "biolink:SmallMolecule"


@pytest.fixture()
def router():
    with respx.mock(assert_all_called=False) as r:
        healthy_routes(r)
        yield r


def _mr(**kw) -> MappingResult:
    return MappingResult.from_api_response(entity_response("x", **kw), query_name="x")


async def _wait_for_pins(client: Client) -> None:
    for _ in range(200):
        info = (await client.call_tool("server_info", {})).structured_content
        if info["provenance"]["pins_status"] == "ready":
            return
        await asyncio.sleep(0.01)
    raise AssertionError("pins never landed")


# ---------------------------------------------------------------------------
# is_weak
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kw", "entity_type", "trigger"),
    [
        ({"chosen": None, "cert": None}, ST, "unresolved"),
        ({"chosen": None, "cert": None, "refusal": "sum_composition"}, ST, "refused"),
        ({"cert": "uncorroborated"}, ST, "uncorroborated"),
        ({"cert": "contradicted"}, "biolink:Gene", "contradicted"),
        ({"cert": "unavailable"}, ST, "unavailable"),
        ({"cert": "unavailable"}, "metabolite", "unavailable"),
        ({"cert": "unavailable"}, "lipid", "unavailable"),
        ({"cert": "unavailable"}, "SmallMolecule", "unavailable"),
        ({"cert": "unavailable"}, "biolink:Gene", None),
        ({"cert": "unavailable"}, "biolink:Protein", None),
        ({"cert": "not_applicable"}, ST, None),
        ({"cert": "not_applicable"}, "biolink:Protein", None),
        ({"cert": "corroborated"}, ST, None),
        ({"cert": None}, ST, None),
    ],
)
def test_weak_trigger(kw, entity_type, trigger) -> None:
    result = _mr(**kw)
    assert tools.weak_trigger(result, entity_type) == trigger
    assert tools.is_weak(result, entity_type) is (trigger is not None)


def test_an_error_row_is_never_weak() -> None:
    result = MappingResult(query_name="x", error="Server error (HTTP 503)")
    assert tools.weak_trigger(result, ST) is None


# ---------------------------------------------------------------------------
# RefStore
# ---------------------------------------------------------------------------


def _issue(store: RefStore, **kw):
    result = _mr(chosen="CHEBI:1", cert="uncorroborated",
                 equivalents={"CHEBI": [str(i) for i in range(30)]})
    defaults = {
        "source": "entity",
        "query": {"name": "x", "entity_type": ST, "identifiers": {"HMDB": "HMDB1"},
                  "annotation_mode": "missing"},
        "result": result,
        "weak_trigger": "uncorroborated",
        "kg_build": "2.3.0 (3dd08a5b)",
    }
    defaults.update(kw)
    return store.issue(**defaults)


def test_an_issued_ref_records_full_context_privately(tmp_path) -> None:
    store = RefStore(tmp_path / "home", wall_clock=lambda: 1_700_000_000.0)
    rec = _issue(store)
    assert parse_ref(rec.ref_id) == ("interactive", None)
    path = tmp_path / "home" / "refs" / f"{rec.ref_id}.json"
    assert path.is_file()
    assert (path.stat().st_mode & 0o777) == 0o600
    assert ((tmp_path / "home" / "refs").stat().st_mode & 0o777) == 0o700
    data = json.loads(path.read_text())
    assert data["query"]["identifiers"] == {"HMDB": "HMDB1"}
    assert data["query"]["entity_type"] == ST
    assert data["engine"]["chosen_kg_id"] == "CHEBI:1"
    assert data["engine"]["certificate_state"] == "uncorroborated"
    # The FULL equivalents, never the truncated interactive view.
    assert len(data["engine"]["kg_equivalent_ids"]["CHEBI"]) == 30
    assert data["weak_trigger"] == "uncorroborated"
    assert data["pins"]["kg_build"] == "2.3.0 (3dd08a5b)"
    assert "assumed, not verified" in data["pins"]["kg_label"]
    assert data["source"] == "entity"
    assert data["created_utc"].startswith("2023-11-14")
    assert store.load(rec.ref_id).engine.chosen_kg_id == "CHEBI:1"


def test_ref_ids_are_random(tmp_path) -> None:
    store = RefStore(tmp_path / "home")
    assert len({_issue(store).ref_id for _ in range(20)}) == 20


@pytest.mark.parametrize(
    "bad",
    ["", "ref-", "ref-" + "g" * 32, "../../etc/passwd", "ref-" + "a" * 31,
     "ref-" + "a" * 32 + "/x", "jref-" + "a" * 16, "jref-../" + "a" * 24, "REF-" + "A" * 32,
     "ref-" + "a" * 32 + "\n"],
)
def test_malformed_or_traversal_refs_are_rejected_without_file_access(
    tmp_path, bad, monkeypatch
) -> None:
    store = RefStore(tmp_path / "home")
    touched: list[str] = []
    real_open = Path.open

    def spy(self, *a, **k):
        touched.append(str(self))
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "open", spy)
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: touched.append(str(self)))
    with pytest.raises(RefError, match="not a reference"):
        store.load(bad)
    assert touched == []


def test_an_unknown_ref_is_rejected(tmp_path) -> None:
    store = RefStore(tmp_path / "home")
    with pytest.raises(RefError, match="No reference"):
        store.load("ref-" + "a" * 32)


def test_an_interactive_ref_resolves_in_a_new_store(tmp_path) -> None:
    """A restarted server process (same home, same uid) still resolves it."""
    rec = _issue(RefStore(tmp_path / "home"))
    again = RefStore(tmp_path / "home").load(rec.ref_id)
    assert again.ref_id == rec.ref_id and again.query.name == "x"


def test_an_interactive_ref_older_than_30_days_is_gc_d_lazily(tmp_path) -> None:
    now = [1_700_000_000.0]
    store = RefStore(tmp_path / "home", wall_clock=lambda: now[0])
    old = _issue(store)
    now[0] += 29 * 86400
    assert store.load(old.ref_id)  # still fine
    now[0] += 2 * 86400
    with pytest.raises(RefError, match="expired"):
        store.load(old.ref_id)
    assert not (tmp_path / "home" / "refs" / f"{old.ref_id}.json").exists()


def test_issue_sweeps_expired_refs(tmp_path) -> None:
    now = [1_700_000_000.0]
    store = RefStore(tmp_path / "home", wall_clock=lambda: now[0])
    old = _issue(store)
    now[0] += 31 * 86400
    fresh = RefStore(tmp_path / "home", wall_clock=lambda: now[0])
    _issue(fresh)
    assert not (tmp_path / "home" / "refs" / f"{old.ref_id}.json").exists()


def test_a_ref_owned_by_another_uid_is_refused(tmp_path, monkeypatch) -> None:
    store = RefStore(tmp_path / "home")
    rec = _issue(store)
    monkeypatch.setattr(os, "getuid", lambda: os.geteuid() + 1)
    with pytest.raises(RefError, match="another user"):
        store.load(rec.ref_id)


def test_job_refs_live_under_the_job_dir(tmp_path) -> None:
    store = RefStore(tmp_path / "home")
    job_id = "0123456789abcdef"
    rec = _issue(store, source="job", job_id=job_id, row_key="7")
    kind, jid = parse_ref(rec.ref_id)
    assert (kind, jid) == ("job", job_id)
    assert (tmp_path / "home" / "jobs" / job_id / "refs" / f"{rec.ref_id}.json").is_file()
    assert store.load(rec.ref_id).row_key == "7"


def test_job_refs_never_expire_on_their_own(tmp_path) -> None:
    now = [1_700_000_000.0]
    store = RefStore(tmp_path / "home", wall_clock=lambda: now[0])
    rec = _issue(store, source="job", job_id="0123456789abcdef", row_key="1")
    now[0] += 365 * 86400
    assert store.load(rec.ref_id).ref_id == rec.ref_id


# ---------------------------------------------------------------------------
# Through the server
# ---------------------------------------------------------------------------


async def test_an_unresolved_map_entity_result_is_weak_with_a_ref(router, tmp_path) -> None:
    router.post(f"{API}/map/entity").mock(return_value=httpx.Response(
        200, json=entity_response("zzz", chosen=None, cert=None)
    ))
    config = make_config(tmp_path)
    async with Client(build_server(config)) as client:
        await _wait_for_pins(client)
        r = (await client.call_tool("map_entity", {
            "name": "zzz", "entity_type": ST, "identifiers": {"HMDB": "HMDB9"},
            "annotation_mode": "all",
        })).structured_content["result"]
    assert r["status"] == "unresolved"
    assert r["weak"] is True
    rec = RefStore(config.home).load(r["ref"])
    assert rec.source == "entity"
    assert rec.query.name == "zzz"
    assert rec.query.identifiers == {"HMDB": "HMDB9"}
    assert rec.query.annotation_mode == "all"
    assert rec.weak_trigger == "unresolved"
    assert rec.pins.kg_build.startswith("2.3.0")


async def test_map_batch_marks_only_the_weak_rows(router, tmp_path) -> None:
    router.post(f"{API}/map/batch").mock(return_value=httpx.Response(200, json={"results": [
        entity_result("a"), entity_result("b", cert="contradicted"),
    ], "metadata": {}}))
    config = make_config(tmp_path)
    async with Client(build_server(config)) as client:
        out = (await client.call_tool("map_batch", {
            "entities": [{"name": "a"}, {"name": "b"}], "entity_type": ST,
        })).structured_content["results"]
    assert "weak" not in out[0] and "ref" not in out[0]
    assert out[1]["weak"] is True
    assert RefStore(config.home).load(out[1]["ref"]).source == "batch"


async def test_a_weak_row_still_maps_when_the_ref_store_is_unwritable(router, tmp_path) -> None:
    """A ref is a convenience: failing to write one never fails or alters the mapping."""
    router.post(f"{API}/map/entity").mock(return_value=httpx.Response(
        200, json=entity_response("zzz", cert="uncorroborated")
    ))
    config = make_config(tmp_path)
    config.home.mkdir(parents=True)
    (config.home / "refs").write_text("not a directory")
    async with Client(build_server(config)) as client:
        res = await client.call_tool("map_entity", {"name": "zzz", "entity_type": ST})
    assert not res.is_error
    r = res.structured_content["result"]
    assert r["status"] == "resolved" and r["weak"] is True and "ref" not in r


@pytest.mark.parametrize("tool", ["map_entity", "map_batch"])
async def test_a_strong_row_serializes_exactly_as_1_6_3(router, tmp_path, tool) -> None:
    from tests.conftest import SAMPLE_API_RESPONSE

    golden = json.loads((FIXTURES / "golden_strong_row_1.6.3.json").read_text())[tool]
    body = json.loads(json.dumps(SAMPLE_API_RESPONSE))
    body["result"]["resolution_certificate"] = {"state": "corroborated"}
    router.post(f"{API}/map/entity").mock(return_value=httpx.Response(200, json=body))
    router.post(f"{API}/map/batch").mock(return_value=httpx.Response(200, json={
        "results": [body["result"]], "metadata": {}
    }))
    args = (
        {"name": "L-Histidine", "entity_type": ST} if tool == "map_entity"
        else {"entities": [{"name": "L-Histidine"}], "entity_type": ST}
    )
    async with Client(build_server(make_config(tmp_path))) as client:
        await _wait_for_pins(client)
        res = await client.call_tool(tool, args)
    version = res.structured_content["provenance"]["client_version"]
    golden_structured = json.loads(
        json.dumps(golden["structured"]).replace('"client_version": "1.6.3"',
                                                 f'"client_version": "{version}"')
    )
    golden_text = golden["text"].replace('"client_version": "1.6.3"',
                                         f'"client_version": "{version}"')
    assert res.structured_content == golden_structured
    assert res.content[0].text == golden_text
    assert not (tmp_path / "mcp-home" / "refs").exists() or not any(
        (tmp_path / "mcp-home" / "refs").iterdir()
    )


async def test_output_schema_gains_only_optional_weak_and_ref(router, tmp_path) -> None:
    async with Client(build_server(make_config(tmp_path))) as client:
        listed = {t.name: t for t in (await client.list_tools()).tools}
    schema = listed["map_entity"].output_schema
    entity = schema["$defs"]["EntityResult"]
    assert {"weak", "ref"} <= set(entity["properties"])
    assert "weak" not in entity.get("required", []) and "ref" not in entity.get("required", [])
