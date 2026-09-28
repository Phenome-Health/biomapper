"""Cross-dataset equivalence linking (offline, mocked CURIE sets, no network).

Mirrors the engine's ``studies/external_benchmarks/tests/test_cross_cohort_overlap.py``:
every case is built from literal CURIE sets, so the linker is exercised without a mapper,
a knowledge graph, or an HTTP call.
"""

from __future__ import annotations

import pytest

from biomapper.harmonize import (
    curie_set,
    curie_sets_from_results,
    harmonize,
    link_by_intersection,
)
from biomapper.models import MappingResult


def _result(name: str, chosen: str | None = None, equivalents=None, error: str | None = None):
    return MappingResult(
        query_name=name,
        chosen_kg_id=chosen,
        kg_equivalent_ids=equivalents or {},
        error=error,
    )


# ---------------------------------------------------------------------------
# link_by_intersection — the ported linking rule
# ---------------------------------------------------------------------------


def test_shared_curie_links_disjoint_does_not():
    a = {"glucose": curie_set("CHEBI:17234", {"KEGG": ["C00031"]})}
    b = {"glc": curie_set("KEGG:C00031", None), "urea": curie_set("CHEBI:16199", None)}
    res = link_by_intersection(a, b)
    assert res.n_links == 1
    assert res.links[0].a_key == "glucose"
    assert res.links[0].b_key == "glc"
    assert "KEGG:C00031" in res.links[0].shared


def test_prefix_synonym_links_but_a_different_id_space_does_not():
    a = {"m": curie_set("KEGG.COMPOUND:C00031", None)}
    assert link_by_intersection(a, {"n": curie_set("KEGG:C00031", None)}).n_links == 1
    assert link_by_intersection(a, {"n": curie_set("KEGG.GLYCAN:G00031", None)}).n_links == 0


def test_shared_inchikey_alone_never_links():
    # The linker is identifier-only. Two entities that agree ONLY on structure do not link;
    # admitting the structure hash would make a downstream structural check circular.
    key = "WQZGKKKJIJFFOK-GASJEMHNSA-N"
    a = {"glucose": curie_set(None, {"INCHIKEY": [key]})}
    b = {"glc": curie_set(None, {"INCHIKEY": [key]})}
    assert link_by_intersection(a, b).n_links == 0


def test_empty_curie_set_never_links_and_is_not_comparable():
    res = link_by_intersection(
        {"unresolved": curie_set("", None)}, {"glc": curie_set("KEGG:C00031", None)}
    )
    assert res.n_links == 0
    assert res.n_a_comparable == 0
    assert res.a_unresolved == ("unresolved",)


def test_comparable_denominator_counts_resolved_entities_only():
    a = {"x": curie_set("CHEBI:1", None), "y": curie_set("", None)}
    b = {"p": curie_set("CHEBI:1", None), "q": curie_set("", "")}
    res = link_by_intersection(a, b)
    assert (res.n_a_comparable, res.n_b_comparable) == (1, 1)
    assert res.a_unresolved == ("y",)
    assert res.b_unresolved == ("q",)


def test_multiple_shared_curies_yield_one_link_carrying_all_of_them():
    both = curie_set("CHEBI:17234", {"KEGG": ["C00031"]})
    res = link_by_intersection({"m": both}, {"n": both})
    assert res.n_links == 1
    assert res.links[0].shared == frozenset({"CHEBI:17234", "KEGG:C00031"})


def test_one_a_entity_links_to_multiple_b_entities():
    a = {"m": curie_set("CHEBI:1", None)}
    b = {"p": curie_set("CHEBI:1", None), "q": curie_set("CHEBI:1", None)}
    res = link_by_intersection(a, b)
    assert (res.n_links, res.n_a_linked, res.n_b_linked) == (2, 1, 2)


def test_no_links_when_both_sides_are_unresolved():
    res = link_by_intersection({"a": curie_set("", None)}, {"b": curie_set(None, None)})
    assert (res.n_links, res.n_a_linked, res.n_b_linked) == (0, 0, 0)


