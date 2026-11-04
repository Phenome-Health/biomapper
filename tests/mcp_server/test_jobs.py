"""Unit 4: resumable background file-mapping jobs.

Exercised end to end through the in-memory MCP client with respx-mocked HTTP. Two "processes"
are two servers sharing one ``BIOMAPPER_MCP_HOME``: ``flock`` locks are per open file
description, so two servers in one test process contend for a job exactly as two OS processes do.
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import time
from pathlib import Path

import httpx
import pytest
import respx
from mcp import Client, types

from biomapper.mcp.server import build_server
from tests.mcp_server.mcp_helpers import (
    API,
    KESTREL,
    KG_BUILD,
    InFlight,
    batch_echo,
    batch_response,
    healthy_routes,
    make_config,
    resolved_entry,
)

ST = "biolink:SmallMolecule"


@pytest.fixture()
def router():
    with respx.mock(assert_all_called=False) as r:
        healthy_routes(r)
        yield r


@pytest.fixture()
def data(tmp_path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


def write_tsv(path: Path, names: list[str], extra: dict[str, list[str]] | None = None) -> Path:
    cols = ["name", *(extra or {})]
    with path.open("w", newline="") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n")
        w.writerow(cols)
        for i, n in enumerate(names):
            w.writerow([n, *((extra or {})[c][i] for c in (extra or {}))])
    return path


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def payload(res) -> dict:
    assert not res.is_error, res.content[0].text
    assert res.structured_content is not None
    return res.structured_content


async def status(client: Client, job_id: str) -> dict:
    out = payload(await client.call_tool("job_status", {"job_id": job_id}))
    assert len(out["jobs"]) == 1
    return out["jobs"][0]


async def wait_state(client: Client, job_id: str, *states: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        s = await status(client, job_id)
        if s["state"] in states:
            return s
        if time.monotonic() > deadline:
            raise AssertionError(f"job stayed {s['state']!r} ({s.get('reason')}), wanted {states}")
        await asyncio.sleep(0.02)


def cfg(tmp_path, data, **kw):
    kw.setdefault("env_roots", [data])
    return make_config(tmp_path, **kw)


async def submit(client: Client, path: Path, **kw) -> dict:
    args = {"path": str(path), "name_column": "name", "entity_type": ST, **kw}
    return payload(await client.call_tool("map_file", args))


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_a_12_row_file_completes_in_3_chunks_in_input_order(router, tmp_path, data) -> None:
    inflight = InFlight()
    router.post(f"{API}/map/batch").mock(side_effect=batch_echo(inflight))
    names = [f"metabolite-{i:02d}" for i in range(12)]
    src = write_tsv(data / "panel.tsv", names)
    async with Client(build_server(cfg(tmp_path, data))) as client:
        job = await submit(client, src)
        assert job["rows_total"] == 12
        assert job["output_path"] == str(data / "panel.biomapper.tsv")
        assert job["eta_s"] is not None
        done = await wait_state(client, job["job_id"], "complete")
    assert inflight.calls == 3 and inflight.peak == 1
    assert [len(b["entities"]) for b in inflight.bodies] == [5, 5, 2]
    out = read_tsv(data / "panel.biomapper.tsv")
    assert [r["name"] for r in out] == names
    assert list(out[0]) == [
        "key", "name", "identifiers_sent", "status", "chosen_kg_id", "primary_curie", "resolved",
        "confidence_score", "confidence_tier", "refusal_reason", "kg_equivalent_ids", "error",
    ]
    assert all(r["status"] == "resolved" for r in out)
    # Scored rows keep the client's tier in the file output (not_scored is unit-tested in
    # test_server_tools: only resolved rows WITHOUT a score change).
    assert {r["confidence_tier"] for r in out} == {"high"}
    assert not (data / "panel.biomapper.tsv.partial").exists()
    prov = json.loads((data / "panel.biomapper.provenance.json").read_text())
    assert prov["pins"]["kestrel"]["kg_build"]["kg_version"] == KG_BUILD["kg_version"]
    assert prov["input"]["file_name"] == "panel.tsv"
    assert str(data) not in json.dumps(prov), "outputs must not carry absolute paths"
    assert done["kg_verified"] is True
    assert done["entities_done"] == 12


async def test_unscored_resolved_rows_read_not_scored_in_the_output_tsv(
    router, tmp_path, data
) -> None:
    """ID-anchored rows: resolved, no annotator score. The TSV must say not_scored, not the
    client's 'unknown' (2026-11-04 live trials: agents read 'unknown' as a weak match)."""

    async def unscored(request: httpx.Request) -> httpx.Response:
        names = [e["name"] for e in json.loads(request.content)["entities"]]
        entries = [{**resolved_entry(n), "assigned_ids": {}} for n in names]
        return httpx.Response(200, json=batch_response(entries))

    router.post(f"{API}/map/batch").mock(side_effect=unscored)
    src = write_tsv(data / "ids.tsv", ["glucose", "lactate", "citrate"])
    async with Client(build_server(cfg(tmp_path, data))) as client:
        job = await submit(client, src)
        await wait_state(client, job["job_id"], "complete")
    out = read_tsv(data / "ids.biomapper.tsv")
    assert [r["status"] for r in out] == ["resolved"] * 3
    assert [r["confidence_score"] for r in out] == [""] * 3
    assert [r["confidence_tier"] for r in out] == ["not_scored"] * 3

