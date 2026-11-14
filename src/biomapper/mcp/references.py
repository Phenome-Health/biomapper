"""Server-issued references for weak results (1.7.0).

A weak row (see :func:`biomapper.mcp.tools.weak_trigger`) gets an opaque, random ``ref``. The ref
names one private JSON record holding everything needed to revisit that row later without
trusting the agent to replay it: the original query and options, the engine's choice and
certificate, the FULL equivalent ids (the interactive view truncates them), the weak trigger, the
KG build the answer came from, and where it came from (``entity``, ``batch`` or ``job``).

Two scopes:

* interactive refs live at ``<home>/refs/<ref>.json``. Any server process with the same home and
  the same uid can use them; they expire 30 days after issue (deleted lazily);
* job refs live at ``<home>/jobs/<job_id>/refs/<ref>.json``, so they share the job's lifecycle (a
  rerun deletes them). The server checks the job's folders against the session's allowed roots
  before using one.

Ref ids are validated against a strict pattern before any path is built from them, so a malformed
or traversal id never touches the filesystem.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from biomapper._pins import KESTREL_LABEL
from biomapper.mcp._storage import private_dir, utc, write_private
from biomapper.mcp.tools import entity_status
from biomapper.models import MappingResult

logger = logging.getLogger("biomapper.mcp.references")

REF_TTL_S = 30 * 24 * 3600.0
GC_INTERVAL_S = 3600.0

RefSource = Literal["entity", "batch", "job"]
CandidateStatus = Literal["present", "none_searched", "fetch_failed", "run_mismatch"]

_INTERACTIVE = re.compile(r"ref-([0-9a-f]{32})")
_JOB = re.compile(r"jref-([0-9a-f]{16})-([0-9a-f]{24})")
# 1.9.0: a link review item of a harmonize job. Its record is an entry of the job's review
# index (see biomapper.mcp.review_entries), not a RefRecord: a link has two sides and no query.
_LINK = re.compile(r"lref-([0-9a-f]{16})-([0-9a-f]{24})")


class RefError(Exception):
    """A ref that cannot be used: malformed, unknown, expired, or not this user's."""


def parse_ref(ref_id: object) -> tuple[Literal["interactive", "job", "link"], str | None]:
    """``("interactive", None)``, ``("job", job_id)`` or ``("link", job_id)``.

    :class:`RefError` for anything else. The format is checked before any path is built.
    """
    if isinstance(ref_id, str):
        if _INTERACTIVE.fullmatch(ref_id):
            return "interactive", None
        match = _JOB.fullmatch(ref_id)
        if match:
            return "job", match.group(1)
        match = _LINK.fullmatch(ref_id)
        if match:
            return "link", match.group(1)
    raise RefError(
        f"{str(ref_id)[:80]!r} is not a reference. Refs are issued by map_entity, map_batch and "
        "map_file (weak.tsv) on weak rows, and by list_review_items; pass one exactly as "
        "returned."
    )


def new_link_ref(job_id: str) -> str:
    """A fresh, random link reference for ``job_id`` (a harmonize job's 16-hex id)."""
    if not re.fullmatch(r"[0-9a-f]{16}", job_id):
        raise RefError("a link ref needs the job's 16-hex id")
    return f"lref-{job_id}-{secrets.token_hex(12)}"


class RefQuery(BaseModel):
    """The original call, so a re-map can repeat it exactly."""

    name: str
    entity_type: str
    identifiers: dict[str, str] = Field(default_factory=dict)
    annotation_mode: str = "missing"


class RefEngine(BaseModel):
    """What the engine answered. ``kg_equivalent_ids`` is the full set, never truncated."""

    status: str
    chosen_kg_id: str | None = None
    primary_curie: str | None = None
    certificate_state: str | None = None
    kg_equivalent_ids: dict[str, list[str]] = Field(default_factory=dict)

    @classmethod
    def of(cls, result: MappingResult) -> RefEngine:
        return cls(
            status=entity_status(result),
            chosen_kg_id=result.chosen_kg_id,
            primary_curie=result.primary_curie,
            certificate_state=result.certificate.state if result.certificate is not None else None,
            kg_equivalent_ids={k: list(v) for k, v in result.kg_equivalent_ids.items()},
        )


