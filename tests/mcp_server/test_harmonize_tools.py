"""The harmonize submit tools and job-tool dispatch across both job kinds."""

from __future__ import annotations

import http.server
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest
import respx
from mcp import Client, StdioServerParameters

from biomapper.many_cohorts import GRADE_ITERATION
from biomapper.mcp.server import build_server
from tests.mcp_server.harmonize_helpers import (
    Scripted,
    cfg,
    error_text,
    payload,
    spec,
    wait_h,
    write_cohort,
)
from tests.mcp_server.mcp_helpers import KG_BUILD, batch_response, entity_result, healthy_routes

ST = "biolink:SmallMolecule"
FILE_STATUS_KEYS = {
    "job_id", "state", "reason", "input_path", "output_path", "partial_path", "provenance_path",
    "rows_total", "entities_total", "entities_done", "entities_failed", "pass_index", "eta_s",
    "kg_build", "kg_verified", "owner", "created_utc", "updated_utc",
}


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


def trio(data: Path) -> list[dict]:
    return [spec(write_cohort(data / "c1.tsv", ["glucose", "urea", "alanine"])),
            spec(write_cohort(data / "c2.tsv", ["glucose", "citrate"])),
            spec(write_cohort(data / "c3.tsv", ["urea", "citrate"]))]


async def test_submit_poll_and_read_a_many_cohort_summary(router, tmp_path, data) -> None:
    Scripted().mount(router)
    async with Client(build_server(cfg(tmp_path, data))) as client:
        job = payload(await client.call_tool(
            "harmonize_many", {"cohorts": trio(data), "entity": "metabolites"}))
        assert job["stage"] in ("queued", "mapping") and job["entities_total"] == 4
        assert [c["label"] for c in job["cohorts"]] == ["c1", "c2", "c3"]
        assert job["output_dir"].startswith(str(data / "harmonize_many_"))
        done = await wait_h(client, job["job_id"], "complete")
    summary = done["summary"]
    assert done["stage"] == "done" and done["entities_mapped"] == 4
    assert summary["output_dir"] == done["output_dir"]
    assert summary["publication_grade"] == GRADE_ITERATION
    names = summary["arms"]["names_only"]
    assert names["groups"] == 4 and names["multi_cohort_groups"] == 3
    assert names["per_cohort"]["c1"]["grouped"] == 3
    assert summary["arms"]["identifier"] is None and summary["identifier_arm_skipped"]
    assert summary["mapping"]["names_only"]["c2"] == {
        "n_rows": 2, "n_reused": 0, "n_fresh": 2, "n_errored": 0}
    assert done["review_items"] is not None
    assert set(done["review_items"]) >= {"row", "link", "skipped_no_node"}
    assert json.loads((Path(done["output_dir"]) / "summary.json").read_text())["n_cohorts"] == 3


async def test_a_two_cohort_summary_has_no_publication_grade(router, tmp_path, data) -> None:
    Scripted().mount(router)
    cohorts = trio(data)
    async with Client(build_server(cfg(tmp_path, data))) as client:
        job = payload(await client.call_tool(
            "harmonize_cohorts", {"a": cohorts[0], "b": cohorts[1], "entity": "metabolites"}))
        assert job["store_dir"] is None
        done = await wait_h(client, job["job_id"], "complete")
    summary = done["summary"]
    assert "publication_grade" not in summary and "grade_note" not in summary
    assert summary["labels"] == ["c1", "c2"]
    assert summary["arms"]["names_only"]["n_links"] == 1
    assert summary["status"] == "ok"


async def test_job_status_lists_both_kinds_and_file_jobs_are_unchanged(
    router, tmp_path, data
) -> None:
    Scripted().mount(router)
    cohorts = trio(data)
    async with Client(build_server(cfg(tmp_path, data))) as client:
        only_files = payload(await client.call_tool("job_status", {}))
        assert only_files == {"jobs": [], "note": None}, "no harmonize key until there is one"
        file_job = payload(await client.call_tool("map_file", {
            "path": cohorts[0]["path"], "name_column": "name", "entity_type": ST}))
        assert set(file_job) == FILE_STATUS_KEYS
        harmonize = payload(await client.call_tool(
            "harmonize_many", {"cohorts": cohorts, "entity": "metabolites"}))
        await wait_h(client, harmonize["job_id"], "complete")
        listing = payload(await client.call_tool("job_status", {}))
        assert [j["job_id"] for j in listing["jobs"]] == [file_job["job_id"]]
        assert [j["job_id"] for j in listing["harmonize_jobs"]] == [harmonize["job_id"]]
        one = payload(await client.call_tool("job_status", {"job_id": file_job["job_id"]}))
        assert set(one) == {"jobs", "note"} and set(one["jobs"][0]) == FILE_STATUS_KEYS
        assert one["jobs"][0]["state"] == "complete"
        # resume and cancel still answer with the file job's own fields.
        again = payload(await client.call_tool("resume_job", {"job_id": file_job["job_id"]}))
        assert set(again) == FILE_STATUS_KEYS
        stopped = payload(await client.call_tool("cancel_job", {"job_id": file_job["job_id"]}))
        assert set(stopped) == FILE_STATUS_KEYS
        # ... and with the harmonize job's own fields for a harmonize job.
        hres = payload(await client.call_tool("cancel_job", {"job_id": harmonize["job_id"]}))
        assert hres["kind"] == "harmonize_many" and hres["state"] == "complete"


