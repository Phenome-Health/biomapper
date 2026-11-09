"""1.7.0 Unit 3: ``record_suggestion`` and the server-owned ``choices.tsv`` sidecar.

A suggestion is the agent's pick for a weak ref: a CURIE from that ref's latest usable candidate
set, the engine's own choice, or ``none``. It is never a BioMapper resolution and never reaches
the main output. One current suggestion per ref; re-recording supersedes and keeps history.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx
from mcp import Client

from biomapper.mcp.judgments import CHOICES_MARKER, SuggestionStore
from biomapper.mcp.references import RefStore
from biomapper.mcp.server import build_server
from biomapper.models import MappingResult
from tests.mcp_server.mcp_helpers import (
    API,
    batch_response,
    entity_response,
    entity_result,
    healthy_routes,
    kestrel_block,
    kestrel_row,
    make_config,
)

ST = "biolink:SmallMolecule"
BUILD = "2.3.0 (3dd08a5b)"


@pytest.fixture()
def router():
    with respx.mock(assert_all_called=False) as r:
        healthy_routes(r)
        yield r


def _seed(config, n: int = 1) -> list[str]:
    store = RefStore(config.home)
    result = MappingResult.from_api_response(
        entity_response("x", chosen="CHEBI:1", cert="uncorroborated"), query_name="x"
    )
    return [
        store.issue(source="entity",
                    query={"name": f"glucose{i}", "entity_type": ST},
                    result=result, weak_trigger="uncorroborated", kg_build=BUILD).ref_id
        for i in range(n)
    ]


def _cands(curies: list[str], chosen: str = "CHEBI:1") -> dict:
    rows = [kestrel_row(c, 1 - i / 10, f"name {c}") for i, c in enumerate(curies)]
    return entity_response("x", chosen=chosen, cert="uncorroborated",
                           kestrel=[kestrel_block("hybrid-search", rows)])


async def _pins(client) -> None:
    for _ in range(200):
        info = (await client.call_tool("server_info", {})).structured_content
        if info["provenance"]["pins_status"] == "ready":
            return
        await asyncio.sleep(0.01)
    raise AssertionError("pins never landed")


def _ok(res) -> dict:
    assert not res.is_error, res.content[0].text
    return res.structured_content


async def _suggest(client, ref, choice, rationale="because", **extra):
    return await client.call_tool("record_suggestion",
                                  {"ref": ref, "choice": choice, "rationale": rationale, **extra})


# ---------------------------------------------------------------------------
# Interactive refs
# ---------------------------------------------------------------------------


async def test_a_candidate_from_the_stored_set_is_recorded_with_full_context(
    router, tmp_path
) -> None:
    config = make_config(tmp_path)
    [ref] = _seed(config)
    router.post(f"{API}/map/entity").mock(
        return_value=httpx.Response(200, json=_cands(["CHEBI:1", "CHEBI:2"]))
    )
    async with Client(build_server(config)) as client:
        await _pins(client)
        _ok(await client.call_tool("get_candidates", {"refs": [ref]}))
        out = _ok(await _suggest(client, ref, "CHEBI:2", "Matches the stereo descriptor."))
    assert out["ref"] == ref and out["choice"] == "CHEBI:2"
    assert out["choice_source"] == "candidate"
    assert out["supersedes"] is None
    assert "not a BioMapper resolution" in out["note"]
    [rec] = SuggestionStore(RefStore(config.home)).history(ref)
    assert rec.suggestion_id == out["suggestion_id"]
    assert rec.query.name == "glucose0" and rec.engine.chosen_kg_id == "CHEBI:1"
    assert rec.weak_trigger == "uncorroborated"
    assert rec.pins.kg_build == BUILD
    assert rec.candidate_set_fetched_utc is not None and rec.candidate_set_n == 10
    assert rec.rationale == "Matches the stereo descriptor."


async def test_without_a_fetch_only_the_engine_choice_or_none_is_accepted(router, tmp_path) -> None:
    config = make_config(tmp_path)
    [ref] = _seed(config)
    async with Client(build_server(config)) as client:
        engine = _ok(await _suggest(client, ref, "CHEBI:1"))
        none = _ok(await _suggest(client, ref, "none"))
        other = await _suggest(client, ref, "CHEBI:2")
    assert engine["choice_source"] == "engine"
    assert none["choice_source"] == "none"
    assert other.is_error and "get_candidates" in other.content[0].text


async def test_a_curie_from_another_refs_set_is_rejected(router, tmp_path) -> None:
    config = make_config(tmp_path)
    a, b = _seed(config, 2)
    router.post(f"{API}/map/entity").mock(side_effect=[
        httpx.Response(200, json=_cands(["CHEBI:1", "CHEBI:2"])),
        httpx.Response(200, json=_cands(["CHEBI:1", "CHEBI:3"])),
    ])
    async with Client(build_server(config)) as client:
        await _pins(client)
        _ok(await client.call_tool("get_candidates", {"refs": [a, b]}))
        res = await _suggest(client, a, "CHEBI:3")
    assert res.is_error and "not in" in res.content[0].text


async def test_a_run_mismatch_set_cannot_back_a_suggestion(router, tmp_path) -> None:
    config = make_config(tmp_path)
    [ref] = _seed(config)
    router.post(f"{API}/map/entity").mock(
        return_value=httpx.Response(200, json=_cands(["CHEBI:2", "CHEBI:5"], chosen="CHEBI:2"))
    )
    async with Client(build_server(config)) as client:
        await _pins(client)
        [r] = _ok(await client.call_tool("get_candidates", {"refs": [ref]}))["results"]
        assert r["status"] == "run_mismatch"
        on_old = await _suggest(client, ref, "CHEBI:5")
        on_new = _ok(await _suggest(client, r["superseding_ref"], "CHEBI:5"))
    assert on_old.is_error and r["superseding_ref"] in on_old.content[0].text
    assert on_new["choice_source"] == "candidate"


async def test_re_recording_supersedes_and_keeps_history(router, tmp_path) -> None:
    config = make_config(tmp_path)
    [ref] = _seed(config)
    async with Client(build_server(config)) as client:
        first = _ok(await _suggest(client, ref, "CHEBI:1", "first"))
        second = _ok(await _suggest(client, ref, "none", "second thoughts"))
    assert second["supersedes"] == first["suggestion_id"]
    store = SuggestionStore(RefStore(config.home))
    history = store.history(ref)
    assert [h.choice for h in history] == ["CHEBI:1", "none"]
    assert history[0].superseded_by == second["suggestion_id"]
    assert store.current(ref).suggestion_id == second["suggestion_id"]


@pytest.mark.parametrize("extra", [{"status": "confirmed"}, {"human_confirmed": True}])
async def test_status_like_extra_keys_are_rejected_by_the_schema(router, tmp_path, extra) -> None:
    config = make_config(tmp_path)
    [ref] = _seed(config)
    async with Client(build_server(config)) as client:
        res = await _suggest(client, ref, "CHEBI:1", **extra)
    assert res.is_error
    assert not SuggestionStore(RefStore(config.home)).history(ref)


async def test_the_input_schema_has_no_status_like_fields(router, tmp_path) -> None:
    async with Client(build_server(make_config(tmp_path))) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    schema = tools["record_suggestion"].input_schema
    assert set(schema["properties"]) == {"ref", "choice", "rationale"}
    assert schema.get("additionalProperties") is False
    assert "not a BioMapper resolution" in tools["record_suggestion"].description


async def test_the_rationale_is_sanitized_and_capped(router, tmp_path) -> None:
    config = make_config(tmp_path)
    [ref] = _seed(config)
    async with Client(build_server(config)) as client:
        _ok(await _suggest(client, ref, "none", "line one\nline two\x00" + "x" * 900))
    rec = SuggestionStore(RefStore(config.home)).current(ref)
    assert "\n" not in rec.rationale and "\x00" not in rec.rationale
    assert len(rec.rationale) <= 500


# ---------------------------------------------------------------------------
# Jobs: choices.tsv
# ---------------------------------------------------------------------------


async def _finished_job(client, data: Path, names: list[str]) -> dict:
    src = data / "p.tsv"
    src.write_text("name\n" + "\n".join(names) + "\n")
    job = _ok(await client.call_tool("map_file", {"path": str(src), "name_column": "name",
                                                  "entity_type": ST}))
    for _ in range(500):
        st = _ok(await client.call_tool("job_status", {"job_id": job["job_id"]}))["jobs"][0]
        if st["state"] == "complete":
            return st
        await asyncio.sleep(0.02)
    raise AssertionError("job never completed")


def _weak_refs(data: Path) -> dict[str, str]:
    lines = (data / "p.biomapper.weak.tsv").read_text().splitlines()
    head = lines[0].split("\t")
    return {row[head.index("name")]: row[head.index("ref")]
            for row in (line.split("\t") for line in lines[1:])}


def _job_routes(router) -> None:
    async def batch(request):
        ents = json.loads(request.content)["entities"]
        rows = [entity_result(e["name"], chosen="CHEBI:1",
                              cert="uncorroborated" if e["name"] != "ok" else "corroborated",
                              kestrel=_cands(["CHEBI:1", "CHEBI:2"])["result"]["kestrel_results"]
                              if "kestrel_top_n" in e["options"] else None)
                for e in ents]
        return httpx.Response(200, json=batch_response(rows))

    router.post(f"{API}/map/batch").mock(side_effect=batch)


async def test_choices_tsv_holds_only_current_suggestions_and_the_main_tsv_is_untouched(
    router, tmp_path
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    config = make_config(tmp_path, env_roots=[data])
    _job_routes(router)
    async with Client(build_server(config)) as client:
        await _finished_job(client, data, ["a", "ok", "b"])
        main_before = (data / "p.biomapper.tsv").read_bytes()
        refs = _weak_refs(data)
        out = _ok(await _suggest(client, refs["a"], "CHEBI:1", "first"))
        assert out["choices_path"] == str(data / "p.biomapper.choices.tsv")
        _ok(await _suggest(client, refs["a"], "none", "changed\tmy mind"))
        _ok(await _suggest(client, refs["b"], "CHEBI:1", "fine"))
    text = (data / "p.biomapper.choices.tsv").read_text()
    lines = text.splitlines()
    assert lines[0] == CHOICES_MARKER
    import csv
    import io

    rows = list(csv.DictReader(io.StringIO("\n".join(lines[1:])), delimiter="\t"))
    assert [(r["name"], r["suggested_choice"]) for r in rows] == [("a", "none"), ("b", "CHEBI:1")]
    assert rows[0]["rationale"] == "changed my mind"
    assert rows[0]["engine_choice"] == "CHEBI:1" and rows[0]["ref"] == refs["a"]
    assert (data / "p.biomapper.tsv").read_bytes() == main_before


async def test_an_existing_choices_tsv_without_the_marker_is_refused(router, tmp_path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    config = make_config(tmp_path, env_roots=[data])
    _job_routes(router)
    async with Client(build_server(config)) as client:
        await _finished_job(client, data, ["a"])
        (data / "p.biomapper.choices.tsv").write_text("my own notes\n")
        res = await _suggest(client, _weak_refs(data)["a"], "CHEBI:1")
    assert res.is_error and "p.biomapper.choices.tsv" in res.content[0].text
    assert (data / "p.biomapper.choices.tsv").read_text() == "my own notes\n"
    assert not SuggestionStore(RefStore(config.home)).history(_weak_refs(data)["a"])


async def test_a_job_suggestion_outside_the_allowed_folders_is_refused(router, tmp_path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    config = make_config(tmp_path, env_roots=[data])
    _job_routes(router)
    async with Client(build_server(config)) as client:
        await _finished_job(client, data, ["a"])
    ref = _weak_refs(data)["a"]
    other = tmp_path / "other"
    other.mkdir()
    async with Client(build_server(make_config(tmp_path, env_roots=[other]))) as client:
        res = await _suggest(client, ref, "CHEBI:1")
    assert res.is_error and "outside the folders" in res.content[0].text


async def test_store_files_are_private_and_never_hold_the_api_key(tmp_path) -> None:
    secret = "sk-very-secret-value"
    data = tmp_path / "data"
    data.mkdir()
    config = make_config(tmp_path, env_roots=[data], api_key=secret)
    with respx.mock(assert_all_called=False) as router:
        healthy_routes(router)
        _job_routes(router)
        router.post(f"{API}/map/entity").mock(
            return_value=httpx.Response(200, json=_cands(["CHEBI:1", "CHEBI:2"]))
        )
        async with Client(build_server(config)) as client:
            await _pins(client)
            await _finished_job(client, data, ["a"])
            ref = _weak_refs(data)["a"]
            _ok(await client.call_tool("get_candidates", {"refs": [ref]}))
            _ok(await _suggest(client, ref, "CHEBI:2"))
            m = _ok(await client.call_tool("map_entity", {"name": "z", "entity_type": ST}))
            _ok(await _suggest(client, m["result"]["ref"], "none"))
    files = [p for p in config.home.rglob("*") if p.is_file()]
    judgments = [p for p in files if "judgments" in p.parts]
    assert len(judgments) == 2
    for path in files:
        if path.name != "owner.lock":
            assert (path.stat().st_mode & 0o077) == 0, f"{path} is readable by others"
        assert secret not in path.read_text(errors="replace")
    for path in config.home.rglob("*"):
        if path.is_dir():
            assert (path.stat().st_mode & 0o777) == 0o700, path
    for path in data.rglob("*"):
        assert secret not in path.read_text(errors="replace")