def test_links_are_ordered_deterministically():
    a = {"a2": curie_set("CHEBI:1", None), "a1": curie_set("CHEBI:1", None)}
    b = {"b2": curie_set("CHEBI:1", None), "b1": curie_set("CHEBI:1", None)}
    pairs = [(lk.a_key, lk.b_key) for lk in link_by_intersection(a, b).links]
    assert pairs == sorted(pairs)


# ---------------------------------------------------------------------------
# curie_sets_from_results — MappingResult -> linker input
# ---------------------------------------------------------------------------


def test_curie_sets_from_results_keys_on_query_name():
    sets = curie_sets_from_results([_result("Glucose", "CHEBI:17234", {"KEGG": ["C00031"]})])
    assert sets == {"Glucose": frozenset({"CHEBI:17234", "KEGG:C00031"})}


def test_curie_sets_from_results_rejects_duplicate_keys():
    # Collapsing two distinct entities onto one key would silently drop one of them.
    with pytest.raises(ValueError, match="duplicate"):
        curie_sets_from_results([_result("Glucose", "CHEBI:1"), _result("Glucose", "CHEBI:2")])


def test_curie_sets_from_results_accepts_a_custom_key():
    sets = curie_sets_from_results(
        [_result("Glucose", "CHEBI:1"), _result("Glucose", "CHEBI:2")],
        key=lambda r, i: f"{r.query_name}#{i}",
    )
    assert set(sets) == {"Glucose#0", "Glucose#1"}


# ---------------------------------------------------------------------------
# harmonize — the end-to-end client-side operation
# ---------------------------------------------------------------------------


def test_harmonize_links_the_same_node_across_two_cohorts():
    report = harmonize(
        [_result("Glucose", "CHEBI:17234", {"KEGG": ["C00031"]})],
        [_result("D-glucose", "KEGG.COMPOUND:C00031")],
        a_label="ukbb",
        b_label="arivale",
    )
    assert report.n_links == 1
    assert report.links[0].a_key == "Glucose"
    assert report.links[0].b_key == "D-glucose"
    assert (report.a_label, report.b_label) == ("ukbb", "arivale")


def test_harmonize_surfaces_unresolved_entities_as_refusal_candidates():
    report = harmonize(
        [_result("Glucose", "CHEBI:17234"), _result("X-12345 unknown")],
        [_result("D-glucose", "CHEBI:17234")],
    )
    assert report.n_links == 1
    assert report.a_unresolved == ("X-12345 unknown",)
    assert report.n_a_unresolved == 1


def test_harmonize_reports_errored_entities_separately_from_unresolved():
    # An error means "we do not know", which is not the same claim as "it did not resolve".
    report = harmonize(
        [_result("Glucose", "CHEBI:1"), _result("Boom", error="HTTP 500")],
        [_result("D-glucose", "CHEBI:1")],
    )
    assert report.a_errors == ("Boom",)
    assert report.a_unresolved == ()


def test_harmonize_accounts_for_every_input_entity():
    report = harmonize(
        [_result("ok", "CHEBI:1"), _result("unresolved"), _result("boom", error="nope")],
        [_result("ok2", "CHEBI:1")],
    )
    assert report.n_a_total == 3
    assert report.n_a_comparable + report.n_a_unresolved + report.n_a_errors == report.n_a_total


def test_harmonize_shared_inchikey_alone_does_not_harmonize():
    key = "WQZGKKKJIJFFOK-GASJEMHNSA-N"
    report = harmonize(
        [_result("Glucose", None, {"INCHIKEY": [key]})],
        [_result("D-glucose", None, {"INCHIKEY": [key]})],
    )
    assert report.n_links == 0
    assert report.a_unresolved == ("Glucose",)


