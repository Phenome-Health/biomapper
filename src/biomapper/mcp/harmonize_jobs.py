"""``harmonize_cohorts()`` and ``harmonize_many()`` as resumable background jobs (1.9.0).

Both functions are synchronous, print to stdout, write straight into their output directory and
map through ``asyncio.run``. None of that fits a stdio MCP server, so a harmonize job wraps the
unchanged function:

* **Worker thread.** The function runs through ``anyio.to_thread.run_sync`` on the server's one
  job worker, after file jobs or before them, never beside them.
* **Gated mapper.** :class:`GatedJobMapper` subclasses ``BatchedApiMapper``, so
  ``harmonize_many`` does not class it as a custom mapper and uses the saved-mapping store exactly
  as the Python API does. Per chunk it bridges to the server loop, holds the one request gate,
  and calls the job client. Every bridge call is bounded, checks the cancel flag, and applies the
  file jobs' stall and per-row retry rules.
* **Job cache.** Every answered entity also goes to an ``EntityCache`` in the job's home, under
  the file jobs' KG-build rules, so a two-cohort job (no store) and a many-cohort job on an
  unknown build (store disabled) still resume without re-mapping.
* **Staging.** The function writes into ``<home>/jobs/<id>/staging``. Only when the run, the
  review items and the candidate capture are complete is the report placed into the
  researcher's output directory: once, owner-only, never through a link, never over a file.
* **Review items.** Built from the names-only arm before placement, then a capture stage fetches
  candidates for weak entities and the names of link nodes (see
  :mod:`biomapper.mcp.review_entries`).

A resumed ``remap_all`` job passes ``remap_all`` off (passing it again would move the job's own
saved chunks aside on every resume and never converge), so it finishes iteration-grade and its
summary says why.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import threading
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import anyio.from_thread
import anyio.to_thread
from pydantic import BaseModel, ConfigDict, Field

from biomapper._checkpoint import CachePinMismatch, EntityCache
from biomapper._mapping_store import (
    ARMS,
    StoreBuildMismatch,
    StoreLocked,
    _entity_key,
    _safe_dir,
)
from biomapper._pins import kg_build_label, kg_verified
from biomapper.cohorts import (
    BatchedApiMapper,
    CohortHarmonizationReport,
    _require_cohort_labels,
    harmonize_cohorts,
    read_cohort,
    resolve_category,
)
from biomapper.exceptions import BioMapperAuthError
from biomapper.many_cohorts import (
    GRADE_ITERATION,
    STORE_DIRNAME,
    ManyCohortReport,
    _require_labels,
    harmonize_many,
)
from biomapper.mcp import tools
from biomapper.mcp._storage import private_dir, utc, write_private
from biomapper.mcp.jobs import (
    HARMONIZE_KINDS,
    PINS_FILE,
    UNFINISHED,
    CohortStatus,
    HarmonizeJob,
    HarmonizeStatus,
    JobError,
    ReviewCounts,
    _cache_pin,
    _pin_known,
    _sha256,
    _Stop,
    _under,
    probe_pins,
)
from biomapper.models import MappingResult

if TYPE_CHECKING:
    from biomapper.mcp.jobs import JobManager

logger = logging.getLogger("biomapper.mcp.harmonize_jobs")

STAGING_DIR = "staging"
CACHE_DIR = "cache"
ANNOTATION_MODES = ("missing", "none")  # every mode the two functions send
# Cohort labels become file names, table columns and path parts of nothing else: a short safe set.
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}")
# Names an existing output directory may not already hold: the functions' own report files.
REPORT_PATTERNS = (
    "summary.json", "settings.json", "diff.tsv", "review_queue.tsv", "mapping_*.tsv",
    "links_*.tsv", "possible_missed_*.tsv", "provenance_*.json", "membership_*.tsv",
    "matrix_*.tsv", "overlap_*.tsv", "cross_node_*.tsv", "withheld_*.tsv",
    "flagged_groups_*.tsv", "not_grouped_*.tsv", "id_check_review_*.tsv",
)
GRADE_NOTE = (
    "The publication grade dropped to iteration-grade because this remap_all run was "
    "interrupted and resumed: the resumed pass reuses the chunks saved before the interruption "
    "instead of mapping them again. A publishable run needs one uninterrupted remap_all pass."
)


class CohortSpec(BaseModel):
    """One cohort file and its column declarations (the tools' input)."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(description="Short name for the cohort: letters, digits, '.', '-', '_'")
    path: str = Field(description="TSV/CSV file inside an allowed folder")
    name_column: str = "name"
    key_column: str | None = Field(
        default=None, description="Column of unique row keys; default is row number plus name"
    )
    vocabularies: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Identifier columns per vocabulary, e.g. {\"HMDB\": [\"hmdb_id\"]}. Note "
        "the direction: vocabulary to column(s), the reverse of map_file's identifier_columns.",
    )


class _Cancelled(Exception):
    """Raised into the worker thread: the job was cancelled, or the server is shutting down."""


class _Stalled(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Fatal(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class GatedJobMapper(BatchedApiMapper):
    """The job's mapper: ``BatchedApiMapper`` whose every chunk goes through the request gate.

    It is called on the worker thread by the unchanged harmonize function (and by
    ``map_requests``, whose chunk size is the same ``batch_size``). Each chunk is handed to the
    server loop, which is the only place the gate and the job client may be used.
    """

    def __init__(
        self, runner: HarmonizeRunner, job: HarmonizeJob, caches: dict[str, EntityCache]
    ) -> None:
        cfg = runner.m.config
        super().__init__(batch_size=max(cfg.chunk_size, 1), timeout=cfg.job_timeout_s,
                         base_url=cfg.base_url, api_key=cfg.api_key)
        self._runner = runner
        self._job = job
        self._caches = caches

    def __call__(
        self, records: list[dict[str, Any]], *, entity_type: str, annotation_mode: str
    ) -> list[MappingResult]:
        out: list[MappingResult] = []
        for start in range(0, len(records), self.batch_size):
            chunk = records[start : start + self.batch_size]
            out.extend(anyio.from_thread.run(
                self._runner.map_chunk, self._job, self._caches, chunk, entity_type,
                annotation_mode,
            ))
        return out


def _chmod_tree(root: Path) -> None:
    """Owner-only for a directory tree this job wrote (the functions use plain writes)."""
    if not root.is_dir() or root.is_symlink():
        return
    with contextlib.suppress(OSError):
        os.chmod(root, 0o700)
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        with contextlib.suppress(OSError):
            os.chmod(path, 0o700 if path.is_dir() else 0o600)


_umask_lock = threading.Lock()
_umask_depth = 0
_umask_saved = 0


@contextlib.contextmanager
def _private_umask() -> Iterator[None]:
    """Owner-only for every file and directory created while the unchanged function runs.

    The function and the saved-mapping store use plain writes, so the only way to have their
    files private from the moment they exist (not after a later chmod, which a killed job never
    reaches) is the process umask. It is process-wide; that is safe here because the server's
    own writes all pass an explicit owner-only mode, so the only effect on another thread is a
    more private file. Counted, so overlapping uses restore the original value once.
    """
    global _umask_depth, _umask_saved
    with _umask_lock:
        if _umask_depth == 0:
            _umask_saved = os.umask(0o077)
        _umask_depth += 1
    try:
        yield
    finally:
        with _umask_lock:
            _umask_depth -= 1
            if _umask_depth == 0:
                os.umask(_umask_saved)


def _matches_report(name: str) -> bool:
    return any(Path(name).match(pattern) for pattern in REPORT_PATTERNS)


class HarmonizeRunner:
    """Submit, status and the run of harmonize jobs, on top of the shared :class:`JobManager`."""

    def __init__(self, manager: JobManager) -> None:
        self.m = manager
        self._bad_chunks = 0
        self._attempts: dict[str, int] = {}
        self._batch_index = 0
        self._submit_lock: anyio.Lock | None = None

    # -- status ------------------------------------------------------------

    def status(self, job: HarmonizeJob) -> HarmonizeStatus:
        state, owner = self.m._live_state(job)
        eta: float | None = None
        if state not in ("complete", "failed", "cancelled", "partial") and job.stage in (
            "queued", "mapping"
        ):
            remaining = max(job.entities_total - job.entities_mapped, 0)
            eta = round(remaining * job.seconds_per_entity, 1)
        return HarmonizeStatus(
            job_id=job.job_id,
            kind=job.kind,  # type: ignore[arg-type]
            state=state,  # type: ignore[arg-type]
            stage=job.stage,  # type: ignore[arg-type]
            reason=job.reason,
            cohorts=[CohortStatus(label=c["label"], path=c["path"], rows=c["rows"])
                     for c in job.cohorts],
            entity=job.entity,
            output_dir=job.output_dir,
            store_dir=job.store_dir,
            entities_total=job.entities_total,
            entities_mapped=job.entities_mapped,
            rows_errored=job.rows_errored,
            eta_s=eta,
            kg_build=job.kg_build,
            kg_verified=job.kg_verified,
            owner=owner,
            created_utc=job.created_utc,
            updated_utc=job.updated_utc,
            summary=job.summary,
            review_items=ReviewCounts(**job.review) if job.review is not None else None,
        )

    # -- submit ------------------------------------------------------------

    def _resolve_dir(self, raw: str, base: Path, roots: list[Path], what: str) -> Path:
        """A directory this job may create or write into: real parent, no link, inside roots."""
        given = Path(raw).expanduser()
        target = given if given.is_absolute() else base / given
        try:
            parent = target.parent.resolve(strict=True)
        except OSError:
            raise JobError(f"The folder that would hold {what} {target} does not exist.") from None
        target = parent / target.name
        if target.is_symlink():
            raise JobError(f"{what} {target} is a link; a harmonize job never writes through "
                           "links. Give the real folder.")
        if not _under(target, roots):
            raise JobError(
                f"{what} {target} is outside the allowed folders "
                f"({', '.join(str(r) for r in roots)})."
            )
        if target.exists() and not target.is_dir():
            raise JobError(f"{what} {target} exists and is not a folder.")
        return target

    @staticmethod
    def _check_output_free(output: Path) -> None:
        """Refuse an output directory that already holds report files (or links)."""
        if not output.exists():
            if not os.access(output.parent, os.W_OK):
                raise JobError(f"Output folder {output.parent} is not writable.")
            return
        for entry in output.iterdir():
            if _matches_report(entry.name):
                raise JobError(
                    f"{entry} already exists and a harmonize job never overwrites. Pass a "
                    "different output_dir, or move the earlier report away first."
                )
        if not os.access(output, os.W_OK):
            raise JobError(f"Output folder {output} is not writable.")

    def _read_inputs(
        self, kind: str, specs: list[CohortSpec], sources: list[Path], entity: str,
        category: str | None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Validate every cohort exactly as the function will, and describe it for the record."""
        cap = self.m.config.harmonize_max_rows
        labels = [s.label for s in specs]
        try:
            resolve_category(entity, category)
            if kind == "harmonize_many":
                _require_labels(labels)
            else:
                _require_cohort_labels(labels[0], labels[1])
            names: set[str] = set()
            cohorts: list[dict[str, Any]] = []
            for item, source in zip(specs, sources, strict=True):
                cohort = read_cohort(
                    source, label=item.label, name_column=item.name_column,
                    vocabularies=item.vocabularies or None, key_column=item.key_column,
                )
                rows = int(cohort.source.get("n_rows", len(cohort.records)))
                if rows > cap:
                    raise JobError(
                        f"{source.name} has {rows} rows; a harmonize job takes at most {cap} "
                        "rows per cohort file."
                    )
                if not cohort.records:
                    raise JobError(
                        f"{source.name} has no rows with a value in {item.name_column!r}."
                    )
                names.update(r.name for r in cohort.records)
                cohorts.append({
                    "label": item.label, "path": str(source), "sha256": _sha256(source),
                    "rows": len(cohort.records), "name_column": item.name_column,
                    "key_column": item.key_column,
                    "vocabularies": {v: list(c) for v, c in sorted(item.vocabularies.items())},
                })
        except (ValueError, TypeError) as exc:
            raise JobError(str(exc)) from None
        return cohorts, len(names)

    async def submit(
        self,
        *,
        kind: str,
        specs: list[CohortSpec],
        entity: str,
        category: str | None,
        output_dir: str | None,
        store_dir: str | None,
        remap_all: bool,
        client_roots: list[Path],
    ) -> HarmonizeStatus:
        assert kind in HARMONIZE_KINDS
        m = self.m
        cfg = m.config
        roots = m._require_roots(client_roots, kind)
        await m._note_roots(roots)

        # Caps and label rules first: nothing is read or created for a request they refuse.
        if len(specs) < 2:
            raise JobError(f"{kind} needs at least two cohorts (got {len(specs)}).")
        if len(specs) > cfg.harmonize_max_cohorts:
            raise JobError(
                f"{kind} takes at most {cfg.harmonize_max_cohorts} cohorts per job (got "
                f"{len(specs)})."
            )
        for item in specs:
            if not LABEL.fullmatch(item.label):
                raise JobError(
                    f"Cohort label {item.label[:60]!r} is not allowed. A label is 1 to 40 "
                    "characters: letters, digits, '.', '-' or '_', starting with a letter or "
                    "digit. It names files and table columns."
                )
        labels = [s.label for s in specs]
        if len(set(labels)) != len(labels):
            raise JobError(f"Cohort labels must be distinct (got {labels}).")
        sources = [m._resolve_input(item.path, roots) for item in specs]
        for source in sources:
            size = source.stat().st_size
            if size > cfg.harmonize_max_bytes:
                raise JobError(
                    f"{source.name} is {size} bytes; a harmonize job takes at most "
                    f"{cfg.harmonize_max_bytes} bytes per cohort file."
                )
        cohorts, entities_total = await anyio.to_thread.run_sync(
            self._read_inputs, kind, specs, sources, entity, category
        )

        options = {"remap_all": bool(remap_all)} if kind == "harmonize_many" else {}
        identity = {
            "kind": kind, "entity": entity, "category": category, "options": options,
            "cohorts": [{k: c[k] for k in ("label", "sha256", "name_column", "key_column",
                                           "vocabularies")} for c in cohorts],
        }
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        base = sources[0].parent
        output = self._resolve_dir(output_dir or f"{kind}_{digest[:12]}", base, roots,
                                   "Output folder")
        store: Path | None = None
        if kind == "harmonize_many":
            store = self._resolve_dir(store_dir or str(output.parent / STORE_DIRNAME), base,
                                      roots, "Saved-mapping store")
        elif store_dir is not None:
            raise JobError("store_dir applies to harmonize_many only.")
        job_id = hashlib.sha256(json.dumps(
            {**identity, "output_dir": str(output), "store_dir": str(store) if store else None},
            sort_keys=True,
        ).encode()).hexdigest()[:16]

        # One registration at a time: the checks below await (the health probe), so without the
        # lock two submissions could both pass them, and the second would replace the job object
        # the worker already runs (status and cancel would then miss it) or exceed the queue cap.
        if self._submit_lock is None:
            self._submit_lock = anyio.Lock()
        async with self._submit_lock:
            existing = m._load(job_id)
            if existing is not None:
                if not isinstance(existing, HarmonizeJob):  # pragma: no cover - a 64-bit collision
                    raise JobError(f"Job id {job_id} is taken by a map_file job.")
                state, _ = m._live_state(existing)
                # A paused job is picked up; a failed one is retried (it placed nothing, or only
                # files it recorded as its own). A live job is never touched: there is no rerun.
                if state in ("paused", "failed") and m._busy_job != job_id and m._take(existing):
                    job = m._jobs[job_id]
                    assert isinstance(job, HarmonizeJob)
                    job.state = "queued"
                    job.reason = None
                    m._save(job)
                    m._enqueue(job, health=True, roots=roots)
                    return self.status(job)
                return self.status(existing)

            self._check_output_free(output)
            unfinished = [
                j for j in m._jobs.values()
                if isinstance(j, HarmonizeJob) and j.job_id in m._locks and j.state in UNFINISHED
            ]
            if len(unfinished) >= cfg.harmonize_max_queued:
                raise JobError(
                    f"This session already has {len(unfinished)} unfinished harmonize job(s) "
                    f"(limit {cfg.harmonize_max_queued}); jobs run one at a time. Wait for one "
                    "to finish, or cancel_job it."
                )
            problem = await m._health_ok()
            if problem is not None:
                raise JobError(f"Not queued: {problem}")

            job = HarmonizeJob(
                job_id=job_id, kind=kind, cohorts=cohorts, entity=entity, category=category,
                output_dir=str(output), store_dir=str(store) if store else None, options=options,
                entities_total=entities_total,
                seconds_per_entity=cfg.seconds_per_entity_seed,
                created_utc=utc(cfg.wall_clock()),
            )
            private_dir(cfg.home)
            private_dir(m.jobs_dir)
            private_dir(m._dir(job_id))
            if not m._take(job):
                raise JobError(f"Job {job_id} is owned by another session.")
            m._jobs[job_id] = job
            m._save(job)
            m._enqueue(job, health=False, roots=roots)
            return self.status(job)

    # -- the bridge (runs on the server loop, called from the worker thread) --

    def _check_stop(self, job: HarmonizeJob) -> None:
        if job.state == "cancelled" or self.m.shutting_down:
            raise _Cancelled

    async def map_chunk(
        self,
        job: HarmonizeJob,
        caches: dict[str, EntityCache],
        records: list[dict[str, Any]],
        entity_type: str,
        annotation_mode: str,
    ) -> list[MappingResult]:
        """Map one chunk under the gate, with the file jobs' retry and stall rules.

        Rows already in the job cache are served from it. A chunk that fails as a whole is sent
        again (three in a row stall the job); a row that errors on its own is retried up to
        ``max_row_attempts`` and then returned as an errored row, never as unresolved.
        """
        m = self.m
        cfg = m.config
        self._check_stop(job)
        cache = caches[annotation_mode]
        options = {"entity_type": entity_type, "annotation_mode": annotation_mode}
        keys = [_entity_key(r, options) for r in records]
        results: dict[int, MappingResult] = {}
        for i, key in enumerate(keys):
            hit = cache.get(key)
            if hit is not None:
                results[i] = hit
        pending = [i for i in range(len(records)) if i not in results]
        while pending:
            self._check_stop(job)
            batch = [{"name": records[i]["name"],
                      "identifiers": dict(records[i].get("identifiers") or {})} for i in pending]
            started = cfg.monotonic()
            try:
                with anyio.fail_after(cfg.bridge_timeout_s):
                    async with m.gate.hold(wait_budget_s=None):
                        # The client only warns when a batch comes back out of order. Only this
                        # worker records warnings, one request at a time, so the scopes never
                        # overlap.
                        with warnings.catch_warnings(record=True) as caught:
                            warnings.simplefilter("always")
                            got = await m.client.map_entities(
                                batch, entity_type=entity_type, annotation_mode=annotation_mode,
                                sleep_on_rate_limit=False,
                            )
            except TimeoutError:
                raise _Fatal(
                    f"A mapping request did not finish within {cfg.bridge_timeout_s:g} s (gate "
                    "wait included), so the job was stopped rather than left holding the "
                    "worker. Nothing was written to the output folder. Submit the same call "
                    "again to retry; entities already mapped are kept."
                ) from None
            except BioMapperAuthError as exc:
                raise _Fatal(tools.explain_client_error(exc)) from None
            elapsed = cfg.monotonic() - started
            for warned in caught:  # recorded, not shown: pass the others on to the log
                if "Batch order mismatch" not in str(warned.message):
                    logger.warning("%s: %s", warned.category.__name__, warned.message)
            if any("Batch order mismatch" in str(w.message) for w in caught):
                raise _Fatal(
                    "BioMapper returned a batch out of request order, so its results cannot be "
                    "trusted to belong to the names they were returned for. The job was stopped "
                    "and no report was written."
                )
            failure = tools.systemic_error(got, len(batch))
            if failure is not None and len(batch) == 1 and not tools.looks_systemic(failure):
                failure = None  # one row, one error: that row's own answer
            if failure is not None:
                self._bad_chunks += 1
                if self._bad_chunks >= cfg.stall_bad_chunks:
                    raise _Stalled(
                        f"{self._bad_chunks} chunks in a row failed as a whole ({failure}). "
                        "Stopped sending requests; resume_job retries after a health check."
                    )
                if cfg.systemic_backoff_s > 0:  # gate released: interactive calls go through
                    await anyio.sleep(cfg.systemic_backoff_s)
                continue
            self._bad_chunks = 0
            m._last_good_chunk = cfg.monotonic()
            good = [(keys[i], res) for i, res in zip(pending, got, strict=True)
                    if res.error is None]
            self._batch_index += 1
            cache.commit_batch(vocab=None, batch_index=self._batch_index, keyed_results=good)
            again: list[int] = []
            for i, res in zip(pending, got, strict=True):
                if res.error is None:
                    results[i] = res
                    continue
                tries = self._attempts[keys[i]] = self._attempts.get(keys[i], 0) + 1
                if tries >= cfg.max_row_attempts:
                    results[i] = res
                else:
                    again.append(i)
            if good:
                job.entities_mapped += len(good)
                per_entity = elapsed / len(batch)
                job.seconds_per_entity = round(
                    0.7 * job.seconds_per_entity + 0.3 * per_entity, 3
                )
            # Only counters change here; a cancel accepted during the await keeps its state.
            m._save(job)
            if elapsed > cfg.stall_chunk_s:
                raise _Stalled(
                    f"Prod is very slow: one chunk of {len(batch)} took {elapsed:.0f} s. "
                    "Stopped sending requests; resume_job retries after a health check."
                )
            pending = again
        return [results[i].model_copy() for i in range(len(records))]

    # -- the run -------------------------------------------------------------

    def _call(
        self, job: HarmonizeJob, pins: dict[str, Any], staging: Path, mapper: GatedJobMapper,
        resumed: bool,
    ) -> ManyCohortReport | CohortHarmonizationReport:
        """The unchanged function, on the worker thread, writing into staging."""
        cfg = self.m.config

        def quiet(message: str) -> None:  # stdout belongs to the MCP transport
            logger.info("%s", message)

        common: dict[str, Any] = {
            "entity": job.entity, "category": job.category, "batch_size": mapper.batch_size,
            "timeout": cfg.job_timeout_s, "mapper": mapper, "base_url": cfg.base_url,
            "api_key": cfg.api_key, "kestrel_url": cfg.kestrel_url, "pins": pins,
            "output_dir": staging, "save": True, "on_message": quiet,
        }
        with _private_umask():
            return self._call_function(job, common, resumed)

    @staticmethod
    def _call_function(
        job: HarmonizeJob, common: dict[str, Any], resumed: bool
    ) -> ManyCohortReport | CohortHarmonizationReport:
        if job.kind == "harmonize_many":
            return harmonize_many(
                {c["label"]: c["path"] for c in job.cohorts},
                name_columns={c["label"]: c["name_column"] for c in job.cohorts},
                vocabularies={c["label"]: c["vocabularies"] for c in job.cohorts
                              if c["vocabularies"]} or None,
                key_columns={c["label"]: c["key_column"] for c in job.cohorts
                             if c["key_column"]} or None,
                store_dir=job.store_dir,
                # Passing remap_all again on resume would move this job's own saved chunks aside.
                remap_all=bool(job.options.get("remap_all")) and not resumed,
                **common,
            )
        a, b = job.cohorts
        return harmonize_cohorts(
            a["path"], b["path"],
            a_name_column=a["name_column"], b_name_column=b["name_column"],
            a_vocabularies=a["vocabularies"] or None, b_vocabularies=b["vocabularies"] or None,
            a_key_column=a["key_column"], b_key_column=b["key_column"],
            a_label=a["label"], b_label=b["label"],
            **common,
        )

    def _discard_cache(self, job: HarmonizeJob) -> None:
        """Drop everything the invalidated run produced: its mappings AND its review evidence.

        The review index is matched to a rebuilt list by content and build label, and two
        unverified builds carry the same label, so keeping it would show fresh mappings beside
        candidates and node names captured on the earlier, unverified build.
        """
        from biomapper.mcp import review_entries

        jdir = self.m._dir(job.job_id)
        shutil.rmtree(jdir / CACHE_DIR, ignore_errors=True)
        review_entries.discard(jdir)
        job.entities_mapped = 0
        job.review = None

    def _check_store(self, job: HarmonizeJob, category: str) -> None:
        """Refuse a saved-mapping store with a link anywhere the unchanged writer will go.

        Submit checks the store root only. ``MappingStore`` then creates and opens
        ``<store>/<arm>/<category>/`` and its files with plain calls that follow links, so a
        link at any of those levels would take the job's writes outside the allowed folders.
        """
        if job.store_dir is None:
            return
        root = Path(job.store_dir)
        roots = self.m._run_roots.get(job.job_id, [])
        for arm in ARMS:
            leaf = root / arm / _safe_dir(category)
            parts = [root, root / arm, leaf]
            if leaf.is_dir() and not leaf.is_symlink():
                parts.extend(leaf.iterdir())
            for part in parts:
                if part.is_symlink():
                    raise JobError(
                        f"{part} in the saved-mapping store is a link; a harmonize job never "
                        "writes through links. Nothing was written. Remove the link, or pass "
                        "another store_dir."
                    )
            if not _under(leaf.resolve(), roots):  # a missing tail is allowed
                raise JobError(
                    f"{leaf} in the saved-mapping store is outside the folders allowed in this "
                    "session. Nothing was written."
                )
            self._privatize_leaf(leaf)

    @staticmethod
    def _privatize_leaf(leaf: Path) -> None:
        """Owner-only for the one store folder this job appends cohort data to, before it does.

        The private umask covers what the job creates. A store made earlier by a Python API run
        under an ordinary umask already has a readable folder and cache file, and the unchanged
        writer appends to that same file, so the new rows would be readable too. Only this
        ``<store>/<arm>/<category>/`` folder and the files directly in it are changed: the store
        root and anything else under it keep their modes. A readable entry that belongs to another
        user cannot be made private, so the job is refused before any request.
        """
        if not leaf.is_dir():
            return  # created by the run, under the private umask
        uid = os.getuid() if hasattr(os, "getuid") else None
        for entry in [leaf, *sorted(leaf.iterdir())]:
            info = entry.lstat()
            is_dir = stat.S_ISDIR(info.st_mode)
            if not (is_dir or stat.S_ISREG(info.st_mode)) or (is_dir and entry != leaf):
                continue
            if stat.S_IMODE(info.st_mode) & 0o077 == 0:
                continue
            if uid is not None and info.st_uid != uid:
                raise JobError(
                    f"{entry} in the saved-mapping store is readable by other users and belongs "
                    "to another user, so this job cannot make it private before adding cohort "
                    "data to it. Nothing was written. Pass another store_dir."
                )
            os.chmod(entry, 0o700 if is_dir else 0o600)

    def _open_caches(self, job: HarmonizeJob, pin: dict[str, Any]) -> dict[str, EntityCache]:
        root = self.m._dir(job.job_id) / CACHE_DIR
        private_dir(root)
        caches: dict[str, EntityCache] = {}
        for mode in ANNOTATION_MODES:
            directory = root / mode
            private_dir(directory)
            caches[mode] = EntityCache(directory, {**pin, "annotation_mode": mode})
        _chmod_tree(root)
        return caches

    async def run(self, job: HarmonizeJob) -> None:
        m = self.m
        cfg = m.config
        m._require_within_roots(job)
        for cohort in job.cohorts:
            source = Path(cohort["path"])
            try:
                sha = await anyio.to_thread.run_sync(_sha256, source)
            except OSError:
                m._finish(job, "failed", f"Input changed: {source} can no longer be read.")
            if sha != cohort["sha256"]:
                m._finish(
                    job, "failed",
                    f"{source.name} changed since the job was queued, so cached mappings may "
                    "not match it. Submit the harmonize call again (the new content gets a new "
                    "job).",
                )
        self._check_placeable(job, names=None)

        if job.job_id in m._needs_health:
            m._needs_health.discard(job.job_id)
            problem = await m._health_ok()
            if problem is not None:
                m._finish(job, "stalled", f"Health check before resuming failed: {problem}")
        m._stop_if_cancelled(job)

        pins = await probe_pins(cfg, m.gate)
        m._stop_if_cancelled(job)
        category = resolve_category(job.entity, job.category)
        new_pin = _cache_pin(pins, cfg.base_url,
                             {"entity_type": category, "annotation_mode": "missing"})
        jdir = m._dir(job.job_id)
        note: str | None = None
        if job.pin is not None and job.pin_session != m.session_id:
            if not _pin_known(job.pin):
                self._discard_cache(job)
                note = (
                    "Mapping restarted from the start: the earlier entities were cached while "
                    "the KG build was unverified (Kestrel unreachable), so they cannot be "
                    "trusted across sessions."
                )
            elif not _pin_known(new_pin):
                m._finish(
                    job, "stalled",
                    "Cannot verify that the KG build still matches the cached entities (Kestrel "
                    "unreachable). resume_job once Kestrel answers.",
                )
        try:
            caches = await anyio.to_thread.run_sync(self._open_caches, job, new_pin)
        except CachePinMismatch:
            m._finish(
                job, "failed",
                "The KG build changed since this job's entities were cached; mixing builds in "
                "one report is refused. Submit the harmonize call with a new output_dir to "
                "start over on the current build.",
            )
        # A cancel accepted during the awaits above must not be overwritten by "running".
        m._stop_if_cancelled(job)
        write_private(jdir / PINS_FILE, json.dumps(pins, indent=2, sort_keys=True))
        resumed = job.runs_started > 0
        job.pin = new_pin
        job.pin_session = m.session_id
        job.kg_build = kg_build_label(pins)
        job.kg_verified = kg_verified(pins)
        job.state = "running"
        job.stage = "mapping"
        job.reason = note
        job.runs_started += 1
        m._save(job)

        staging = jdir / STAGING_DIR
        shutil.rmtree(staging, ignore_errors=True)
        private_dir(staging)
        await anyio.to_thread.run_sync(self._check_store, job, category)
        self._bad_chunks = 0
        self._attempts = {}
        mapper = GatedJobMapper(self, job, caches)
        try:
            report = await anyio.to_thread.run_sync(
                functools.partial(self._call, job, pins, staging, mapper, resumed)
            )
        except _Cancelled:
            raise _Stop from None  # cancelled (state already saved) or shutting down (resumable)
        except _Stalled as exc:
            m._finish(job, "stalled", exc.reason)
        except _Fatal as exc:
            m._finish(job, "failed", exc.reason)
        except (StoreBuildMismatch, StoreLocked) as exc:
            m._finish(job, "failed", f"{str(exc)[:1].upper()}{str(exc)[1:]}")
        finally:
            await anyio.to_thread.run_sync(self._tighten, job, staging)
        m._stop_if_cancelled(job)

        job.rows_errored = _errored_rows(report)
        summary = _summary(job, report, resumed)
        await self._review(job, report, staging, pins, category)
        m._stop_if_cancelled(job)

        job.stage = "placing"
        m._save(job)
        m._require_within_roots(job)
        await anyio.to_thread.run_sync(self._place, job, staging)
        await anyio.to_thread.run_sync(functools.partial(shutil.rmtree, staging,
                                                         ignore_errors=True))
        job.summary = summary
        job.stage = "done"
        job.completed_utc = utc(cfg.wall_clock())
        if job.rows_errored:
            m._finish(
                job, "partial",
                f"{job.rows_errored} row(s) errored and are marked as errors in the report, "
                "never as unresolved. This job is finished. To retry them, submit again with a "
                "new output_dir; a harmonize_many resubmission reuses the saved mappings, so "
                "only the errored rows are mapped.",
            )
        m._finish(job, "complete", job.reason)

    async def _review(
        self, job: HarmonizeJob, report: Any, staging: Path, pins: dict[str, Any],  # noqa: ANN401
        category: str,
    ) -> None:
        """Build the review items and capture their evidence (see review_entries)."""
        from biomapper.mcp import review_entries

        await review_entries.build_and_capture(self, job, report, staging, pins, category)

    def _tighten(self, job: HarmonizeJob, staging: Path) -> None:
        """Owner-only for the job's OWN directories (belt and braces: they are created private).

        The saved-mapping store is not touched here: it may be a shared or pre-existing folder.
        What the job creates there is private from creation (see :func:`_private_umask`), and the
        one folder it appends to is made private before the run (see :meth:`_privatize_leaf`).
        """
        _chmod_tree(staging)
        _chmod_tree(self.m._dir(job.job_id) / CACHE_DIR)

    # -- placement -----------------------------------------------------------

    def _check_placeable(self, job: HarmonizeJob, names: list[str] | None) -> None:
        """Refuse before writing anything if any report file's place is taken.

        ``names=None`` (before the run) checks for any report file; with the staged names (at
        placement) it checks exactly those. A file this job recorded as its own, byte for byte,
        is an interrupted placement of this job and is not a conflict.
        """
        output = job.output
        if output.is_symlink():
            raise JobError(f"{output} became a link while the job ran; not overwritten.")
        if not output.exists():
            return
        if not output.is_dir():
            raise JobError(f"{output} is not a folder; not overwritten.")
        entries = ([output / n for n in names] if names is not None
                   else [e for e in output.iterdir() if _matches_report(e.name)])
        for entry in entries:
            if not (entry.exists() or entry.is_symlink()):
                continue
            if self._own(job, entry):
                continue
            raise JobError(
                f"{entry} appeared in the output folder while the job ran; not overwritten. "
                "Nothing was written. Move it away and submit the same harmonize call again."
            )

    @staticmethod
    def _own(job: HarmonizeJob, path: Path) -> bool:
        recorded = job.placed.get(path.name)
        if recorded is None or path.is_symlink() or not path.is_file():
            return False
        try:
            return _sha256(path) == recorded
        except OSError:
            return False

    def _place(self, job: HarmonizeJob, staging: Path) -> None:
        """Copy the staged report into the output folder: checked first, then all or nothing
        this job can tell apart, owner-only, never through a link, never over a file."""
        names = sorted(p.name for p in staging.iterdir() if p.is_file())
        self._check_placeable(job, names)
        output = job.output
        if not output.exists():
            output.mkdir(mode=0o700)
            os.chmod(output, 0o700)
        for name in names:  # files this job placed before an interruption; rebuilt below
            if self._own(job, output / name):
                (output / name).unlink()
            # A temp file of this job (its id is in the name) left by a crash mid-write.
            self._temp(job, output, name).unlink(missing_ok=True)
        # Record ownership BEFORE placing: a crash mid-placement must still find them ours.
        job.placed = {name: _sha256(staging / name) for name in names}
        self.m._save(job)
        for name in names:
            # Written in full under a temp name, then linked to its final name: a crash or a
            # failed write never leaves a partial file under a report name (which this job
            # could not tell from somebody else's and would refuse on every retry). os.link
            # fails if the name exists, link or file, so the no-overwrite guarantee holds.
            final, tmp = output / name, self._temp(job, output, name)
            data = (staging / name).read_bytes()
            try:
                self._write_new(tmp, data)
                try:
                    os.link(tmp, final)
                except FileExistsError:
                    raise
                except OSError:  # a filesystem without hard links: exclusive create instead
                    self._write_new(final, data)
            except FileExistsError:
                raise JobError(
                    f"{final} appeared while the report was being written; not overwritten. "
                    "Move it away and submit the same harmonize call again."
                ) from None
            finally:
                tmp.unlink(missing_ok=True)

    @staticmethod
    def _write_new(path: Path, data: bytes) -> None:
        """Create ``path`` owner-only with ``data``; never through a link, never over a file."""
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            # The exclusive create succeeded, so the file is this job's. Without this, a failed
            # write on a filesystem with no hard links (where the final name is written
            # directly) would leave a partial report this job could not tell from a stranger's.
            with contextlib.suppress(OSError):
                os.unlink(path)
            raise

    @staticmethod
    def _temp(job: HarmonizeJob, output: Path, name: str) -> Path:
        return output / f".{name}.{job.job_id}.tmp"


def _errored_rows(report: Any) -> int:  # noqa: ANN401 — either report class
    """Rows whose mapping errored in any arm (each row counted once)."""
    seen: set[tuple[str, str]] = set()
    if isinstance(report, ManyCohortReport):
        for arm in report.arms.values():
            for label, results in arm.results.items():
                for record, result in zip(arm.records[label], results, strict=True):
                    if result.error:
                        seen.add((label, record.key))
        return len(seen)
    for by_label in report.mappings.values():
        for label, rows in by_label.items():
            for record, _sent, result in rows:
                if result.error:
                    seen.add((label, record.key))
    return len(seen)


def _summary(job: HarmonizeJob, report: Any, resumed: bool) -> dict[str, Any]:  # noqa: ANN401
    """The compact summary job_status returns. Counts only; the tables stay in output_dir."""
    full = report.summary()
    if isinstance(report, ManyCohortReport):
        arm_keys = ("groups", "multi_cohort_groups", "clean_groups", "flagged_groups",
                    "cross_node_entries", "cross_node_row_pairs", "per_cohort")
        out: dict[str, Any] = {
            "status": full["status"],
            "publication_grade": full["publication_grade"],
            "kg_build": full["kg_build"],
            "kg_verified": full["kg_verified"],
            "labels": full["labels"],
            "arms": {
                name: ({k: arm[k] for k in arm_keys} if arm is not None else None)
                for name, arm in full["arms"].items()
            },
            "identifier_arm_skipped": full["identifier_arm_skipped"],
            "mapping": {
                arm: {label: {k: p[k] for k in ("n_rows", "n_reused", "n_fresh", "n_errored")}
                      for label, p in by_label.items()}
                for arm, by_label in full["mapping"].items()
            },
            "store": {k: full["store"][k] for k in ("reuse_enabled", "saving", "reason")},
            "warnings": full["warnings"],
        }
        if resumed and job.options.get("remap_all") and out["publication_grade"] == (
            GRADE_ITERATION
        ):
            out["grade_note"] = GRADE_NOTE
    else:
        out = {
            "status": "partial" if job.rows_errored else "ok",
            "labels": [full["labels"]["a"], full["labels"]["b"]],
            "arms": full["arms"],
            "identifier_arm_skipped": full["identifier_arm_skipped"],
            "diff": full["diff"],
            "one_sided_code_review": full["review"],
            "vocabularies": full["vocabularies"],
            "warnings": full["warnings"],
        }
    out["output_dir"] = job.output_dir
    return out