class RefPins(BaseModel):
    kg_build: str
    kg_label: str = KESTREL_LABEL


class CandidateItem(BaseModel):
    """One compacted candidate. ``name``/``synonyms`` are untrusted KG text, sanitized."""

    curie: str
    name: str = ""
    synonyms: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    score: float | None = None
    endpoints: list[str] = Field(default_factory=list)
    relation: Literal["equals", "equivalent"] | None = Field(
        default=None,
        description="equals = the engine's own choice; equivalent = the same KG entity under "
        "another id (from the full equivalent-id set); null = a different entity",
    )


class CandidateSet(BaseModel):
    """The latest candidate fetch stored on a ref. Only ``present`` sets back a suggestion."""

    status: CandidateStatus
    reason: str | None = None
    n: int
    fetched_utc: str
    raw_sha256: str | None = None
    items: list[CandidateItem] = Field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.status == "present"


class RefRecord(BaseModel):
    ref_id: str
    source: RefSource
    job_id: str | None = None
    row_key: str | None = None
    query: RefQuery
    engine: RefEngine
    weak_trigger: str | None
    pins: RefPins
    created_utc: str
    created_ts: float
    uid: int | None = None
    supersedes: str | None = None
    superseded_by: str | None = None
    candidates: CandidateSet | None = None
    candidate_history: list[CandidateSet] = Field(default_factory=list)

    @property
    def engine_choice(self) -> str | None:
        return self.engine.chosen_kg_id or self.engine.primary_curie


def _uid() -> int | None:
    getuid = getattr(os, "getuid", None)
    return getuid() if getuid is not None else None


