"""Harmonize jobs: ``harmonize_cohorts()`` / ``harmonize_many()`` as resumable background jobs.

End to end through the in-memory MCP client with respx-mocked HTTP, like ``test_jobs.py``. The
first block adapts the six lifecycle failures Greptile found in the ``map_file`` job code
(``docs/solutions/best-practices/mcp-server-file-jobs-review-gap-2026-11-03.md``) to harmonize
jobs; they were written before the runner.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import time
from pathlib import Path

import anyio
import httpx
import pytest
import respx
from mcp import Client

from biomapper.cohorts import harmonize_cohorts
from biomapper.many_cohorts import GRADE_ITERATION, GRADE_PUBLISHABLE, harmonize_many
from biomapper.mcp.server import build_server
from tests.mcp_server.harmonize_helpers import (
    ST,
    Scripted,
    cfg,
    error_text,
    hstatus,
    node,
    payload,
    report_files,
    spec,
    wait_h,
    wait_until,
    write_cohort,
)
from tests.mcp_server.mcp_helpers import API, KESTREL, KG_BUILD, InFlight, healthy_routes

C1 = ["glucose", "urea", "alanine", "m1", "m2", "m3", "m4"]
C2 = ["glucose", "citrate", "m1", "m5"]
C3 = ["urea", "citrate", "m6"]
UNIQUE = 10


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


def three(data: Path) -> list[dict]:
    return [spec(write_cohort(data / "c1.tsv", C1)), spec(write_cohort(data / "c2.tsv", C2)),
            spec(write_cohort(data / "c3.tsv", C3))]


async def submit_many(client: Client, cohorts: list[dict], **kw) -> dict:
    return payload(await client.call_tool(
        "harmonize_many", {"cohorts": cohorts, "entity": "metabolites", **kw}))


async def submit_two(client: Client, a: dict, b: dict, **kw) -> dict:
    return payload(await client.call_tool(
        "harmonize_cohorts", {"a": a, "b": b, "entity": "metabolites", **kw}))


def job_dir(config, job_id: str) -> Path:
    return config.home / "jobs" / job_id


def lock_files(root: Path) -> list[Path]:
    return sorted(root.rglob("store.lock"))


# ---------------------------------------------------------------------------
# The six map_file lifecycle failures, adapted (written first)
# ---------------------------------------------------------------------------


async def test_a_cancel_accepted_during_startup_awaits_is_not_overwritten(
    router, tmp_path, data
) -> None:
    """(1) Cancel lands while the worker awaits its pin probe; the job must not go on to run."""
    scripted = Scripted().mount(router)
    hits = {"n": 0}

    def slow_kestrel(request):
        hits["n"] += 1
        if hits["n"] > 1:  # the job's pin fetch, not the startup probe
            time.sleep(0.5)
        return httpx.Response(200, json={"kestrel_version": "1.9", "kg_build": KG_BUILD})

    router.get(f"{KESTREL}/health").mock(side_effect=slow_kestrel)
    config = cfg(tmp_path, data)
    async with Client(build_server(config)) as client:
        await asyncio.sleep(0.1)  # let the startup probe land
        job = await submit_many(client, three(data))
        await wait_until(lambda: hits["n"] >= 2)
        cancelled = payload(await client.call_tool("cancel_job", {"job_id": job["job_id"]}))
        assert cancelled["state"] == "cancelled"
        await asyncio.sleep(0.8)
        assert (await hstatus(client, job["job_id"]))["state"] == "cancelled"
    assert scripted.inflight.calls == 0
    assert not Path(job["output_dir"]).exists()


async def test_resubmitting_while_the_worker_owns_the_job_returns_it_and_deletes_nothing(
    router, tmp_path, data
) -> None:
    """(2) There is no rerun for harmonize jobs: a resubmit never touches a live job's files."""
    scripted = Scripted(delay=0.05).mount(router)
    config = cfg(tmp_path, data)
    async with Client(build_server(config)) as client:
        cohorts = three(data)
        job = await submit_many(client, cohorts)
        await wait_until(lambda: scripted.inflight.calls >= 1)
        before = sorted(p.name for p in job_dir(config, job["job_id"]).iterdir())
        again = await submit_many(client, cohorts)
        assert again["job_id"] == job["job_id"]
        assert again["state"] in ("queued", "running")
        assert set(before) <= {p.name for p in job_dir(config, job["job_id"]).iterdir()}
        done = await wait_h(client, job["job_id"], "complete")
    assert done["state"] == "complete"
    assert len(scripted.sent_names) == UNIQUE, "the resubmit must not restart the mapping"


