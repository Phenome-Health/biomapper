"""Capability/regression gate for the LMSD lipid arm (post-Goslin).

Once Goslin (parse) + a lipid id-binding lookup are in the resolution path, LMSD stops being an
ACCURACY test and becomes a CAPABILITY/REGRESSION check: shorthand resolvability should jump from
~5.6% toward ~100%, and the run asserts a FLOOR (the capability is wired and has not regressed). This
is deliberately NOT an accuracy number — the LMSD gold is LIPID MAPS and the binding may descend from
LIPID MAPS too, so a high LMSD resolvability certifies only that the grammar capability is present.
"""

from __future__ import annotations

from typing import Any


def capability_resolvability(result: dict[str, Any], regime: str = "shorthand") -> float | None:
    """Resolvability (coverage fraction) for a name-source regime, or ``None`` if the regime is
    absent or has zero observations.

    Reads ONLY the per-regime coverage (``by_name_source_regime[regime].coverage.fraction``). It does
    NOT fall back to blended coverage: the capability gate is specifically about the target regime
    (LMSD is ~90% lipid shorthand — the hard class Goslin targets), so a high NON-shorthand blended
    number must never stand in for an absent shorthand measurement. When the regime is missing (e.g.
    an LMSD release with no recognized ABBREVIATION field) or empty, the honest answer is "no
    observations" (``None``), which the gate treats as a failure — not a pass on blended coverage.
    """
    regime_entry = (result.get("by_name_source_regime") or {}).get(regime)
    if not regime_entry:
        return None
    coverage = regime_entry.get("coverage", {})
    if int(coverage.get("total", 0)) == 0:
        return None
    return float(coverage["fraction"])


def assert_capability_floor(result: dict[str, Any], floor: float, regime: str = "shorthand") -> None:
    """Raise ``ValueError`` if the regime resolvability is absent or below ``floor`` (the gate).

    Fails CLOSED both when the target regime produced no observations (the capability arm measured
    nothing in its hard class — a blended number is not a valid substitute) and when the measured
    regime resolvability is below the floor (the Goslin lipid capability has regressed).
    """
    value = capability_resolvability(result, regime=regime)
    if value is None:
        raise ValueError(
            f"LMSD capability regression gate: no '{regime}' observations — the capability arm "
            f"measured nothing in its target class (regime absent or empty). Refusing to satisfy the "
            f"shorthand floor on blended/absent coverage; the Goslin lipid capability is unwired."
        )
    if value < floor:
        raise ValueError(
            f"LMSD capability regression floor not met: {regime} resolvability {value:.3f} < "
            f"regression floor {floor:.3f}. The Goslin lipid capability is missing or has regressed."
        )
