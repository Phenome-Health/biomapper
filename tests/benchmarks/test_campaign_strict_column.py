"""A "Top-1 (strict)" column must never print the name-fallback figure.

Greptile round 2 on PR #14: the first fix preferred `comparable_core_strict_kg_only` but fell back
to `comparable_core` for older artifacts. That reintroduced the exact confusion the column heading
was being fixed for, silently: on Hajjar-100 the fallback figure is 95/100 where published strict
is 92/100, and a reader of the column cannot tell which they were handed.

Legacy artifacts do carry `per_row` with `needed_fallback`, so strict is RECOMPUTABLE rather than
unavailable. Where it genuinely cannot be established, the cell says so.
"""

from __future__ import annotations

from typing import Any

from biomapper.benchmarks.report.campaign import (
    STRICT_UNAVAILABLE,
    _metabolite_row,
    _strict_core,
)


def _legacy_result() -> dict[str, Any]:
    """An artifact from before `comparable_core_strict_kg_only` existed.

    Three scored rows, all correct, but one leaned on the name fallback. Strict is 2/3; the stored
    `comparable_core` says 3/3.
    """
    return {
        "comparable_core": {
            "metric": "top1_accuracy",
            "top1_accuracy": 1.0,
            "correct": 3,
            "scored_denominator": 3,
        },
        "coverage": {"n_predicted": 3, "total": 3},
        "fallback_bucket": {"count": 1},
        "per_row": [
            {"scored": True, "correct": True, "needed_fallback": False},
            {"scored": True, "correct": True, "needed_fallback": False},
            {"scored": True, "correct": True, "needed_fallback": True},
            {"scored": False, "correct": False, "needed_fallback": False},
        ],
    }


def test_legacy_artifact_recomputes_strict_rather_than_substituting() -> None:
    """The stored 3/3 must not be handed back. Strict is 2/3, recomputed from per_row."""
    core = _strict_core(_legacy_result())
    assert core is not None
    assert (core["correct"], core["scored_denominator"]) == (2, 3)
    assert core["metric"] == "top1_accuracy_strict_kg_only"
    assert core["is_published_strict"] is True
    assert core["recomputed_from_per_row"] is True


def test_recomputation_is_flagged_so_it_is_not_mistaken_for_a_stored_figure() -> None:
    """A derived number and a measured one must be distinguishable in the artifact."""
    stored = _strict_core(
        {
            "comparable_core_strict_kg_only": {
                "metric": "top1_accuracy_strict_kg_only",
                "top1_accuracy": 0.92,
                "correct": 92,
                "scored_denominator": 100,
            },
            "comparable_core": {"top1_accuracy": 0.95, "correct": 95, "scored_denominator": 100},
        }
    )
    assert stored is not None
    assert "recomputed_from_per_row" not in stored


def test_strict_is_none_when_it_cannot_be_established() -> None:
    """No strict field and no per_row: return None so the caller prints an explicit gap."""
    assert (
        _strict_core(
            {
                "comparable_core": {
                    "top1_accuracy": 0.95,
                    "correct": 95,
                    "scored_denominator": 100,
                }
            }
        )
        is None
    )


def test_no_scored_rows_is_not_a_zero() -> None:
    """An empty scored set has no accuracy; it must not recompute to 0/0 or 0%."""
    assert (
        _strict_core(
            {
                "comparable_core": {"top1_accuracy": None, "correct": 0, "scored_denominator": 0},
                "per_row": [{"scored": False, "correct": False, "needed_fallback": False}],
            }
        )
        is None
    )


def test_report_row_prints_the_recomputed_strict_not_the_fallback() -> None:
    """End to end: the rendered row carries 66.7% (2/3), never 100.0% (3/3)."""
    row = _metabolite_row({"key": "legacy-arm", "result": _legacy_result()})
    assert "66.7%" in row
    assert "100.0%" not in row


def test_report_row_states_the_gap_rather_than_guessing() -> None:
    """When strict cannot be established the cell says so, and is not blank and not a number."""
    result = {
        "comparable_core": {"top1_accuracy": 0.95, "correct": 95, "scored_denominator": 100},
        "coverage": {"n_predicted": 100, "total": 100},
        "fallback_bucket": {"count": 0},
    }
    row = _metabolite_row({"key": "ancient-arm", "result": result})
    assert STRICT_UNAVAILABLE in row
    assert "95.0%" not in row
