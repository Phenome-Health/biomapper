"""Shared pieces for the harmonize-job tests: cohort files, a scripted /map/batch, polling."""

from __future__ import annotations

import asyncio
import csv
import json
import time
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from mcp import Client

from tests.mcp_server.mcp_helpers import (
    API,
    InFlight,
    batch_response,
    entity_result,
    kestrel_block,
    kestrel_row,
    make_config,
)

ST = "biolink:SmallMolecule"
TERMINAL = ("complete", "partial", "failed", "stalled", "cancelled")


def write_cohort(path: Path, names: list[str], *, column: str = "name") -> Path:
    with path.open("w", newline="") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n")
        w.writerow(["key", column])
        for i, n in enumerate(names):
            w.writerow([f"{path.stem}-{i}", n])
    return path


def spec(path: Path, label: str | None = None, **kw: Any) -> dict[str, Any]:  # noqa: ANN401
    return {"label": label or path.stem, "path": str(path), "key_column": "key", **kw}


def payload(res: Any) -> dict:  # noqa: ANN401
    assert not res.is_error, res.content[0].text
    assert res.structured_content is not None
    return res.structured_content


def error_text(res: Any) -> str:  # noqa: ANN401
    assert res.is_error, res.structured_content
    return res.content[0].text


def cfg(tmp_path: Path, data: Path, **kw: Any) -> Any:  # noqa: ANN401
    kw.setdefault("env_roots", [data])
    return make_config(tmp_path, **kw)


async def hstatus(client: Client, job_id: str) -> dict:
    out = payload(await client.call_tool("job_status", {"job_id": job_id}))
    assert out["jobs"] == []
    assert len(out["harmonize_jobs"]) == 1
    return out["harmonize_jobs"][0]


async def wait_h(client: Client, job_id: str, *states: str, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        s = await hstatus(client, job_id)
        if s["state"] in states:
            return s
        if time.monotonic() > deadline:
            raise AssertionError(
                f"job stayed {s['state']!r}/{s.get('stage')!r} ({s.get('reason')}), wanted {states}"
            )
        await asyncio.sleep(0.02)


async def wait_until(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.01)


def node(curie: str, *, cert: str | None = "corroborated", **extra_ids: list[str]) -> dict:
    """A table entry: resolve to ``curie`` with ``extra_ids`` as further equivalent ids."""
    prefix, local = curie.split(":")
    return {"chosen": curie, "cert": cert, "equivalents": {prefix: [local], **extra_ids}}


UNRESOLVED = {"chosen": None, "cert": None}


class Scripted:
    """A /map/batch (and /map/entity) side effect answering from a ``name -> node(...)`` table.

    Names missing from the table resolve to a node derived from the name, corroborated. When the
    request asks for ``kestrel_top_n`` the answer carries one passthrough block listing the
    chosen node (named ``<name> (KG)``) and one alternative, so candidate capture has rows.
    """

    def __init__(self, table: dict[str, dict] | None = None, *, delay: float = 0.0) -> None:
        self.table = dict(table or {})
        self.inflight = InFlight()
        self.delay = delay
        self.fail_next = 0  # whole-chunk 500s still to serve
        self.error_names: set[str] = set()  # per-row errors
        self.reorder = False
        self.candidate_requests: list[str] = []
        self.kg_names: dict[str, str] = {}

    @property
    def sent_names(self) -> list[str]:
        return [e["name"] for b in self.inflight.bodies for e in b.get("entities", [])
                if not (e.get("options") or {}).get("kestrel_top_n")]

    def entry(self, name: str, *, top_n: int | None = None) -> dict[str, Any]:
        if name in self.error_names:
            return {"name": name, "curies": [], "error": "annotator exploded for this row"}
        spec = self.table.get(name)
        if spec is None:
            spec = node(f"TEST:{zlib.crc32(name.encode())}")
        kestrel = None
        if top_n:
            self.candidate_requests.append(name)
            rows = []
            if spec["chosen"]:
                rows.append(kestrel_row(spec["chosen"], 3.0,
                                        self.kg_names.get(spec["chosen"], f"{name} (KG)")))
            rows.append(kestrel_row("ALT:1", 1.0, "an alternative"))
            kestrel = [kestrel_block("hybrid-search", rows)]
        return entity_result(name, chosen=spec["chosen"], cert=spec["cert"],
                             equivalents=spec.get("equivalents"), kestrel=kestrel)

    async def batch(self, request: httpx.Request) -> httpx.Response:
        await self.inflight.enter(request, self.delay)
        if self.fail_next > 0:
            self.fail_next -= 1
            return httpx.Response(500, json={"detail": "boom"})
        entities = json.loads(request.content)["entities"]
        entries = [self.entry(e["name"], top_n=(e.get("options") or {}).get("kestrel_top_n"))
                   for e in entities]
        if self.reorder and len(entries) > 1:
            entries = entries[::-1]
        return httpx.Response(200, json=batch_response(entries))

    async def entity(self, request: httpx.Request) -> httpx.Response:
        await self.inflight.enter(request, self.delay)
        body = json.loads(request.content)
        top_n = (body.get("options") or {}).get("kestrel_top_n")
        return httpx.Response(200, json={"result": self.entry(body["name"], top_n=top_n),
                                         "metadata": {}})

    def mount(self, router: Any) -> Scripted:  # noqa: ANN401
        router.post(f"{API}/map/batch").mock(side_effect=self.batch)
        router.post(f"{API}/map/entity").mock(side_effect=self.entity)
        return self


def strip_run_fields(value: Any) -> Any:  # noqa: ANN401
    """Drop the fields a job and a direct call legitimately differ on (mapper name, session)."""
    if isinstance(value, dict):
        return {k: strip_run_fields(v) for k, v in value.items()
                if k not in {"mapper", "session_id"}}
    if isinstance(value, list):
        return [strip_run_fields(v) for v in value]
    return value


def report_files(directory: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for p in sorted(directory.iterdir()):
        if not p.is_file() or p.name == "choices.tsv":
            continue
        out[p.name] = (strip_run_fields(json.loads(p.read_text())) if p.suffix == ".json"
                       else p.read_text())
    return out
