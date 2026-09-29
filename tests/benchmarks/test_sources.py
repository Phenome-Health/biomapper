"""Source-integrity tests. These guard the two behaviours with actual scars behind them."""

from __future__ import annotations

import hashlib

import httpx
import pytest

from biomapper.benchmarks import sources


def test_empty_body_is_refused_even_on_http_200(monkeypatch):
    """The dead-but-200 guard: a zero-byte body must never read as success.

    This is the SwissLipids failure mode. A streaming adapter treats an empty 200 as a valid
    empty dataset and produces an empty-but-successful arm.
    """

    def fake_get(self, url, **kwargs):
        return httpx.Response(200, content=b"", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    with pytest.raises(sources.SourceIntegrityError, match="EMPTY body"):
        sources.fetch("https://example.invalid/lipids.tsv")


def test_sha_mismatch_is_refused_and_names_both_hashes(monkeypatch):
    body = b"some other bytes"

    def fake_get(self, url, **kwargs):
        return httpx.Response(200, content=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    with pytest.raises(sources.SourceIntegrityError) as excinfo:
        sources.fetch("https://example.invalid/x.csv", expected_sha256="0" * 64)
    message = str(excinfo.value)
    assert "expected 0000" in message
    assert hashlib.sha256(body).hexdigest() in message
    # A mismatch must not read as "try again": re-fetching a changed source just re-confirms it.
    assert "Do not re-fetch" in message


def test_matching_sha_passes_through(monkeypatch):
    body = b"header\n1,2\n"
    digest = hashlib.sha256(body).hexdigest()

    def fake_get(self, url, **kwargs):
        return httpx.Response(200, content=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    assert sources.fetch("https://example.invalid/x.csv", expected_sha256=digest) == body


def test_transport_failure_becomes_a_skip_with_a_reason(monkeypatch):
    """Unreachable is a SKIP. It must be distinguishable from reachable-but-wrong."""

    def fake_get(self, url, **kwargs):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    with pytest.raises(sources.SourceUnavailable) as excinfo:
        sources.fetch("https://example.invalid/x.csv", dataset="somearm")
    assert excinfo.value.dataset == "somearm"
    assert "fetch failed" in excinfo.value.reason


def test_swisslipids_is_declared_unavailable_with_the_dead_but_200_reason():
    """SwissLipids must skip with a reason, before any network call is attempted."""
    with pytest.raises(sources.SourceUnavailable) as excinfo:
        sources.acquire("swisslipids", "https://www.swisslipids.org/api/file.php")
    reason = excinfo.value.reason
    assert "zero-byte" in reason
    assert "not a HEAD artifact" in reason


def test_srm1950_reads_the_pin_and_verifies_it(tmp_path, monkeypatch):
    """SRM 1950 comes from the SHA-verified local pin, not the expired-certificate URL."""
    pin = sources.PINNED_SOURCES["srm1950"]
    body = b"HMDB_ID,NAME\nHMDB0000001,x\n"
    (tmp_path / pin.filename).write_bytes(body)
    monkeypatch.setattr(sources, "PINNED_SOURCES_DIR", tmp_path)
    # The real pin's SHA will not match this fixture, which is the point: a wrong-bytes pin stops
    # the arm rather than being scored.
    with pytest.raises(sources.SourceIntegrityError, match="sha256 mismatch"):
        sources.acquire("srm1950", None)

    monkeypatch.setitem(
        sources.PINNED_SOURCES,
        "srm1950",
        sources.PinnedSource(
            filename=pin.filename,
            sha256=hashlib.sha256(body).hexdigest(),
            upstream_url=pin.upstream_url,
            reason=pin.reason,
        ),
    )
    raw, provenance = sources.acquire("srm1950", None)
    assert raw == body
    assert provenance["route"] == "pinned_local"
    # The provenance must say it came from a pin, so a reader is never left to infer that a
    # certificate-expired URL was fetched.
    assert "certificate expired" in provenance["pinned_reason"]


def test_missing_pin_skips_rather_than_fetching_the_expired_url(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "PINNED_SOURCES_DIR", tmp_path / "absent")
    with pytest.raises(sources.SourceUnavailable) as excinfo:
        sources.acquire("srm1950", "https://srm1950-data.wishartlab.com/metabolites.csv")
    assert "is absent" in excinfo.value.reason


def test_no_source_url_and_no_pin_is_a_skip():
    with pytest.raises(sources.SourceUnavailable, match="no source_url configured"):
        sources.acquire("nonexistent-arm", "")
