"""Resumable background file-mapping jobs (``map_file``, ``job_status``, ``resume_job``,
``cancel_job``).

A researcher's file can hold thousands of entities and prod answers about 4.4 s each, so a file
is never mapped inside one tool call. ``map_file`` validates synchronously and queues a job; one
worker coroutine maps it in chunks of 5, taking the request gate per chunk and releasing it
between chunks so interactive calls interleave.

Durability, per chunk:

* successful rows go to the fsynced :class:`~biomapper._checkpoint.EntityCache`, keyed on the
  work (name + identifiers + options), so a resume re-maps nothing that was answered;
* ``job.json`` (state, counters, per-row attempts) is replaced atomically;
* rows are appended to ``<output>.partial``, which is clearly not the final file. At completion
  the output is rebuilt from cache + input in input order, written to a temp file, and moved into
  place without overwriting anything.

Ownership is an OS advisory lock on ``owner.lock`` in the job directory. The OS releases it when
the owning process dies, which is what turns a "running" job into a "paused" one that the next
session resumes.

KG provenance follows the benchmark rule: two unknown builds compare equal, so a cache written
while the KG build was unknown is trusted only by the process that wrote it. A new process
discards it and restarts from row 0. A cache pinned to a known build that meets a different known
build fails the job rather than mixing two builds into one file.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import datetime as dt
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Literal, NoReturn
from urllib.parse import unquote, urlparse

import anyio
import anyio.to_thread
from pydantic import AnyUrl, BaseModel, Field

from biomapper._checkpoint import CachePinMismatch, EntityCache, cache_key
from biomapper._pins import api_health, collect_pins, kg_build_label, kg_verified
from biomapper._provenance import UNKNOWN
from biomapper._version import resolve_version
from biomapper.client import BioMapperClient
from biomapper.cohorts import _blank, _read_delimited, clean_id
from biomapper.exceptions import BioMapperError
from biomapper.mcp import tools
from biomapper.mcp.config import ServerConfig
from biomapper.mcp.gate import GateBusy, RequestGate
from biomapper.models import MappingResult

logger = logging.getLogger("biomapper.mcp.jobs")

JobState = Literal[
    "queued", "running", "paused", "stalled", "expired", "cancelled", "complete", "failed",
    "owned_elsewhere",
]
UNFINISHED = frozenset({"queued", "running", "stalled"})
RESUMABLE = frozenset({"stalled", "expired", "paused", "cancelled"})

OUTPUT_COLUMNS = [
    "key", "name", "identifiers_sent", "status", "chosen_kg_id", "primary_curie", "resolved",
    "confidence_score", "confidence_tier", "refusal_reason", "kg_equivalent_ids", "error",
]
JOB_FILE = "job.json"
LOCK_FILE = "owner.lock"
OWNER_FILE = "owner.json"
PINS_FILE = "pins.json"

JOB_TOOL_DESCRIPTIONS = {
    "map_file": (
        "Map every row of a TSV/CSV file as a resumable BACKGROUND job and return immediately "
        "with a job_id and an ETA (about 4-5 s per unique entity). Use this for more than 5 "
        "entities. The file must be inside an allowed folder (the client's roots or "
        "BIOMAPPER_MCP_ROOTS). Writes <input>.biomapper.tsv next to the input (never overwrites) "
        "plus a provenance JSON. identifier_columns maps a column name to its vocabulary, e.g. "
        "{\"hmdb_id\": \"HMDB\"}. Re-submitting the same file and options returns the existing "
        "job; rerun=true starts over with a fresh cache. Poll job_status about once a minute."
    ),
    "job_status": (
        "Status of one map_file job (job_id) or of all jobs in the current folders: state "
        "(queued, running, paused, stalled, expired, cancelled, complete, failed, "
        "owned_elsewhere), progress, ETA estimate, output path, and whether the KG build is "
        "verified. Makes no request to BioMapper."
    ),
    "resume_job": (
        "Resume a stalled, expired, paused, or cancelled job after a fresh health check. Rows "
        "already mapped are not re-mapped."
    ),
    "cancel_job": (
        "Cancel a job. It stops after the current chunk; the partial output and cache are kept, "
        "so resume_job can continue it later."
    ),
}


class JobError(Exception):
    """A refusal the agent should read: validation, roots, ownership, unknown job."""


class JobOwner(BaseModel):
    host: str | None = None
    pid: int | None = None


class JobStatus(BaseModel):
    job_id: str
    state: JobState
    reason: str | None = None
    input_path: str
    output_path: str
    partial_path: str
    provenance_path: str
    rows_total: int
    entities_total: int = Field(description="Unique entities (duplicate rows are mapped once)")
    entities_done: int
    entities_failed: int = 0
    pass_index: int = 0
    eta_s: float | None = Field(default=None, description="Best-effort estimate, not a promise")
    kg_build: str = UNKNOWN
    kg_verified: bool = False
    owner: JobOwner | None = None
    created_utc: str
    updated_utc: str


class JobStatusList(BaseModel):
    jobs: list[JobStatus]
    note: str | None = None


# ---------------------------------------------------------------------------
# Files and paths
# ---------------------------------------------------------------------------


def roots_from_uris(uris: list[Any]) -> list[Path]:
    """``file://`` root URIs (or Root objects) to paths. Non-file roots are ignored."""
    paths: list[Path] = []
    for item in uris:
        uri = item.uri if hasattr(item, "uri") else item
        parsed = urlparse(str(uri) if isinstance(uri, AnyUrl) else str(uri))
        if parsed.scheme == "file":
            paths.append(Path(unquote(parsed.path)))
    return paths


