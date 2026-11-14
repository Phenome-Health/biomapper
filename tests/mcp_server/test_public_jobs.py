"""``biomapper.jobs.HarmonizeJobs``: the MCP job runner as public API, with no MCP client.

Same harness as ``test_harmonize_jobs.py`` (respx-mocked HTTP, a scripted ``/map/batch``), driven
through the public class instead of the in-memory MCP client. The point of these tests is that a
non-MCP caller gets the same durability: a process that dies mid-run is continued by the next one
without mapping any finished entity again.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
import respx

from biomapper.cohorts import HarmonizeProgress
from biomapper.jobs import (
    FINISHED_STATES,
    CohortSpec,
    HarmonizeJobs,
    HarmonizeStatus,
    JobError,
    load_report,
)
from tests.mcp_server.harmonize_helpers import Scripted, node, wait_until, write_cohort
from tests.mcp_server.mcp_helpers import API, KESTREL, healthy_routes

C1 = ["glucose", "urea", "alanine", "m1", "m2", "m3", "m4"]
C2 = ["glucose", "citrate", "m1", "m5"]
C3 = ["urea", "citrate", "m6"]


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


def service(tmp_path: Path, data: Path, **kw) -> HarmonizeJobs:
    kw.setdefault("systemic_backoff_s", 0.0)
    return HarmonizeJobs(tmp_path / "home", [data], base_url=API, kestrel_url=KESTREL, **kw)


def specs(data: Path) -> list[CohortSpec]:
    return [
        CohortSpec(label=p.stem, path=str(p), key_column="key")
        for p in (write_cohort(data / "c1.tsv", C1), write_cohort(data / "c2.tsv", C2),
                  write_cohort(data / "c3.tsv", C3))
    ]


async def test_a_two_cohort_job_runs_to_a_report_without_any_mcp_client(
    router, tmp_path, data
) -> None:
    scripted = Scripted().mount(router)
    a, b, _ = specs(data)
    async with service(tmp_path, data) as jobs:
        job = await jobs.submit_cohorts(a, b.model_dump(), entity="metabolites")
        assert isinstance(job, HarmonizeStatus) and job.kind == "harmonize_cohorts"
        done = await jobs.wait(job.job_id, poll_s=0.02, timeout_s=15)
        assert done.state == "complete" and done.state in FINISHED_STATES
        report = jobs.load_report(job.job_id)
        listed = await jobs.list_jobs()
    assert [j.job_id for j in listed] == [job.job_id]
    assert sorted(scripted.sent_names) == sorted(set(C1 + C2)), "each entity mapped once"
    assert report["kind"] == "harmonize_cohorts"
    assert report == load_report(done.output_dir), "the module function reads the same file"
    assert {(lk["a_name"], lk["b_name"]) for lk in report["arms"]["names_only"]["links"]} == {
        ("glucose", "glucose"), ("m1", "m1")
    }
    assert done.summary is not None and done.summary["name_linking"]["enabled"] is True
    json.dumps(done.model_dump(mode="json"))  # the status is plain data too


async def _kill_after_first_saved_batch(jobs: HarmonizeJobs, job_id: str) -> int:
    """Wait until at least one batch is saved; return how many entities were saved by then."""
    while True:
        saved = (await jobs.status(job_id)).entities_mapped
        if saved:
            return saved
        await asyncio.sleep(0.005)


def _assert_saved_entities_were_not_sent_again(
    sent: list[str], saved: int, unique: set[str], chunk: int = 5
) -> None:
    # Batches go out one at a time and in order, so the first ``saved`` names sent are exactly
    # the ones whose answers were on disk when the process died.
    for name in sent[:saved]:
        assert sent.count(name) == 1, f"{name!r} was saved before the restart and sent again"
    assert set(sent) == unique
    # Only the batch that was in flight at the kill (its answer never arrived) may repeat.
    assert len(sent) <= len(unique) + chunk


async def test_a_job_killed_mid_run_is_continued_by_the_next_process_without_re_mapping(
    router, tmp_path, data
) -> None:
    """The restart case: a second HarmonizeJobs on the same home picks the job up by itself."""
    scripted = Scripted(delay=0.05).mount(router)
    a, b, _ = specs(data)
    unique = set(C1 + C2)
    async with service(tmp_path, data) as jobs:
        job = await jobs.submit_cohorts(a, b, entity="metabolites")
        saved = await _kill_after_first_saved_batch(jobs, job.job_id)
    # The first "process" is gone mid-run: nothing was placed, and some entities were saved.
    sent_before = list(scripted.sent_names)
    assert 0 < saved < len(unique)
    assert not Path(job.output_dir).exists()
    await asyncio.sleep(0.2)
    assert scripted.sent_names == sent_before, "a thread kept mapping after the exit"

    async with service(tmp_path, data) as jobs:  # no submit: the unfinished job is queued again
        done = await jobs.wait(job.job_id, poll_s=0.02, timeout_s=15)
        assert done.state == "complete"
        report = jobs.load_report(job.job_id)
    _assert_saved_entities_were_not_sent_again(scripted.sent_names, saved, unique)
    assert report["summary"]["arms"]["names_only"]["n_links"] == 2


async def test_a_many_cohort_job_resumes_after_a_restart_and_reports_groups(
    router, tmp_path, data
) -> None:
    scripted = Scripted(delay=0.05).mount(router)
    unique = set(C1 + C2 + C3)
    async with service(tmp_path, data) as jobs:
        job = await jobs.submit_many(specs(data), entity="metabolites",
                                     store_dir=data / "store")
        saved = await _kill_after_first_saved_batch(jobs, job.job_id)
    assert 0 < saved < len(unique)
    async with service(tmp_path, data) as jobs:
        done = await jobs.wait(job.job_id, poll_s=0.02, timeout_s=15)
        assert done.state == "complete", done.reason
        report = jobs.load_report(job.job_id)
    _assert_saved_entities_were_not_sent_again(scripted.sent_names, saved, unique)
    assert report["kind"] == "harmonize_many" and report["status"] == "ok"
    multi = {g["node"]: g["cohorts"] for g in report["arms"]["names_only"]["groups"]
             if g["n_cohorts"] > 1}
    assert sorted(map(tuple, multi.values())) == [("c1", "c2"), ("c1", "c2"), ("c1", "c3"),
                                                  ("c2", "c3")]
    assert done.store_dir == str(data / "store")


async def test_progress_reaches_the_callback_on_the_event_loop_and_the_status(
    router, tmp_path, data
) -> None:
    Scripted().mount(router)
    a, b, _ = specs(data)
    loop = asyncio.get_running_loop()
    seen: list[tuple[str, HarmonizeProgress]] = []
    off_loop: list[bool] = []

    def on_progress(job_id: str, event: HarmonizeProgress) -> None:
        off_loop.append(asyncio.get_running_loop() is not loop)
        seen.append((job_id, event))

    async with service(tmp_path, data, on_progress=on_progress, chunk_size=3) as jobs:
        job = await jobs.submit_cohorts(a, b, entity="metabolites")
        done = await jobs.wait(job.job_id, poll_s=0.02, timeout_s=15)
    assert done.state == "complete"
    assert seen and not any(off_loop), "the callback runs on the caller's event loop"
    assert {job_id for job_id, _ in seen} == {job.job_id}
    by_cohort: dict[str, list[tuple[int, int]]] = {}
    for _, event in seen:
        assert event.function == "harmonize_cohorts" and event.arm == "names_only"
        by_cohort.setdefault(event.cohort or "", []).append(
            (event.entities_done, event.entities_total))
    assert by_cohort == {"c1": [(0, 7), (3, 7), (6, 7), (7, 7)], "c2": [(0, 4), (3, 4), (4, 4)]}
    assert done.progress == seen[-1][1].to_dict(), "a polling caller sees the latest update"


async def test_a_failing_progress_callback_does_not_stop_the_job(router, tmp_path, data) -> None:
    Scripted().mount(router)
    a, b, _ = specs(data)

    def boom(job_id: str, event: HarmonizeProgress) -> None:
        raise RuntimeError("the websocket is gone")

    async with service(tmp_path, data, on_progress=boom) as jobs:
        job = await jobs.submit_cohorts(a, b, entity="metabolites")
        done = await jobs.wait(job.job_id, poll_s=0.02, timeout_s=15)
    assert done.state == "complete"


async def test_cancel_then_resume_through_the_public_api(router, tmp_path, data) -> None:
    scripted = Scripted(delay=0.05).mount(router)
    a, b, _ = specs(data)
    async with service(tmp_path, data) as jobs:
        job = await jobs.submit_cohorts(a, b, entity="metabolites")
        await wait_until(lambda: scripted.inflight.calls >= 1)
        cancelled = await jobs.cancel(job.job_id)
        assert cancelled.state == "cancelled"
        stopped = await jobs.wait(job.job_id, poll_s=0.02, timeout_s=5)
        assert stopped.state == "cancelled", "wait returns a state that needs the caller"
        with pytest.raises(JobError, match="available once"):
            jobs.load_report(job.job_id)
        await asyncio.sleep(0.2)
        resumed = await jobs.resume(job.job_id)
        assert resumed.state in ("queued", "running")
        done = await jobs.wait(job.job_id, poll_s=0.02, timeout_s=15)
    assert done.state == "complete"
    assert sorted(scripted.sent_names) == sorted(set(C1 + C2))


async def test_review_items_of_a_finished_job_are_readable_without_mcp(
    router, tmp_path, data
) -> None:
    Scripted({"m1": node("TEST:1", cert="uncorroborated")}).mount(router)
    a, b, _ = specs(data)
    async with service(tmp_path, data) as jobs:
        job = await jobs.submit_cohorts(a, b, entity="metabolites")
        done = await jobs.wait(job.job_id, poll_s=0.02, timeout_s=15)
        assert done.state == "complete" and done.review_items is not None
        page = jobs.review_items(job.job_id)
        assert page.total == done.review_items.row + done.review_items.link >= 1
        rows = jobs.review_items(job.job_id, kind="row", limit=1)
        assert [i.label for i in rows.items] == ["m1"]
        detail = jobs.review_item(job.job_id, rows.items[0].ref)
        assert detail.ref == rows.items[0].ref and detail.kind == "row"
        json.dumps(page.model_dump(mode="json"))
        json.dumps(detail.model_dump(mode="json"))
        with pytest.raises(JobError, match="No review item"):
            jobs.review_item(job.job_id, "jref-nope")
        with pytest.raises(JobError, match="limit must be"):
            jobs.review_items(job.job_id, limit=500)


async def test_refusals_are_job_errors_with_a_message_for_the_user(
    router, tmp_path, data
) -> None:
    Scripted().mount(router)
    a, b, _ = specs(data)
    outside = write_cohort(tmp_path / "outside.tsv", ["glucose"])
    async with service(tmp_path, data) as jobs:
        with pytest.raises(JobError, match="outside the allowed folders"):
            await jobs.submit_cohorts(
                a, CohortSpec(label="x", path=str(outside), key_column="key"),
                entity="metabolites",
            )
        with pytest.raises(JobError, match="at least two cohorts"):
            await jobs.submit_many([a], entity="metabolites")
        with pytest.raises(JobError, match="not a job ID"):
            await jobs.status("nope")
        with pytest.raises(JobError, match="No job"):
            await jobs.status("0" * 16)
        assert await jobs.list_jobs() == []


async def test_limits_are_passed_through_and_unknown_ones_are_refused(
    router, tmp_path, data
) -> None:
    Scripted(delay=0.2).mount(router)
    a, b, c = specs(data)
    async with service(tmp_path, data, harmonize_max_queued=1) as jobs:
        await jobs.submit_cohorts(a, b, entity="metabolites")
        with pytest.raises(JobError, match="limit 1"):
            await jobs.submit_cohorts(a, c, entity="metabolites")
    with pytest.raises(TypeError, match="unknown limit"):
        HarmonizeJobs(tmp_path / "home", [data], not_a_limit=1)
    with pytest.raises(TypeError, match="list of folders"):
        HarmonizeJobs(tmp_path / "home", str(data))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="at least one folder"):
        HarmonizeJobs(tmp_path / "home", [])


async def test_methods_outside_the_context_manager_say_how_to_use_it(tmp_path, data) -> None:
    jobs = service(tmp_path, data)
    with pytest.raises(RuntimeError, match="async with HarmonizeJobs"):
        await jobs.list_jobs()


def test_importing_the_jobs_module_does_not_import_the_mcp_sdk() -> None:
    """A web backend that never serves MCP must not pay for, or depend on, the SDK import."""
    code = (
        "import sys; import biomapper.jobs; "
        "bad = [m for m in sys.modules if m == 'mcp' or m.startswith('mcp.')]; "
        "print(bad); sys.exit(1 if bad else 0)"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          check=False)
    assert done.returncode == 0, done.stdout + done.stderr
