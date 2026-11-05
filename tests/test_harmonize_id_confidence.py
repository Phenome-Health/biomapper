"""Metabolon identification-confidence markers on harmonized links (offline; literal results).

Metabolon suffixes a compound name with "*" (identified with confidence, not confirmed with a
standard) or "**" (reasonably identified, no standard available); no marker means confirmed with an
authentic standard. A link whose two names carry different markers needs review even when it is
one-to-one. The real cases are NECS x Xu et al. 2022 links that share a Metabolon compound ID.
"""

from __future__ import annotations

import pytest

from biomapper.cohorts import harmonize_cohorts
from biomapper.harmonize import harmonize, id_confidence_marker
from biomapper.harmonize.linking import Link
from biomapper.models import MappingResult
from tests.cohorts_helpers import FakeMapper, by_name, mr

# The six NECS x Xu links beyond Monti et al.'s published pairs that share a Metabolon compound ID
# and differ only in the marker (cross-cohort run 1ee319e, 2026-11-05). NECS name first.
MARKER_ONLY_PAIRS = [
    ("1-palmitoyl-gpi (16:0)", "1-palmitoyl-GPI* (16:0)"),
    ("3-hydroxybutyroylglycine**", "3-hydroxybutyroylglycine"),
    ("decadienedioic acid (c10:2-dc)**", "decadienedioic acid (c10:2-dc)"),
    ("glyco-beta-muricholate**", "glyco-beta-muricholate"),
    (
        "hydroxypalmitoyl sphingomyelin (d18:1/16:0(oh))**",
        "hydroxypalmitoyl sphingomyelin (d18:1/16:0(oh))",
    ),
    ("octadecenedioate (c18:1-dc)", "octadecenedioate (c18:1-dc)*"),
]


def _r(name: str, chosen: str | None) -> MappingResult:
    return MappingResult(query_name=name, chosen_kg_id=chosen, kg_equivalent_ids={})


def _pairs(a_names: list[str], b_names: list[str]):
    """One distinct entry per pair index, so every link is one-to-one."""
    return (
        [_r(n, f"RM:{i}") for i, n in enumerate(a_names)],
        [_r(n, f"RM:{i}") for i, n in enumerate(b_names)],
    )


@pytest.mark.parametrize(
    "name, marker",
    [
        ("glyco-beta-muricholate", 0),
        ("glyco-beta-muricholate*", 1),
        ("glyco-beta-muricholate**", 2),
        ("1-palmitoyl-gpi* (16:0)", 1),
        ("1-palmitoyl-gpi** (16:0)", 2),
        ("hydroxypalmitoyl sphingomyelin (d18:1/16:0(oh))**", 2),
        ("octadecenedioate (c18:1-dc)*", 1),
        ("  glyco-beta-muricholate**  ", 2),
        # Xu et al. repeat the marker; the longest run wins, so this is one "*", not "**".
        ("1-arachidonoyl-GPC* (20:4)*", 1),
        # Not Metabolon markers: ignored.
        ("compound***", 0),
        ("*leading", 0),
        ("a * b", 0),
        ("a*b", 0),
        ("name **", 0),
        ("gpi*(16:0)", 0),
        ("", 0),
    ],
)
def test_id_confidence_marker(name: str, marker: int) -> None:
    assert id_confidence_marker(name) == marker


def test_marker_only_pairs_are_flagged_for_review() -> None:
    a, b = _pairs([p[0] for p in MARKER_ONLY_PAIRS], [p[1] for p in MARKER_ONLY_PAIRS])
    report = harmonize(a, b)
    assert report.n_links == len(MARKER_ONLY_PAIRS)
    for lk in report.links:
        assert not lk.ambiguous
        assert lk.id_confidence_differs
        assert lk.needs_review
    by_pair = {(lk.a_key, lk.b_key): lk for lk in report.links}
    muricholate = by_pair[("glyco-beta-muricholate**", "glyco-beta-muricholate")]
    assert (muricholate.a_id_confidence, muricholate.b_id_confidence) == (2, 0)
    gpi = by_pair[("1-palmitoyl-gpi (16:0)", "1-palmitoyl-GPI* (16:0)")]
    assert (gpi.a_id_confidence, gpi.b_id_confidence) == (0, 1)
    # ambiguous is unchanged; the new flag widens review, not one-to-one.
    assert len(report.one_to_one_links) == len(MARKER_ONLY_PAIRS)
    assert report.ambiguous_links == ()
    assert set(report.review_links) == set(report.links)


def test_identical_markers_are_not_flagged() -> None:
    names = ["glyco-beta-muricholate**", "octadecenedioate (c18:1-dc)*", "1-palmitoyl-gpi* (16:0)"]
    # Case differs on one pair; the marker is all that is compared.
    report = harmonize(*_pairs(names, [names[0], names[1], "1-palmitoyl-GPI* (16:0)"]))
    assert report.n_links == 3
    assert not any(lk.id_confidence_differs or lk.needs_review for lk in report.links)
    assert [(lk.a_id_confidence, lk.b_id_confidence) for lk in report.links] == [
        (1, 1), (2, 2), (1, 1),
    ]


