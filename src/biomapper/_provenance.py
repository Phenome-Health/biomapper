"""Pandas-free run provenance: what graph build actually served a mapping run.

Moved out of ``biomapper.benchmarks.provenance`` (which re-exports both names) so the core install,
which has no pandas, can pin a :func:`biomapper.harmonize_cohorts` run. The benchmark package's
``__init__`` imports pandas transitively; this module imports only httpx and pydantic, which are
core dependencies.

**Never hardcode a build.** ``GET {kestrel}/health`` is the only authority for the KG build. Do not
read the service version from ``/openapi.json`` either: its ``info.version`` is a stale framework
default.
"""

from __future__ import annotations

import logging

import httpx
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_KESTREL_URL = "https://kestrel.krakenkg.com/api"
HEALTH_TIMEOUT = 15.0

UNKNOWN = "unknown"

logger = logging.getLogger(__name__)


class KgBuildInfo(BaseModel):
    """KG build metadata as served by Kestrel ``/health`` (originates in KRAKEN build_info).

    ``extra="allow"`` deliberately: operational fields (``steps_run``,
    ``build_duration_minutes``) and future enrichments pass through untyped rather than
    being dropped. For a provenance record, silently discarding a field the upstream gained
    is worse than carrying one we do not model.
    """

    model_config = ConfigDict(extra="allow")

    kg_version: str = UNKNOWN
    kraken_package_version: str = UNKNOWN
    biolink_version: str = UNKNOWN
    build_timestamp: str = UNKNOWN
    git_commit: str = UNKNOWN
    sources: list[str] = Field(default_factory=list)
    source_versions: dict[str, str] = Field(default_factory=dict)
    kg_label: str | None = None


def fetch_kg_build_info(
    kestrel_url: str = DEFAULT_KESTREL_URL,
) -> tuple[str, KgBuildInfo, str | None]:
    """Return ``(kestrel_version, KgBuildInfo, error)`` from Kestrel ``/health``.

    Keyless by design: the public KRAKEN endpoint takes no key and must never be sent one.

    Degrades rather than raising — the caller always gets a usable object, so a manifest
    records that provenance was unavailable instead of omitting the field. The third element
    is the error text, present precisely so "we could not read the build" is distinguishable
    from "the build self-reports unknown".
    """
    url = f"{kestrel_url.rstrip('/')}/health"
    try:
        with httpx.Client(timeout=HEALTH_TIMEOUT, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:  # noqa: BLE001 — provenance must never abort a run
        message = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "Could not read Kestrel /health at %s (%s); recording 'unknown'", url, message
        )
        return UNKNOWN, KgBuildInfo(), message

    kestrel_version = data.get("kestrel_version", UNKNOWN)
    kg_build_raw = data.get("kg_build") or {}
    if not kg_build_raw:
        message = f"Kestrel /health at {url} returned an empty kg_build — KG provenance unavailable"
        logger.warning(message)
        return kestrel_version, KgBuildInfo(), message
    return kestrel_version, KgBuildInfo.model_validate(kg_build_raw), None