async def test_map_file_returns_fast_while_the_worker_keeps_running(
    router, tmp_path, data
) -> None:
    inflight = InFlight()
    router.post(f"{API}/map/batch").mock(side_effect=batch_echo(inflight, delay=0.3))
    src = write_tsv(data / "p.tsv", [f"m{i}" for i in range(10)])
    async with Client(build_server(cfg(tmp_path, data))) as client:
        start = time.monotonic()
        job = await submit(client, src)
        assert time.monotonic() - start < 0.3
        assert job["state"] in ("queued", "running")
        mid = await wait_state(client, job["job_id"], "running")
        await asyncio.sleep(0.4)
        assert (data / "p.biomapper.tsv.partial").exists()
        assert not (data / "p.biomapper.tsv").exists()
        assert mid["state"] == "running"
        await wait_state(client, job["job_id"], "complete")
    assert (data / "p.biomapper.tsv").exists()


async def test_identifier_columns_are_sent_as_hints(router, tmp_path, data) -> None:
    inflight = InFlight()
    router.post(f"{API}/map/batch").mock(side_effect=batch_echo(inflight))
    src = write_tsv(data / "p.tsv", ["a", "b"], {"hmdb": ["HMDB0000177", ""]})
    async with Client(build_server(cfg(tmp_path, data))) as client:
        job = await submit(client, src, identifier_columns={"hmdb": "HMDB"})
        await wait_state(client, job["job_id"], "complete")
    sent = inflight.bodies[0]["entities"]
    assert sent[0]["identifiers"] == {"HMDB": "HMDB0000177"}
    assert sent[1]["identifiers"] == {}
    out = read_tsv(data / "p.biomapper.tsv")
    assert json.loads(out[0]["identifiers_sent"]) == {"HMDB": "HMDB0000177"}


async def test_duplicate_names_are_mapped_once(router, tmp_path, data) -> None:
    inflight = InFlight()
    router.post(f"{API}/map/batch").mock(side_effect=batch_echo(inflight))
    src = write_tsv(data / "p.tsv", ["a", "b", "a", "a"])
    async with Client(build_server(cfg(tmp_path, data))) as client:
        job = await submit(client, src)
        await wait_state(client, job["job_id"], "complete")
    assert [e["name"] for e in inflight.bodies[0]["entities"]] == ["a", "b"]
    assert [r["name"] for r in read_tsv(data / "p.biomapper.tsv")] == ["a", "b", "a", "a"]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


async def test_resubmitting_returns_the_existing_job(router, tmp_path, data) -> None:
    router.post(f"{API}/map/batch").mock(side_effect=batch_echo(InFlight()))
    src = write_tsv(data / "p.tsv", ["a", "b"])
    async with Client(build_server(cfg(tmp_path, data))) as client:
        first = await submit(client, src)
        second = await submit(client, src)
        assert first["job_id"] == second["job_id"]
        await wait_state(client, first["job_id"], "complete")
        third = await submit(client, src)  # completed output exists; still returns the job
        assert third["job_id"] == first["job_id"] and third["state"] == "complete"
        listing = payload(await client.call_tool("job_status", {}))
    assert len(listing["jobs"]) == 1


async def test_existing_output_is_refused_and_nothing_is_queued(router, tmp_path, data) -> None:
    route = router.post(f"{API}/map/batch")
    src = write_tsv(data / "p.tsv", ["a"])
    (data / "p.biomapper.tsv").write_text("someone else's file\n")
    async with Client(build_server(cfg(tmp_path, data))) as client:
        res = await client.call_tool("map_file", {"path": str(src), "name_column": "name",
                                                  "entity_type": ST})
        listing = payload(await client.call_tool("job_status", {}))
    assert res.is_error and "exists" in res.content[0].text
    assert listing["jobs"] == [] and route.call_count == 0


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
async def test_read_only_output_directory_is_refused(router, tmp_path, data) -> None:
    src = write_tsv(data / "p.tsv", ["a"])
    ro = data / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        async with Client(build_server(cfg(tmp_path, data))) as client:
            res = await client.call_tool("map_file", {
                "path": str(src), "name_column": "name", "entity_type": ST,
                "output_path": str(ro / "out.tsv"),
            })
    finally:
        ro.chmod(0o700)
    assert res.is_error and "writable" in res.content[0].text


@pytest.mark.parametrize("content, needle", [
    ("label\nglucose\n", "name"),            # missing name column
    ("", "no data rows"),                    # empty file
    ("# only\n# comments\n", "no data rows"),
    ("name\n\n", "no data rows"),            # header only
])
async def test_bad_inputs_get_clear_validation_errors(router, tmp_path, data, content,
                                                      needle) -> None:
    src = data / "bad.tsv"
    src.write_text(content)
    async with Client(build_server(cfg(tmp_path, data))) as client:
        res = await client.call_tool("map_file", {"path": str(src), "name_column": "name",
                                                  "entity_type": ST})
    assert res.is_error and needle in res.content[0].text


async def test_missing_identifier_column_is_refused(router, tmp_path, data) -> None:
    src = write_tsv(data / "p.tsv", ["a"])
    async with Client(build_server(cfg(tmp_path, data))) as client:
        res = await client.call_tool("map_file", {
            "path": str(src), "name_column": "name", "entity_type": ST,
            "identifier_columns": {"hmdb": "HMDB"},
        })
    assert res.is_error and "hmdb" in res.content[0].text