async def test_a_failure_in_the_worker_becomes_a_job_state_and_the_server_keeps_serving(
    router, tmp_path, data
) -> None:
    """(3) A file appearing in the output directory fails the job; nothing escapes the worker."""
    scripted = Scripted(delay=0.02).mount(router)
    config = cfg(tmp_path, data)
    async with Client(build_server(config)) as client:
        job = await submit_many(client, three(data))
        await wait_until(lambda: scripted.inflight.calls >= 1)
        out = Path(job["output_dir"])
        out.mkdir(exist_ok=True)
        (out / "summary.json").write_text("mine")
        failed = await wait_h(client, job["job_id"], "failed")
        assert "not overwritten" in failed["reason"]
        assert (out / "summary.json").read_text() == "mine"
        assert sorted(p.name for p in out.iterdir()) == ["summary.json"], "nothing half-placed"
        info = payload(await client.call_tool("server_info", {}))
        assert info["allowed_roots"] == [str(data)]
        # And the worker still runs the next job.
        other = await submit_many(client, three(data), output_dir=str(data / "second"))
        assert (await wait_h(client, other["job_id"], "complete"))["state"] == "complete"


async def test_resume_rechecks_every_path_against_the_current_roots(
    router, tmp_path, data
) -> None:
    """(4) A cohort folder that became a link to somewhere outside is refused at resume."""
    scripted = Scripted(delay=0.05).mount(router)
    sub = data / "sub"
    sub.mkdir()
    cohorts = [spec(write_cohort(sub / "c1.tsv", C1)), spec(write_cohort(data / "c2.tsv", C2))]
    config = cfg(tmp_path, data)
    async with Client(build_server(config)) as client:
        job = await submit_many(client, cohorts)
        await wait_until(lambda: scripted.inflight.calls >= 1)
        payload(await client.call_tool("cancel_job", {"job_id": job["job_id"]}))
        await asyncio.sleep(0.2)
        outside = tmp_path / "elsewhere"
        sub.rename(outside)
        sub.symlink_to(outside, target_is_directory=True)
        res = await client.call_tool("resume_job", {"job_id": job["job_id"]})
        assert "outside" in error_text(res)
        listing = payload(await client.call_tool("job_status", {}))
        assert listing.get("harmonize_jobs", []) == [], "nor is it listed"


async def test_output_paths_never_follow_links(router, tmp_path, data) -> None:
    """(5) A linked output directory is refused at submit; a linked file inside it at placement."""
    scripted = Scripted().mount(router)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (data / "linked").symlink_to(outside, target_is_directory=True)
    victim = outside / "victim.json"
    victim.write_text("precious")
    config = cfg(tmp_path, data)
    async with Client(build_server(config)) as client:
        res = await client.call_tool("harmonize_many", {
            "cohorts": three(data), "entity": "metabolites", "output_dir": str(data / "linked")})
        assert "outside" in error_text(res) or "link" in error_text(res)
        out = data / "out"
        out.mkdir()
        (out / "settings.json").symlink_to(victim)
        res = await client.call_tool("harmonize_many", {
            "cohorts": three(data), "entity": "metabolites", "output_dir": str(out)})
        assert "never overwrites" in error_text(res)
        # A link planted after submit is caught at placement.
        out2 = data / "out2"
        job = await submit_many(client, three(data), output_dir=str(out2))
        out2.mkdir(exist_ok=True)
        (out2 / "settings.json").symlink_to(victim)
        failed = await wait_h(client, job["job_id"], "failed", "complete")
        assert failed["state"] == "failed" and "not overwritten" in failed["reason"]
    assert victim.read_text() == "precious"
    assert list(outside.iterdir()) == [victim]
    assert scripted.inflight.peak <= 1


