"""Source acquisition with the integrity assertions this suite has scars from.

Two behaviours are load-bearing and must never be relaxed:

1. **A fetch asserts a non-empty body, and an expected SHA when one is pinned.** The
   SwissLipids source began returning ``HTTP 200`` with a **zero-byte body**; a streaming
   adapter read that as success and produced an empty-but-successful arm. The same shape
   shows up as a HEAD artifact on the Hajjar supplement (HEAD reports 200 / 0 bytes while
   GET returns 916,657). So: GET only, assert bytes, assert the SHA.

2. **A dataset that cannot be sourced is SKIPPED WITH A REASON, never an empty success.**
   :class:`SourceUnavailable` carries the reason all the way into the suite manifest as
   ``status="skipped"``. An empty frame that reaches a scorer produces a confident 0%, which
   reads as a measurement rather than a broken input.

Certificate verification is never disabled. NIST SRM 1950's upstream certificate expired
2026-09-15, and the response to that is a SHA-verified local pin (see
:data:`PINNED_SOURCES`), not ``verify=False`` — disabling verification inside the run path
would silently accept any future substitution, whereas a pinned SHA cannot.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

import httpx

DEFAULT_FETCH_TIMEOUT = 120.0

# Pinned local artifacts, read instead of fetching. Keyed by dataset key.
#
# The directory is overridable via BIOMAPPER_PINNED_SOURCES so a CI runner or another
# machine can supply the same SHA-verified bytes from elsewhere; the SHA is what makes the
# copy trustworthy, not its path.
PINNED_SOURCES_DIR = Path(
    os.getenv("BIOMAPPER_PINNED_SOURCES")
    or (Path.home() / "external_benchmark_runs" / "_pinned_sources")
)


@dataclass(frozen=True)
class PinnedSource:
    """A source read from disk rather than fetched, with the reason it is pinned."""

    filename: str
    sha256: str
    upstream_url: str
    reason: str


PINNED_SOURCES: dict[str, PinnedSource] = {
    "srm1950": PinnedSource(
        filename="srm1950_metabolites.csv",
        sha256="c4662210ee08c5c741d741ab309c482a7d95beff5d2fbae94f5ddeaae8f44ccb",
        upstream_url="https://srm1950-data.wishartlab.com/metabolites.csv",
        reason=(
            "upstream TLS certificate expired 2026-09-15; these bytes verify against the SHA "
            "recorded before the lapse (RERUN_CAMPAIGN_PLAN.md 2026-07-21), so the content is "
            "byte-identical to the pre-expiry artifact. Re-point at the live URL only once the "
            "certificate is renewed AND a clean verified fetch reproduces this exact SHA."
        ),
    ),
}

# Datasets with no reachable source at all. These report as skipped, with this reason.
UNAVAILABLE_SOURCES: dict[str, str] = {
    "swisslipids": (
        "https://www.swisslipids.org/api/file.php?cast=normal&file=lipids.tsv returns HTTP 200 "
        "with a zero-byte text/html body on a full GET (verified 2026-09-23, not a HEAD "
        "artifact) and no usable local copy exists. This is the dead-but-200 failure mode: the "
        "arm has no available source until upstream is fixed or another distribution is located."
    ),
}


class SourceIntegrityError(RuntimeError):
    """A source was reachable but its bytes are not the bytes we pinned.

    Distinct from :class:`SourceUnavailable`: this is a *wrong* source, not a missing one,
    and it must stop the arm rather than skip it. A silent mismatch is how a substituted
    upstream gets scored as if it were the pinned artifact.
    """


class SourceUnavailable(RuntimeError):
    """A source could not be obtained. Carries the reason into the manifest as a skip.

    Raised (not swallowed) so a caller cannot mistake it for an empty success; the suite
    runner catches it specifically and records ``status="skipped"`` plus ``reason``.
    """

    def __init__(self, dataset: str, reason: str) -> None:
        self.dataset = dataset
        self.reason = reason
        super().__init__(f"{dataset}: {reason}")


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def assert_nonempty(raw: bytes, *, url: str) -> None:
    """Refuse a zero-byte body. The dead-but-200 guard.

    Checked separately from the SHA so the error names the actual failure: an empty body is
    a dead source, whereas a SHA mismatch is a changed one, and conflating them sends the
    next reader looking in the wrong place.
    """
    if not raw:
        raise SourceIntegrityError(
            f"{url} returned an EMPTY body. An HTTP 200 with zero bytes reads as success to a "
            f"streaming adapter and yields an empty-but-successful run; refusing it. This is the "
            f"SwissLipids failure mode."
        )


def assert_sha256(raw: bytes, expected: str, *, what: str) -> str:
    """Assert the bytes hash to ``expected``; return the actual SHA.

    A mismatch is never retried automatically: if the pinned artifact changed, the correct
    response is to look at why, not to fetch again and hope.
    """
    actual = sha256_bytes(raw)
    if expected and actual != expected:
        raise SourceIntegrityError(
            f"{what}: sha256 mismatch.\n  expected {expected}\n  actual   {actual}\n"
            f"Do not re-fetch. A mismatch means the source changed; verify the new bytes are "
            f"legitimate and re-pin deliberately."
        )
    return actual


def fetch(
    url: str,
    *,
    expected_sha256: str | None = None,
    timeout: float = DEFAULT_FETCH_TIMEOUT,
    dataset: str | None = None,
) -> bytes:
    """GET ``url``, assert a non-empty body, and assert ``expected_sha256`` when pinned.

    Always a GET, never a HEAD: HEAD against the Hajjar supplement reports 200 with 0 bytes,
    which is indistinguishable from a dead source unless the body is actually read.

    Transport/HTTP failures become :class:`SourceUnavailable` so the arm skips with a reason
    rather than crashing the suite. Integrity failures become :class:`SourceIntegrityError`
    and stop the arm: reachable-but-wrong is not the same as unreachable.
    """
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout) as client:
            response = client.get(url)
            response.raise_for_status()
            raw = response.content
    except httpx.HTTPError as exc:
        raise SourceUnavailable(
            dataset or url, f"fetch failed: {type(exc).__name__}: {exc}"
        ) from exc

    assert_nonempty(raw, url=url)
    if expected_sha256:
        assert_sha256(raw, expected_sha256, what=url)
    return raw


def read_pinned(dataset: str) -> tuple[bytes, PinnedSource]:
    """Read a pinned local artifact and verify its SHA before handing it back.

    Missing file -> :class:`SourceUnavailable` (skip with a reason). Present but wrong SHA ->
    :class:`SourceIntegrityError` (stop).
    """
    pin = PINNED_SOURCES[dataset]
    path = PINNED_SOURCES_DIR / pin.filename
    if not path.is_file():
        raise SourceUnavailable(
            dataset,
            f"pinned source {path} is absent. Expected sha256 {pin.sha256}. Pinned because: "
            f"{pin.reason}",
        )
    raw = path.read_bytes()
    assert_nonempty(raw, url=str(path))
    assert_sha256(raw, pin.sha256, what=str(path))
    return raw, pin


def acquire(
    dataset: str, url: str | None, *, expected_sha256: str | None = None
) -> tuple[bytes, dict[str, object]]:
    """Obtain a dataset's bytes by the right route, and describe how.

    Resolution order, most-trusted first:

    1. Declared unavailable -> :class:`SourceUnavailable` immediately. No network call: a
       source we have already established is dead should not cost a timeout per run.
    2. Pinned locally -> read from disk, SHA-verified.
    3. Otherwise -> fetch, non-empty-asserted and SHA-asserted when a SHA is pinned.

    Returns ``(raw_bytes, provenance)`` where ``provenance`` is manifest-ready: it records
    which route served the bytes, so a reader can tell a live fetch from a pin without
    cross-referencing anything.
    """
    if dataset in UNAVAILABLE_SOURCES:
        raise SourceUnavailable(dataset, UNAVAILABLE_SOURCES[dataset])

    if dataset in PINNED_SOURCES:
        raw, pin = read_pinned(dataset)
        return raw, {
            "route": "pinned_local",
            "path": str(PINNED_SOURCES_DIR / pin.filename),
            "source_url": pin.upstream_url,
            "source_sha256": pin.sha256,
            "source_bytes": len(raw),
            "pinned_reason": pin.reason,
        }

    if not url:
        raise SourceUnavailable(dataset, "no source_url configured and no pinned local artifact")

    raw = fetch(url, expected_sha256=expected_sha256, dataset=dataset)
    return raw, {
        "route": "fetched",
        "source_url": url,
        "source_sha256": sha256_bytes(raw),
        "source_bytes": len(raw),
        "expected_sha256": expected_sha256,
    }
