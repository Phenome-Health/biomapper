"""Per-link partner counts and the ambiguous flag in harmonize() (offline; literal results).

A link is ambiguous when either side links to more than one entity. The real case: distinct
sphingomyelins in one cohort resolved to one entry, so each linked to every sphingomyelin in the
other cohort.
"""

from __future__ import annotations

import pytest

from biomapper.harmonize import harmonize, link_by_intersection, with_partner_counts
from biomapper.harmonize.linking import Link
from biomapper.models import MappingResult


def _r(name: str, chosen: str | None) -> MappingResult:
    return MappingResult(query_name=name, chosen_kg_id=chosen, kg_equivalent_ids={})


def _link(report, a, b):
    return next(lk for lk in report.links if (lk.a_key, lk.b_key) == (a, b))


def test_one_to_one_link_is_not_ambiguous():
    report = harmonize([_r("cotinine", "CHEBI:68641")], [_r("cotinine", "CHEBI:68641")])
    link = _link(report, "cotinine", "cotinine")
    assert (link.a_partners, link.b_partners) == (1, 1)
    assert not link.ambiguous
    assert report.one_to_one_links == (link,)
    assert report.ambiguous_links == ()


def test_collapse_onto_one_entry_flags_every_link():
    # Two distinct NECS sphingomyelins and two Xu sphingomyelins all resolved to one entry.
    entry = "HMDB:HMDB0013468"
    report = harmonize(
        [
            _r("palmitoyl sphingomyelin (d18:1/16:0)", entry),
            _r("stearoyl sphingomyelin (d18:1/18:0)", entry),
        ],
        [
            _r("palmitoyl sphingomyelin (d18:1/16:0)", entry),
            _r("behenoyl sphingomyelin (d18:1/22:0)*", entry),
        ],
    )
    assert report.n_links == 4
    assert all(lk.ambiguous for lk in report.links)
    assert all((lk.a_partners, lk.b_partners) == (2, 2) for lk in report.links)
    assert report.one_to_one_links == ()
    assert len(report.ambiguous_links) == 4


def test_one_sided_fan_out_flags_only_the_shared_side():
    # Xu fumarate links to NECS fumarate and maleate (one entry); a separate clean pair stays clean.
    report = harmonize(
        [_r("fumarate", "RM:1"), _r("maleate", "RM:1"), _r("cotinine", "CHEBI:68641")],
        [_r("fumarate", "RM:1"), _r("cotinine", "CHEBI:68641")],
    )
    fum = _link(report, "fumarate", "fumarate")
    mal = _link(report, "maleate", "fumarate")
    cot = _link(report, "cotinine", "cotinine")
    assert (fum.a_partners, fum.b_partners) == (1, 2) and fum.ambiguous
    assert (mal.a_partners, mal.b_partners) == (1, 2) and mal.ambiguous
    assert not cot.ambiguous
    assert report.one_to_one_links == (cot,)


def test_name_links_count_toward_partners():
    # A name-match link adds a partner just like an identifier link does.
    report = harmonize(
        [_r("acetylcarnitine (c2)", "HMDB:HMDB0000201")],
        [_r("acetylcarnitine (C2)", "RM:0154009"), _r("acetylcarnitine", "HMDB:HMDB0000201")],
        link_by_name=True,
    )
    assert report.n_links == 2
    assert all(lk.a_partners == 2 for lk in report.links)
    assert all(lk.ambiguous for lk in report.links)


def test_summary_reports_one_to_one_and_ambiguous_counts():
    report = harmonize(
        [_r("fumarate", "RM:1"), _r("maleate", "RM:1"), _r("cotinine", "CHEBI:68641")],
        [_r("fumarate", "RM:1"), _r("cotinine", "CHEBI:68641")],
        a_label="necs",
        b_label="xu",
    )
    s = report.summary()
    assert s["n_links"] == 3
    assert s["n_one_to_one_links"] == 1
    assert s["n_ambiguous_links"] == 2
    assert s["n_one_to_one_links"] + s["n_ambiguous_links"] == s["n_links"]


@pytest.mark.parametrize("label", ["n_one_to_one_links", "n_ambiguous_links"])
def test_new_summary_fields_are_reserved_labels(label):
    with pytest.raises(ValueError, match="reserved"):
        harmonize([_r("x", "RM:1")], [_r("x", "RM:1")], a_label=label, b_label="b")


def test_link_by_intersection_sets_partner_counts():
    overlap = link_by_intersection(
        {"a1": frozenset({"CHEBI:1"}), "a2": frozenset({"CHEBI:1"})},
        {"b1": frozenset({"CHEBI:1"})},
    )
    assert [(lk.a_partners, lk.b_partners) for lk in overlap.links] == [(1, 2), (1, 2)]


def test_with_partner_counts_recomputes_from_the_set():
    links = (Link("a", "b", frozenset()), Link("a", "c", frozenset()))
    counted = with_partner_counts(links)
    assert [(lk.a_partners, lk.b_partners) for lk in counted] == [(2, 1), (2, 1)]
    assert with_partner_counts(counted[:1])[0].ambiguous is False