async def test_every_health_and_pin_probe_goes_through_the_gate(tmp_path, data) -> None:
    """(6) /health probes and mapping requests never overlap."""
    inflight = InFlight()
    scripted = Scripted(delay=0.03)
    scripted.inflight = inflight

    def api_health(request):
        # Sync, because the pin probe reaches it from a worker thread through sync httpx.
        inflight.now += 1
        inflight.calls += 1
        inflight.peak = max(inflight.peak, inflight.now)
        time.sleep(0.05)
        inflight.now -= 1
        return httpx.Response(200, json={"status": "healthy", "version": "0.1.0"})

    with respx.mock(assert_all_called=False) as r:
        r.get(f"{API}/health").mock(side_effect=api_health)
        r.get(f"{KESTREL}/health").mock(
            return_value=httpx.Response(200, json={"kestrel_version": "1.9",
                                                   "kg_build": KG_BUILD}))
        scripted.mount(r)
        async with Client(build_server(cfg(tmp_path, data))) as client:
            job = await submit_many(client, three(data))
            mapped = client.call_tool("map_entity", {"name": "x", "entity_type": ST})
            await asyncio.gather(mapped, wait_h(client, job["job_id"], "complete"))
    assert inflight.peak == 1
    assert inflight.calls >= 4


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


async def test_a_three_cohort_job_matches_a_direct_harmonize_many_call(
    router, tmp_path, data, capsys
) -> None:
    scripted = Scripted({"alanine": node("CHEBI:1", cert="uncorroborated")}).mount(router)
    config = cfg(tmp_path, data)
    cohorts = three(data)
    async with Client(build_server(config)) as client:
        job = await submit_many(client, cohorts)
        assert job["kind"] == "harmonize_many" and job["state"] in ("queued", "running")
        done = await wait_h(client, job["job_id"], "complete")
    assert capsys.readouterr().out == "", "stdout belongs to the MCP transport"
    assert scripted.inflight.peak == 1
    assert sorted(scripted.sent_names) == sorted(set(C1 + C2 + C3))
    out = Path(done["output_dir"])
    pins = json.loads((job_dir(config, job["job_id"]) / "pins.json").read_text())

    def direct() -> None:
        harmonize_many(
            {c["label"]: c["path"] for c in cohorts}, entity="metabolites",
            key_columns={c["label"]: "key" for c in cohorts}, batch_size=config.chunk_size,
            base_url=API, kestrel_url=KESTREL, pins=pins,
            store_dir=tmp_path / "direct" / "harmonize_many_store",
            output_dir=tmp_path / "direct" / "out", on_message=lambda _m: None,
        )

    await anyio.to_thread.run_sync(direct)
    assert report_files(out) == report_files(tmp_path / "direct" / "out")
    assert "summary.json" in report_files(out) and "matrix_names_only.tsv" in report_files(out)
    assert not (job_dir(config, job["job_id"]) / "staging").exists()


async def test_a_two_cohort_job_matches_a_direct_harmonize_cohorts_call(
    router, tmp_path, data
) -> None:
    Scripted().mount(router)
    config = cfg(tmp_path, data)
    a = spec(write_cohort(data / "c1.tsv", C1))
    b = spec(write_cohort(data / "c2.tsv", C2))
    async with Client(build_server(config)) as client:
        job = await submit_two(client, a, b)
        assert job["kind"] == "harmonize_cohorts"
        done = await wait_h(client, job["job_id"], "complete")
    pins = json.loads((job_dir(config, job["job_id"]) / "pins.json").read_text())

    def direct() -> None:
        harmonize_cohorts(
            a["path"], b["path"], entity="metabolites", a_key_column="key", b_key_column="key",
            a_label="c1", b_label="c2", batch_size=config.chunk_size, base_url=API,
            kestrel_url=KESTREL, pins=pins,
            output_dir=tmp_path / "direct", on_message=lambda _m: None,
        )

    await anyio.to_thread.run_sync(direct)
    assert report_files(Path(done["output_dir"])) == report_files(tmp_path / "direct")