class RefStore:
    """Issues, loads and updates ref records under one MCP home."""

    def __init__(
        self,
        home: Path,
        *,
        wall_clock: Callable[[], float] = time.time,
        ttl_s: float = REF_TTL_S,
    ) -> None:
        self.home = home
        self.refs_dir = home / "refs"
        self.jobs_dir = home / "jobs"
        self._clock = wall_clock
        self.ttl_s = ttl_s
        self._last_gc: float | None = None

    # -- paths ----------------------------------------------------------

    def path_for(self, ref_id: str) -> Path:
        kind, job_id = parse_ref(ref_id)
        if kind == "job":
            assert job_id is not None
            return self.jobs_dir / job_id / "refs" / f"{ref_id}.json"
        if kind == "link":
            # No record lives here (the review index holds link entries); the path anchors a
            # link's judgments directory inside its job, so they share the job's lifecycle.
            assert job_id is not None
            return self.jobs_dir / job_id / "links" / f"{ref_id}.json"
        return self.refs_dir / f"{ref_id}.json"

    def judgments_dir(self, ref_id: str) -> Path:
        """Where judgment records for ``ref_id`` live (next to the ref, same lifecycle)."""
        return self.path_for(ref_id).parent / "judgments" / ref_id

    # -- issue ------------------------------------------------------------

    def issue(
        self,
        *,
        source: RefSource,
        query: dict[str, Any],
        result: MappingResult,
        weak_trigger: str | None,
        kg_build: str,
        job_id: str | None = None,
        row_key: str | None = None,
        supersedes: str | None = None,
    ) -> RefRecord:
        if source == "job":
            if job_id is None or not re.fullmatch(r"[0-9a-f]{16}", job_id):
                raise RefError("a job ref needs the job's 16-hex id")
            ref_id = f"jref-{job_id}-{secrets.token_hex(12)}"
        else:
            ref_id = f"ref-{secrets.token_hex(16)}"
            self._maybe_gc()
        now = self._clock()
        record = RefRecord(
            ref_id=ref_id,
            source=source,
            job_id=job_id,
            row_key=row_key,
            query=RefQuery(**query),
            engine=RefEngine.of(result),
            weak_trigger=weak_trigger,
            pins=RefPins(kg_build=kg_build),
            created_utc=utc(now),
            created_ts=now,
            uid=_uid(),
            supersedes=supersedes,
        )
        self.save(record)
        return record

    def update(self, ref_id: str, mutate: Callable[[RefRecord], None]) -> RefRecord:
        """Reload ``ref_id`` from disk under its per-ref lock, apply ``mutate``, save it.

        A caller's in-memory copy may be stale: two get_candidates calls (in this process or in
        another server process on the same home) can both load a ref before either stores what it
        fetched. Merging into the copy on disk keeps the other call's set in the history instead
        of overwriting it. Raises ``OSError`` when the ref can no longer be read or written.
        """
        path = self.path_for(ref_id)
        with self._ref_lock(path):
            try:
                record = RefRecord.model_validate_json(path.read_text(encoding="utf-8"))
            except ValueError as exc:
                raise OSError(f"reference {ref_id} is unreadable: {exc}") from None
            if record.ref_id != ref_id:
                raise OSError(f"reference {ref_id} is unreadable")
            mutate(record)
            self.save(record)
        return record

    @staticmethod
    @contextlib.contextmanager
    def _ref_lock(path: Path) -> Iterator[None]:
        lock_path = path.with_name(f".{path.name}.lock")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _lock(fd)
            try:
                yield
            finally:
                _unlock(fd)
        finally:
            os.close(fd)

    def save(self, record: RefRecord) -> None:
        path = self.path_for(record.ref_id)
        if parse_ref(record.ref_id)[0] == "interactive":
            private_dir(self.home)
        private_dir(path.parent)
        write_private(path, record.model_dump_json(indent=2))

    # -- load -------------------------------------------------------------

    def load(self, ref_id: str) -> RefRecord:
        kind, _ = parse_ref(ref_id)
        if kind == "link":
            raise RefError(
                f"{ref_id} is a link review item: it has two sides and evidence, not "
                "candidates. Read it with get_review_item, and record same, different or "
                "cannot_tell with record_suggestion."
            )
        path = self.path_for(ref_id)
        try:
            st = os.lstat(path)
        except OSError:
            raise RefError(f"No reference {ref_id!r} (unknown, expired, or its job was "
                           "rerun).") from None
        uid = _uid()
        if uid is not None and st.st_uid != uid:
            raise RefError(f"Reference {ref_id!r} belongs to another user.")
        try:
            record = RefRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise RefError(f"Reference {ref_id!r} is unreadable.") from None
        if record.ref_id != ref_id:
            raise RefError(f"Reference {ref_id!r} is unreadable.")
        if uid is not None and record.uid is not None and record.uid != uid:
            raise RefError(f"Reference {ref_id!r} belongs to another user.")
        if kind == "interactive" and self._expired(record.created_ts):
            self._delete(ref_id)
            raise RefError(
                f"Reference {ref_id!r} expired (interactive refs last 30 days). Map the entity "
                "again to get a fresh one."
            )
        return record

    # -- lifecycle ------------------------------------------------------

    def _expired(self, created_ts: float) -> bool:
        return self._clock() - created_ts > self.ttl_s

    def _delete(self, ref_id: str) -> None:
        path = self.path_for(ref_id)
        with contextlib.suppress(OSError):
            path.unlink()
        with contextlib.suppress(OSError):
            path.with_name(f".{path.name}.lock").unlink()
        shutil.rmtree(self.judgments_dir(ref_id), ignore_errors=True)

    def _maybe_gc(self) -> None:
        now = self._clock()
        if self._last_gc is not None and now - self._last_gc < GC_INTERVAL_S:
            return
        self._last_gc = now
        try:
            self.gc()
        except OSError:  # GC is housekeeping; it never fails an issue
            logger.warning("ref GC failed", exc_info=True)

    def gc(self) -> int:
        """Delete expired interactive refs. Returns how many were removed."""
        if not self.refs_dir.is_dir():
            return 0
        removed = 0
        for path in self.refs_dir.glob("ref-*.json"):
            ref_id = path.name[: -len(".json")]
            if not _INTERACTIVE.fullmatch(ref_id):
                continue
            try:
                created = float(json.loads(path.read_text(encoding="utf-8"))["created_ts"])
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if self._expired(created):
                self._delete(ref_id)
                removed += 1
        return removed


if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
    import msvcrt

    def _lock(fd: int) -> None:
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
