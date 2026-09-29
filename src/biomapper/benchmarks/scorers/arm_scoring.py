"""Unit 4 (pure-logic) — frozen refusal-sensitive score + anti-pooling guard.

Two guards against this project's collapse mode (aggregate rates collapse; per-artifact claims
survive):

1. **The score is pinned to a formula** (R8/R8a/R8b): ``score = certified / |comparable|`` where
   ``|comparable|`` is the FIXED count of resolved (comparable) rows — NOT ``certified / asserted``.
   Because the denominator is fixed, refusing a link can only ever remove from the numerator, so
   refusing can never raise the score (the gaming move R8 forbids). Both metrics are reported, and
   a scalar is NEVER emitted without the raw counts beside it.

2. **No cross-pair pooled rate** (adversarial finding): a single rate spanning Arivale (98%-covered
   reproduction) and BLSA (counts-only) is exactly the heterogeneous aggregate that collapses.
   ``pooled_rate`` refuses to compute one; per-pair reporting is the only supported path.

Pure/offline: integer counts in, floats/dataclasses out. No I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, NoReturn

Mode = Literal["reproduction", "recovery", "counts_only"]


class PooledRateError(RuntimeError):
    """Raised on any attempt to pool a rate across cohort pairs (R8 anti-pooling guard)."""


def refusal_sensitive_score(certified: int, comparable: int) -> float:
    """The frozen score: certified over the FIXED comparable-row denominator. 0.0 when none comparable."""
    if comparable <= 0:
        return 0.0
    return certified / comparable


@dataclass(frozen=True)
class PairScore:
    cohort: str
    asserted: int  # links asserted by Arm M / M+ID
    certified: int  # links with a KG-independent structural certificate
    refused: int  # links refused (no independent structure / refuted)
    comparable: int  # resolved rows — the fixed denominator (R9)
    mode: Mode  # reproduction (Arivale) | recovery (LLFS/Xu) | counts_only (BLSA)
    certifiable: bool  # False => structural certification is not available for this pair

    @property
    def score(self) -> float:
        return refusal_sensitive_score(self.certified, self.comparable)

    @property
    def metrics(self) -> dict[str, float]:
        """Both refusal-sensitive metrics (R8a). Reported together, never one alone."""
        return {
            "certified_over_comparable": refusal_sensitive_score(self.certified, self.comparable),
            "certified_over_asserted": (self.certified / self.asserted) if self.asserted else 0.0,
        }

    @property
    def raw_counts(self) -> dict[str, int]:
        """The surviving artifacts — always emitted beside any scalar."""
        return {
            "asserted": self.asserted,
            "certified": self.certified,
            "refused": self.refused,
            "comparable": self.comparable,
        }


def pooled_rate(scores: list[PairScore]) -> NoReturn:
    """Refuse to compute a cross-pair pooled rate — always raises (R8 anti-pooling guard).

    A pooled certified/comparable over heterogeneous pairs lets Arivale-easy reproductions inflate
    the numerator and BLSA counts-only rows pollute the denominator. There is no valid pooled rate;
    report per pair, tagged by mode.
    """
    pairs = ", ".join(f"{s.cohort}({s.mode})" for s in scores)
    raise PooledRateError(
        f"cross-pair pooled rate is prohibited over heterogeneous pairs [{pairs}]; "
        "report per-pair scores tagged by mode instead"
    )