async def test_store_reuse_and_publication_grade(router, tmp_path, data) -> None:
    scripted = Scripted().mount(router)
    config = cfg(tmp_path, data)
    cohorts = three(data)
    async with Client(build_server(config)) as client:
        first = await submit_many(client, cohorts, remap_all=True)
        done = await wait_h(client, first["job_id"], "complete")
        assert done["summary"]["publication_grade"] == GRADE_PUBLISHABLE
        assert len(scripted.sent_names) == UNIQUE
        second = await submit_many(client, cohorts, output_dir=str(data / "again"))
        assert second["job_id"] != first["job_id"]
        done2 = await wait_h(client, second["job_id"], "complete")
        assert done2["summary"]["publication_grade"] == GRADE_ITERATION
        assert len(scripted.sent_names) == UNIQUE, "the second job reuses every saved mapping"
        store = Path(done2["store_dir"])
        assert store == data / "harmonize_many_store"

    # A mapping saved by a job is reused by a direct Python API run on the same store.
    pins = json.loads((job_dir(config, first["job_id"]) / "pins.json").read_text())

    def direct():  # noqa: ANN202
        return harmonize_many(
            {c["label"]: c["path"] for c in cohorts}, entity="metabolites",
            key_columns={c["label"]: "key" for c in cohorts}, base_url=API, pins=pins,
            store_dir=store, save=False,
        )

    report = await anyio.to_thread.run_sync(direct)
    assert len(scripted.sent_names) == UNIQUE
    assert report.mapping["names_only"]["c1"]["n_reused"] == len(C1)


async def test_the_same_inputs_and_options_submitted_twice_give_one_job(
    router, tmp_path, data
) -> None:
    Scripted().mount(router)
    config = cfg(tmp_path, data)
    async with Client(build_server(config)) as client:
        cohorts = three(data)
        first = await submit_many(client, cohorts)
        await wait_h(client, first["job_id"], "complete")
        second = await submit_many(client, cohorts)
        assert second["job_id"] == first["job_id"] and second["state"] == "complete"
        listing = payload(await client.call_tool("job_status", {}))
        assert [j["job_id"] for j in listing["harmonize_jobs"]] == [first["job_id"]]


async def test_every_file_the_job_creates_is_owner_only(router, tmp_path, data) -> None:
    Scripted().mount(router)
    config = cfg(tmp_path, data)
    async with Client(build_server(config)) as client:
        job = await submit_many(client, three(data))
        done = await wait_h(client, job["job_id"], "complete")
    roots = [job_dir(config, job["job_id"]), Path(done["output_dir"]), Path(done["store_dir"])]
    checked = 0
    for root in roots:
        assert stat.S_IMODE(root.stat().st_mode) == 0o700, root
        for path in root.rglob("*"):
            mode = stat.S_IMODE(path.lstat().st_mode)
            assert mode == (0o700 if path.is_dir() else 0o600), (path, oct(mode))
            checked += 1
    assert checked > 10


# ---------------------------------------------------------------------------
# Gate, cancel, resume, shutdown
# ---------------------------------------------------------------------------


async def test_one_request_in_flight_while_an_interactive_tool_waits(
    router, tmp_path, data
) -> None:
    scripted = Scripted(delay=0.03).mount(router)
    async with Client(build_server(cfg(tmp_path, data))) as client:
        job = await submit_many(client, three(data))
        await wait_until(lambda: scripted.inflight.calls >= 1)
        results = await asyncio.gather(
            *(client.call_tool("map_entity", {"name": f"x{i}", "entity_type": ST})
              for i in range(3)))
        assert all(not r.is_error for r in results)
        await wait_h(client, job["job_id"], "complete")
    assert scripted.inflight.peak == 1


