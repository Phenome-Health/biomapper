"""Shared fixtures for the MCP server tests: config, respx routes, an in-flight counter."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx

API = "https://api.test/api/v1"
KESTREL = "https://kestrel.test/api"
KG_BUILD = {"kg_version": "2.3.0", "git_commit": "3dd08a5b9f", "build_timestamp": "2026-10-01"}

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "mcp"


def prod_batch_payload() -> dict[str, Any]:
    """A /map/batch response captured from prod on 2026-11-03 (2 entities, keyless)."""
    return json.loads((FIXTURES / "prod_map_batch_2026-11-03.json").read_text())


def make_config(tmp_path: Path, **overrides: Any) -> Any:  # noqa: ANN401
    from biomapper.mcp.config import ServerConfig

    values: dict[str, Any] = {
        "base_url": API,
        "kestrel_url": KESTREL,
        "api_key": None,
        "home": tmp_path / "mcp-home",
        "env_roots": [],
        "systemic_backoff_s": 0.0,
    }
    values.update(overrides)
    return ServerConfig(**values)


def resolved_entry(name: str, curie: str = "CHEBI:15971", score: float = 2.5) -> dict[str, Any]:
    return {
        "name": name,
        "curies": [curie],
        "chosen_kg_id": curie,
        "kg_equivalent_ids": {"CHEBI": [curie.split(":")[1]]},
        "assigned_ids": {
            "kestrel-hybrid-search": {"CHEBI": {curie.split(":")[1]: {"score": score}}}
        },
        "error": None,
    }


def batch_response(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {"results": entries, "metadata": {"request_id": "t", "processing_time_ms": 1.0}}


class InFlight:
    """Counts concurrent /map requests; ``peak`` must never exceed 1 (R8)."""

    def __init__(self) -> None:
        self.now = 0
        self.peak = 0
        self.calls = 0
        self.bodies: list[dict[str, Any]] = []

    async def enter(self, request: httpx.Request, delay: float = 0.0) -> None:
        self.now += 1
        self.calls += 1
        self.peak = max(self.peak, self.now)
        if request.content:
            self.bodies.append(json.loads(request.content))
        try:
            await asyncio.sleep(delay)
        finally:
            self.now -= 1


def healthy_routes(router: Any) -> None:  # noqa: ANN401
    router.get(f"{API}/health").mock(
        return_value=httpx.Response(200, json={"status": "healthy", "version": "0.1.0"})
    )
    router.get(f"{KESTREL}/health").mock(
        return_value=httpx.Response(200, json={"kestrel_version": "1.9", "kg_build": KG_BUILD})
    )


def batch_echo(inflight: InFlight, delay: float = 0.0, resolve: bool = True) -> Any:  # noqa: ANN401
    """A /map/batch side effect that resolves every entity it is sent."""

    async def handler(request: httpx.Request) -> httpx.Response:
        await inflight.enter(request, delay)
        names = [e["name"] for e in json.loads(request.content)["entities"]]
        entries = [resolved_entry(n) if resolve else {"name": n, "curies": []} for n in names]
        return httpx.Response(200, json=batch_response(entries))

    return handler
