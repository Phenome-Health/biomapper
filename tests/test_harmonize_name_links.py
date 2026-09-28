"""Name-match linking in harmonize() (offline; literal mapping results, no network).

Entities whose names match link even when resolution put them on different nodes, and every link
records its basis so a report can keep identifier evidence apart from a name match.
"""

from __future__ import annotations

import functools

import pytest

from biomapper.harmonize import NAME_CASEFOLD_MIN_LENGTH_EXCLUSIVE, normalize_name
from biomapper.harmonize import harmonize as _harmonize
from biomapper.models import MappingResult

# Name linking is opt-in; every case here exercises it unless it says otherwise.
harmonize = functools.partial(_harmonize, link_by_name=True)


def _r(name: str, chosen: str | None, equivalents=None, error: str | None = None) -> MappingResult:
    return MappingResult(
        query_name=name, chosen_kg_id=chosen, kg_equivalent_ids=equivalents or {}, error=error
    )


def _basis(report, a, b):
    return next(lk for lk in report.links if (lk.a_key, lk.b_key) == (a, b))


def test_case_split_pair_links_by_casefold():
    # The real defect: the two spellings resolved to different nodes with no shared identifier.
    report = harmonize(
        [_r("acetylcarnitine (c2)", "HMDB:HMDB0000201")],
        [_r("acetylcarnitine (C2)", "RM:0154009")],
    )
    link = _basis(report, "acetylcarnitine (c2)", "acetylcarnitine (C2)")
    assert link.basis == "name_casefold"
    assert link.bases == frozenset({"name_casefold"})
    assert link.shared == frozenset()
    assert link.name_only


def test_short_casefold_match_is_withheld_not_linked():
    # Co (cobalt) vs CO (carbon monoxide): a case-only match on a short name is not evidence.
    report = harmonize([_r("Co", "CHEBI:27638")], [_r("CO", "CHEBI:17245")])
    assert report.n_links == 0
    assert report.name_match_withheld == (("Co", "CO"),)
    assert report.summary()["n_name_match_withheld"] == 1


def test_threshold_is_inclusive_of_its_value():
    four = "a" * NAME_CASEFOLD_MIN_LENGTH_EXCLUSIVE
    five = "a" * (NAME_CASEFOLD_MIN_LENGTH_EXCLUSIVE + 1)
    withheld = harmonize([_r(four, "X:1")], [_r(four.upper(), "X:2")])
    linked = harmonize([_r(five, "X:1")], [_r(five.upper(), "X:2")])
    assert withheld.n_links == 0 and withheld.name_match_withheld
    assert linked.n_links == 1 and linked.links[0].basis == "name_casefold"


def test_short_exact_match_still_links():
    report = harmonize([_r("CO", "CHEBI:17245")], [_r("CO", "CHEBI:99999")])
    assert _basis(report, "CO", "CO").basis == "name_exact"
    assert report.name_match_withheld == ()


def test_exact_name_on_different_nodes_links_as_name_exact():
    report = harmonize([_r("12,13-dihome", "RM:0138124")], [_r("12,13-dihome", "RM:0224117")])
    assert _basis(report, "12,13-dihome", "12,13-dihome").basis == "name_exact"


def test_whitespace_is_normalized_but_punctuation_is_not():
    assert normalize_name("  glucose   6-phosphate ") == "glucose 6-phosphate"
    report = harmonize(
        [_r("glucose  6-phosphate", "X:1"), _r("glucose-6-phosphate", "X:3")],
        [_r("glucose 6-phosphate", "X:2")],
    )
    assert [(lk.a_key, lk.basis) for lk in report.links] == [("glucose  6-phosphate", "name_exact")]


def test_strongest_basis_wins_and_all_bases_are_kept():
    report = harmonize([_r("Glucose", "CHEBI:17234")], [_r("glucose", "CHEBI:17234")])
    link = _basis(report, "Glucose", "glucose")
    assert link.basis == "node"
    assert link.bases == frozenset({"node", "identifier", "name_casefold"})
    assert not link.name_only


