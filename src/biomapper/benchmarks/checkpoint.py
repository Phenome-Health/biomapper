"""Durable, resumable entity cache for long arms.

MetaboliteAnnotator is 4,314 names per ion mode across four vocab passes, 34,512 API entities in
all, measured at 3.31 s/entity: about 31.7 hours. The 2026-09-26 run burned 11.9 hours on the
positive mode alone and was then terminated, and everything it had fetched was lost because
results only reached disk when a whole vocab pass finished. A kill at hour 11 meant restarting at
hour 0.

This module makes that a resume instead:

* **One line per batch, fsynced.** After every ``/map/batch`` response the batch's successful
  results are appended to ``entity_cache.jsonl`` and flushed to disk before the next request.
  That line IS the checkpoint for ``(ion_mode, vocab, batch)``: the cache file lives in the ion
  mode's directory and each line names its vocab and batch index. A kill loses at most the batch
  in flight.
* **Keyed on entity name + target vocab.** A re-run looks each entity up before sending it and only
  requests the misses. Writing a key that is already present is a no-op, so replaying a batch is
  idempotent.
* **Pinned to one build.** The first line records the backend the answers came from. Resuming
  against a different KG commit, Kestrel service, endpoint, entity type, or annotation mode is
  REFUSED rather than silently mixing two builds into one number.
* **Errors are never cached.** A row that came back with an error is re-requested on resume. A
  cached failure would turn a transient outage into a permanent miss.

A torn final line (a kill mid-write) is skipped on load with a warning; every earlier line is
intact because each was flushed and fsynced before the next was started.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Any

from biomapper.models import MappingResult

logger = logging.getLogger(__name__)

CACHE_FILENAME = "entity_cache.jsonl"
PROGRESS_FILENAME = "checkpoint.json"

# Dropped from cached results. Neither is read by the scored column surface
# (``ApiMapper._assemble_frame``) or the stats, and ``raw_response`` alone would grow a
# 34,512-entity cache by hundreds of megabytes. A cache hit therefore reproduces the MAPPED.tsv
# exactly but does not carry the raw response for inspection.
CACHE_EXCLUDED_FIELDS: frozenset[str] = frozenset({"raw_response", "kestrel_results"})

# The fields that decide what answer the backend gives. Two runs that differ in any of these must
# not share cached answers.
PIN_FIELDS: tuple[str, ...] = (
    "api_endpoint",
    "kestrel_version",
    "kg_version",
    "kg_git_commit",
    "entity_type",
    "annotation_mode",
)


class CachePinMismatch(RuntimeError):
    """A resume was attempted against a cache written under a different backend or request."""


def cache_key(name: str, vocab: str | list[str] | None, identifiers: dict[str, str]) -> str:
    """The cache key: entity name + target vocab.

    Provided identifiers are folded in when present, because the same name with a different
    provided id is a different request. Name-only arms (MetaboliteAnnotator) send none, so for them
    the key is exactly name + vocab.
    """
    vocab_part = ",".join(vocab) if isinstance(vocab, list) else (vocab or "")
    key: dict[str, Any] = {"name": name, "vocab": vocab_part}
    if identifiers:
        key["identifiers"] = dict(sorted(identifiers.items()))
    return json.dumps(key, sort_keys=True, ensure_ascii=False)


class EntityCache:
    """Append-only, fsynced cache of successful mapping results for one ion mode (or arm).

    Args:
        directory: Where ``entity_cache.jsonl`` and ``checkpoint.json`` live.
        pin: The run's backend and request pin, see :data:`PIN_FIELDS`. Compared against the pin
            recorded in an existing cache file; any difference raises :class:`CachePinMismatch`.
    """

    def __init__(self, directory: Path | str, pin: dict[str, Any]) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / CACHE_FILENAME
        self.progress_path = self.directory / PROGRESS_FILENAME
        self.pin = {k: pin.get(k) for k in PIN_FIELDS}
        self._entries: dict[str, MappingResult] = {}
        self._progress: dict[str, Any] = {}
        self.loaded_from_disk = 0
        if self.path.exists() and self.path.stat().st_size > 0:
            self._load()
        else:
            self._append({"type": "pin", "pin": self.pin, "created_utc": _now()})
        if self.progress_path.exists():
            try:
                self._progress = json.loads(self.progress_path.read_text())
            except json.JSONDecodeError:
                # Progress is a human-readable summary, rebuilt from the cache as batches land.
                self._progress = {}

    # ------------------------------------------------------------------
    # Lookup and write
    # ------------------------------------------------------------------

    def get(self, key: str) -> MappingResult | None:
        return self._entries.get(key)

    def __len__(self) -> int:
        return len(self._entries)

    def commit_batch(
        self,
        *,
        vocab: str | list[str] | None,
        batch_index: int,
        keyed_results: list[tuple[str, MappingResult]],
    ) -> int:
        """Durably record one batch's successful results. Returns how many were new.

        Error rows and keys already cached are skipped, so committing the same batch twice writes
        nothing the second time.
        """
        fresh = [
            (key, result)
            for key, result in keyed_results
            if result.error is None and key not in self._entries
        ]
        if fresh:
            self._append(
                {
                    "type": "batch",
                    "vocab": vocab,
                    "batch": batch_index,
                    "committed_utc": _now(),
                    "entries": [
                        {
                            "key": key,
                            "result": result.model_dump(
                                mode="json", exclude=set(CACHE_EXCLUDED_FIELDS)
                            ),
                        }
                        for key, result in fresh
                    ],
                }
            )
            for key, result in fresh:
                # Store what a resume will see, so a hit this run and a hit after a kill agree.
                self._entries[key] = result.model_copy(update=dict.fromkeys(CACHE_EXCLUDED_FIELDS))
        return len(fresh)

    def record_progress(
        self, vocab: str | list[str] | None, *, batches_done: int, n_batches: int, total: int
    ) -> None:
        """Update the per-vocab progress summary atomically (write temp, then rename)."""
        label = ",".join(vocab) if isinstance(vocab, list) else (vocab or "")
        self._progress[label] = {
            "batches_done": batches_done,
            "n_batches": n_batches,
            "entities_total": total,
            "complete": batches_done >= n_batches,
            "updated_utc": _now(),
        }
        self._progress["_cache_entries"] = len(self._entries)
        tmp = self.progress_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._progress, indent=2))
        os.replace(tmp, self.progress_path)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _append(self, record: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _load(self) -> None:
        lines = self.path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                if number == len(lines):
                    logger.warning(
                        "%s: skipping a torn final line (a kill mid-write); %d earlier line(s) "
                        "are intact.",
                        self.path,
                        number - 1,
                    )
                    self._truncate_torn_tail(line)
                    continue
                raise ValueError(
                    f"{self.path}: line {number} is not valid JSON and is not the final line, so "
                    f"the cache is corrupt rather than torn. Refusing to resume from it."
                ) from None
            if record.get("type") == "pin":
                stored = record.get("pin") or {}
                if stored != self.pin:
                    diff = {
                        k: {"cache": stored.get(k), "now": self.pin.get(k)}
                        for k in PIN_FIELDS
                        if stored.get(k) != self.pin.get(k)
                    }
                    raise CachePinMismatch(
                        f"{self.path} was written under a different backend or request: {diff}. "
                        f"Resuming would mix two builds into one number. Start a fresh run dir, "
                        f"or delete this cache deliberately."
                    )
            elif record.get("type") == "batch":
                for entry in record.get("entries", []):
                    self._entries[entry["key"]] = MappingResult.model_validate(entry["result"])
        self.loaded_from_disk = len(self._entries)

    def _truncate_torn_tail(self, torn: str) -> None:
        """Drop the torn line so the next append starts on a clean line boundary."""
        data = self.path.read_bytes()
        cut = data.rfind(torn.encode("utf-8"))
        if cut >= 0:
            with self.path.open("r+b") as fh:
                fh.truncate(cut)
                fh.flush()
                os.fsync(fh.fileno())


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()