# The startup pin probe reaches the same mocked /health from a worker thread (sync httpx), where the
# async side effect is never awaited; that probe only records "unreachable", which is fine here.
@pytest.mark.filterwarnings("ignore:coroutine .* was never awaited")
async def test_slow_health_probe_refuses_and_queues_nothing(tmp_path, data) -> None:
    async def slow(request):
        await asyncio.sleep(5)
        return httpx.Response(200, json={"status": "healthy"})  # pragma: no cover

    src = write_tsv(data / "p.tsv", ["a"])
    with respx.mock(assert_all_called=False) as r:
        r.get(f"{KESTREL}/health").mock(return_value=httpx.Response(200, json={}))
        r.get(f"{API}/health").mock(side_effect=slow)
        async with Client(build_server(cfg(tmp_path, data, health_timeout_s=0.2))) as client:
            res = await client.call_tool("map_file", {"path": str(src), "name_column": "name",
                                                      "entity_type": ST})
            listing = payload(await client.call_tool("job_status", {}))
    assert res.is_error and "slow" in res.content[0].text.lower()
    assert listing["jobs"] == []


# ---------------------------------------------------------------------------
# Roots / path allow-list
# ---------------------------------------------------------------------------


async def test_input_outside_the_roots_is_refused_before_reading(router, tmp_path, data) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    src = write_tsv(outside / "p.tsv", ["a"])
    async with Client(build_server(cfg(tmp_path, data))) as client:
        res = await client.call_tool("map_file", {"path": str(src), "name_column": "name",
                                                  "entity_type": ST})
    assert res.is_error and "outside" in res.content[0].text


async def test_a_symlink_inside_a_root_pointing_outside_is_refused(router, tmp_path, data) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    real = write_tsv(outside / "secret.tsv", ["a"])
    link = data / "innocent.tsv"
    link.symlink_to(real)
    async with Client(build_server(cfg(tmp_path, data))) as client:
        res = await client.call_tool("map_file", {"path": str(link), "name_column": "name",
                                                  "entity_type": ST})
    assert res.is_error and "outside" in res.content[0].text


async def test_output_outside_the_roots_is_refused(router, tmp_path, data) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    src = write_tsv(data / "p.tsv", ["a"])
    async with Client(build_server(cfg(tmp_path, data))) as client:
        res = await client.call_tool("map_file", {
            "path": str(src), "name_column": "name", "entity_type": ST,
            "output_path": str(outside / "out.tsv"),
        })
    assert res.is_error and "outside" in res.content[0].text


async def test_no_roots_refuses_map_file_with_setup_guidance(router, tmp_path, data) -> None:
    router.post(f"{API}/map/entity").mock(return_value=httpx.Response(200, json={
        "result": {"name": "a", "curies": ["X:1"], "chosen_kg_id": "X:1"}}))
    src = write_tsv(data / "p.tsv", ["a"])
    async with Client(build_server(cfg(tmp_path, data, env_roots=[]))) as client:
        res = await client.call_tool("map_file", {"path": str(src), "name_column": "name",
                                                  "entity_type": ST})
        ok = await client.call_tool("map_entity", {"name": "a", "entity_type": ST})
    assert res.is_error and "BIOMAPPER_MCP_ROOTS" in res.content[0].text
    assert not ok.is_error


async def test_client_roots_are_honored(router, tmp_path, data) -> None:
    router.post(f"{API}/map/batch").mock(side_effect=batch_echo(InFlight()))
    src = write_tsv(data / "p.tsv", ["a"])

    async def roots_cb(context):
        return types.ListRootsResult(roots=[types.Root(uri=data.as_uri())])

    server = build_server(cfg(tmp_path, data, env_roots=[]))
    async with Client(server, list_roots_callback=roots_cb) as client:
        job = await submit(client, src)
        await wait_state(client, job["job_id"], "complete")


# ---------------------------------------------------------------------------
# Failure handling: stalls, attempts, input changes
# ---------------------------------------------------------------------------


async def test_three_systemic_chunks_stall_then_resume_continues(router, tmp_path, data) -> None:
    calls = {"n": 0}
    inflight = InFlight()
    echo = batch_echo(inflight)

    async def flaky(request):
        calls["n"] += 1
        if calls["n"] <= 3:
            return httpx.Response(503, text="overloaded")
        return await echo(request)

    router.post(f"{API}/map/batch").mock(side_effect=flaky)
    src = write_tsv(data / "p.tsv", [f"m{i}" for i in range(15)])
    async with Client(build_server(cfg(tmp_path, data))) as client:
        job = await submit(client, src)
        stalled = await wait_state(client, job["job_id"], "stalled")
        await asyncio.sleep(0.1)
        assert calls["n"] == 3, "a stalled job must stop sending requests"
        assert "503" in stalled["reason"]
        resumed = payload(await client.call_tool("resume_job", {"job_id": job["job_id"]}))
        assert resumed["state"] in ("queued", "running")
        await wait_state(client, job["job_id"], "complete")
    assert calls["n"] == 3 + 3
    assert all(r["status"] == "resolved" for r in read_tsv(data / "p.biomapper.tsv"))


async def test_a_very_slow_chunk_stalls_the_job(router, tmp_path, data) -> None:
    router.post(f"{API}/map/batch").mock(side_effect=batch_echo(InFlight(), delay=0.2))
    src = write_tsv(data / "p.tsv", [f"m{i}" for i in range(10)])
    async with Client(build_server(cfg(tmp_path, data, stall_chunk_s=0.1))) as client:
        job = await submit(client, src)
        s = await wait_state(client, job["job_id"], "stalled")
    assert "slow" in s["reason"].lower()