def test_identifier_link_on_different_nodes_is_basis_identifier():
    report = harmonize(
        [_r("isovalerylcarnitine (c5)", "HMDB:HMDB0000688", {"KEGG": ["C20826"]})],
        [_r("isovaleryl carnitine", "RM:0154016", {"KEGG": ["C20826"]})],
    )
    link = report.links[0]
    assert link.basis == "identifier"
    assert link.shared == frozenset({"KEGG:C20826"})


def test_summary_counts_links_by_basis():
    report = harmonize(
        [
            _r("glucose", "CHEBI:17234"),
            _r("isovalerylcarnitine", "HMDB:HMDB0000688", {"KEGG": ["C20826"]}),
            _r("12,13-dihome", "RM:0138124"),
            _r("acetylcarnitine (c2)", "HMDB:HMDB0000201"),
            _r("Co", "CHEBI:27638"),
        ],
        [
            _r("glucose", "CHEBI:17234"),
            _r("isovaleryl carnitine", "RM:0154016", {"KEGG": ["C20826"]}),
            _r("12,13-dihome", "RM:0224117"),
            _r("acetylcarnitine (C2)", "RM:0154009"),
            _r("CO", "CHEBI:17245"),
        ],
    )
    summary = report.summary()
    assert summary["links_by_basis"] == {
        "node": 1,
        "identifier": 1,
        "name_exact": 1,
        "name_casefold": 1,
    }
    assert summary["n_links"] == 4
    assert summary["n_name_only_links"] == 2
    assert summary["n_name_match_withheld"] == 1


def test_unresolved_and_errored_entities_never_link_by_name():
    report = harmonize(
        [_r("mystery", None), _r("failed", None, error="boom")],
        [_r("mystery", None), _r("failed", "X:1")],
    )
    assert report.n_links == 0
    assert report.a_unresolved == ("mystery",)
    assert report.a_errors == ("failed",)


def test_name_matching_compares_query_name_not_a_custom_key():
    # Real runs key rows as "row_id|name"; matching the key would never fire.
    report = harmonize(
        [_r("Glucose", "X:1")],
        [_r("glucose", "X:2")],
        key=lambda r, i: f"{i + 100}|{r.query_name}",
    )
    link = report.links[0]
    assert (link.a_key, link.b_key) == ("100|Glucose", "100|glucose")
    assert link.basis == "name_casefold"


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ([_r("acetylcarnitine (c2)", "N:1"), _r("Co", "N:2"), _r("glucose", "CHEBI:17234")],
         [_r("acetylcarnitine (C2)", "N:3"), _r("CO", "N:4"), _r("glucose", "CHEBI:17234")]),
    ],
)
def test_link_by_name_false_reproduces_identifier_only_linking(a, b):
    from biomapper.harmonize import curie_sets_from_results, link_by_intersection

    report = _harmonize(a, b, link_by_name=False)
    baseline = link_by_intersection(curie_sets_from_results(a), curie_sets_from_results(b))
    assert [(lk.a_key, lk.b_key, lk.shared) for lk in report.links] == [
        (lk.a_key, lk.b_key, lk.shared) for lk in baseline.links
    ]
    assert (report.n_links, report.n_a_linked, report.n_b_linked) == (
        baseline.n_links,
        baseline.n_a_linked,
        baseline.n_b_linked,
    )
    assert report.name_match_withheld == ()
    assert report.n_name_only_links == 0


def test_name_linking_is_off_by_default():
    report = _harmonize(
        [_r("acetylcarnitine (c2)", "HMDB:HMDB0000201")],
        [_r("acetylcarnitine (C2)", "RM:0154009")],
    )
    assert report.n_links == 0
    assert report.summary()["links_by_basis"]["name_casefold"] == 0