async def test_cancel_during_mapping_stops_and_resume_maps_only_what_is_missing(
    router, tmp_path, data
) -> None:
    """A two-cohort job has no store: resume relies on the job's own entity cache."""
    scripted = Scripted(delay=0.05).mount(router)
    config = cfg(tmp_path, data)
    a = spec(write_cohort(data / "c1.tsv", C1))
    b = spec(write_cohort(data / "c2.tsv", C2))
    unique = len(set(C1 + C2))
    async with Client(build_server(config)) as client:
        job = await submit_two(client, a, b)
        await wait_until(lambda: scripted.inflight.calls >= 1)
        payload(await client.call_tool("cancel_job", {"job_id": job["job_id"]}))
        await asyncio.sleep(0.3)
        cancelled = await hstatus(client, job["job_id"])
        assert cancelled["state"] == "cancelled"
        sent_before = len(scripted.sent_names)
        assert 0 < sent_before < unique
        assert not Path(job["output_dir"]).exists(), "nothing reaches the output before the end"
        await asyncio.sleep(0.2)
        assert len(scripted.sent_names) == sent_before, "no request after the cancel"
        resumed = payload(await client.call_tool("resume_job", {"job_id": job["job_id"]}))
        assert resumed["state"] in ("queued", "running")
        done = await wait_h(client, job["job_id"], "complete")
    assert sorted(scripted.sent_names) == sorted(set(C1 + C2)), "each entity mapped exactly once"
    assert (Path(done["output_dir"]) / "summary.json").is_file()


async def test_resume_with_an_unknown_kg_build_uses_the_job_cache(tmp_path, data) -> None:
    """Kestrel is down: the store saves nothing, so only the job cache makes resume cheap."""
    scripted = Scripted(delay=0.05)
    with respx.mock(assert_all_called=False) as r:
        r.get(f"{API}/health").mock(return_value=httpx.Response(200, json={"status": "ok"}))
        r.get(f"{KESTREL}/health").mock(side_effect=httpx.ConnectError("kestrel down"))
        scripted.mount(r)
        async with Client(build_server(cfg(tmp_path, data))) as client:
            job = await submit_many(client, three(data))
            await wait_until(lambda: scripted.inflight.calls >= 1)
            payload(await client.call_tool("cancel_job", {"job_id": job["job_id"]}))
            await asyncio.sleep(0.3)
            payload(await client.call_tool("resume_job", {"job_id": job["job_id"]}))
            done = await wait_h(client, job["job_id"], "complete")
    assert sorted(scripted.sent_names) == sorted(set(C1 + C2 + C3))
    assert done["kg_verified"] is False
    assert done["summary"]["publication_grade"] == GRADE_ITERATION


async def test_a_resumed_remap_all_job_is_iteration_grade_and_says_why(
    router, tmp_path, data
) -> None:
    scripted = Scripted(delay=0.05).mount(router)
    async with Client(build_server(cfg(tmp_path, data))) as client:
        job = await submit_many(client, three(data), remap_all=True)
        await wait_until(lambda: scripted.inflight.calls >= 1)
        payload(await client.call_tool("cancel_job", {"job_id": job["job_id"]}))
        await asyncio.sleep(0.3)
        payload(await client.call_tool("resume_job", {"job_id": job["job_id"]}))
        done = await wait_h(client, job["job_id"], "complete")
    assert sorted(scripted.sent_names) == sorted(set(C1 + C2 + C3)), "resume must converge"
    assert done["summary"]["publication_grade"] == GRADE_ITERATION
    assert "interrupted" in done["summary"]["grade_note"]
    assert "uninterrupted" in done["summary"]["grade_note"]


async def test_cancel_during_the_identifier_arm_then_resume_places_the_report_once(
    router, tmp_path, data
) -> None:
    scripted = Scripted(delay=0.04).mount(router)
    a = data / "a.tsv"
    a.write_text("key\tname\thmdb\nk1\tglucose\tHMDB0000122\nk2\turea\tHMDB0000294\n")
    b = data / "b.tsv"
    b.write_text("key\tname\thmdb\nk1\tglucose\tHMDB0000122\nk2\tcitrate\tHMDB0000094\n")
    cohorts = [spec(a, vocabularies={"HMDB": ["hmdb"]}), spec(b, vocabularies={"HMDB": ["hmdb"]})]

    def with_ids() -> bool:
        return any(e.get("identifiers") for body in scripted.inflight.bodies
                   for e in body["entities"])

    async with Client(build_server(cfg(tmp_path, data))) as client:
        job = await submit_many(client, cohorts)
        await wait_until(with_ids)
        payload(await client.call_tool("cancel_job", {"job_id": job["job_id"]}))
        await asyncio.sleep(0.3)
        assert not Path(job["output_dir"]).exists()
        payload(await client.call_tool("resume_job", {"job_id": job["job_id"]}))
        done = await wait_h(client, job["job_id"], "complete")
    out = Path(done["output_dir"])
    assert (out / "matrix_identifier.tsv").is_file() and (out / "summary.json").is_file()
    assert done["summary"]["arms"]["identifier"] is not None