async def test_a_row_that_always_errors_is_written_as_error_after_3_attempts(
    router, tmp_path, data
) -> None:
    inflight = InFlight()

    async def handler(request):
        await inflight.enter(request)
        names = [e["name"] for e in json.loads(request.content)["entities"]]
        return httpx.Response(200, json=batch_response([
            {"name": n, "error": "unparseable name"} if n == "bad" else resolved_entry(n)
            for n in names
        ]))

    router.post(f"{API}/map/batch").mock(side_effect=handler)
    src = write_tsv(data / "p.tsv", ["good", "bad", "fine"])
    async with Client(build_server(cfg(tmp_path, data))) as client:
        job = await submit(client, src)
        done = await wait_state(client, job["job_id"], "complete")
    sent_bad = sum(1 for b in inflight.bodies for e in b["entities"] if e["name"] == "bad")
    assert sent_bad == 3
    rows = {r["name"]: r for r in read_tsv(data / "p.biomapper.tsv")}
    assert rows["bad"]["status"] == "error" and "unparseable" in rows["bad"]["error"]
    assert rows["good"]["status"] == "resolved"
    assert done["entities_failed"] == 1


async def test_attempts_survive_a_restart(router, tmp_path, data) -> None:
    """Process 1 makes ONE attempt at the bad row and dies; process 2 makes only the other two."""
    inflight = InFlight()
    block = asyncio.Event()

    async def per_row(request):
        names = [e["name"] for e in json.loads(request.content)["entities"]]
        if inflight.calls >= 1 and not block.is_set():
            await asyncio.sleep(30)  # process 1 dies while this second attempt is in flight
        await inflight.enter(request)
        return httpx.Response(200, json=batch_response([
            {"name": n, "error": "nope"} if n == "bad" else resolved_entry(n) for n in names]))

    router.post(f"{API}/map/batch").mock(side_effect=per_row)
    src = write_tsv(data / "p.tsv", ["ok", "bad"])
    config = cfg(tmp_path, data, max_row_attempts=3)
    job_file = None
    async with Client(build_server(config)) as client:
        job = await submit(client, src)
        job_file = config.home / "jobs" / job["job_id"] / "job.json"
        for _ in range(500):
            if json.loads(job_file.read_text())["attempts"]:
                break
            await asyncio.sleep(0.01)
    assert sum(json.loads(job_file.read_text())["attempts"].values()) == 1
    block.set()
    async with Client(build_server(config)) as client:
        done = await wait_state(client, job["job_id"], "complete")
    sent_bad = sum(1 for b in inflight.bodies for e in b["entities"] if e["name"] == "bad")
    assert sent_bad == 3, "a restart must not reset the attempt count"
    assert done["entities_failed"] == 1


async def test_a_systemic_429_chunk_does_not_count_against_rows(router, tmp_path, data) -> None:
    calls = {"n": 0}
    echo = batch_echo(InFlight())

    async def rate_limited_twice(request):
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return await echo(request)

    router.post(f"{API}/map/batch").mock(side_effect=rate_limited_twice)
    src = write_tsv(data / "p.tsv", ["a", "b"])
    config = cfg(tmp_path, data, max_row_attempts=1)
    async with Client(build_server(config)) as client:
        job = await submit(client, src)
        await wait_state(client, job["job_id"], "complete")
    j = json.loads((config.home / "jobs" / job["job_id"] / "job.json").read_text())
    assert j["attempts"] == {}
    assert all(r["status"] == "resolved" for r in read_tsv(data / "p.biomapper.tsv"))


async def test_a_modified_input_fails_the_job_on_resume(router, tmp_path, data) -> None:
    router.post(f"{API}/map/batch").mock(return_value=httpx.Response(503))
    src = write_tsv(data / "p.tsv", [f"m{i}" for i in range(6)])
    async with Client(build_server(cfg(tmp_path, data))) as client:
        job = await submit(client, src)
        await wait_state(client, job["job_id"], "stalled")
        write_tsv(src, ["something", "else"])
        res = await client.call_tool("resume_job", {"job_id": job["job_id"]})
        s = await wait_state(client, job["job_id"], "failed")
    assert "input changed" in s["reason"].lower() or (
        res.is_error and "input changed" in res.content[0].text.lower()
    )


# ---------------------------------------------------------------------------
# Cancel / rerun / output path
# ---------------------------------------------------------------------------


async def test_cancel_stops_after_the_current_chunk(router, tmp_path, data) -> None:
    inflight = InFlight()
    router.post(f"{API}/map/batch").mock(side_effect=batch_echo(inflight, delay=0.2))
    src = write_tsv(data / "p.tsv", [f"m{i}" for i in range(30)])
    async with Client(build_server(cfg(tmp_path, data))) as client:
        job = await submit(client, src)
        await wait_state(client, job["job_id"], "running")
        await asyncio.sleep(0.05)
        payload(await client.call_tool("cancel_job", {"job_id": job["job_id"]}))
        calls_at_cancel = inflight.calls
        await asyncio.sleep(0.6)
        s = await status(client, job["job_id"])
    assert s["state"] == "cancelled"
    assert inflight.calls <= calls_at_cancel + 0, "no request may start after cancel"
    assert (data / "p.biomapper.tsv.partial").exists()


async def test_rerun_and_new_output_path_create_fresh_jobs(router, tmp_path, data) -> None:
    inflight = InFlight()
    router.post(f"{API}/map/batch").mock(side_effect=batch_echo(inflight))
    src = write_tsv(data / "p.tsv", ["a", "b"])
    async with Client(build_server(cfg(tmp_path, data))) as client:
        first = await submit(client, src)
        await wait_state(client, first["job_id"], "complete")
        other = await submit(client, src, output_path=str(data / "other.tsv"))
        assert other["job_id"] != first["job_id"]
        await wait_state(client, other["job_id"], "complete")
        calls_before = inflight.calls
        (data / "other.tsv").unlink()
        (data / "other.provenance.json").unlink()
        rerun = await submit(client, src, output_path=str(data / "other.tsv"), rerun=True)
        await wait_state(client, rerun["job_id"], "complete")
    assert inflight.calls == calls_before + 1, "rerun must re-map instead of reusing the cache"


