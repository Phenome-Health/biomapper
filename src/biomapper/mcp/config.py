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
from biomapper.mcp.gate import DEFAULT_GATE_WAIT_S, DEFAULT_LOOP_LIMIT, DEFAULT_LOOP_WINDOW_S

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
