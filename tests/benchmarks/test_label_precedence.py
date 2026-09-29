"""Label precedence: the weaker claim wins, because overstating is the error that misleads."""

from __future__ import annotations

import pytest

from biomapper.benchmarks.suite import weakest_claim


@pytest.mark.parametrize(
    "declared,circularity,expected,why",
    [
        # RefMet: role defaults to "accuracy", but refmet is an ingested KRAKEN source.
        ("accuracy", "coverage", "coverage", "a config default must not outrank a live verdict"),
        # MetaboliteAnnotator: coverage BY CONSTRUCTION (name-hit rate). Circularity cannot see
        # that, because the gold source is not in the graph, so it says accuracy_candidate.
        (
            "coverage",
            "accuracy_candidate",
            "coverage",
            "an arm that is coverage by construction stays coverage",
        ),
        ("partly_circular", "accuracy_candidate", "partly_circular", "partly-circular is weaker"),
        ("capability_regression", "coverage", "capability_regression", "both weakest, stable"),
        ("accuracy", "accuracy_candidate", "accuracy_candidate", "candidate is the weaker claim"),
        ("", "coverage", "coverage", "one source only"),
        ("accuracy", "", "accuracy", "one source only"),
        ("", "", "", "no source"),
        ("something_new", "accuracy", "something_new", "an unknown label is not strong evidence"),
    ],
)
def test_weakest_claim(declared: str, circularity: str, expected: str, why: str) -> None:
    assert weakest_claim(declared, circularity) == expected, why


def test_never_upgrades_a_coverage_arm() -> None:
    """The property that matters: coverage in, coverage out, whatever the other source says."""
    for other in ("accuracy", "accuracy_candidate", "partly_circular", "coverage", ""):
        assert weakest_claim("coverage", other) == "coverage"


# ------------------------------------------------------------------------------------------------
# Greptile round 2 on PR #14: the label's REASON must travel with the row.
# ------------------------------------------------------------------------------------------------

from biomapper.benchmarks.suite import label_basis  # noqa: E402


def test_basis_names_circularity_when_it_decided() -> None:
    """RefMet: role defaults to accuracy, the per-run verdict overrides it to coverage."""
    assert label_basis("accuracy", "coverage") == "per-run circularity verdict"


def test_basis_names_the_declared_role_when_it_decided() -> None:
    """MetaboliteAnnotator: coverage by construction, which circularity cannot see."""
    assert label_basis("coverage", "accuracy_candidate") == "arm's declared role"


def test_basis_never_invents_an_underlying_reason() -> None:
    """It names the deciding SOURCE only.

    An earlier version also asserted why, and was wrong twice: it claimed "gold source is ingested"
    for Hajjar, whose verdict is accuracy_candidate because NO gold source is in the build, and
    "coverage by construction" for MetaBench, which is partly_circular on xref provenance. The
    per-arm reason already lives in the manifest's circularity register, derived from the build.
    """
    for declared, circ in (
        ("accuracy", "accuracy_candidate"),
        ("accuracy", "coverage"),
        ("coverage", "accuracy_candidate"),
        ("partly_circular", "accuracy_candidate"),
    ):
        basis = label_basis(declared, circ)
        assert "ingested" not in basis
        assert "construction" not in basis


def test_basis_is_stated_when_the_sources_agree() -> None:
    assert label_basis("coverage", "coverage") == "both sources agree"


def test_basis_handles_a_single_source() -> None:
    assert label_basis("", "coverage") == "per-run circularity verdict"
    assert label_basis("accuracy", "") == "arm's declared role"


def test_basis_is_empty_when_there_is_no_label() -> None:
    assert label_basis("", "") == ""


def test_basis_attributes_a_coverage_by_construction_arm_to_its_declared_role() -> None:
    """Whenever the declared role is what made it coverage, say so and blame nothing else."""
    for circ in ("accuracy", "accuracy_candidate", "partly_circular"):
        assert label_basis("coverage", circ) == "arm's declared role"