async def test_rerun_refuses_to_overwrite_a_finished_output(router, tmp_path, data) -> None:
    router.post(f"{API}/map/batch").mock(side_effect=batch_echo(InFlight()))
    src = write_tsv(data / "p.tsv", ["a"])
    async with Client(build_server(cfg(tmp_path, data))) as client:
        job = await submit(client, src)
        await wait_state(client, job["job_id"], "complete")
        res = await client.call_tool("map_file", {"path": str(src), "name_column": "name",
                                                  "entity_type": ST, "rerun": True})
    assert res.is_error and "exists" in res.content[0].text


# ---------------------------------------------------------------------------
# Cross-process ownership and resume
# ---------------------------------------------------------------------------


async def test_a_job_owned_by_another_process_is_reported_and_left_alone(
    router, tmp_path, data
) -> None:
    gate = asyncio.Event()
    inflight = InFlight()
    echo = batch_echo(inflight)

    async def held(request):
        if inflight.calls >= 1:
            await gate.wait()
        return await echo(request)

    router.post(f"{API}/map/batch").mock(side_effect=held)
    src = write_tsv(data / "p.tsv", [f"m{i}" for i in range(20)])
    config = cfg(tmp_path, data)
    async with Client(build_server(config)) as a:
        job = await submit(a, src)
        await wait_state(a, job["job_id"], "running")
        while inflight.calls < 1:
            await asyncio.sleep(0.01)
        async with Client(build_server(config)) as b:
            s = await status(b, job["job_id"])
            assert s["state"] == "owned_elsewhere"
            assert s["owner"]["pid"] == os.getpid()
            res = await b.call_tool("resume_job", {"job_id": job["job_id"]})
            assert res.is_error and "another" in res.content[0].text.lower()
        gate.set()
        # Simulated process A exit happens when this block ends mid-job.
        await asyncio.sleep(0)
    cached_before = _cached_count(config, job["job_id"])
    calls_before = inflight.calls
    async with Client(build_server(config)) as b:
        await wait_state(b, job["job_id"], "complete")
    remaining = 20 - cached_before
    assert inflight.calls - calls_before == -(-remaining // 5)
    assert [r["name"] for r in read_tsv(data / "p.biomapper.tsv")] == [f"m{i}" for i in
                                                                        range(20)]


def _cached_count(config, job_id: str) -> int:
    path = config.home / "jobs" / job_id / "entity_cache.jsonl"
    n = 0
    for line in path.read_text().splitlines():
        rec = json.loads(line)
        if rec.get("type") == "batch":
            n += len(rec["entries"])
    return n


async def test_a_paused_job_is_returned_on_resubmit_not_refused(router, tmp_path, data) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    echo = batch_echo(InFlight())

    async def handler(request):
        if started.is_set():
            await release.wait()
        started.set()
        return await echo(request)

    router.post(f"{API}/map/batch").mock(side_effect=handler)
    src = write_tsv(data / "p.tsv", [f"m{i}" for i in range(10)])
    config = cfg(tmp_path, data)
    async with Client(build_server(config)) as a:
        job = await submit(a, src)
        await started.wait()
        await asyncio.sleep(0.05)
    assert (data / "p.biomapper.tsv.partial").exists()
    release.set()
    async with Client(build_server(config, )) as b:
        again = await submit(b, src)
        assert again["job_id"] == job["job_id"]
        await wait_state(b, job["job_id"], "complete")


async def test_a_torn_cache_line_is_truncated_and_only_that_work_is_redone(
    router, tmp_path, data
) -> None:
    inflight = InFlight()
    router.post(f"{API}/map/batch").mock(side_effect=batch_echo(inflight))
    src = write_tsv(data / "p.tsv", [f"m{i}" for i in range(10)])
    config = cfg(tmp_path, data)
    async with Client(build_server(config)) as client:
        job = await submit(client, src)
        await wait_state(client, job["job_id"], "complete")
    jdir = config.home / "jobs" / job["job_id"]
    # Rewind: drop the last committed chunk and leave a torn half-line in its place, and mark the
    # job as if the process had died mid-run.
    cache = jdir / "entity_cache.jsonl"
    lines = cache.read_text().splitlines(keepends=True)
    cache.write_text("".join(lines[:-1]) + lines[-1][: len(lines[-1]) // 2])
    j = json.loads((jdir / "job.json").read_text())
    j["state"] = "running"
    (jdir / "job.json").write_text(json.dumps(j))
    (data / "p.biomapper.tsv").unlink()
    (data / "p.biomapper.provenance.json").unlink()
    calls_before = inflight.calls
    async with Client(build_server(config)) as client:
        await wait_state(client, job["job_id"], "complete")
    assert inflight.calls - calls_before == 1
    assert [len(e["entities"]) for e in inflight.bodies[-1:]] == [5]


async def test_an_unverified_kg_cache_is_discarded_in_a_new_process(tmp_path, data) -> None:
    inflight = InFlight()
    src = write_tsv(data / "p.tsv", [f"m{i}" for i in range(10)])
    config = cfg(tmp_path, data)
    release = asyncio.Event()
    echo = batch_echo(inflight)

    async def handler(request):
        if inflight.calls >= 1:
            await release.wait()
        return await echo(request)

    with respx.mock(assert_all_called=False) as r:
        r.get(f"{API}/health").mock(return_value=httpx.Response(200, json={"status": "ok"}))
        r.get(f"{KESTREL}/health").mock(side_effect=httpx.ConnectError("kestrel down"))
        r.post(f"{API}/map/batch").mock(side_effect=handler)
        async with Client(build_server(config)) as a:
            job = await submit(a, src)
            s = await wait_state(a, job["job_id"], "running")
            assert s["kg_verified"] is False
            while inflight.calls < 1:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
        assert _cached_count(config, job["job_id"]) == 5
        release.set()
        calls_before = inflight.calls
        async with Client(build_server(config)) as b:
            done = await wait_state(b, job["job_id"], "complete")
    assert inflight.calls - calls_before == 2, "the unverified cache must be re-mapped from row 0"
    assert "unverified" in (done["reason"] or "").lower()
    assert done["kg_verified"] is False


async def test_a_changed_kg_build_fails_the_job_with_a_rerun_hint(tmp_path, data) -> None:
    src = write_tsv(data / "p.tsv", [f"m{i}" for i in range(10)])
    config = cfg(tmp_path, data)
    inflight = InFlight()
    release = asyncio.Event()
    echo = batch_echo(inflight)

    async def handler(request):
        if inflight.calls >= 1:
            await release.wait()
        return await echo(request)

    with respx.mock(assert_all_called=False) as r:
        healthy_routes(r)
        r.post(f"{API}/map/batch").mock(side_effect=handler)
        async with Client(build_server(config)) as a:
            job = await submit(a, src)
            while inflight.calls < 1:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
    release.set()
    with respx.mock(assert_all_called=False) as r:
        r.get(f"{API}/health").mock(return_value=httpx.Response(200, json={"status": "ok"}))
        r.get(f"{KESTREL}/health").mock(return_value=httpx.Response(200, json={
            "kestrel_version": "1.9", "kg_build": {**KG_BUILD, "kg_version": "2.4.0"}}))
        r.post(f"{API}/map/batch").mock(side_effect=echo)
        async with Client(build_server(config)) as b:
            s = await wait_state(b, job["job_id"], "failed")
    assert "rerun=true" in s["reason"]


async def test_auto_resume_skips_jobs_outside_roots_and_expires_old_ones(
    router, tmp_path, data
) -> None:
    release = asyncio.Event()
    echo = batch_echo(InFlight())
    started = asyncio.Event()

    async def handler(request):
        started.set()
        await release.wait()
        return await echo(request)

    router.post(f"{API}/map/batch").mock(side_effect=handler)
    other = tmp_path / "other"
    other.mkdir()
    src_a = write_tsv(data / "a.tsv", ["x"])
    src_b = write_tsv(other / "b.tsv", ["y"])
    now = [time.time()]
    config = cfg(tmp_path, data, env_roots=[data, other], wall_clock=lambda: now[0])
    async with Client(build_server(config)) as a:
        ja = await submit(a, src_a)
        jb = await submit(a, src_b)
        await started.wait()
    # Next session: only `data` is a root, and a week and a day have passed.
    now[0] += 8 * 24 * 3600
    release.set()
    config2 = cfg(tmp_path, data, env_roots=[data], wall_clock=lambda: now[0])
    async with Client(build_server(config2)) as b:
        await asyncio.sleep(0.2)
        sa = await status(b, ja["job_id"])
        assert sa["state"] == "expired"
        listing = payload(await b.call_tool("job_status", {}))
        assert [j["job_id"] for j in listing["jobs"]] == [ja["job_id"]]
        res = await b.call_tool("job_status", {"job_id": jb["job_id"]})
        assert res.is_error
        payload(await b.call_tool("resume_job", {"job_id": ja["job_id"]}))
        await wait_state(b, ja["job_id"], "complete")
    jb_state = json.loads((config.home / "jobs" / jb["job_id"] / "job.json").read_text())["state"]
    assert jb_state != "expired", "a job outside the current roots is not touched"


# ---------------------------------------------------------------------------
# Interleaving, secrets, permissions
# ---------------------------------------------------------------------------


async def test_interactive_calls_interleave_with_a_running_job(router, tmp_path, data) -> None:
    inflight = InFlight()
    router.post(f"{API}/map/batch").mock(side_effect=batch_echo(inflight, delay=0.1))

    async def entity(request):
        await inflight.enter(request, delay=0.05)
        return httpx.Response(200, json={"result": resolved_entry("x"), "metadata": {}})

    router.post(f"{API}/map/entity").mock(side_effect=entity)
    src = write_tsv(data / "p.tsv", [f"m{i}" for i in range(25)])
    async with Client(build_server(cfg(tmp_path, data))) as client:
        job = await submit(client, src)
        await wait_state(client, job["job_id"], "running")
        start = time.monotonic()
        res = await client.call_tool("map_entity", {"name": "x", "entity_type": ST})
        assert time.monotonic() - start < 0.5, "an interactive call waits at most one chunk"
        assert not res.is_error
        s = await status(client, job["job_id"])
        assert s["state"] == "running", "the job was still running when the call finished"
        await wait_state(client, job["job_id"], "complete")
    assert inflight.peak == 1


async def test_a_429_job_chunk_does_not_hold_the_gate_during_backoff(
    router, tmp_path, data
) -> None:
    calls = {"n": 0}
    echo = batch_echo(InFlight())

    async def limited(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "60"})
        return await echo(request)

    router.post(f"{API}/map/batch").mock(side_effect=limited)
    router.post(f"{API}/map/entity").mock(
        return_value=httpx.Response(200, json={"result": resolved_entry("x"), "metadata": {}})
    )
    src = write_tsv(data / "p.tsv", ["a", "b"])
    async with Client(build_server(cfg(tmp_path, data, systemic_backoff_s=5.0))) as client:
        await submit(client, src)
        while calls["n"] < 1:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        start = time.monotonic()
        res = await client.call_tool("map_entity", {"name": "x", "entity_type": ST})
        assert time.monotonic() - start < 1.0
    assert not res.is_error


async def test_the_api_key_never_reaches_job_files_or_results(tmp_path, data) -> None:
    secret = "sk-very-secret-value"
    src = write_tsv(data / "p.tsv", ["a", "b"])
    config = cfg(tmp_path, data, api_key=secret)
    texts: list[str] = []
    with respx.mock(assert_all_called=False) as r:
        healthy_routes(r)

        async def failing(request):
            assert request.headers["X-API-Key"] == secret
            return httpx.Response(500, text="boom")

        r.post(f"{API}/map/batch").mock(side_effect=batch_echo(InFlight()))
        r.post(f"{API}/map/entity").mock(side_effect=failing)
        async with Client(build_server(config)) as client:
            job = await submit(client, src)
            texts.append(json.dumps(job))
            await wait_state(client, job["job_id"], "complete")
            for res in [
                await client.call_tool("job_status", {}),
                await client.call_tool("map_entity", {"name": "a", "entity_type": ST}),
                await client.call_tool("server_info", {}),
            ]:
                texts.extend(c.text for c in res.content)
    for path in [*config.home.rglob("*"), *data.rglob("*")]:
        if path.is_file():
            texts.append(path.read_text(errors="replace"))
    assert texts and not any(secret in t for t in texts)


async def test_job_state_is_private_to_the_user(router, tmp_path, data) -> None:
    router.post(f"{API}/map/batch").mock(side_effect=batch_echo(InFlight()))
    src = write_tsv(data / "p.tsv", ["a"])
    config = cfg(tmp_path, data)
    async with Client(build_server(config)) as client:
        job = await submit(client, src)
        await wait_state(client, job["job_id"], "complete")
    jdir = config.home / "jobs" / job["job_id"]
    assert (config.home.stat().st_mode & 0o777) == 0o700
    assert (jdir.stat().st_mode & 0o777) == 0o700
    for f in jdir.iterdir():
        assert (f.stat().st_mode & 0o077) == 0, f"{f.name} is readable by others"


def test_job_lock_is_released_by_the_os_when_the_owner_process_dies(tmp_path) -> None:
    """A real second process, not a simulation: the OS frees the lock when it exits."""
    import subprocess
    import sys

    from biomapper.mcp.jobs import JobLock

    code = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from biomapper.mcp.jobs import JobLock\n"
        "lock = JobLock(Path(sys.argv[1]))\n"
        "assert lock.acquire()\n"
        "print('locked', flush=True)\n"
        "sys.stdin.readline()\n"
    )
    child = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "locked"
        mine = JobLock(tmp_path)
        assert not mine.acquire()
        assert mine.owner().pid == child.pid
    finally:
        child.kill()
        child.wait()
    assert mine.acquire()
    assert mine.owner().pid == os.getpid()
    mine.release()


# ---------------------------------------------------------------------------
# Review round: worker survival, rerun/cancel races, path and ID hardening
# ---------------------------------------------------------------------------


async def test_an_output_appearing_mid_job_fails_only_that_job(router, tmp_path, data) -> None:
    release = asyncio.Event()
    started = asyncio.Event()
    echo = batch_echo(InFlight())

    async def held(request):
        started.set()
        await release.wait()
        return await echo(request)

    router.post(f"{API}/map/batch").mock(side_effect=held)
    first = write_tsv(data / "p.tsv", ["a"])
    second = write_tsv(data / "q.tsv", ["b"])
    async with Client(build_server(cfg(tmp_path, data))) as client:
        job = await submit(client, first)
        await started.wait()
        (data / "p.biomapper.tsv").write_text("someone else's file\n")
        release.set()
        s = await wait_state(client, job["job_id"], "failed")
        assert "not overwritten" in s["reason"]
        other = await submit(client, second)
        await wait_state(client, other["job_id"], "complete")
    assert (data / "p.biomapper.tsv").read_text() == "someone else's file\n"


async def test_rerun_of_a_job_still_running_here_is_refused(router, tmp_path, data) -> None:
    release = asyncio.Event()
    started = asyncio.Event()
    echo = batch_echo(InFlight())

    async def held(request):
        started.set()
        await release.wait()
        return await echo(request)

    router.post(f"{API}/map/batch").mock(side_effect=held)
    src = write_tsv(data / "p.tsv", ["a", "b"])
    async with Client(build_server(cfg(tmp_path, data))) as client:
        job = await submit(client, src)
        await started.wait()
        res = await client.call_tool("map_file", {"path": str(src), "name_column": "name",
                                                  "entity_type": ST, "rerun": True})
        assert res.is_error and "still running" in res.content[0].text
        release.set()
        await wait_state(client, job["job_id"], "complete")


async def test_a_cancel_during_the_pin_fetch_is_not_lost(tmp_path, data) -> None:
    inflight = InFlight()
    with respx.mock(assert_all_called=False) as r:
        healthy_routes(r)
        kestrel_hits = {"n": 0}

        def slow_kestrel(request):
            kestrel_hits["n"] += 1
            if kestrel_hits["n"] > 1:  # the job's pin fetch, not the startup probe
                time.sleep(0.5)
            return httpx.Response(200, json={"kestrel_version": "1.9", "kg_build": KG_BUILD})

        r.get(f"{KESTREL}/health").mock(side_effect=slow_kestrel)
        r.post(f"{API}/map/batch").mock(side_effect=batch_echo(inflight))
        src = write_tsv(data / "p.tsv", ["a", "b"])
        async with Client(build_server(cfg(tmp_path, data))) as client:
            await asyncio.sleep(0.1)  # let the startup probe land
            job = await submit(client, src)
            while kestrel_hits["n"] < 2:
                await asyncio.sleep(0.01)
            payload(await client.call_tool("cancel_job", {"job_id": job["job_id"]}))
            await asyncio.sleep(0.8)
            s = await status(client, job["job_id"])
    assert s["state"] == "cancelled"
    assert inflight.calls == 0, "a cancelled job must not start mapping"


async def test_resume_refuses_a_job_whose_output_left_the_roots(tmp_path, data) -> None:
    with respx.mock(assert_all_called=False) as r:
        healthy_routes(r)
        r.post(f"{API}/map/batch").mock(return_value=httpx.Response(503))
        out_dir = tmp_path / "outdir"
        out_dir.mkdir()
        src = write_tsv(data / "p.tsv", [f"m{i}" for i in range(6)])
        config = cfg(tmp_path, data, env_roots=[data, out_dir])
        async with Client(build_server(config)) as a:
            job = await submit(a, src, output_path=str(out_dir / "o.tsv"))
            await wait_state(a, job["job_id"], "stalled")
        # Next session: only the input folder is allowed.
        r.post(f"{API}/map/batch").mock(side_effect=batch_echo(InFlight()))
        async with Client(build_server(cfg(tmp_path, data, env_roots=[data]))) as b:
            res = await b.call_tool("resume_job", {"job_id": job["job_id"]})
            assert res.is_error and "outside" in res.content[0].text
            listing = payload(await b.call_tool("job_status", {}))
            assert listing["jobs"] == []
            res = await b.call_tool("cancel_job", {"job_id": job["job_id"]})
            assert res.is_error and "outside" in res.content[0].text
    assert not (out_dir / "o.tsv").exists()


async def test_a_leftover_or_linked_partial_file_is_refused(router, tmp_path, data) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("")
    src = write_tsv(data / "p.tsv", ["a"])
    (data / "p.biomapper.tsv.partial").symlink_to(outside)
    async with Client(build_server(cfg(tmp_path, data))) as client:
        res = await client.call_tool("map_file", {"path": str(src), "name_column": "name",
                                                  "entity_type": ST})
    assert res.is_error and "partial" in res.content[0].text
    assert outside.read_text() == ""


async def test_an_existing_provenance_file_is_refused(router, tmp_path, data) -> None:
    src = write_tsv(data / "p.tsv", ["a"])
    (data / "p.biomapper.provenance.json").write_text("{}")
    async with Client(build_server(cfg(tmp_path, data))) as client:
        res = await client.call_tool("map_file", {"path": str(src), "name_column": "name",
                                                  "entity_type": ST})
    assert res.is_error and "provenance" in res.content[0].text
    assert (data / "p.biomapper.provenance.json").read_text() == "{}"


async def test_cancel_job_without_allowed_folders_is_refused(router, tmp_path, data) -> None:
    router.post(f"{API}/map/batch").mock(return_value=httpx.Response(503))
    src = write_tsv(data / "p.tsv", [f"m{i}" for i in range(6)])
    config = cfg(tmp_path, data)
    async with Client(build_server(config)) as a:
        job = await submit(a, src)
        await wait_state(a, job["job_id"], "stalled")
    async with Client(build_server(cfg(tmp_path, data, env_roots=[]))) as b:
        res = await b.call_tool("cancel_job", {"job_id": job["job_id"]})
    assert res.is_error and str(data) not in res.content[0].text


@pytest.mark.parametrize("bad", ["../jobs/x", "/etc", "ABCDEF0123456789", "0123", "a" * 17])
async def test_job_ids_that_are_not_generated_ids_are_refused(router, tmp_path, data, bad) -> None:
    async with Client(build_server(cfg(tmp_path, data))) as client:
        for tool in ("job_status", "resume_job", "cancel_job"):
            res = await client.call_tool(tool, {"job_id": bad})
            assert res.is_error and "not a job ID" in res.content[0].text, tool


async def test_the_api_health_probe_waits_for_the_request_gate(tmp_path) -> None:
    from biomapper.mcp.gate import RequestGate
    from biomapper.mcp.jobs import probe_pins

    # A host of its own: abandoned startup-probe threads from earlier tests hit API/KESTREL.
    api = "https://gated-probe.test/api/v1"
    with respx.mock(assert_all_called=False) as r:
        healthy_routes(r)
        api_route = r.get(f"{api}/health").mock(
            return_value=httpx.Response(200, json={"status": "healthy", "version": "0.1.0"})
        )
        gate = RequestGate()
        config = make_config(tmp_path, base_url=api)
        async with gate.hold(wait_budget_s=None):
            task = asyncio.create_task(probe_pins(config, gate))
            await asyncio.sleep(0.2)
            assert not api_route.called, "the API probe must not run while the gate is held"
        pins = await asyncio.wait_for(task, 5)
    assert api_route.call_count == 1
    assert pins["api"]["status"] == "healthy"
