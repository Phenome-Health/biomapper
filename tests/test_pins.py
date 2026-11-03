"""Unit 1: mapper-free pin collection, shared by harmonize_cohorts and the MCP server.

``cohorts._collect_pins`` needs a cohort ``mapper``; the server has none. The api/kestrel core was
factored into ``biomapper._pins.collect_pins`` with the same honesty labels, and the cohort
function now adds its mapper keys on top. Its output must be unchanged.
"""

from __future__ import annotations

import httpx
import respx

from biomapper import _pins, cohorts
from biomapper._version import resolve_version

API = "https://api.example/api/v1"
KESTREL = "https://kestrel.example/api"
KG_BUILD = {"kg_version": "2.3.0", "git_commit": "abc123", "build_timestamp": "2026-10-01"}


def _mock_healthy() -> None:
    respx.get(f"{API}/health").mock(
        return_value=httpx.Response(200, json={"status": "healthy", "version": "0.1.0"})
    )
    respx.get(f"{KESTREL}/health").mock(
        return_value=httpx.Response(200, json={"kestrel_version": "1.9", "kg_build": KG_BUILD})
    )


class _FakeMapper:
    pins = {"mode": "fake"}


def _without_timestamp(d: dict) -> dict:
    return {k: v for k, v in d.items() if k != "timestamp_utc"}


@respx.mock
def test_collect_pins_reads_api_and_kestrel_with_honesty_labels() -> None:
    _mock_healthy()
    pins = _pins.collect_pins(probe=True, base_url=API, api_key=None, kestrel_url=KESTREL)

    assert pins["biomapper_version"] == resolve_version()
    assert pins["api"]["base_url"] == API
    assert pins["api"]["status"] == "healthy"
    assert pins["api"]["self_reported_version_label"] == "self-reported, known stale"
    assert pins["api"]["engine_release"] == "unavailable"
    assert pins["kestrel"]["url"] == KESTREL
    assert pins["kestrel"]["kestrel_version"] == "1.9"
    assert pins["kestrel"]["kg_build"]["kg_version"] == "2.3.0"
    assert pins["kestrel"]["error"] is None
    assert "assumed, not verified" in pins["kestrel"]["label"]
    assert "mapper" not in pins


@respx.mock
def test_cohort_pins_are_the_core_pins_plus_mapper_keys() -> None:
    """The refactor is output-preserving: same keys, labels, values, in the same order."""
    _mock_healthy()
    core = _pins.collect_pins(probe=True, base_url=API, api_key=None, kestrel_url=KESTREL)
    cohort = cohorts._collect_pins(
        probe=True, base_url=API, api_key=None, kestrel_url=KESTREL, mapper=_FakeMapper()
    )
    assert list(cohort) == [
        "timestamp_utc", "biomapper_version", "api", "kestrel", "mapper", "mapper_pins"
    ]
    assert cohort["mapper"] == "_FakeMapper"
    assert cohort["mapper_pins"] == {"mode": "fake"}
    trimmed = {k: v for k, v in cohort.items() if k not in ("mapper", "mapper_pins")}
    assert _without_timestamp(trimmed) == _without_timestamp(core)


def test_unprobed_pins_say_so() -> None:
    pins = _pins.collect_pins(probe=False, base_url=API, api_key=None, kestrel_url=KESTREL)
    assert pins["api"]["status"] == "not probed"
    assert pins["kestrel"]["kestrel_version"] == "not probed"
    assert pins["kestrel"]["kg_build"] is None


@respx.mock
def test_unreachable_kestrel_records_unknown_and_never_raises() -> None:
    respx.get(f"{API}/health").mock(return_value=httpx.Response(200, json={"status": "healthy"}))
    respx.get(f"{KESTREL}/health").mock(side_effect=httpx.ConnectError("down"))
    pins = _pins.collect_pins(probe=True, base_url=API, api_key=None, kestrel_url=KESTREL)
    assert pins["kestrel"]["kestrel_version"] == "unknown"
    assert pins["kestrel"]["kg_build"]["kg_version"] == "unknown"
    assert "ConnectError" in pins["kestrel"]["error"]


@respx.mock
def test_unreachable_api_is_recorded_not_raised() -> None:
    respx.get(f"{API}/health").mock(side_effect=httpx.ConnectError("down"))
    respx.get(f"{KESTREL}/health").mock(side_effect=httpx.ConnectError("down"))
    pins = _pins.collect_pins(probe=True, base_url=API, api_key="k", kestrel_url=KESTREL)
    assert pins["api"]["status"] == "unreachable"
    assert "ConnectError" in pins["api"]["error"]


def test_kestrel_url_defaults_to_the_packaged_default() -> None:
    from biomapper._provenance import DEFAULT_KESTREL_URL

    pins = _pins.collect_pins(probe=False, base_url=API, api_key=None)
    assert pins["kestrel"]["url"] == DEFAULT_KESTREL_URL


def test_kg_verified_is_false_for_unknown_or_unprobed_builds() -> None:
    assert _pins.kg_verified({"kestrel": {"kg_build": {"kg_version": "2.3.0"}}}) is True
    assert _pins.kg_verified({"kestrel": {"kg_build": {"kg_version": "unknown"}}}) is False
    assert _pins.kg_verified({"kestrel": {"kg_build": None}}) is False
    assert _pins.kg_verified({}) is False
