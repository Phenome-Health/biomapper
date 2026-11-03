"""Mapper-free run pins: which API and which KG build answered, labeled honestly.

Factored out of ``biomapper.cohorts._collect_pins`` so the MCP server, which has no cohort mapper,
records exactly the same api/kestrel block with the same honesty labels. The cohort function now
calls :func:`collect_pins` and adds its ``mapper`` keys on top, so its output is unchanged.

Never raises: a pin that could not be read is recorded with its error, because "we could not read
the build" must stay distinguishable from "the build self-reports unknown".
"""

from __future__ import annotations

import datetime as dt
import os
from typing import Any

import httpx

from biomapper._provenance import DEFAULT_KESTREL_URL, UNKNOWN, fetch_kg_build_info
from biomapper._version import resolve_version

API_HEALTH_TIMEOUT_S = 15.0

KESTREL_LABEL = (
    "Kestrel's own /health; the graph the API used is assumed, not verified, to be this build"
)


def api_health(
    base_url: str, api_key: str | None, *, timeout: float = API_HEALTH_TIMEOUT_S
) -> dict[str, Any]:
    """``GET {base_url}/health`` as a pin record. Never raises."""
    key = api_key or os.getenv("BIOMAPPER_API_KEY")
    headers = {"X-API-Key": key} if key else {}
    try:
        with httpx.Client(timeout=timeout, headers=headers) as client:
            response = client.get(f"{base_url.rstrip('/')}/health")
            response.raise_for_status()
            data = response.json()
        return {"status": data.get("status"), "self_reported_version": data.get("version"),
                "error": None}
    except Exception as exc:  # noqa: BLE001 — a pin must never abort a run
        return {"status": "unreachable", "self_reported_version": None,
                "error": f"{type(exc).__name__}: {exc}"}


def collect_pins(
    *,
    probe: bool,
    base_url: str,
    api_key: str | None,
    kestrel_url: str = DEFAULT_KESTREL_URL,
) -> dict[str, Any]:
    """The api/kestrel pin block. ``probe=False`` records "not probed" instead of calling out."""
    if probe:
        api = api_health(base_url, api_key)
        kestrel_version, kg_build, kestrel_error = fetch_kg_build_info(kestrel_url)
        kg: dict[str, Any] | None = kg_build.model_dump()
    else:
        api = {"status": "not probed", "self_reported_version": None, "error": None}
        kestrel_version, kg, kestrel_error = "not probed", None, None
    return {
        "timestamp_utc": dt.datetime.now(dt.UTC).isoformat(),
        "biomapper_version": resolve_version(),
        "api": {
            "base_url": base_url,
            **api,
            "self_reported_version_label": "self-reported, known stale",
            # The API does not expose the engine release it runs.
            "engine_release": "unavailable",
        },
        "kestrel": {
            "url": kestrel_url,
            "kestrel_version": kestrel_version,
            "kg_build": kg,
            "error": kestrel_error,
            "label": KESTREL_LABEL,
        },
    }


def kg_build_label(pins: dict[str, Any]) -> str:
    """The KG build a pin block names (``kg_version`` + short commit), or ``"unknown"``."""
    kg = (pins.get("kestrel") or {}).get("kg_build") or {}
    version = kg.get("kg_version") or UNKNOWN
    if version == UNKNOWN:
        return UNKNOWN
    commit = kg.get("git_commit") or UNKNOWN
    return version if commit == UNKNOWN else f"{version} ({str(commit)[:8]})"


def kg_verified(pins: dict[str, Any]) -> bool:
    """Whether the pin block names a real KG build. ``False`` for unknown or unprobed."""
    return kg_build_label(pins) != UNKNOWN
