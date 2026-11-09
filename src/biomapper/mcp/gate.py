"""The request gate and the loop guard: how one MCP process stays polite to a fragile prod.

Prod answers about 4.4 s per entity, slows superlinearly with batch size, and wedges under
concurrent load. So, per process:

* :class:`RequestGate` admits exactly one HTTP request at a time (R8). Interactive tools and the
  file-job worker both hold it for one request and release it between requests, so an interactive
  call interleaves with a running job instead of waiting for the whole job. Interactive callers
  give up after a short wait budget with :class:`GateBusy`, because Claude Desktop cancels any
  tool call at about 60 s.
* :class:`LoopGuard` counts interactive mapping calls in a sliding window (R16). An agent mapping
  a list one entity at a time is refused and pointed to ``map_file``.

Pure logic, no SDK import: the server turns these exceptions into ``ToolError``.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

DEFAULT_GATE_WAIT_S = 10.0
DEFAULT_LOOP_LIMIT = 20
DEFAULT_LOOP_WINDOW_S = 300.0


class GateBusy(Exception):
    """The gate stayed held past the caller's wait budget."""


class LoopGuardTripped(Exception):
    """Too many interactive mapping calls in the rolling window."""


class RequestGate:
    """An ``asyncio.Lock`` held for exactly one HTTP request, with an optional wait budget."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._in_flight = 0
        self.max_in_flight = 0
        self.acquisitions = 0

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    @asynccontextmanager
    async def hold(self, *, wait_budget_s: float | None) -> AsyncIterator[None]:
        """Hold the gate for one request. ``wait_budget_s=None`` waits as long as it takes."""
        if wait_budget_s is None:
            await self._lock.acquire()
        else:
            try:
                await asyncio.wait_for(self._lock.acquire(), timeout=wait_budget_s)
            except TimeoutError:
                raise GateBusy(
                    "BioMapper is busy with another request from this session (usually a chunk of "
                    "a running map_file job). Retry shortly; each request takes a few seconds."
                ) from None
        self._in_flight += 1
        self.acquisitions += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            yield
        finally:
            self._in_flight -= 1
            self._lock.release()


class LoopGuard:
    """Sliding-window limit on interactive mapping calls. Refused calls are not counted."""

    def __init__(
        self,
        limit: int = DEFAULT_LOOP_LIMIT,
        window_s: float = DEFAULT_LOOP_WINDOW_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1:
            raise ValueError("loop guard limit must be at least 1")
        if window_s <= 0:
            raise ValueError("loop guard window must be positive")
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._calls: deque[float] = deque()

    def _expire(self, now: float) -> None:
        while self._calls and now - self._calls[0] >= self.window_s:
            self._calls.popleft()

    def record(self) -> float:
        """Count one call or raise :class:`LoopGuardTripped`. Returns a release token."""
        now = self._clock()
        self._expire(now)
        if len(self._calls) >= self.limit:
            minutes = self.window_s / 60
            raise LoopGuardTripped(
                f"Refused: {len(self._calls)} interactive mapping calls in the last "
                f"{minutes:g} minutes (limit {self.limit}). Mapping a list one call at a time "
                f"overloads the shared BioMapper deployment. Write the entities to a TSV/CSV and "
                f"use map_file, which runs them as a resumable background job."
            )
        self._calls.append(now)
        return now

    def release(self, token: float) -> None:
        """Un-count a call that never reached the API (e.g. refused as busy)."""
        with contextlib.suppress(ValueError):
            self._calls.remove(token)


DEFAULT_CANDIDATE_BUDGET = 15
DEFAULT_CANDIDATE_WINDOW_S = 300.0


class CandidateBudgetExhausted(Exception):
    """Too many candidate refs fetched in the rolling window."""


class CandidateBudget:
    """Sliding-window budget on candidate refs (1.7.0), separate from :class:`LoopGuard`.

    Each candidate ref is a full re-map with Kestrel passthrough, so it is charged when it is
    attempted. Keeping it apart from the loop guard means reviewing weak rows never locks an
    agent out of ordinary lookups, and lookups never eat the review budget.
    """

    def __init__(
        self,
        limit: int = DEFAULT_CANDIDATE_BUDGET,
        window_s: float = DEFAULT_CANDIDATE_WINDOW_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1:
            raise ValueError("candidate budget must be at least 1")
        if window_s <= 0:
            raise ValueError("candidate budget window must be positive")
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._charges: deque[float] = deque()

    def _expire(self, now: float) -> None:
        while self._charges and now - self._charges[0] >= self.window_s:
            self._charges.popleft()

    def available(self) -> int:
        self._expire(self._clock())
        return self.limit - len(self._charges)

    def refusal(self, wanted: int) -> str:
        minutes = self.window_s / 60
        return (
            f"Refused: candidate lookups are limited to {self.limit} refs per {minutes:g} "
            f"minutes, and {self.available()} remain (this call asked for {wanted}). Each ref "
            f"re-maps the entity on the shared BioMapper deployment. Review the most important "
            f"weak rows first, and retry later for the rest."
        )

    def charge(self) -> None:
        now = self._clock()
        self._expire(now)
        if len(self._charges) >= self.limit:
            raise CandidateBudgetExhausted(self.refusal(1))
        self._charges.append(now)