def _utc(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).isoformat()


def _parse_utc(value: str) -> float:
    return dt.datetime.fromisoformat(value).timestamp()


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def _write_private(path: Path, text: str) -> None:
    """Atomically replace ``path`` with ``text``, readable by the owner only."""
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _under(path: Path, roots: list[Path]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


_JOB_ID = re.compile(r"[0-9a-f]{16}")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _valid_job_id(job_id: str) -> bool:
    """Job IDs are generated 16-hex-character digests; anything else never names a directory."""
    return isinstance(job_id, str) and _JOB_ID.fullmatch(job_id) is not None


def _resolved_under(path: Path, roots: list[Path]) -> bool:
    """``path`` re-resolved now (symlinks followed, missing tail allowed) is inside ``roots``."""
    try:
        resolved = path.parent.resolve(strict=True) / path.name
    except OSError:
        return False
    if resolved.is_symlink():
        try:
            resolved = resolved.resolve(strict=True)
        except OSError:
            return False
    return _under(resolved, roots)


def _create_exclusive(path: Path) -> IO[str]:
    """Open a new file for writing; refuses an existing name, including a symlink."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o644)
    return os.fdopen(fd, "w", encoding="utf-8", newline="")


async def probe_pins(config: ServerConfig, gate: RequestGate) -> dict[str, Any]:
    """Collect pins with the BioMapper ``/health`` probe held behind the shared request gate.

    The Kestrel probe does not touch the BioMapper API, so it runs outside the gate.
    """
    async with gate.hold(wait_budget_s=None):
        api = await anyio.to_thread.run_sync(
            lambda: api_health(config.base_url, config.api_key), abandon_on_cancel=True
        )
    return await anyio.to_thread.run_sync(
        lambda: collect_pins(
            probe=True,
            base_url=config.base_url,
            api_key=config.api_key,
            kestrel_url=config.kestrel_url,
            api=api,
        ),
        abandon_on_cancel=True,
    )


class JobLock:
    """An OS advisory lock held while this process owns a job; released by the OS on death."""

    def __init__(self, directory: Path) -> None:
        self.path = directory / LOCK_FILE
        self.owner_path = directory / OWNER_FILE
        self._fh: IO[bytes] | None = None

    def acquire(self, *, record: bool = True) -> bool:
        """Take the lock without blocking. ``record=False`` only probes (owner.json untouched)."""
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        fh = os.fdopen(fd, "r+b")
        try:
            _lock_nonblocking(fh)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        if not record:
            return True
        _write_private(
            self.owner_path, json.dumps({"host": socket.gethostname(), "pid": os.getpid()})
        )
        return True

    def owner(self) -> JobOwner:
        try:
            return JobOwner.model_validate_json(self.owner_path.read_text())
        except (OSError, ValueError):
            return JobOwner()

    def release(self) -> None:
        if self._fh is not None:
            with contextlib.suppress(OSError):
                _unlock(self._fh)
            self._fh.close()
            self._fh = None


if sys.platform == "win32":  # pragma: no cover - best effort, documented in the README
    import msvcrt

    def _lock_nonblocking(fh: IO[bytes]) -> None:
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(fh: IO[bytes]) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock_nonblocking(fh: IO[bytes]) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fh: IO[bytes]) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Job record
# ---------------------------------------------------------------------------


@dataclass
class InputRow:
    key: str
    name: str
    identifiers: dict[str, str]
    cache_key: str | None  # None for a blank name, which is never sent


@dataclass
class Job:
    """The persisted job record (``job.json``)."""

    job_id: str
    input_path: str
    input_sha256: str
    output_path: str
    options: dict[str, Any]
    state: str = "queued"
    reason: str | None = None
    rows_total: int = 0
    entities_total: int = 0
    entities_done: int = 0
    entities_failed: int = 0
    pass_index: int = 0
    attempts: dict[str, int] = field(default_factory=dict)
    last_errors: dict[str, str] = field(default_factory=dict)
    seconds_per_entity: float = 4.4
    pin: dict[str, Any] | None = None
    pin_session: str | None = None
    kg_build: str = UNKNOWN
    kg_verified: bool = False
    created_utc: str = ""
    updated_utc: str = ""
    completed_utc: str | None = None

    @property
    def output(self) -> Path:
        return Path(self.output_path)

    @property
    def partial(self) -> Path:
        return self.output.with_name(self.output.name + ".partial")

    @property
    def provenance(self) -> Path:
        name = self.output.name
        stem = name[: -len(".tsv")] if name.endswith(".tsv") else name
        return self.output.with_name(stem + ".provenance.json")

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=2, sort_keys=True)

    @classmethod
    def load(cls, path: Path) -> Job:
        data = json.loads(path.read_text())
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def _job_id(input_sha: str, options: dict[str, Any], output_path: str) -> str:
    blob = json.dumps(
        {"input_sha256": input_sha, "options": options, "output_path": output_path},
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _read_rows(path: Path, options: dict[str, Any]) -> list[InputRow]:
    rows, columns = _read_delimited(path)
    if not columns:
        raise JobError(
            f"{path.name} has no data rows: it is empty or holds only '#' comment lines. The "
            f"first non-comment line must be a header row."
        )
    name_column = options["name_column"]
    id_columns: dict[str, str] = options.get("identifier_columns") or {}
    key_column = options.get("key_column")
    missing = [c for c in [name_column, *id_columns, *([key_column] if key_column else [])]
               if c not in columns]
    if missing:
        shown = ", ".join(columns) if columns else "(none)"
        raise JobError(
            f"Column(s) {', '.join(repr(m) for m in missing)} not found in {path.name}. "
            f"Columns present: {shown}."
        )
    request_options = {
        "entity_type": options["entity_type"],
        "annotation_mode": options["annotation_mode"],
    }
    out: list[InputRow] = []
    for index, row in enumerate(rows, start=1):
        name = "" if _blank(row.get(name_column)) else str(row[name_column]).strip()
        identifiers: dict[str, str] = {}
        for column, vocab in id_columns.items():
            value = clean_id(row.get(column))
            if value is not None:
                identifiers[vocab] = value
        key = str(row[key_column]) if key_column and not _blank(row.get(key_column)) else str(
            index
        )
        ck = cache_key(name, None, identifiers, request_options) if name else None
        out.append(InputRow(key=key, name=name, identifiers=identifiers, cache_key=ck))
    return out


def _output_row(row: InputRow, result: MappingResult | None, error: str | None) -> list[Any]:
    sent = json.dumps(row.identifiers, sort_keys=True) if row.identifiers else ""
    if result is None:
        message = error or "not mapped"
        return [row.key, row.name, sent, "error", "", "", False, "", "unknown", "",
                "", message]
    shaped = tools.shape_result(result)
    return [
        row.key,
        row.name,
        sent,
        shaped.status,
        result.chosen_kg_id or "",
        result.primary_curie or "",
        shaped.status == "resolved",
        "" if result.confidence_score is None else result.confidence_score,
        result.confidence_tier,
        result.refusal_reason or "",
        json.dumps(result.kg_equivalent_ids, sort_keys=True) if result.kg_equivalent_ids else "",
        shaped.error or "",
    ]


def _tsv_line(values: list[Any]) -> str:
    import io

    buf = io.StringIO()
    csv.writer(buf, delimiter="\t", lineterminator="\n").writerow(
        ["" if v is None else v for v in values]
    )
    return buf.getvalue()


def _cache_pin(pins: dict[str, Any], base_url: str, options: dict[str, Any]) -> dict[str, Any]:
    kestrel = pins.get("kestrel") or {}
    kg = kestrel.get("kg_build") or {}
    return {
        "api_endpoint": base_url,
        "kestrel_version": kestrel.get("kestrel_version") or UNKNOWN,
        "kg_version": kg.get("kg_version") or UNKNOWN,
        "kg_git_commit": kg.get("git_commit") or UNKNOWN,
        "entity_type": options["entity_type"],
        "annotation_mode": options["annotation_mode"],
    }


def _pin_known(pin: dict[str, Any] | None) -> bool:
    return bool(pin) and (pin or {}).get("kg_version", UNKNOWN) != UNKNOWN


class _Stop(Exception):
    """Internal: the job left the running state (stalled, failed, cancelled)."""


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class JobManager:
    """Owns this process's jobs: the registry on disk, the locks, the queue, the worker."""

    def __init__(self, config: ServerConfig, *, gate: RequestGate, client: BioMapperClient) -> None:
        self.config = config
        self.gate = gate
        self.client = client
        self.session_id = uuid.uuid4().hex
        self.jobs_dir = config.home / "jobs"
        self._locks: dict[str, JobLock] = {}
        self._jobs: dict[str, Job] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._queued: set[str] = set()
        self._needs_health: set[str] = set()
        self._seen_roots: set[Path] = set()
        self._last_good_chunk: float | None = None
        self._busy_job: str | None = None
        self._run_roots: dict[str, list[Path]] = {}

    # -- roots ---------------------------------------------------------

    def allowed_roots(self, client_roots: list[Path]) -> list[Path]:
        roots: list[Path] = []
        for root in [*client_roots, *self.config.env_roots]:
            try:
                resolved = root.expanduser().resolve(strict=True)
            except OSError:
                continue
            if resolved.is_dir() and resolved not in roots:
                roots.append(resolved)
        return roots

    def _require_roots(self, client_roots: list[Path]) -> list[Path]:
        roots = self.allowed_roots(client_roots)
        if not roots:
            raise JobError(
                "map_file can only read and write inside allowed folders, and none are set. "
                "This client did not share any workspace roots. Set BIOMAPPER_MCP_ROOTS to the "
                "folder(s) holding your data (separate several with "
                f"'{os.pathsep}') in the MCP server's env and restart it. The map_entity and "
                "map_batch tools work without it."
            )
        return roots

    # -- disk helpers -------------------------------------------------

    def _dir(self, job_id: str) -> Path:
        return self.jobs_dir / job_id

    def _save(self, job: Job) -> None:
        job.updated_utc = _utc(self.config.wall_clock())
        _write_private(self._dir(job.job_id) / JOB_FILE, job.to_json())

    def _load(self, job_id: str) -> Job | None:
        if not _valid_job_id(job_id):
            return None
        if job_id in self._jobs:
            return self._jobs[job_id]
        path = self._dir(job_id) / JOB_FILE
        if not path.is_file():
            return None
        try:
            job = Job.load(path)
        except (OSError, ValueError, TypeError):
            logger.warning("unreadable job record %s", path)
            return None
        return job if job.job_id == job_id else None

    def _require_job(self, job_id: str) -> Job:
        if not _valid_job_id(job_id):
            raise JobError(
                f"{job_id!r} is not a job ID; job IDs are 16 lowercase hex characters, as "
                "returned by map_file or job_status."
            )
        job = self._load(job_id)
        if job is None:
            raise JobError(f"No job {job_id!r}.")
        return job

    def _all_job_ids(self) -> list[str]:
        if not self.jobs_dir.is_dir():
            return []
        return sorted(
            p.name for p in self.jobs_dir.iterdir()
            if _valid_job_id(p.name) and (p / JOB_FILE).is_file()
        )

    @staticmethod
    def _within(job: Job, roots: list[Path]) -> bool:
        """Every path the job reads or writes is, re-resolved now, inside ``roots``."""
        if not roots:
            return False
        return all(
            _resolved_under(p, roots)
            for p in (Path(job.input_path), job.output, job.partial, job.provenance)
        )

    def _take(self, job: Job) -> bool:
        """Acquire ownership of ``job``. True if this process now owns it."""
        if job.job_id in self._locks:
            return True
        lock = JobLock(self._dir(job.job_id))
        if not lock.acquire():
            return False
        self._locks[job.job_id] = lock
        fresh = self._load_from_disk(job.job_id) or job
        self._jobs[job.job_id] = fresh
        return True

    def _load_from_disk(self, job_id: str) -> Job | None:
        if not _valid_job_id(job_id):
            return None
        path = self._dir(job_id) / JOB_FILE
        try:
            job = Job.load(path)
        except (OSError, ValueError, TypeError):
            return None
        return job if job.job_id == job_id else None

    def _enqueue(self, job: Job, *, health: bool, roots: list[Path]) -> None:
        self._run_roots[job.job_id] = list(roots)
        if health:
            self._needs_health.add(job.job_id)
        if job.job_id not in self._queued:
            self._queued.add(job.job_id)
            self._queue.put_nowait(job.job_id)

    def close(self) -> None:
        for lock in self._locks.values():
            lock.release()
        self._locks.clear()

    # -- status --------------------------------------------------------

    def _eta(self, job: Job) -> float | None:
        if job.state in ("complete", "failed", "cancelled"):
            return None
        remaining = max(job.entities_total - job.entities_done - job.entities_failed, 0)
        return round(remaining * job.seconds_per_entity, 1)

    def _status(self, job: Job) -> JobStatus:
        state: str = job.state
        owner: JobOwner | None = None
        if job.job_id not in self._locks and state in UNFINISHED | {"expired", "cancelled"}:
            lock = JobLock(self._dir(job.job_id))
            if lock.acquire(record=False):
                lock.release()
                if state in ("queued", "running"):
                    state = "paused"
            else:
                state = "owned_elsewhere"
                owner = lock.owner()
        return JobStatus(
            job_id=job.job_id,
            state=state,  # type: ignore[arg-type]
            reason=job.reason,
            input_path=job.input_path,
            output_path=job.output_path,
            partial_path=str(job.partial),
            provenance_path=str(job.provenance),
            rows_total=job.rows_total,
            entities_total=job.entities_total,
            entities_done=job.entities_done,
            entities_failed=job.entities_failed,
            pass_index=job.pass_index,
            eta_s=self._eta(job),
            kg_build=job.kg_build,
            kg_verified=job.kg_verified,
            owner=owner,
            created_utc=job.created_utc,
            updated_utc=job.updated_utc,
        )

    async def status(self, job_id: str | None, *, client_roots: list[Path]) -> JobStatusList:
        roots = self.allowed_roots(client_roots)
        await self._note_roots(roots)
        if job_id is not None:
            job = self._require_job(job_id)
            if not self._within(job, roots):
                raise JobError(
                    f"Job {job_id!r} reads or writes files outside the folders allowed in this "
                    "session."
                )
            return JobStatusList(jobs=[self._status(job)])
        jobs = [j for j in (self._load(i) for i in self._all_job_ids()) if j is not None]
        visible = [j for j in jobs if self._within(j, roots)]
        visible.sort(key=lambda j: j.created_utc)
        note = None if roots else "No allowed folders in this session, so no jobs are listed."
        return JobStatusList(jobs=[self._status(j) for j in visible], note=note)

    # -- validation ----------------------------------------------------

    def _resolve_input(self, path: str, roots: list[Path]) -> Path:
        raw = Path(path).expanduser()
        candidates = [raw] if raw.is_absolute() else [root / raw for root in roots]
        for candidate in candidates:
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                continue
            if not _under(resolved, roots):
                raise JobError(
                    f"{path} resolves to a location outside the allowed folders "
                    f"({', '.join(str(r) for r in roots)}). map_file only reads files inside them."
                )
            if not resolved.is_file():
                raise JobError(f"{path} is not a file.")
            if not os.access(resolved, os.R_OK):
                raise JobError(f"{path} is not readable.")
            return resolved
        raise JobError(f"Input file {path} does not exist.")

    def _resolve_output(self, output_path: str | None, source: Path, roots: list[Path]) -> Path:
        if output_path is None:
            target = source.with_name(f"{source.stem}.biomapper.tsv")
        else:
            raw = Path(output_path).expanduser()
            target = raw if raw.is_absolute() else source.parent / raw
        try:
            parent = target.parent.resolve(strict=True)
        except OSError:
            raise JobError(f"Output directory {target.parent} does not exist.") from None
        target = parent / target.name
        if not _under(target, roots):
            raise JobError(
                f"Output path {target} is outside the allowed folders "
                f"({', '.join(str(r) for r in roots)})."
            )
        return target

    def _check_output_free(self, job: Job, *, own_partial: bool = False) -> None:
        """Refuse if any output path is taken. ``own_partial``: the partial is this job's."""
        paths = [job.output, job.provenance] + ([] if own_partial else [job.partial])
        for out in paths:
            if out.exists() or out.is_symlink():
                what = "is left over from another run" if out == job.partial else "already exists"
                raise JobError(
                    f"{out} {what} and map_file never overwrites. Pass a different "
                    f"output_path, or move the existing file away first."
                )
        out = job.output
        if not os.access(out.parent, os.W_OK):
            raise JobError(f"Output directory {out.parent} is not writable.")

    async def _health_ok(self) -> str | None:
        """None when prod answered ``/health`` in time, else the reason it did not."""
        now = self.config.monotonic()
        if self._last_good_chunk is not None and now - self._last_good_chunk < 60:
            return None  # a chunk just succeeded: prod is evidently answering
        try:
            with anyio.fail_after(self.config.health_timeout_s):
                async with self.gate.hold(wait_budget_s=self.config.gate_wait_s):
                    await self.client.health_check()
        except TimeoutError:
            return (
                f"BioMapper /health did not answer within {self.config.health_timeout_s:g} s; "
                "prod is slow or down right now. Try again in a few minutes."
            )
        except GateBusy as exc:
            return str(exc)
        except BioMapperError as exc:
            return f"BioMapper /health failed: {tools.explain_client_error(exc)}"
        except Exception as exc:  # noqa: BLE001 — transport errors become a refusal
            return f"BioMapper /health failed: {type(exc).__name__}"
        return None

    # -- tools ---------------------------------------------------------

    async def submit(
        self,
        *,
        path: str,
        name_column: str,
        entity_type: str,
        identifier_columns: dict[str, str],
        output_path: str | None,
        annotation_mode: str,
        key_column: str | None,
        rerun: bool,
        client_roots: list[Path],
    ) -> JobStatus:
        roots = self._require_roots(client_roots)
        await self._note_roots(roots)
        source = self._resolve_input(path, roots)
        target = self._resolve_output(output_path, source, roots)
        options = {
            "name_column": name_column,
            "entity_type": entity_type,
            "identifier_columns": dict(sorted(identifier_columns.items())),
            "annotation_mode": annotation_mode,
            "key_column": key_column,
        }
        sha = await anyio.to_thread.run_sync(_sha256, source)
        job_id = _job_id(sha, options, str(target))

        existing = self._load(job_id)
        if existing is not None and not rerun:
            status = self._status(existing)
            if status.state == "paused" and self._take(existing):
                job = self._jobs[job_id]
                job.state = "queued"
                self._save(job)
                self._enqueue(job, health=True, roots=roots)
                return self._status(job)
            return status
        if existing is not None and rerun:
            if self._status(existing).state == "owned_elsewhere":
                raise JobError(f"Job {job_id} is running in another session; cannot rerun it.")
            if self._busy_job == job_id:
                raise JobError(
                    f"Job {job_id} is still running in this session, so its files cannot be "
                    "replaced yet. cancel_job it, wait a few seconds for the current chunk to "
                    "finish, then rerun."
                )

        rows = await anyio.to_thread.run_sync(_read_rows, source, options)
        names = [r for r in rows if r.cache_key is not None]
        if not names:
            raise JobError(
                f"{source.name} has no data rows with a value in {name_column!r} (blank lines "
                f"and '#' comment lines are skipped)."
            )
        job = Job(
            job_id=job_id,
            input_path=str(source),
            input_sha256=sha,
            output_path=str(target),
            options=options,
            rows_total=len(rows),
            entities_total=len({r.cache_key for r in names}),
            seconds_per_entity=self.config.seconds_per_entity_seed,
            created_utc=_utc(self.config.wall_clock()),
        )
        self._check_output_free(job, own_partial=existing is not None and rerun)
        problem = await self._health_ok()
        if problem is not None:
            raise JobError(f"Not queued: {problem}")

        jdir = self._dir(job_id)
        _private_dir(self.config.home)
        _private_dir(self.jobs_dir)
        if existing is not None and rerun:
            if self._busy_job == job_id:  # started while the health check awaited
                raise JobError(f"Job {job_id} started running meanwhile; cancel it, then rerun.")
            self._forget(job_id)
            shutil.rmtree(jdir, ignore_errors=True)
            existing.partial.unlink(missing_ok=True)
        _private_dir(jdir)
        if not self._take(job):
            raise JobError(f"Job {job_id} is owned by another session.")
        self._jobs[job_id] = job
        self._save(job)
        self._enqueue(job, health=False, roots=roots)
        return self._status(job)

    def _forget(self, job_id: str) -> None:
        lock = self._locks.pop(job_id, None)
        if lock is not None:
            lock.release()
        self._jobs.pop(job_id, None)

    async def resume(self, job_id: str, *, client_roots: list[Path]) -> JobStatus:
        roots = self._require_roots(client_roots)
        await self._note_roots(roots)
        job = self._require_job(job_id)
        if not self._within(job, roots):
            raise JobError(f"Job {job_id!r} reads or writes files outside the allowed folders.")
        status = self._status(job)
        if status.state == "owned_elsewhere":
            pid = status.owner.pid if status.owner else None
            raise JobError(
                f"Job {job_id} is owned by another session (pid {pid}). It continues there; "
                "resume it here only after that session ends."
            )
        if status.state in ("queued", "running", "complete"):
            return status
        if status.state == "failed":
            raise JobError(
                f"Job {job_id} failed ({job.reason}). Submit map_file again with rerun=true to "
                f"start it over."
            )
        if not self._take(job):
            raise JobError(f"Job {job_id} is owned by another session.")
        job = self._jobs[job_id]
        problem = await self._health_ok()
        if problem is not None:
            raise JobError(f"Not resumed: {problem}")
        job.state = "queued"
        self._save(job)
        self._enqueue(job, health=False, roots=roots)
        return self._status(job)

    async def cancel(self, job_id: str, *, client_roots: list[Path]) -> JobStatus:
        roots = self.allowed_roots(client_roots)
        await self._note_roots(roots)
        job = self._require_job(job_id)
        if not self._within(job, roots):
            raise JobError(
                f"Job {job_id!r} reads or writes files outside the folders allowed in this "
                "session."
            )
        if job.state in ("complete", "failed", "cancelled"):
            return self._status(job)
        if not self._take(job):
            raise JobError(f"Job {job_id} is owned by another session; cancel it there.")
        job = self._jobs[job_id]
        job.state = "cancelled"
        job.reason = "Cancelled; partial output and cache kept. resume_job continues it."
        self._save(job)
        return self._status(job)

    # -- auto-resume ---------------------------------------------------

    async def _note_roots(self, roots: list[Path]) -> None:
        new = [r for r in roots if r not in self._seen_roots]
        if new:
            await self.auto_resume(new)

    async def auto_resume(self, extra_roots: list[Path]) -> None:
        """Queue unfinished jobs under the session's roots; expire ones idle for over 7 days."""
        roots = self.allowed_roots(extra_roots)
        self._seen_roots.update(roots)
        if not roots:
            return
        now = self.config.wall_clock()
        for job_id in self._all_job_ids():
            if job_id in self._locks:
                continue
            job = self._load_from_disk(job_id)
            if job is None or job.state not in UNFINISHED:
                continue
            if not self._within(job, roots):
                continue
            if not self._take(job):
                continue  # owned elsewhere; status reports it
            job = self._jobs[job_id]
            try:
                idle = now - _parse_utc(job.updated_utc or job.created_utc)
            except ValueError:
                idle = 0.0
            if idle > self.config.auto_resume_max_age_s:
                job.state = "expired"
                job.reason = "Idle for more than 7 days; resume_job continues it."
                self._save(job)
                continue
            job.state = "queued"
            self._save(job)
            self._enqueue(job, health=True, roots=roots)

    # -- worker --------------------------------------------------------

    async def run_worker(self) -> None:
        while True:
            job_id = await self._queue.get()
            self._queued.discard(job_id)
            job = self._jobs.get(job_id)
            if job is None or job_id not in self._locks or job.state != "queued":
                continue
            self._busy_job = job_id
            try:
                await self._run(job)
            except _Stop:
                pass
            except JobError as exc:
                self._fail_quietly(job, str(exc))
            except Exception as exc:  # noqa: BLE001 — a job failure must not kill the worker
                logger.exception("job %s crashed", job_id)
                self._fail_quietly(job, f"Internal error: {type(exc).__name__}: {exc}")
            finally:
                self._busy_job = None

    def _finish(self, job: Job, state: str, reason: str | None) -> NoReturn:
        if job.state == "cancelled":
            raise _Stop  # a cancel that landed mid-chunk wins over whatever the chunk decided
        job.state = state
        job.reason = reason
        self._save(job)
        raise _Stop

    def _fail_quietly(self, job: Job, reason: str) -> None:
        """Record a failure from the worker's own handler; nothing may escape the worker."""
        try:
            self._finish(job, "failed", reason)
        except _Stop:
            pass
        except Exception:  # noqa: BLE001 — e.g. the job record could not be saved
            logger.exception("could not record failure of job %s", job.job_id)

    def _require_within_roots(self, job: Job) -> None:
        roots = self._run_roots.get(job.job_id, [])
        if not self._within(job, roots):
            self._finish(
                job, "failed",
                "The job's input or output is no longer inside the folders allowed in this "
                "session (a folder moved, or a path became a link elsewhere). Nothing was written.",
            )

    def _stop_if_cancelled(self, job: Job) -> None:
        if job.state == "cancelled":
            raise _Stop

    async def _run(self, job: Job) -> None:
        self._require_within_roots(job)
        source = Path(job.input_path)
        try:
            sha = await anyio.to_thread.run_sync(_sha256, source)
        except OSError:
            self._finish(job, "failed", f"Input changed: {source} can no longer be read.")
        if sha != job.input_sha256:
            self._finish(
                job, "failed",
                "Input changed since the job was queued, so cached rows may not match it. Submit "
                "map_file again (the new content gets a new job).",
            )
        rows = await anyio.to_thread.run_sync(_read_rows, source, job.options)
        if job.output.exists() or job.output.is_symlink() or job.provenance.exists() or (
            job.provenance.is_symlink()
        ):
            self._finish(
                job, "failed",
                f"{job.output.name} or its provenance file now exists and map_file never "
                "overwrites. Move it away, then submit map_file again with rerun=true.",
            )

        if job.job_id in self._needs_health:
            self._needs_health.discard(job.job_id)
            problem = await self._health_ok()
            if problem is not None:
                self._finish(job, "stalled", f"Health check before resuming failed: {problem}")
        self._stop_if_cancelled(job)

        pins = await probe_pins(self.config, self.gate)
        self._stop_if_cancelled(job)
        new_pin = _cache_pin(pins, self.config.base_url, job.options)
        jdir = self._dir(job.job_id)
        note: str | None = None
        if job.pin is not None and job.pin_session != self.session_id:
            if not _pin_known(job.pin):
                self._discard_cache(job)
                note = (
                    "Restarted from row 0: the earlier rows were cached while the KG build was "
                    "unverified (Kestrel unreachable), so they cannot be trusted across sessions."
                )
            elif not _pin_known(new_pin):
                self._finish(
                    job, "stalled",
                    "Cannot verify that the KG build still matches the cached rows (Kestrel "
                    "unreachable). resume_job once Kestrel answers.",
                )
        try:
            cache = await anyio.to_thread.run_sync(EntityCache, jdir, new_pin)
        except CachePinMismatch:
            self._finish(
                job, "failed",
                "The KG build changed since this job's rows were cached; mixing builds in one "
                "file is refused. Submit map_file again with rerun=true to start over on the "
                "current build.",
            )
        # A cancel accepted during the awaits above must not be overwritten by "running".
        self._stop_if_cancelled(job)
        for name in ("entity_cache.jsonl", "checkpoint.json"):
            with contextlib.suppress(OSError):
                os.chmod(jdir / name, 0o600)
        _write_private(jdir / PINS_FILE, json.dumps(pins, indent=2, sort_keys=True))
        job.pin = new_pin
        job.pin_session = self.session_id
        job.kg_build = kg_build_label(pins)
        job.kg_verified = kg_verified(pins)
        job.state = "running"
        job.reason = note
        self._recount(job, rows, cache)
        self._save(job)

        await self._map_passes(job, rows, cache)
        self._stop_if_cancelled(job)
        self._require_within_roots(job)
        await anyio.to_thread.run_sync(self._write_final, job, rows, cache, pins)
        job.completed_utc = _utc(self.config.wall_clock())
        self._recount(job, rows, cache)
        self._finish(job, "complete", job.reason)

    def _discard_cache(self, job: Job) -> None:
        jdir = self._dir(job.job_id)
        for name in ("entity_cache.jsonl", "checkpoint.json"):
            (jdir / name).unlink(missing_ok=True)
        job.partial.unlink(missing_ok=True)
        job.attempts = {}
        job.last_errors = {}
        job.pass_index = 0
        job.entities_done = 0

    def _recount(self, job: Job, rows: list[InputRow], cache: EntityCache) -> None:
        keys = {r.cache_key for r in rows if r.cache_key is not None}
        job.entities_done = sum(1 for k in keys if cache.get(k) is not None)
        job.entities_failed = sum(
            1 for k in keys
            if cache.get(k) is None and job.attempts.get(k, 0) >= self.config.max_row_attempts
        )

    def _pending(self, job: Job, rows: list[InputRow], cache: EntityCache) -> list[InputRow]:
        seen: set[str] = set()
        pending: list[InputRow] = []
        for row in rows:
            k = row.cache_key
            if k is None or k in seen:
                continue
            seen.add(k)
            if cache.get(k) is None and job.attempts.get(k, 0) < self.config.max_row_attempts:
                pending.append(row)
        return pending

    async def _map_passes(self, job: Job, rows: list[InputRow], cache: EntityCache) -> None:
        bad = 0
        size = max(self.config.chunk_size, 1)
        while True:
            pending = self._pending(job, rows, cache)
            if not pending:
                return
            for start in range(0, len(pending), size):
                self._stop_if_cancelled(job)
                chunk = pending[start : start + size]
                failure, elapsed = await self._map_chunk(job, chunk, cache)
                self._recount(job, rows, cache)
                self._save(job)
                if elapsed > self.config.stall_chunk_s:
                    self._finish(
                        job, "stalled",
                        f"Prod is very slow: one chunk of {len(chunk)} took {elapsed:.0f} s. "
                        "Stopped sending requests; resume_job retries after a health check.",
                    )
                if failure is None:
                    bad = 0
                    continue
                bad += 1
                if bad >= self.config.stall_bad_chunks:
                    self._finish(
                        job, "stalled",
                        f"{bad} chunks in a row failed as a whole ({failure}). Stopped sending "
                        "requests; resume_job retries after a health check.",
                    )
                # Back off with the gate released, so interactive calls are not blocked.
                if self.config.systemic_backoff_s > 0:
                    await anyio.sleep(self.config.systemic_backoff_s)
            job.pass_index += 1

    async def _map_chunk(
        self, job: Job, chunk: list[InputRow], cache: EntityCache
    ) -> tuple[str | None, float]:
        records = [{"name": r.name, "identifiers": dict(r.identifiers)} for r in chunk]
        started = self.config.monotonic()
        async with self.gate.hold(wait_budget_s=None):
            results = await self.client.map_entities(
                records,
                entity_type=job.options["entity_type"],
                annotation_mode=job.options["annotation_mode"],
                sleep_on_rate_limit=False,
            )
        elapsed = self.config.monotonic() - started
        failure = tools.systemic_error(results, len(records))
        if failure is not None and len(records) == 1 and not tools.looks_systemic(failure):
            failure = None  # one row, one error: that row's own answer
        if failure is not None:
            return failure, elapsed

        self._last_good_chunk = self.config.monotonic()
        keyed = [(r.cache_key, res) for r, res in zip(chunk, results, strict=True)
                 if r.cache_key is not None]
        good = [(k, res) for k, res in keyed if res.error is None]
        cache.commit_batch(vocab=None, batch_index=job.pass_index, keyed_results=good)
        for k, res in keyed:
            if res.error is not None:
                job.attempts[k] = job.attempts.get(k, 0) + 1
                job.last_errors[k] = tools.error_text(res)
        if good:
            per_entity = elapsed / len(records)
            job.seconds_per_entity = round(0.7 * job.seconds_per_entity + 0.3 * per_entity, 3)
        self._append_partial(job, chunk, results)
        return None, elapsed

    def _append_partial(
        self, job: Job, chunk: list[InputRow], results: list[MappingResult]
    ) -> None:
        # O_NOFOLLOW: a partial swapped for a symlink is refused rather than followed.
        fd = os.open(job.partial, os.O_WRONLY | os.O_APPEND | os.O_CREAT | _NOFOLLOW, 0o644)
        with os.fdopen(fd, "a", encoding="utf-8", newline="") as fh:
            if os.fstat(fh.fileno()).st_size == 0:
                fh.write(_tsv_line(OUTPUT_COLUMNS))
            for row, res in zip(chunk, results, strict=True):
                fh.write(_tsv_line(_output_row(row, res, None)))

    def _write_final(
        self, job: Job, rows: list[InputRow], cache: EntityCache, pins: dict[str, Any]
    ) -> None:
        out = job.output
        for taken in (out, job.provenance):
            if taken.exists() or taken.is_symlink():
                raise JobError(f"{taken} appeared while the job ran; not overwritten.")
        tmp = out.with_name(f".{out.name}.{job.job_id}.tmp")
        tmp.unlink(missing_ok=True)  # a stale temp (or a planted link) of this job only
        with _create_exclusive(tmp) as fh:
            fh.write(_tsv_line(OUTPUT_COLUMNS))
            for row in rows:
                if row.cache_key is None:
                    fh.write(_tsv_line(_output_row(row, None, "Blank name; not sent.")))
                    continue
                hit = cache.get(row.cache_key)
                error = job.last_errors.get(row.cache_key)
                if hit is None and error is not None:
                    error = f"Failed after {job.attempts.get(row.cache_key, 0)} attempts: {error}"
                fh.write(_tsv_line(_output_row(row, hit, error)))
        try:
            _place_without_overwrite(tmp, out)
        except FileExistsError:
            tmp.unlink(missing_ok=True)
            raise JobError(f"{out} appeared while the job ran; not overwritten.") from None
        provenance = {
            "job_id": job.job_id,
            "biomapper_version": resolve_version(),
            "completed_utc": _utc(self.config.wall_clock()),
            "input": {"file_name": Path(job.input_path).name, "sha256": job.input_sha256,
                      "rows": job.rows_total},
            "output": {"file_name": out.name, "columns": OUTPUT_COLUMNS},
            "options": job.options,
            "counts": {"entities_total": job.entities_total,
                       "entities_done": job.entities_done,
                       "entities_failed": job.entities_failed},
            "kg_verified": job.kg_verified,
            "pins": pins,
        }
        prov_tmp = job.provenance.with_name(f".{job.provenance.name}.{job.job_id}.tmp")
        prov_tmp.unlink(missing_ok=True)
        with _create_exclusive(prov_tmp) as fh:
            fh.write(json.dumps(provenance, indent=2, sort_keys=True))
        try:
            _place_without_overwrite(prov_tmp, job.provenance)
        except FileExistsError:
            prov_tmp.unlink(missing_ok=True)
            raise JobError(
                f"{job.provenance} appeared while the job ran; not overwritten. The mapped "
                f"output {out.name} was written without its provenance file."
            ) from None
        job.partial.unlink(missing_ok=True)


def _place_without_overwrite(tmp: Path, final: Path) -> None:
    """Move ``tmp`` to ``final`` atomically, refusing if ``final`` exists."""
    try:
        os.link(tmp, final)
    except FileExistsError:
        raise
    except OSError:  # pragma: no cover - filesystems without hard links
        if final.exists():
            raise FileExistsError(str(final)) from None
        os.replace(tmp, final)
        return
    tmp.unlink()
