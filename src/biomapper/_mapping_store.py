"""The saved-mapping store behind :func:`biomapper.harmonize_many`.

Mapping is the expensive part of harmonizing cohorts (about 4.4 s per entity on the shared
deployment), so each entity is mapped at most once per arm and the result is saved for later runs.
This is a thin layer over :class:`biomapper._checkpoint.EntityCache`, the same fsynced JSONL cache
the MCP server resumes file jobs from, and over the MCP server's pin rules:

* **One cache per arm and category** under ``store_dir`` (``<arm>/<category>/``). Entries are keyed
  per entity by :func:`~biomapper._checkpoint.cache_key` over the name, the identifiers sent and
  the request options, never by cohort label, so two analyses that both call a table "QMDiab"
  cannot collide, and a torn tail or an errored row costs only those entities.
* **One store per mapper.** Mappings from an injected ``mapper`` are kept under
  ``custom_mappers/<mapper id>/`` inside ``store_dir``, apart from the hosted API's, so toy or test
  answers are never served to a later run against the API (or vice versa).
* **The KG build is read before any request.** A known build that differs from the saved one is
  refused before anything is sent (:class:`StoreBuildMismatch`), naming ``remap_all=True`` as the
  way out. An unknown build (Kestrel unreachable, not probed, or no git commit reported) disables
  reuse: the run maps fresh, saves nothing, and says why, because answers whose build cannot be
  named must not be served to a later run as if it could.
* **One writer at a time.** A lockfile created with ``O_EXCL`` beside each cache guards it. A
  second run that finds it held is refused (:class:`StoreLocked`) with the holder's pid and start
  time; a lock whose process is gone is reported, never silently broken.
* **Errored results are never saved** (``EntityCache`` drops them), so only those rows are re-sent.

``remap_all=True`` moves the existing cache aside (``*.superseded-<stamp>``, not deleted) and maps
everything again on the current build.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any

from biomapper._checkpoint import (
    CACHE_FILENAME,
    PROGRESS_FILENAME,
    CachePinMismatch,
    EntityCache,
    cache_key,
)
from biomapper._provenance import UNKNOWN
from biomapper.models import MappingResult

__all__ = [
    "ARMS",
    "LOCK_FILENAME",
    "ArmStore",
    "MapOutcome",
    "MappingStore",
    "StoreBuildMismatch",
    "StoreLocked",
    "map_requests",
    "pin_known",
    "store_pin",
]

ARMS: tuple[str, ...] = ("names_only", "identifier")
LOCK_FILENAME = "store.lock"
DEFAULT_COMMIT_BATCH = 10

MapperFn = Callable[..., Sequence[MappingResult]]


class StoreBuildMismatch(CachePinMismatch):
    """The saved mappings were made on a different KG build (or backend) than this run's."""


class StoreLocked(RuntimeError):
    """Another run holds the store's lock."""


def store_pin(
    pins: dict[str, Any], *, base_url: str, entity_type: str, annotation_mode: str
) -> dict[str, Any]:
    """The cache pin: the same fields and fallbacks as the MCP server's job cache pin."""
    kestrel = pins.get("kestrel") or {}
    kg = kestrel.get("kg_build") or {}
    return {
        "api_endpoint": base_url,
        "kestrel_version": kestrel.get("kestrel_version") or UNKNOWN,
        "kg_version": kg.get("kg_version") or UNKNOWN,
        "kg_git_commit": kg.get("git_commit") or UNKNOWN,
        "entity_type": entity_type,
        "annotation_mode": annotation_mode,
    }


def pin_known(pin: dict[str, Any] | None) -> bool:
    """Whether a pin names a real KG build (unknown builds are never reused).

    Both the KG version and its git commit must be known: two builds that report the same
    version but no commit cannot be told apart, so their mappings must not be shared.
    """
    if not pin:
        return False
    return all(pin.get(f, UNKNOWN) not in (None, "", UNKNOWN)
               for f in ("kg_version", "kg_git_commit"))


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by someone else
        return True
    return True


def _safe_dir(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text) or "default"


@dataclass
class ArmStore:
    """One arm's cache directory, its lock and its ``EntityCache`` (``None`` when not reusing)."""

    arm: str
    directory: Path
    cache: EntityCache | None
    batch_size: int = DEFAULT_COMMIT_BATCH
    lock_path: Path | None = None
    _batches: int = field(default=0, repr=False)

    def next_batch_index(self) -> int:
        self._batches += 1
        return self._batches

    def release(self) -> None:
        if self.lock_path is not None:
            with contextlib.suppress(FileNotFoundError):
                self.lock_path.unlink()
            self.lock_path = None


