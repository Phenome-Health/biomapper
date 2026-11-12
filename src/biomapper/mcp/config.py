"""Server configuration: environment overrides plus the timing knobs tests shrink.

Every number here is tied to a hard external limit, so each carries the reason it has the value it
has. Claude Desktop cancels a tool call at about 60 s and ignores timeout settings, so every
interactive tool must answer well inside that; prod runs about 4.4 s per entity, so the
interactive batch cap and the job chunk are both 5.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from biomapper._provenance import DEFAULT_KESTREL_URL
from biomapper.client import DEFAULT_BASE_URL
from biomapper.mcp.gate import (
    DEFAULT_CANDIDATE_BUDGET,
    DEFAULT_CANDIDATE_WINDOW_S,
    DEFAULT_GATE_WAIT_S,
    DEFAULT_LOOP_LIMIT,
    DEFAULT_LOOP_WINDOW_S,
)

DEFAULT_HOME = Path("~/.cache/biomapper/mcp")
SECONDS_PER_ENTITY_SEED = 4.4


def _split_roots(value: str | None) -> list[Path]:
    if not value:
        return []
    return [Path(p).expanduser() for p in value.split(os.pathsep) if p.strip()]


@dataclass
class ServerConfig:
    """Everything the server reads from its environment, plus test-tunable timings."""

    base_url: str = DEFAULT_BASE_URL
    kestrel_url: str = DEFAULT_KESTREL_URL
    api_key: str | None = None
    home: Path = field(default_factory=lambda: DEFAULT_HOME.expanduser())
    env_roots: list[Path] = field(default_factory=list)

    # Interactive tools. 35 s HTTP timeout and a 50 s wall-clock deadline keep every call inside
    # Desktop's ~60 s cancel; httpx timeouts are per phase, so the deadline is what bounds a call.
    interactive_timeout_s: float = 35.0
    tool_deadline_s: float = 50.0
    gate_wait_s: float = DEFAULT_GATE_WAIT_S
    batch_cap: int = 5
    loop_limit: int = DEFAULT_LOOP_LIMIT
    loop_window_s: float = DEFAULT_LOOP_WINDOW_S

    # Candidate review (1.7.0). Each ref is one re-map with Kestrel passthrough, about 0.5-1 s on a
    # warm cache (Unit 0 spike) but up to a full map on a cold one, so at most 3 per call keeps a
    # call inside the deadline, and the budget is its own window so it never starves lookups.
    candidates_default_n: int = 10
    candidates_max_n: int = 25
    candidates_refs_per_call: int = 3
    candidate_budget: int = DEFAULT_CANDIDATE_BUDGET
    candidate_window_s: float = DEFAULT_CANDIDATE_WINDOW_S
    ref_ttl_s: float = 30 * 24 * 3600.0

    # File jobs. 300 s matches cohorts.DEFAULT_TIMEOUT_S; chunks of 5 so an interactive call waits
    # at most one chunk for the gate.
    job_timeout_s: float = 300.0
    chunk_size: int = 5
    health_timeout_s: float = 15.0
    stall_bad_chunks: int = 3
    stall_chunk_s: float = 240.0
    max_row_attempts: int = 3
    auto_resume_max_age_s: float = 7 * 24 * 3600.0
    systemic_backoff_s: float = 30.0
    seconds_per_entity_seed: float = SECONDS_PER_ENTITY_SEED

    # Harmonize jobs (1.9.0). Hard caps, checked at submit before any file is created: a job maps
    # every unique name at about 4.4 s each behind the one request gate, so an unbounded cohort
    # would hold the worker (and every file job queued behind it) for days.
    harmonize_max_cohorts: int = 12
    harmonize_max_rows: int = 20_000
    harmonize_max_bytes: int = 50 * 1024 * 1024
    harmonize_max_queued: int = 3
    # One mapping request from the worker thread, gate wait included. Above job_timeout_s (the
    # HTTP timeout) so a slow request fails as an HTTP timeout first; this bound is what stops a
    # wedged bridge from holding the thread, and with it the saved-mapping store's lock, forever.
    bridge_timeout_s: float = 900.0
    # The capture stage re-maps weak entities with candidates and looks up link-node names inside
    # the job, in impact order, up to this many requests. Entries past it are "not_captured".
    capture_max_entities: int = 200
    capture_top_n: int = 10
    review_page_default: int = 25
    review_page_max: int = 50
    link_member_cap: int = 50
    link_identifier_cap: int = 12

    # Clocks: wall time for persisted timestamps, monotonic for durations and the loop guard.
    wall_clock: Callable[[], float] = time.time
    monotonic: Callable[[], float] = time.monotonic

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ServerConfig:
        env = os.environ if env is None else env
        home = env.get("BIOMAPPER_MCP_HOME")
        return cls(
            base_url=env.get("BIOMAPPER_BASE_URL") or DEFAULT_BASE_URL,
            kestrel_url=env.get("BIOMAPPER_KESTREL_URL") or DEFAULT_KESTREL_URL,
            api_key=env.get("BIOMAPPER_API_KEY") or None,
            home=Path(home).expanduser() if home else DEFAULT_HOME.expanduser(),
            env_roots=_split_roots(env.get("BIOMAPPER_MCP_ROOTS")),
        )
