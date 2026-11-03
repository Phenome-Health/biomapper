"""Unit 3: the request gate and the rolling-window loop guard (pure logic, injectable clock).

The gate is the one-in-flight invariant (R8): every HTTP request the server makes, interactive or
job, holds it for exactly that one request. The loop guard (R16) refuses an agent that keeps
calling the interactive mapping tools in a loop instead of using ``map_file``.
"""

from __future__ import annotations

import asyncio

import pytest

from biomapper.mcp.gate import GateBusy, LoopGuard, LoopGuardTripped, RequestGate


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# ---------------------------------------------------------------------------
# Loop guard
# ---------------------------------------------------------------------------


def test_loop_guard_allows_up_to_the_limit_then_refuses() -> None:
    clock = FakeClock()
    guard = LoopGuard(limit=20, window_s=300.0, clock=clock)
    for _ in range(20):
        guard.record()
        clock.now += 1
    with pytest.raises(LoopGuardTripped) as exc:
        guard.record()
    assert "map_file" in str(exc.value)
    assert "20" in str(exc.value)


def test_loop_guard_allows_again_once_the_window_slides() -> None:
    clock = FakeClock()
    guard = LoopGuard(limit=3, window_s=60.0, clock=clock)
    for _ in range(3):
        guard.record()
    with pytest.raises(LoopGuardTripped):
        guard.record()
    clock.now += 60.01
    guard.record()  # the oldest three have aged out


def test_a_refused_call_is_not_counted() -> None:
    """Otherwise an agent retrying a refusal would extend its own lockout forever."""
    clock = FakeClock()
    guard = LoopGuard(limit=2, window_s=10.0, clock=clock)
    guard.record()
    guard.record()
    for _ in range(5):
        with pytest.raises(LoopGuardTripped):
            guard.record()
    clock.now += 10.01
    guard.record()
    guard.record()


def test_loop_guard_can_un_count_a_call_that_never_reached_the_api() -> None:
    clock = FakeClock()
    guard = LoopGuard(limit=1, window_s=10.0, clock=clock)
    token = guard.record()
    guard.release(token)
    guard.record()


def test_loop_guard_rejects_nonsense_limits() -> None:
    with pytest.raises(ValueError):
        LoopGuard(limit=0, window_s=10.0)
    with pytest.raises(ValueError):
        LoopGuard(limit=1, window_s=0)


# ---------------------------------------------------------------------------
# Request gate
# ---------------------------------------------------------------------------


async def test_gate_serializes_holders() -> None:
    gate = RequestGate()
    inside = 0
    peak = 0

    async def worker() -> None:
        nonlocal inside, peak
        async with gate.hold(wait_budget_s=5.0):
            inside += 1
            peak = max(peak, inside)
            await asyncio.sleep(0.01)
            inside -= 1

    await asyncio.gather(*(worker() for _ in range(5)))
    assert peak == 1


async def test_gate_refuses_busy_after_the_wait_budget() -> None:
    gate = RequestGate()
    async with gate.hold(wait_budget_s=1.0):
        loop = asyncio.get_running_loop()
        start = loop.time()
        with pytest.raises(GateBusy) as exc:
            async with gate.hold(wait_budget_s=0.05):
                pass  # pragma: no cover
        assert loop.time() - start < 1.0
    assert "retry" in str(exc.value).lower()


async def test_gate_with_no_budget_waits_indefinitely() -> None:
    """The job worker waits as long as it takes; only interactive calls have a budget."""
    gate = RequestGate()
    order: list[str] = []

    async def holder() -> None:
        async with gate.hold(wait_budget_s=None):
            await asyncio.sleep(0.05)
            order.append("first")

    async def waiter() -> None:
        await asyncio.sleep(0.01)
        async with gate.hold(wait_budget_s=None):
            order.append("second")

    await asyncio.gather(holder(), waiter())
    assert order == ["first", "second"]


async def test_gate_is_released_when_the_holder_raises() -> None:
    gate = RequestGate()
    with pytest.raises(RuntimeError):
        async with gate.hold(wait_budget_s=1.0):
            raise RuntimeError("boom")
    assert not gate.busy
    async with gate.hold(wait_budget_s=0.01):
        assert gate.busy


async def test_gate_counts_peak_concurrency_for_diagnostics() -> None:
    gate = RequestGate()
    async with gate.hold(wait_budget_s=1.0):
        pass
    async with gate.hold(wait_budget_s=1.0):
        pass
    assert gate.max_in_flight == 1
    assert gate.acquisitions == 2
