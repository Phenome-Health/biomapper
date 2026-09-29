"""Request pacing (offline; time.sleep is captured, never actually slept)."""

from __future__ import annotations

import pytest

from biomapper.benchmarks.pacing import PUBCHEM_MIN_INTERVAL_S, Pacer


@pytest.fixture
def sleeps(monkeypatch):
    recorded: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: recorded.append(s))
    return recorded


def test_the_first_call_does_not_sleep(sleeps):
    Pacer(0.25).wait()
    assert sleeps == []


def test_every_subsequent_call_is_paced(sleeps):
    # The defect this replaces slept once per ROW while issuing two REQUESTS, so a fallback lookup
    # went out unspaced and could draw a lookup_failed of its own.
    pacer = Pacer(0.25)
    for _ in range(3):
        pacer.wait()
    assert len(sleeps) == 2
    assert all(0 < s <= 0.25 for s in sleeps)


def test_a_disabled_pacer_never_sleeps(sleeps):
    pacer = Pacer(0)
    for _ in range(5):
        pacer.wait()
    assert sleeps == []


def test_a_slow_caller_is_not_penalised(monkeypatch, sleeps):
    # If more than the interval already elapsed, there is nothing to wait for.
    clock = iter([0.0, 0.0, 10.0, 10.0])
    monkeypatch.setattr("time.monotonic", lambda: next(clock))
    pacer = Pacer(0.25)
    pacer.wait()
    pacer.wait()
    assert sleeps == []


def test_the_default_respects_pubchems_published_ceiling():
    # PUG-REST publishes 5 requests per second.
    assert PUBCHEM_MIN_INTERVAL_S >= 1 / 5
    assert Pacer().min_interval_s == PUBCHEM_MIN_INTERVAL_S


# ==================================================================================================
# Pacing must fire on the request path, not on a cache hit
# ==================================================================================================


def test_the_resolver_paces_requests_and_not_cache_hits(sleeps):
    """The defect: pacing at the call site slept on cached identifiers too.

    The cohort panel de-duplicates on NAME, not on identifier, so distinct names can share a PubChem
    CID. Pacing outside the resolver cannot know whether a request is about to be sent, so each
    repeat cost a full interval without issuing one and delayed the next real lookup as well.
    """
    from biomapper.benchmarks.scorers.independent_inchikey import PubChemInChIKeyResolver

    class _Resp:
        status_code = 200
        text = "WQZGKKKJIJFFOK-GASJEMHNSA-N"

    class _Session:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, _url: str, timeout: float = 0) -> _Resp:
            self.calls += 1
            return _Resp()

    session = _Session()
    resolver = PubChemInChIKeyResolver(session=session, pacer=Pacer(0.25))
    path = "compound/cid/5793/property/InChIKey/TXT"
    resolver._cached_resolve("pubchem:5793", path)
    resolver._cached_resolve("pubchem:5793", path)  # same identifier, different panel name
    resolver._cached_resolve("pubchem:5793", path)

    assert session.calls == 1  # one request, two cache hits
    assert sleeps == []  # and the first request has no predecessor to space from

    # A DISTINCT identifier must still be paced. Without this the test would pass with pacing
    # removed altogether, which makes it a test of the cache rather than of the fix.
    resolver._cached_resolve("pubchem:5794", "compound/cid/5794/property/InChIKey/TXT")
    assert session.calls == 2
    assert len(sleeps) == 1
    assert 0 < sleeps[0] <= 0.25


def test_the_resolver_without_a_pacer_is_unchanged(sleeps):
    # The suite constructs this resolver with no pacer; that path must not start sleeping.
    from biomapper.benchmarks.scorers.independent_inchikey import PubChemInChIKeyResolver

    class _Session:
        def get(self, _url: str, timeout: float = 0):  # noqa: ANN202
            class _R:
                status_code = 404
                text = ""

            return _R()

    resolver = PubChemInChIKeyResolver(session=_Session())
    resolver._cached_resolve("k1", "p1")
    resolver._cached_resolve("k2", "p2")
    assert sleeps == []