def _acquire_lock(directory: Path, session_id: str) -> Path:
    path = directory / LOCK_FILENAME
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        try:
            holder = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            holder = {}
        if not isinstance(holder, dict):
            holder = {}
        pid = holder.get("pid")
        started = holder.get("started_utc", "unknown time")
        if not isinstance(pid, int):
            state = (
                f"its lock file names no holder (unreadable or empty, e.g. a run killed while "
                f"taking it). If no harmonize_many run is active, delete {path} and run again"
            )
        elif not _pid_alive(pid):
            state = (
                f"held by pid {pid} since {started}, which is not running on this machine. If no "
                f"harmonize_many run is active, delete {path} and run again"
            )
        else:
            state = f"held by pid {pid} since {started}; wait for that run to finish"
        raise StoreLocked(
            f"the saved-mapping store {directory} is locked ({state}). Two runs writing one "
            f"store at once is refused."
        ) from None
    with os.fdopen(fd, "w") as fh:
        json.dump({"pid": os.getpid(), "started_utc": _now(), "session_id": session_id}, fh)
    return path


class MappingStore:
    """The per-arm saved mappings of one ``harmonize_many`` run (a context manager).

    Opening it reads the KG build from ``pins``, takes every arm's lock and loads every arm's
    cache, so a build mismatch or a held lock is raised before any request is sent.

    Args:
        store_dir: The store root, or ``None`` for no store (everything is mapped fresh).
        pins: A :func:`biomapper._pins.collect_pins` block (only its ``kestrel`` part is read).
        base_url, entity_type, annotation_mode: The rest of the cache pin.
        remap_all: Map everything again; the existing caches are moved aside.
        batch_size: Requests per mapper call; each call's results are saved before the next.
        session_id: Recorded in each lock file.
        mapper_id: Identity of an injected mapper, or ``None`` for the hosted API. A custom
            mapper's mappings live in their own subdirectory and are never shared with the API's.
    """

    def __init__(
        self,
        store_dir: str | os.PathLike[str] | None,
        *,
        pins: dict[str, Any],
        base_url: str,
        entity_type: str,
        annotation_mode: str = "missing",
        remap_all: bool = False,
        batch_size: int = DEFAULT_COMMIT_BATCH,
        session_id: str = "",
        arms: Sequence[str] = ARMS,
        mapper_id: str | None = None,
    ) -> None:
        self.pin = store_pin(
            pins, base_url=base_url, entity_type=entity_type, annotation_mode=annotation_mode
        )
        self.remap_all = remap_all
        self.root = Path(store_dir).resolve() if store_dir is not None else None
        self._arms: dict[str, ArmStore | None] = dict.fromkeys(arms)
        self.superseded: list[str] = []
        if self.root is None:
            self.reason: str | None = "no store directory; every entity is mapped fresh"
            return
        if not pin_known(self.pin):
            self.reason = (
                "KG build unknown (Kestrel /health unreadable, not probed, or no git commit "
                "reported): saved mappings are not reused, and this run's mappings are not saved"
            )
            return
        self.reason = (
            "remap_all=True: every entity is mapped again on the current build" if remap_all
            else None
        )
        category_dir = _safe_dir(entity_type)
        base = (self.root if mapper_id is None
                else self.root / "custom_mappers" / _safe_dir(mapper_id))
        try:
            for arm in arms:
                directory = base / arm / category_dir
                directory.mkdir(parents=True, exist_ok=True)
                arm_store = ArmStore(arm=arm, directory=directory, cache=None,
                                     batch_size=batch_size)
                self._arms[arm] = arm_store
                arm_store.lock_path = _acquire_lock(directory, session_id)
                if remap_all:
                    self._supersede(directory)
                try:
                    arm_store.cache = EntityCache(directory, self.pin)
                except CachePinMismatch as exc:
                    raise StoreBuildMismatch(
                        f"the saved {arm!r} mappings in {directory} were made on a different KG "
                        f"build or backend than this run's ({exc}). Mixing builds in one result "
                        f"is refused. Pass remap_all=True to map every entity again on the "
                        f"current build (the old mappings are kept aside), or use another "
                        f"store_dir."
                    ) from None
        except BaseException:
            self.close()
            raise

    def _supersede(self, directory: Path) -> None:
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%fZ")
        for name in (CACHE_FILENAME, PROGRESS_FILENAME):
            path = directory / name
            if path.exists():
                target = directory / f"{name}.superseded-{stamp}"
                os.replace(path, target)
                self.superseded.append(str(target))

    @property
    def reuse_enabled(self) -> bool:
        """Whether saved mappings are served to this run."""
        return self.root is not None and pin_known(self.pin) and not self.remap_all

    @property
    def saving(self) -> bool:
        """Whether this run's mappings are written to the store."""
        return any(a is not None and a.cache is not None for a in self._arms.values())

    def arm(self, arm: str) -> ArmStore | None:
        if arm not in self._arms:
            raise KeyError(f"unknown arm {arm!r}; expected one of {list(self._arms)}")
        return self._arms[arm]

    def close(self) -> None:
        for arm_store in self._arms.values():
            if arm_store is not None:
                arm_store.release()

    def __enter__(self) -> MappingStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