async def test_shutdown_during_mapping_leaves_a_resumable_job_and_no_store_lock(
    router, tmp_path, data
) -> None:
    scripted = Scripted(delay=0.05).mount(router)
    config = cfg(tmp_path, data)
    async with Client(build_server(config)) as client:
        job = await submit_many(client, three(data))
        await wait_until(lambda: scripted.inflight.calls >= 1)
    # The server is gone. The worker thread must be gone with it.
    assert lock_files(data) == [], "the saved-mapping store's lock was left held"
    sent_at_shutdown = len(scripted.sent_names)
    await asyncio.sleep(0.2)
    assert len(scripted.sent_names) == sent_at_shutdown, "a thread kept mapping after shutdown"
    assert not Path(job["output_dir"]).exists()
    async with Client(build_server(config)) as client:
        done = await wait_h(client, job["job_id"], "complete")  # auto-resumed
    assert (Path(done["output_dir"]) / "summary.json").is_file()
    assert sorted(set(scripted.sent_names)) == sorted(set(C1 + C2 + C3))
    assert lock_files(data) == []


# ---------------------------------------------------------------------------
# Refusals before anything is created
# ---------------------------------------------------------------------------


async def test_paths_outside_the_roots_are_refused_at_submit(router, tmp_path, data) -> None:
    Scripted().mount(router)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    secret = write_cohort(outside / "secret.tsv", ["a"])
    config = cfg(tmp_path, data)
    async with Client(build_server(config)) as client:
        good = spec(write_cohort(data / "c1.tsv", C1))
        res = await client.call_tool("harmonize_many", {
            "cohorts": [good, spec(secret)], "entity": "metabolites"})
        assert "outside" in error_text(res)
        res = await client.call_tool("harmonize_many", {
            "cohorts": three(data), "entity": "metabolites",
            "output_dir": str(outside / "out")})
        assert "outside" in error_text(res)
        res = await client.call_tool("harmonize_many", {
            "cohorts": three(data), "entity": "metabolites",
            "store_dir": str(outside / "store")})
        assert "outside" in error_text(res)
    assert not (config.home / "jobs").exists()


async def test_caps_and_label_rules_are_checked_before_any_file_is_created(
    router, tmp_path, data
) -> None:
    scripted = Scripted(delay=0.2).mount(router)
    config = cfg(tmp_path, data, harmonize_max_rows=5, harmonize_max_queued=1,
                 harmonize_max_cohorts=3)
    small = [spec(write_cohort(data / "s1.tsv", ["a", "b"])),
             spec(write_cohort(data / "s2.tsv", ["a", "c"]))]
    async with Client(build_server(config)) as client:
        bad_label = [spec(write_cohort(data / "x.tsv", ["a"]), label="../evil"), small[0]]
        res = await client.call_tool("harmonize_many", {"cohorts": bad_label,
                                                        "entity": "metabolites"})
        assert "label" in error_text(res)
        big = [spec(write_cohort(data / "big.tsv", [f"n{i}" for i in range(6)])), small[0]]
        res = await client.call_tool("harmonize_many", {"cohorts": big, "entity": "metabolites"})
        assert "at most 5 rows" in error_text(res)
        four = [spec(write_cohort(data / f"f{i}.tsv", ["a"])) for i in range(4)]
        res = await client.call_tool("harmonize_many", {"cohorts": four, "entity": "metabolites"})
        assert "at most 3 cohorts" in error_text(res)
        assert not (config.home / "jobs").exists()
        assert sorted(p.name for p in data.iterdir() if p.is_dir()) == []

        first = await submit_many(client, small)
        res = await client.call_tool("harmonize_many", {
            "cohorts": small, "entity": "metabolites", "output_dir": str(data / "other")})
        assert "unfinished harmonize job" in error_text(res)
        assert [p.name for p in (config.home / "jobs").iterdir()] == [first["job_id"]]
        await wait_h(client, first["job_id"], "complete")
    assert scripted.inflight.peak == 1


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


