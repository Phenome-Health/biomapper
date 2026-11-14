"""Resumable harmonization jobs for your own application (1.9.0).

:func:`biomapper.harmonize_cohorts` and :func:`biomapper.harmonize_many` map every entity at about
4 to 5 seconds each, so a real run takes minutes to hours. :class:`HarmonizeJobs` runs either
function as a background job that survives a restart of your process: it is the job runner the
BioMapper MCP server uses (``biomapper.mcp.jobs`` and ``biomapper.mcp.harmonize_jobs``), opened up
for an asyncio application such as a FastAPI backend. Nothing here needs an MCP client, and the
MCP SDK is not imported.

What a job gives you over calling the function yourself:

* **Resume without re-mapping.** Every answered entity is saved in the job's home before the next
  batch is sent. If the process dies, the next :class:`HarmonizeJobs` opened on the same ``home``
  and ``roots`` queues the unfinished job again and sends only what is missing: at most the one
  batch that was in flight when the process died is sent a second time. This holds for
  two-cohort jobs too, which have no saved-mapping store of their own.
* **One request at a time.** One worker runs one job at a time and holds a gate for each request,
  because the shared deployment slows down sharply under concurrent load.
* **Retries and stalls.** A batch that fails as a whole is sent again; three in a row stall the
  job (``resume`` continues it after a health check). A row that keeps erroring is reported as an
  errored row, never as unresolved.
* **A report placed once.** The run writes into a private staging folder and the finished report
  is copied into ``output_dir`` at the end: owner-only, never through a link, never over an
  existing report. ``report.json`` there is the whole report as plain data
  (:meth:`HarmonizeJobs.load_report`).
* **Review items.** A finished job has a typed list of the weak rows and the links a person
  should look at, with candidates captured inside the job (:meth:`HarmonizeJobs.review_items`).

Use it as an async context manager, entered and exited in one task (a FastAPI lifespan is one)::

    from biomapper.jobs import CohortSpec, HarmonizeJobs

    async with HarmonizeJobs(home="/var/lib/myapp/biomapper", roots=["/var/lib/myapp/runs"],
                             on_progress=lambda job_id, p: print(job_id, p.to_dict())) as jobs:
        job = await jobs.submit_many(
            [CohortSpec(label="a", path="/var/lib/myapp/runs/42/a.tsv"),
             CohortSpec(label="b", path="/var/lib/myapp/runs/42/b.tsv"),
             CohortSpec(label="c", path="/var/lib/myapp/runs/42/c.tsv")],
            entity="metabolites", output_dir="/var/lib/myapp/runs/42/report",
            store_dir="/var/lib/myapp/runs/42/store",
        )
        done = await jobs.wait(job.job_id)
        report = jobs.load_report(job.job_id)      # a dict: links or groups, review queues, pins

Every path a job reads or writes (cohort files, ``output_dir``, ``store_dir``) must be inside one
of ``roots``; a path outside them, or one that reaches outside through a link, is refused with
:class:`JobError`. ``home`` holds the job records, caches and review evidence and should be a
directory only your application writes to.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, Literal

import anyio

from biomapper._provenance import DEFAULT_KESTREL_URL
from biomapper.client import DEFAULT_BASE_URL, BioMapperClient
from biomapper.cohorts import REPORT_FILENAME, HarmonizeProgress
from biomapper.mcp import judgments as _judgments
from biomapper.mcp import review_entries as _review
from biomapper.mcp.config import ServerConfig
from biomapper.mcp.gate import RequestGate
from biomapper.mcp.harmonize_jobs import CohortSpec
from biomapper.mcp.jobs import (
    CohortStatus,
    HarmonizeJob,
    HarmonizeStatus,
    JobError,
    JobManager,
    ReviewCounts,
)
from biomapper.mcp.review_entries import ReviewItemDetail, ReviewItemsPage, ReviewItemSummary

__all__ = [
    "FINISHED_STATES",
    "WAITING_STATES",
    "CohortSpec",
    "CohortStatus",
    "HarmonizeJobs",
    "HarmonizeProgress",
    "HarmonizeStatus",
    "JobError",
    "JobProgressCallback",
    "ReviewCounts",
    "ReviewItemDetail",
    "ReviewItemSummary",
    "ReviewItemsPage",
    "load_report",
]

JobProgressCallback = Callable[[str, HarmonizeProgress], None]

# States in which a job has placed its report: ``complete``, or ``partial`` when some rows
# errored (they are marked as errors in the report).
FINISHED_STATES: frozenset[str] = frozenset({"complete", "partial"})
# States in which a job is still going to change by itself. Any other state is either finished,
# or waiting for you: ``stalled``, ``paused``, ``expired`` and ``cancelled`` resume with
# :meth:`HarmonizeJobs.resume`; ``failed`` is retried by submitting the same call again.
WAITING_STATES: frozenset[str] = frozenset({"queued", "running"})

_LIMITS = frozenset(f.name for f in dataclasses.fields(ServerConfig)) - {
    "base_url", "kestrel_url", "api_key", "home", "env_roots",
}


def load_report(output_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """Read ``report.json`` from a report directory: the whole report as plain data.

    Works on the ``output_dir`` of a finished job and on any directory written by
    ``harmonize_cohorts()``, ``harmonize_many()`` or ``report.write()`` from 1.9.0 on. The layout
    is :meth:`biomapper.CohortHarmonizationReport.to_dict` or
    :meth:`biomapper.ManyCohortReport.to_dict`; ``kind`` says which.

    Raises:
        FileNotFoundError: The directory has no ``report.json`` (the run has not finished, or it
            was written by a release before 1.9.0).
    """
    loaded = json.loads((Path(output_dir) / REPORT_FILENAME).read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{Path(output_dir) / REPORT_FILENAME} does not hold a report object")
    return loaded


class HarmonizeJobs:
    """Run ``harmonize_cohorts()`` and ``harmonize_many()`` as resumable background jobs.

    Args:
        home: Directory for job records, per-job entity caches and review evidence. Created
            owner-only. Reopen the same ``home`` after a restart to continue unfinished jobs.
        roots: The folders jobs may read cohort files from and write reports and stores into.
        base_url, api_key, kestrel_url: The BioMapper deployment (default: the public one, no
            key) and the Kestrel root whose ``/health`` names the KG build.
        on_progress: Called with ``(job_id, HarmonizeProgress)`` at the start of each arm and
            after every batch, on the event loop this object was entered on. Keep it quick and
            do not block; an exception it raises is logged and does not stop the job. The same
            update is also kept on the job, so :meth:`status` returns the latest one as
            ``progress`` for callers that poll.
        **limits: Overrides for the runner's limits and timings, by
            :class:`biomapper.mcp.config.ServerConfig` field name. The ones a backend is likely
            to change: ``harmonize_max_queued`` (unfinished jobs allowed at once, default 3),
            ``harmonize_max_cohorts`` (12), ``harmonize_max_rows`` (20,000 per file),
            ``harmonize_max_bytes`` (50 MB per file), ``chunk_size`` (entities per request and
            per progress update, default 5) and ``capture_max_entities`` (candidate lookups for
            review items, default 200).

    Jobs run one at a time, in submission order, on one worker.
    """

    def __init__(
        self,
        home: str | os.PathLike[str],
        roots: Sequence[str | os.PathLike[str]],
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        kestrel_url: str | None = None,
        on_progress: JobProgressCallback | None = None,
        **limits: Any,  # noqa: ANN401 — ServerConfig fields, validated below
    ) -> None:
        if isinstance(roots, str | os.PathLike):
            raise TypeError("roots must be a list of folders, not a single path")
        unknown = sorted(set(limits) - _LIMITS)
        if unknown:
            raise TypeError(
                f"unknown limit(s) {unknown}; valid names: {sorted(_LIMITS)}"
            )
        if not roots:
            raise ValueError("roots must name at least one folder jobs may read and write in")
        self._config = ServerConfig(
            base_url=base_url or DEFAULT_BASE_URL,
            kestrel_url=kestrel_url or DEFAULT_KESTREL_URL,
            api_key=api_key,
            home=Path(home).expanduser(),
            env_roots=[Path(r).expanduser() for r in roots],
            **limits,
        )
        self._on_progress = on_progress
        self._manager: JobManager | None = None
        self._stack: contextlib.AsyncExitStack | None = None

    # -- lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> HarmonizeJobs:
        if self._stack is not None:
            raise RuntimeError("this HarmonizeJobs is already running")
        cfg = self._config
        key: dict[str, Any] = {"api_key": cfg.api_key} if cfg.api_key else {"anonymous": True}
        stack = contextlib.AsyncExitStack()
        try:
            client = await stack.enter_async_context(
                BioMapperClient(base_url=cfg.base_url, timeout=cfg.job_timeout_s, **key)
            )
            manager = JobManager(cfg, gate=RequestGate(), client=client)
            manager.on_progress = self._on_progress
            # Job locks go last, after the worker and its thread are gone (callbacks unwind in
            # reverse order, so this is registered before the task group).
            stack.callback(manager.close)
            group = await stack.enter_async_context(anyio.create_task_group())
            # On the way out: tell a running job's thread to stop at its next batch, then cancel
            # the worker. The task group does not exit until that thread has, so a job cannot
            # outlive this object holding a saved-mapping store's lock.
            stack.callback(group.cancel_scope.cancel)
            stack.callback(manager.begin_shutdown)
            group.start_soon(manager.run_worker)
            # Unfinished jobs of an earlier process under these roots are queued again.
            await manager.auto_resume([])
        except BaseException:
            await stack.aclose()
            raise
        self._manager, self._stack = manager, stack
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        stack, self._stack, self._manager = self._stack, None, None
        if stack is not None:
            await stack.__aexit__(exc_type, exc, tb)

    @property
    def _m(self) -> JobManager:
        if self._manager is None:
            raise RuntimeError(
                "HarmonizeJobs is not running; use it as `async with HarmonizeJobs(...) as jobs:`"
            )
        return self._manager

    # -- submit ------------------------------------------------------------

    @staticmethod
    def _spec(cohort: CohortSpec | Mapping[str, Any]) -> CohortSpec:
        return cohort if isinstance(cohort, CohortSpec) else CohortSpec.model_validate(dict(cohort))

    async def submit_cohorts(
        self,
        a: CohortSpec | Mapping[str, Any],
        b: CohortSpec | Mapping[str, Any],
        *,
        entity: str,
        category: str | None = None,
        output_dir: str | os.PathLike[str] | None = None,
    ) -> HarmonizeStatus:
        """Queue a two-cohort :func:`biomapper.harmonize_cohorts` job and return its status.

        ``a`` and ``b`` are :class:`CohortSpec` (or dicts with the same fields): a label, a
        TSV/CSV path inside ``roots``, the name column, an optional key column and the
        identifier columns per vocabulary. ``output_dir`` defaults to a folder beside the first
        cohort file and must not already hold a report.

        Submitting the same files, options and ``output_dir`` again returns the existing job: a
        paused or failed one is queued again, a live or finished one is returned as it is.

        Raises:
            JobError: Anything refused before the job is queued (paths, columns, labels, caps,
                the deployment's health check). The message is written for the end user.
        """
        return await self._m.harmonize.submit(
            kind="harmonize_cohorts", specs=[self._spec(a), self._spec(b)], entity=entity,
            category=category, output_dir=None if output_dir is None else os.fspath(output_dir),
            store_dir=None, remap_all=False, client_roots=[],
        )

    async def submit_many(
        self,
        cohorts: Sequence[CohortSpec | Mapping[str, Any]],
        *,
        entity: str,
        category: str | None = None,
        output_dir: str | os.PathLike[str] | None = None,
        store_dir: str | os.PathLike[str] | None = None,
        remap_all: bool = False,
    ) -> HarmonizeStatus:
        """Queue a :func:`biomapper.harmonize_many` job (two or more cohorts); return its status.

        ``store_dir`` is the saved-mapping store. It defaults to ``harmonize_many_store`` beside
        ``output_dir``, which every job writing next to it shares; pass a directory per user or
        per job to keep one user's saved mappings from being reused for another's.
        ``remap_all=True`` maps everything again for a ``publishable`` grade; if such a job is
        interrupted and resumed it finishes ``iteration-grade`` and its summary says why.

        Raises:
            JobError: As for :meth:`submit_cohorts`.
        """
        return await self._m.harmonize.submit(
            kind="harmonize_many", specs=[self._spec(c) for c in cohorts], entity=entity,
            category=category, output_dir=None if output_dir is None else os.fspath(output_dir),
            store_dir=None if store_dir is None else os.fspath(store_dir),
            remap_all=remap_all, client_roots=[],
        )

    # -- status, resume, cancel ----------------------------------------------

    def _harmonize_job(self, job_id: str) -> HarmonizeJob:
        job = self._m._require_job(job_id)
        if not isinstance(job, HarmonizeJob):
            raise JobError(f"Job {job_id} is not a harmonize job.")
        if not self._m._within(job, self._m.allowed_roots([])):
            raise JobError(
                f"Job {job_id!r} reads or writes files outside the folders this HarmonizeJobs "
                "was given."
            )
        return job

    async def status(self, job_id: str) -> HarmonizeStatus:
        """The job's current status. Makes no request to BioMapper.

        ``state`` is ``queued``, ``running``, ``complete``, ``partial`` (finished, some rows
        errored), ``stalled``, ``paused``, ``expired``, ``cancelled``, ``failed`` or
        ``owned_elsewhere`` (another live process runs it). ``stage`` moves through ``mapping``,
        ``building``, ``capture``, ``placing`` and ``done``. ``entities_mapped`` of
        ``entities_total`` is the job-wide estimate; ``progress`` is the latest per-arm update.
        ``summary`` and ``review_items`` are set once the job has finished.
        ``status.model_dump(mode="json")`` is plain data.

        Raises:
            JobError: No such job, or it is not a harmonize job under ``roots``.
        """
        return self._m.harmonize.status(self._harmonize_job(job_id))

    async def list_jobs(self) -> list[HarmonizeStatus]:
        """Every harmonize job in ``home`` whose files are under ``roots``, oldest first."""
        listing = await self._m.status(None, client_roots=[])
        return list(listing.harmonize_jobs)

    async def resume(self, job_id: str) -> HarmonizeStatus:
        """Continue a stalled, paused, expired or cancelled job after a fresh health check.

        Entities already mapped are not mapped again. A job that is queued, running or complete
        is returned as it is.

        Raises:
            JobError: The job is failed or partial (submit it again instead), owned by another
                process, or the deployment did not pass the health check.
        """
        self._harmonize_job(job_id)
        out = await self._m.resume(job_id, client_roots=[])
        assert isinstance(out, HarmonizeStatus)
        return out

    async def cancel(self, job_id: str) -> HarmonizeStatus:
        """Stop a job after its current batch. What was mapped is kept; :meth:`resume` continues.

        Raises:
            JobError: No such job, or another process owns it.
        """
        self._harmonize_job(job_id)
        out = await self._m.cancel(job_id, client_roots=[])
        assert isinstance(out, HarmonizeStatus)
        return out

    async def wait(
        self, job_id: str, *, poll_s: float = 1.0, timeout_s: float | None = None
    ) -> HarmonizeStatus:
        """Poll until the job is no longer ``queued`` or ``running``; return that status.

        The state returned may be a finished one (:data:`FINISHED_STATES`) or one that needs you
        (``stalled``, ``paused``, ``cancelled``, ``failed``, ...); check it.

        Raises:
            TimeoutError: ``timeout_s`` passed first. The job keeps running.
        """
        with anyio.fail_after(timeout_s):
            while True:
                current = await self.status(job_id)
                if current.state not in WAITING_STATES:
                    return current
                await anyio.sleep(poll_s)

    # -- results -------------------------------------------------------------

    def _finished(self, job_id: str, what: str) -> HarmonizeJob:
        job = self._harmonize_job(job_id)
        if job.state not in FINISHED_STATES:
            raise JobError(
                f"Job {job_id} is {job.state} (stage {job.stage}); its {what} available once "
                "it is complete."
            )
        return job

    def load_report(self, job_id: str) -> dict[str, Any]:
        """The finished job's whole report as plain data (``report.json`` in its output_dir).

        Raises:
            JobError: The job has not finished, or its report file is gone.
        """
        job = self._finished(job_id, "report is")
        try:
            return load_report(job.output)
        except (OSError, ValueError) as exc:
            raise JobError(
                f"Job {job_id} finished, but {job.output / REPORT_FILENAME} cannot be read "
                f"({type(exc).__name__})."
            ) from None

    def _index(self, job_id: str) -> _review.EntryIndex:
        self._finished(job_id, "review items are")
        index = _review.EntryStore(self._config.home).load(job_id)
        if index is None:
            raise JobError(f"Job {job_id} has no readable review list.")
        return index

    def _suggestion_of(self) -> Callable[[Any], str | None]:
        return _judgments.suggestion_lookup(self._m.refs, wall_clock=self._config.wall_clock)

    def review_items(
        self,
        job_id: str,
        *,
        kind: Literal["row", "link"] | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> ReviewItemsPage:
        """One page of a finished job's review items, largest impact first. No request is made.

        A ``row`` item is a weak entity (asked once however many rows share the name); a
        ``link`` item is a link a person should look at. ``limit`` defaults to 25 and is at most
        50; ``next_offset`` on the page is the next ``offset``, or ``None`` at the end.
        ``page.model_dump(mode="json")`` is plain data. Names in it are untrusted text from the
        cohort files and the knowledge graph.

        Raises:
            JobError: The job has not finished, or ``offset`` / ``limit`` is out of range.
        """
        cfg = self._config
        size = cfg.review_page_default if limit is None else limit
        if not 1 <= size <= cfg.review_page_max:
            raise JobError(f"limit must be between 1 and {cfg.review_page_max} (got {size}).")
        if offset < 0:
            raise JobError(f"offset must be 0 or more (got {offset}).")
        return _review.page(self._index(job_id), kind=kind, offset=offset, limit=size,
                            suggestion_of=self._suggestion_of())

    def review_item(self, job_id: str, ref: str) -> ReviewItemDetail:
        """One review item in full: a row's candidates status, or a link with both sides.

        ``ref`` is the ``ref`` of an item from :meth:`review_items`.

        Raises:
            JobError: The job has not finished, or it has no such item.
        """
        index = self._index(job_id)
        entry = next(
            (e for e in index.entries if ref in (e.ref, e.superseding_ref)), None
        )
        if entry is None:
            raise JobError(f"No review item {ref!r} in job {job_id}.")
        return _review.detail(entry, index, self._suggestion_of()(entry))