@dataclass(frozen=True)
class MapOutcome:
    """Per-request results (each row its own object) and whether each came from the store."""

    results: list[MappingResult]
    reused: list[bool]

    @property
    def n_reused(self) -> int:
        return sum(self.reused)

    @property
    def n_fresh(self) -> int:
        return len(self.reused) - self.n_reused

    @property
    def n_errored(self) -> int:
        return sum(1 for r in self.results if r.error)


def _entity_key(request: dict[str, Any], options: dict[str, Any]) -> str:
    ids = request.get("identifiers") or {}
    flat = {v: "|".join(sorted(str(c) for c in codes)) for v, codes in ids.items()}
    return cache_key(str(request["name"]), None, flat, options)


def _call(
    mapper: MapperFn, requests: list[dict[str, Any]], *, entity_type: str, annotation_mode: str
) -> list[MappingResult]:
    results = list(mapper(requests, entity_type=entity_type, annotation_mode=annotation_mode))
    if len(results) != len(requests):
        raise RuntimeError(
            f"mapper returned {len(results)} results for {len(requests)} requests; a mapper must "
            "return one result per request, in order"
        )
    return results


def map_requests(
    mapper: MapperFn,
    requests: list[dict[str, Any]],
    *,
    store: ArmStore | None,
    entity_type: str,
    annotation_mode: str,
    batch_size: int | None = None,
    on_batch: Callable[[int, int], None] | None = None,
) -> MapOutcome:
    """Map ``requests`` (``{"name", "identifiers"}``), serving saved results where allowed.

    Requests that repeat (same name and identifiers) are sent once. Misses go to ``mapper`` in
    ``store.batch_size`` chunks, and each chunk's successful results are saved before the next
    chunk is sent, so a kill loses at most the chunk in flight. Every row gets its own copy.

    ``on_batch(done, total)`` counts distinct entities: it is called once before the first
    chunk (``done`` is then the number served from the store) and once after every chunk.
    Without a store the misses go to the mapper in one call, unless ``on_batch`` is given, in
    which case they go in ``batch_size`` chunks so there is something to report between them.
    """
    options = {"entity_type": entity_type, "annotation_mode": annotation_mode}
    keys = [_entity_key(r, options) for r in requests]
    cache = store.cache if store is not None else None
    found: dict[str, MappingResult] = {}
    reused = [False] * len(requests)
    for i, k in enumerate(keys):
        hit = cache.get(k) if cache is not None else None
        if hit is not None:
            found[k] = hit
            reused[i] = True
    order: list[str] = []
    first: dict[str, dict[str, Any]] = {}
    for k, r in zip(keys, requests, strict=True):
        if k not in found and k not in first:
            first[k] = r
            order.append(k)
    if store is not None:
        chunk = store.batch_size
    elif on_batch is not None and batch_size is not None:
        chunk = max(batch_size, 1)
    else:
        chunk = max(len(order), 1)
    total = len(found) + len(order)
    if on_batch is not None:
        on_batch(len(found), total)
    for start in range(0, len(order), chunk):
        batch_keys = order[start : start + chunk]
        batch = [{"name": first[k]["name"], "identifiers": dict(first[k].get("identifiers") or {})}
                 for k in batch_keys]
        results = _call(mapper, batch, entity_type=entity_type, annotation_mode=annotation_mode)
        keyed = list(zip(batch_keys, results, strict=True))
        if cache is not None and store is not None:
            cache.commit_batch(vocab=store.arm, batch_index=store.next_batch_index(),
                               keyed_results=keyed)
        found.update(keyed)
        if on_batch is not None:
            on_batch(len(found), total)
    return MapOutcome(results=[found[k].model_copy() for k in keys], reused=reused)
