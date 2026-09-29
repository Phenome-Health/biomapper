"""Request pacing for external services, shared so one copy cannot drift from the other.

PubChem PUG-REST asks for no more than 5 requests per second. Two modules in this package call it
(:mod:`biomapper.benchmarks.cross_cohort_certify` for vendor-identifier lookups and
:mod:`biomapper.benchmarks.cross_cohort_readjudicate` for name lookups), and the cost of getting it
wrong is specific: a throttled request comes back as ``lookup_failed``, which becomes a refusal that
is a run artifact rather than an absent structure, and a certified/refused split computed over those
is not publishable.

This lived as two separate implementations and they were not equivalent. The re-adjudication side
paced per REQUEST off a monotonic timestamp; the certification side slept once per ROW, so a row
carrying both a PubChem CID and an HMDB accession issued its fallback request unpaced. That is the
failure mode the project's own solutions note calls out: duplicated logic is how one copy gets the
check and its sibling does not. One implementation, both callers.
"""

from __future__ import annotations

import time

# PubChem PUG-REST's published ceiling is 5 requests per second. 0.25s leaves headroom without
# making a 600-lookup pass unreasonably slow.
PUBCHEM_MIN_INTERVAL_S = 0.25


class Pacer:
    """Spaces successive calls by at least ``min_interval_s``.

    Call :meth:`wait` immediately before each outgoing request, and only on a cache miss: pacing a
    cache hit would add latency for no benefit and would make a repeated lookup look expensive.

    The first call does not sleep, because there is no predecessor to space from. Timing is taken
    from :func:`time.monotonic`, so a wall-clock adjustment mid-run cannot make the pacer sleep for
    an unbounded period.
    """

    def __init__(self, min_interval_s: float = PUBCHEM_MIN_INTERVAL_S) -> None:
        self.min_interval_s = min_interval_s
        self._last_at: float | None = None

    def wait(self) -> None:
        if self.min_interval_s <= 0:
            return
        now = time.monotonic()
        if self._last_at is not None:
            elapsed = now - self._last_at
            if elapsed < self.min_interval_s:
                time.sleep(self.min_interval_s - elapsed)
        self._last_at = time.monotonic()