async def test_a_known_kg_build_mismatch_fails_the_job_before_any_mapping_request(
    router, tmp_path, data
) -> None:
    scripted = Scripted().mount(router)
    config = cfg(tmp_path, data)
    async with Client(build_server(config)) as client:
        first = await submit_many(client, three(data))
        await wait_h(client, first["job_id"], "complete")
    sent = len(scripted.sent_names)
    router.get(f"{KESTREL}/health").mock(return_value=httpx.Response(200, json={
        "kestrel_version": "1.9", "kg_build": {**KG_BUILD, "git_commit": "ffffffffff"}}))
    async with Client(build_server(config)) as client:
        second = await submit_many(client, three(data), output_dir=str(data / "other"))
        failed = await wait_h(client, second["job_id"], "failed")
        assert "different KG build" in failed["reason"] and "remap_all" in failed["reason"]
    assert len(scripted.sent_names) == sent
    assert not (data / "other").exists()
    assert lock_files(data) == []


async def test_a_store_locked_by_another_process_fails_the_job_naming_the_lock(
    router, tmp_path, data
) -> None:
    scripted = Scripted().mount(router)
    lock_dir = data / "harmonize_many_store" / "names_only" / "biolink_SmallMolecule"
    lock_dir.mkdir(parents=True)
    (lock_dir / "store.lock").write_text(json.dumps({"pid": os.getpid(), "started_utc": "now"}))
    async with Client(build_server(cfg(tmp_path, data))) as client:
        job = await submit_many(client, three(data))
        failed = await wait_h(client, job["job_id"], "failed")
    assert "locked" in failed["reason"]
    assert str(lock_dir) in failed["reason"]
    assert scripted.inflight.calls == 0


async def test_three_whole_chunk_failures_stall_the_job_and_the_server_keeps_serving(
    router, tmp_path, data
) -> None:
    scripted = Scripted().mount(router)
    scripted.fail_next = 3
    async with Client(build_server(cfg(tmp_path, data))) as client:
        job = await submit_many(client, three(data))
        stalled = await wait_h(client, job["job_id"], "stalled")
        assert "3 chunks in a row failed" in stalled["reason"]
        assert scripted.inflight.calls == 3
        ok = await client.call_tool("map_entity", {"name": "x", "entity_type": ST})
        assert not ok.is_error
        payload(await client.call_tool("resume_job", {"job_id": job["job_id"]}))
        done = await wait_h(client, job["job_id"], "complete")
    assert done["state"] == "complete"
    assert lock_files(data) == []


async def test_a_failed_chunk_is_retried_in_a_job_but_errored_by_the_python_mapper(
    router, tmp_path, data
) -> None:
    """The intended difference: jobs apply the job client's retry rules inside one mapper call;
    ``BatchedApiMapper`` does not retry, so the same hiccup leaves errored rows in a direct run."""
    scripted = Scripted().mount(router)
    scripted.fail_next = 1
    cohorts = three(data)
    async with Client(build_server(cfg(tmp_path, data))) as client:
        job = await submit_many(client, cohorts)
        done = await wait_h(client, job["job_id"], "complete")
    assert done["rows_errored"] == 0 and done["summary"]["status"] == "ok"

    scripted.fail_next = 1

    def direct():  # noqa: ANN202
        return harmonize_many(
            {c["label"]: c["path"] for c in cohorts}, entity="metabolites",
            key_columns={c["label"]: "key" for c in cohorts}, base_url=API, probe_pins=False,
            save=False, batch_size=5,
        )

    report = await anyio.to_thread.run_sync(direct)
    assert report.status == "partial"


async def test_a_reordered_batch_fails_the_job_and_places_no_report(
    router, tmp_path, data
) -> None:
    scripted = Scripted().mount(router)
    scripted.reorder = True
    async with Client(build_server(cfg(tmp_path, data))) as client:
        job = await submit_many(client, three(data))
        failed = await wait_h(client, job["job_id"], "failed")
    assert "order" in failed["reason"]
    assert not Path(job["output_dir"]).exists()
    assert scripted.inflight.calls == 1
    assert lock_files(data) == []