def test_repeated_single_markers_match_one_trailing_marker() -> None:
    report = harmonize(*_pairs(["1-arachidonoyl-gpc (20:4n6)*"], ["1-arachidonoyl-GPC* (20:4)*"]))
    (lk,) = report.links
    assert (lk.a_id_confidence, lk.b_id_confidence) == (1, 1)
    assert not lk.needs_review


def test_panels_without_markers_are_unaffected() -> None:
    names = ["Glucose", "Urea", "creatinine"]
    report = harmonize(*_pairs(names, [n.upper() for n in names]))
    assert report.n_links == 3
    assert all((lk.a_id_confidence, lk.b_id_confidence) == (0, 0) for lk in report.links)
    assert report.review_links == ()
    s = report.summary()
    assert s["n_id_confidence_differs"] == 0 and s["n_needs_review"] == 0


def test_one_marked_side_against_unmarked_panel_is_flagged() -> None:
    # The rule applies whenever at least one side carries a marker.
    report = harmonize(*_pairs(["glucose*"], ["glucose"]))
    (lk,) = report.links
    assert lk.id_confidence_differs and lk.needs_review


def test_markers_come_from_query_name_not_a_custom_key() -> None:
    a, b = _pairs(["glyco-beta-muricholate**"], ["glyco-beta-muricholate"])
    report = harmonize(a, b, key=lambda r, i: f"row{i}")
    (lk,) = report.links
    assert (lk.a_key, lk.b_key) == ("row0", "row0")
    assert (lk.a_id_confidence, lk.b_id_confidence) == (2, 0)


def test_links_built_without_names_default_to_no_marker() -> None:
    lk = Link(a_key="x**", b_key="x", shared=frozenset())
    assert (lk.a_id_confidence, lk.b_id_confidence) == (0, 0)
    assert not lk.id_confidence_differs


def test_summary_counts_review_as_ambiguous_or_marker_differs() -> None:
    entry = "RM:shared"
    report = harmonize(
        [
            _r("glyco-beta-muricholate**", "RM:1"),  # one-to-one, marker differs
            _r("cotinine", "RM:2"),  # one-to-one, clean
            _r("fumarate*", entry),  # ambiguous AND marker differs
            _r("maleate", entry),  # ambiguous only
        ],
        [
            _r("glyco-beta-muricholate", "RM:1"),
            _r("cotinine", "RM:2"),
            _r("fumarate", entry),
        ],
    )
    s = report.summary()
    assert s["n_links"] == 4
    assert s["n_one_to_one_links"] == 2 and s["n_ambiguous_links"] == 2
    assert s["n_id_confidence_differs"] == 2
    assert s["n_needs_review"] == 3
    assert [(lk.a_key, lk.b_key) for lk in report.review_links] == [
        ("fumarate*", "fumarate"),
        ("glyco-beta-muricholate**", "glyco-beta-muricholate"),
        ("maleate", "fumarate"),
    ]


@pytest.mark.parametrize("label", ["n_id_confidence_differs", "n_needs_review"])
def test_new_summary_keys_are_reserved_labels(label: str) -> None:
    with pytest.raises(ValueError, match="reserved"):
        harmonize([_r("x", "RM:1")], [_r("x", "RM:1")], a_label=label)


def test_harmonize_cohorts_propagates_markers_to_summary_and_link_table(tmp_path) -> None:
    table = {
        "glyco-beta-muricholate**": mr("glyco-beta-muricholate**", "RM:1"),
        "glyco-beta-muricholate": mr("glyco-beta-muricholate", "RM:1"),
        "cotinine": mr("cotinine", "RM:2"),
    }
    report = harmonize_cohorts(
        [{"name": "glyco-beta-muricholate**"}, {"name": "cotinine"}],
        [{"name": "glyco-beta-muricholate"}, {"name": "cotinine"}],
        entity="metabolites",
        a_label="necs",
        b_label="xu",
        mapper=FakeMapper(by_name(table)),
        probe_pins=False,
        output_dir=tmp_path / "run",
    )
    arm = report.summary()["arms"]["names_only"]
    assert arm["n_links"] == 2
    assert arm["n_id_confidence_differs"] == 1 and arm["n_needs_review"] == 1
    rows = (tmp_path / "run" / "links_names_only.tsv").read_text().splitlines()
    header = rows[0].split("\t")
    assert header[-4:] == ["a_id_confidence", "b_id_confidence", "id_confidence_differs",
                           "needs_review"]
    # Cohort keys are "<row>|<name>"; the marker is read from the name, not the key.
    by_key = {
        r.split("\t")[0].split("|", 1)[1]: dict(zip(header, r.split("\t"), strict=True))
        for r in rows[1:]
    }
    muricholate = by_key["glyco-beta-muricholate**"]
    assert muricholate["one_to_one"] == "True"
    assert (muricholate["a_id_confidence"], muricholate["b_id_confidence"]) == ("2", "0")
    assert (muricholate["id_confidence_differs"], muricholate["needs_review"]) == ("True", "True")
    assert by_key["cotinine"]["needs_review"] == "False"