def test_harmonize_summary_reports_counts_including_the_refusal_candidates():
    report = harmonize(
        [_result("Glucose", "CHEBI:1"), _result("unknown")],
        [_result("D-glucose", "CHEBI:1")],
        a_label="ukbb",
        b_label="arivale",
    )
    summary = report.summary()
    assert summary["n_links"] == 1
    assert summary["ukbb"]["total"] == 2
    assert summary["ukbb"]["comparable"] == 1
    assert summary["ukbb"]["unresolved"] == 1
    assert summary["ukbb"]["errors"] == 0
    assert summary["arivale"]["linked"] == 1


def test_harmonize_link_rate_is_over_the_comparable_denominator():
    # Rate is over entities that COULD link, so an unresolved row is never scored as a miss.
    report = harmonize(
        [_result("Glucose", "CHEBI:1"), _result("unknown")],
        [_result("D-glucose", "CHEBI:1")],
    )
    assert report.a_link_rate == 1.0


def test_harmonize_link_rate_is_none_when_nothing_is_comparable():
    report = harmonize([_result("unknown")], [_result("also unknown")])
    assert report.a_link_rate is None
    assert report.b_link_rate is None


def test_harmonize_is_offline(monkeypatch):
    # Belt and braces: fail loudly if anything in the harmonization path opens a socket.
    import socket

    def _no_network(*args, **kwargs):
        raise AssertionError("harmonize must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", _no_network)
    report = harmonize([_result("Glucose", "CHEBI:1")], [_result("D-glucose", "CHEBI:1")])
    assert report.n_links == 1


# ---------------------------------------------------------------------------
# Key and label integrity (Greptile review, PR #2)
# ---------------------------------------------------------------------------


def test_custom_key_sees_the_original_index_even_when_an_earlier_row_errored():
    # The key must be computed once against the ORIGINAL position. Keying the error list on
    # original indices and the surviving rows on compacted ones lets an errored row and a later
    # successful row collide, so a link would be attributed to the entity that failed.
    report = harmonize(
        [_result("Glucose", error="boom"), _result("Glucose", "CHEBI:1")],
        [_result("D-glucose", "CHEBI:1")],
        key=lambda r, i: f"{r.query_name}#{i}",
    )
    assert report.a_errors == ("Glucose#0",)
    assert report.links[0].a_key == "Glucose#1"


def test_duplicate_keys_are_rejected_even_when_one_of_the_rows_errored():
    # The uniqueness guard must span errored and resolved rows together; otherwise the same key
    # appears in both a_errors and a link, describing two different entities.
    with pytest.raises(ValueError, match="duplicate"):
        harmonize(
            [_result("Glucose", error="boom"), _result("Glucose", "CHEBI:1")],
            [_result("D-glucose", "CHEBI:1")],
        )


def test_harmonize_rejects_two_cohorts_sharing_a_label():
    # summary() keys on the labels; equal labels would silently drop the A-side counts.
    with pytest.raises(ValueError, match="distinct"):
        harmonize([_result("a", "CHEBI:1")], [_result("b", "CHEBI:1")], a_label="c", b_label="c")


@pytest.mark.parametrize(
    "reserved", ["n_links", "links_by_basis", "n_name_only_links", "n_name_match_withheld"]
)
def test_harmonize_rejects_a_label_that_would_overwrite_a_summary_field(reserved):
    with pytest.raises(ValueError, match="reserved"):
        harmonize([_result("a", "CHEBI:1")], [_result("b", "CHEBI:1")], a_label=reserved)


def test_summary_keeps_both_cohorts_and_the_link_count_separate():
    summary = harmonize(
        [_result("a", "CHEBI:1")], [_result("b", "CHEBI:1")], a_label="x", b_label="y"
    ).summary()
    assert set(summary) == {
        "n_links",
        "links_by_basis",
        "n_name_only_links",
        "n_name_match_withheld",
        "x",
        "y",
    }


def test_harmonization_result_defends_the_label_invariant_when_built_directly():
    from biomapper.harmonize import HarmonizationResult, OverlapResult

    empty = OverlapResult((), 0, 0, 0, 0, 0, (), ())
    with pytest.raises(ValueError, match="distinct"):
        HarmonizationResult(empty, "same", "same", (), (), 0, 0)