async def test_errored_rows_give_a_partial_terminal_job_never_unresolved_rows(
    router, tmp_path, data
) -> None:
    scripted = Scripted().mount(router)
    scripted.error_names = {"alanine"}
    config = cfg(tmp_path, data, max_row_attempts=2)
    async with Client(build_server(config)) as client:
        job = await submit_many(client, three(data))
        done = await wait_h(client, job["job_id"], "partial")
        assert done["rows_errored"] == 1
        assert "new output" in done["reason"]
        res = await client.call_tool("resume_job", {"job_id": job["job_id"]})
        assert "partial" in error_text(res)
    assert scripted.sent_names.count("alanine") == 2, "retried up to max_row_attempts"
    out = Path(done["output_dir"])
    rows = (out / "not_grouped_names_only.tsv").read_text().splitlines()
    alanine = next(r for r in rows if "\talanine\t" in r).split("\t")
    assert alanine[3] == "errored" and "annotator exploded" in alanine[5]
    assert done["summary"]["status"] == "partial"


async def test_a_bridge_call_over_its_timeout_fails_the_job_and_releases_the_gate(
    router, tmp_path, data
) -> None:
    scripted = Scripted(delay=5.0).mount(router)
    config = cfg(tmp_path, data, bridge_timeout_s=0.1)
    async with Client(build_server(config)) as client:
        job = await submit_many(client, three(data))
        failed = await wait_h(client, job["job_id"], "failed")
        assert "did not finish within" in failed["reason"]
        scripted.delay = 0.0
        ok = await client.call_tool("map_entity", {"name": "x", "entity_type": ST})
        assert not ok.is_error, "the gate must be free again"
    assert lock_files(data) == []


# ---------------------------------------------------------------------------
# An interrupted placement
# ---------------------------------------------------------------------------


def _rewind_to_interrupted_placement(config, job_id: str, out: Path, keep: int) -> list[str]:
    """Make a finished job look as if the server died after placing ``keep`` report files."""
    record_path = job_dir(config, job_id) / "job.json"
    record = json.loads(record_path.read_text())
    record.update(state="running", stage="placing", summary=None, completed_utc=None)
    record_path.write_text(json.dumps(record))
    names = sorted(record["placed"])
    for name in names[keep:]:
        (out / name).unlink()
    return names


async def test_a_placement_interrupted_midway_recovers_on_restart(router, tmp_path, data) -> None:
    scripted = Scripted().mount(router)
    config = cfg(tmp_path, data)
    async with Client(build_server(config)) as client:
        job = await submit_many(client, three(data))
        done = await wait_h(client, job["job_id"], "complete")
    out = Path(done["output_dir"])
    names = _rewind_to_interrupted_placement(config, job["job_id"], out, keep=3)
    sent = len(scripted.sent_names)
    async with Client(build_server(config)) as client:
        again = await wait_h(client, job["job_id"], "complete")
    assert sorted(p.name for p in out.iterdir()) == names
    assert len(scripted.sent_names) == sent, "recovery maps nothing: the job cache has it all"
    assert again["summary"]["status"] == "ok"


async def test_a_file_replaced_during_an_interrupted_placement_is_not_overwritten(
    router, tmp_path, data
) -> None:
    Scripted().mount(router)
    config = cfg(tmp_path, data)
    async with Client(build_server(config)) as client:
        job = await submit_many(client, three(data))
        done = await wait_h(client, job["job_id"], "complete")
    out = Path(done["output_dir"])
    names = _rewind_to_interrupted_placement(config, job["job_id"], out, keep=3)
    (out / names[0]).write_text("the researcher's own edit")
    async with Client(build_server(config)) as client:
        failed = await wait_h(client, job["job_id"], "failed")
    assert "not overwritten" in failed["reason"]
    assert (out / names[0]).read_text() == "the researcher's own edit"
    assert sorted(p.name for p in out.iterdir()) == names[:3], "and nothing else was placed"