@pytest.mark.parametrize("mutate, message", [
    (lambda c: c[:1], "at least two cohorts"),
    (lambda c: [c[0], {**c[1], "label": "c1"}], "distinct"),
    (lambda c: [c[0], {**c[1], "name_column": "nope"}], "'nope' is not in the table"),
    (lambda c: [c[0], {**c[1], "vocabularies": {"HMDB": ["missing_col"]}}], "missing_col"),
    (lambda c: [c[0], {**c[1], "label": "node"}], "reserved"),
])
async def test_invalid_cohorts_give_a_clear_error_and_create_no_job(
    router, tmp_path, data, mutate, message
) -> None:
    Scripted().mount(router)
    config = cfg(tmp_path, data)
    async with Client(build_server(config)) as client:
        res = await client.call_tool(
            "harmonize_many", {"cohorts": mutate(trio(data)), "entity": "metabolites"})
        assert message in error_text(res)
        res = await client.call_tool(
            "harmonize_many", {"cohorts": trio(data), "entity": "unicorns"})
        assert "unknown entity" in error_text(res)
    assert not (config.home / "jobs").exists()
    assert [p.name for p in data.iterdir() if p.is_dir()] == []


async def test_unknown_arguments_are_rejected(router, tmp_path, data) -> None:
    Scripted().mount(router)
    config = cfg(tmp_path, data)
    cohorts = trio(data)
    async with Client(build_server(config)) as client:
        res = await client.call_tool("harmonize_many", {
            "cohorts": cohorts, "entity": "metabolites", "remap": True})
        assert res.is_error and "remap" in res.content[0].text
        res = await client.call_tool("harmonize_cohorts", {
            "a": cohorts[0], "b": cohorts[1], "entity": "metabolites", "store_dir": "x"})
        assert res.is_error and "store_dir" in res.content[0].text
        res = await client.call_tool("harmonize_many", {
            "cohorts": [{**cohorts[0], "identifier_columns": {"hmdb": "HMDB"}}, cohorts[1]],
            "entity": "metabolites"})
        assert res.is_error and "identifier_columns" in res.content[0].text
        schemas = {t.name: t.input_schema for t in (await client.list_tools()).tools}
    assert schemas["harmonize_many"]["additionalProperties"] is False
    assert schemas["harmonize_cohorts"]["additionalProperties"] is False
    assert not (config.home / "jobs").exists()


def test_the_submit_descriptions_say_what_is_sent() -> None:
    from biomapper.mcp import tools

    for name in ("harmonize_cohorts", "harmonize_many"):
        text = tools.HARMONIZE_DESCRIPTIONS[name]
        assert "NAMES" in text and "sent to the BioMapper API" in text
        assert "nothing else from the files leaves this machine" in text


# ---------------------------------------------------------------------------
# Over real stdio: a whole job, and nothing but MCP on stdout
# ---------------------------------------------------------------------------


class _FakeApi(http.server.BaseHTTPRequestHandler):
    def _send(self, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/kestrel"):
            self._send({"kestrel_version": "1.9", "kg_build": KG_BUILD})
        else:
            self._send({"status": "healthy", "version": "0.1.0"})

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self._send(batch_response([
            entity_result(e["name"], chosen=f"TEST:{len(e['name'])}{e['name'][0]}")
            for e in body["entities"]]))

    def log_message(self, *args) -> None:
        pass


async def test_a_harmonize_job_over_real_stdio_writes_nothing_but_mcp_to_stdout(
    tmp_path: Path,
) -> None:
    """A whole job over the real transport. The functions print, and the transport owns stdout:
    ``test_a_three_cohort_job_matches_a_direct_harmonize_many_call`` asserts in-process that a
    job writes nothing to stdout; this proves the job still completes when stdout IS the wire."""
    api = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeApi)
    threading.Thread(target=api.serve_forever, daemon=True).start()
    port = api.server_address[1]
    data = tmp_path / "data"
    data.mkdir()
    env = {
        **os.environ,
        "BIOMAPPER_BASE_URL": f"http://127.0.0.1:{port}/api/v1",
        "BIOMAPPER_KESTREL_URL": f"http://127.0.0.1:{port}/kestrel",
        "BIOMAPPER_MCP_HOME": str(tmp_path / "home"),
        "BIOMAPPER_MCP_ROOTS": str(data),
    }
    env.pop("BIOMAPPER_API_KEY", None)
    params = StdioServerParameters(command=sys.executable, args=["-m", "biomapper.mcp"], env=env)
    try:
        async with Client(params) as client:
            job = payload(await client.call_tool(
                "harmonize_many", {"cohorts": trio(data), "entity": "metabolites"}))
            deadline = time.monotonic() + 60
            while True:
                status = payload(await client.call_tool("job_status", {"job_id": job["job_id"]}))
                state = status["harmonize_jobs"][0]["state"]
                if state == "complete":
                    break
                assert state in ("queued", "running"), status
                assert time.monotonic() < deadline
                time.sleep(0.05)
            names = {t.name for t in (await client.list_tools()).tools}
    finally:
        api.shutdown()
    assert "harmonize_many" in names
    out = Path(status["harmonize_jobs"][0]["output_dir"])
    assert (out / "summary.json").is_file() and (out / "matrix_names_only.tsv").is_file()
