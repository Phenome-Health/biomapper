"""Unit 2: the offline N-way grouping core (node-anchored groups, cross-node queue, R15)."""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import random
import time
from itertools import combinations
from pathlib import Path

import pytest

from biomapper.harmonize import harmonize
from biomapper.harmonize.nway import NO_NODE, NwayCohort, group_cohorts
from biomapper.models import LipidResolution, MappingResult
from tests.cohorts_helpers import mr
from tests.nway_helpers import metabolite_panels, protein_names, replay_result


def cohort(label: str, *results: MappingResult, keys: list[str] | None = None) -> NwayCohort:
    return NwayCohort(
        label=label,
        keys=tuple(keys or [f"{i}|{r.query_name}" for i, r in enumerate(results)]),
        results=tuple(results),
    )


def broad(r: MappingResult) -> MappingResult:
    return r.model_copy(update={"lipid_resolution": LipidResolution(mapping_relation="broad")})


def assert_reconciles(cohorts: list[NwayCohort], link_by_name: bool) -> None:
    """R15: same-node row pairs + cross-node link pairs == harmonize() links, for every pair."""
    result = group_cohorts(cohorts, link_by_name=link_by_name)
    by_label = {c.label: c for c in cohorts}
    for counts in result.pair_counts:
        a, b = by_label[counts.a], by_label[counts.b]
        ka, kb = list(a.keys), list(b.keys)
        ra = [r.model_copy() for r in a.results]
        rb = [r.model_copy() for r in b.results]
        pair = harmonize(
            ra, rb, a_label=a.label, b_label=b.label, link_by_name=link_by_name,
            key=lambda r, i, ra=ra, ka=ka, kb=kb: ka[i] if i < len(ra) and ra[i] is r else kb[i],
        )
        assert counts.harmonize_links == pair.n_links, counts
        assert counts.same_node_row_pairs + counts.cross_node_link_pairs == pair.n_links, counts
        assert counts.reconciles


# ---------------------------------------------------------------------------
# Reconciliation first (execution note): it guards the bucket and group bookkeeping.
# ---------------------------------------------------------------------------


def test_reconciles_on_a_synthetic_three_cohort_fixture_with_every_bucket() -> None:
    a = cohort("a",
               mr("glucose", "CHEBI:17234", {"HMDB": ["HMDB0000122"]}),
               mr("Lactate", "CHEBI:24996"),
               mr("fatty acid", "CHEBI:35366", {"LM": ["S1", "S2"]}),
               mr("structure only", "INCHIKEY:WQZGKKKJIJFFOK-GASJEMHNSA-N",
                  {"HMDB": ["HMDB0000122"]}),
               mr("nothing"),
               mr("broken", error="timeout"),
               mr("Co", "CHEBI:27638"))
    b = cohort("b",
               mr("Glucose", "CHEBI:4167", {"HMDB": ["HMDB0000122"]}),
               mr("lactate", "CHEBI:24996"),
               mr("lactate", "CHEBI:24996"),
               mr("palmitate", "LM:S1"),
               mr("CO", "CHEBI:17245"))
    c = cohort("c",
               mr("glucose", "CHEBI:17234"),
               mr("stearate", "LM:S2"),
               mr("nothing"))
    for link_by_name in (True, False):
        assert_reconciles([a, b, c], link_by_name)


def test_reconciles_on_the_ukbb_arivale_metabolite_replay_with_name_linking() -> None:
    panels = metabolite_panels()
    cohorts = [
        NwayCohort(label, tuple(r["key"] for r in rows),
                   tuple(replay_result("metabolites", r["name"]) for r in rows))
        for label, rows in panels.items()
    ]
    assert_reconciles(cohorts, link_by_name=True)
    (counts,) = group_cohorts(cohorts, link_by_name=True).pair_counts
    # Not vacuous: 28 links at kg 2.3.0, all on shared nodes (this panel pair has no cross-node
    # link; the synthetic and protein fixtures cover that half of the identity).
    assert (counts.harmonize_links, counts.same_node_row_pairs) == (28, 28)


def test_reconciles_on_the_protein_replay_split_into_three_cohorts() -> None:
    names = protein_names()
    rng = random.Random(7)
    parts: dict[str, list[str]] = {"p1": [], "p2": [], "p3": []}
    for n in names:
        for label in rng.sample(list(parts), rng.randint(1, 3)):
            parts[label].append(n)
    cohorts = [
        NwayCohort(label, tuple(f"{i}|{n}" for i, n in enumerate(ns)),
                   tuple(replay_result("proteins", n) for n in ns))
        for label, ns in parts.items()
    ]
    assert_reconciles(cohorts, link_by_name=False)


def test_reconciles_on_the_labs_replay() -> None:
    fixture = Path(__file__).parent / "fixtures" / "cohorts_labs_cm"
    with gzip.open(fixture / "replay.json.gz", "rt") as fh:
        table = json.load(fh)

    def lookup(name: str) -> MappingResult:
        payload = json.dumps({"t": "biolink:ClinicalMeasurement", "n": name, "i": {},
                              "m": "missing"}, sort_keys=True)
        return MappingResult.model_validate(table[hashlib.sha256(payload.encode()).hexdigest()])

    cohorts = []
    for label, path in (("ukbb", "ukbb_labs.tsv"), ("arivale", "arivale_labs.tsv")):
        with (fixture / path).open(encoding="utf-8-sig") as fh:
            lines = [line for line in fh if not line.startswith("#")]
        rows = [r for r in csv.DictReader(lines, delimiter="\t") if (r.get("name") or "").strip()]
        cohorts.append(NwayCohort(label, tuple(r["key"] for r in rows),
                                  tuple(lookup(r["name"].strip()) for r in rows)))
    result = group_cohorts(cohorts, link_by_name=False)
    assert result.pair_counts[0].harmonize_links == 37  # the milestone names-only labs number
    assert_reconciles(cohorts, link_by_name=False)


# ---------------------------------------------------------------------------
# Groups and flags
# ---------------------------------------------------------------------------


def test_three_cohorts_on_one_node_form_one_clean_group() -> None:
    cohorts = [cohort(x, mr("glucose", "CHEBI:17234")) for x in ("a", "b", "c")]
    result = group_cohorts(cohorts, link_by_name=True)
    assert len(result.groups) == 1
    group = result.groups[0]
    assert group.node == "CHEBI:17234"
    assert group.cohorts == ("a", "b", "c")
    assert group.clean and not group.multiplicity and not group.broad
    assert {(p.a, p.b): p.overlap_groups for p in result.pair_counts} == {
        ("a", "b"): 1, ("a", "c"): 1, ("b", "c"): 1}
    assert result.cross_node == ()


def test_two_rows_from_one_cohort_on_one_node_flag_multiplicity() -> None:
    a = cohort("a", mr("lactate", "CHEBI:24996"), mr("L-lactate", "CHEBI:24996"))
    b = cohort("b", mr("lactate", "CHEBI:24996"))
    result = group_cohorts([a, b], link_by_name=True)
    (group,) = result.groups
    assert group.multiplicity and not group.clean
    assert len(group.members) == 3
    assert result.pair_counts[0].same_node_row_pairs == 2
    assert result.pair_counts[0].overlap_groups == 1


def test_a_broad_class_node_is_flagged_and_not_clean() -> None:
    cohorts = [cohort(x, broad(mr(f"PC(16:0/18:{i})", "LM:PC"))) for i, x in enumerate("abc")]
    (group,) = group_cohorts(cohorts, link_by_name=True).groups
    assert group.broad and not group.clean and not group.multiplicity


def test_one_broad_member_is_enough_to_flag_the_group() -> None:
    a = cohort("a", broad(mr("PC 34:1", "LM:PC")))
    b = cohort("b", mr("PC(16:0/18:1)", "LM:PC"))
    (group,) = group_cohorts([a, b], link_by_name=True).groups
    assert group.broad


def test_a_class_node_linked_to_five_species_does_not_chain_into_one_group() -> None:
    species = [f"S{i}" for i in range(5)]
    a = cohort("a", mr("fatty acids", "CHEBI:35366", {"LM": species}))
    b = cohort("b", *[mr(f"species {s}", f"LM:{s}") for s in species])
    result = group_cohorts([a, b], link_by_name=True)
    assert len(result.groups) == 6
    assert all(len(g.cohorts) == 1 for g in result.groups)
    assert len(result.cross_node) == 5
    assert {frozenset((e.node_a, e.node_b)) for e in result.cross_node} == {
        frozenset(("CHEBI:35366", f"LM:{s}")) for s in species}
    assert all(e.n_row_pairs == 1 and e.bases == frozenset({"identifier"})
               for e in result.cross_node)
    assert len(result.drilldown or ()) == 5
    counts = result.pair_counts[0]
    assert (counts.same_node_row_pairs, counts.cross_node_link_pairs) == (0, 5)


def test_cross_node_entries_aggregate_by_unordered_node_pair_across_cohort_pairs() -> None:
    a = cohort("a", mr("x", "N:2", {"HMDB": ["HMDB1"]}))
    b = cohort("b", mr("y", "N:1", {"HMDB": ["HMDB1"]}))
    c = cohort("c", mr("z", "N:2", {"HMDB": ["HMDB1"]}))
    result = group_cohorts([a, b, c], link_by_name=False)
    (entry,) = result.cross_node
    assert (entry.node_a, entry.node_b) == ("N:1", "N:2")
    assert entry.cohorts == ("a", "b", "c")
    assert entry.n_row_pairs == 2


def test_a_structure_namespace_chosen_node_goes_to_the_no_node_bucket() -> None:
    a = cohort("a", mr("glucose?", "INCHIKEY:WQZGKKKJIJFFOK-GASJEMHNSA-N",
                       {"HMDB": ["HMDB0000122"]}))
    b = cohort("b", mr("glucose", "CHEBI:17234", {"HMDB": ["HMDB0000122"]}))
    result = group_cohorts([a, b], link_by_name=True)
    assert [g.node for g in result.groups] == ["CHEBI:17234"]
    assert result.bucket_keys("a", "no_node") == ("0|glucose?",)
    (entry,) = result.cross_node
    assert {entry.node_a, entry.node_b} == {NO_NODE, "CHEBI:17234"}
    (pair,) = result.drilldown or ()
    assert pair.a_node == NO_NODE and pair.a_key == "0|glucose?"


def test_unresolved_and_errored_rows_are_listed_not_grouped() -> None:
    a = cohort("a", mr("nothing"), mr("broken", error="HTTP 500"), mr("urea", "CHEBI:16199"))
    result = group_cohorts([a, cohort("b", mr("urea", "CHEBI:16199"))], link_by_name=True)
    assert result.bucket_keys("a", "unresolved") == ("0|nothing",)
    assert result.bucket_keys("a", "errored") == ("1|broken",)
    assert result.bucket_keys("a", "grouped") == ("2|urea",)
    assert result.counts_by_cohort()["a"] == {
        "total": 3, "grouped": 1, "no_node": 0, "unresolved": 1, "errored": 1}


def test_short_casefold_names_are_withheld_not_cross_node_links() -> None:
    a = cohort("a", mr("Co", "CHEBI:27638"))
    b = cohort("b", mr("CO", "CHEBI:17245"))
    result = group_cohorts([a, b], link_by_name=True)
    assert result.cross_node == ()
    assert [(w.a_cohort, w.a_key, w.b_cohort, w.b_key) for w in result.withheld] == [
        ("a", "0|Co", "b", "0|CO")]


def test_name_links_cross_nodes_only_when_name_linking_is_on() -> None:
    a = cohort("a", mr("Glucose", "LOINC:2345-7"))
    b = cohort("b", mr("Glucose", "LOINC:2339-0"))
    assert len(group_cohorts([a, b], link_by_name=True).cross_node) == 1
    assert group_cohorts([a, b], link_by_name=False).cross_node == ()


def test_the_same_default_key_in_three_cohorts_stays_distinct() -> None:
    cohorts = [cohort(x, mr("glucose", "CHEBI:17234"), keys=["0|glucose"]) for x in "abc"]
    (group,) = group_cohorts(cohorts, link_by_name=True).groups
    assert [(m.cohort, m.key) for m in group.members] == [
        ("a", "0|glucose"), ("b", "0|glucose"), ("c", "0|glucose")]


def test_duplicate_keys_within_one_cohort_raise_before_grouping() -> None:
    a = cohort("a", mr("x", "N:1"), mr("y", "N:2"), keys=["k", "k"])
    with pytest.raises(ValueError, match="duplicate key 'k' in cohort 'a'"):
        group_cohorts([a, cohort("b", mr("x", "N:1"))], link_by_name=False)


def test_equal_labels_and_fewer_than_two_cohorts_raise() -> None:
    with pytest.raises(ValueError, match="distinct"):
        group_cohorts([cohort("a", mr("x")), cohort("a", mr("y"))], link_by_name=False)
    with pytest.raises(ValueError, match="at least two"):
        group_cohorts([cohort("a", mr("x"))], link_by_name=False)


def test_keys_and_results_must_have_equal_lengths() -> None:
    with pytest.raises(ValueError, match="2 keys for 1 results"):
        group_cohorts([NwayCohort("a", ("k1", "k2"), (mr("x"),)), cohort("b", mr("x"))],
                      link_by_name=False)


def test_drilldown_can_be_streamed_instead_of_held() -> None:
    species = [f"S{i}" for i in range(3)]
    a = cohort("a", mr("fatty acids", "CHEBI:35366", {"LM": species}))
    b = cohort("b", *[mr(f"species {s}", f"LM:{s}") for s in species])
    sink: list = []
    result = group_cohorts([a, b], link_by_name=True, drilldown_sink=sink.append)
    assert result.drilldown is None
    assert len(sink) == 3


def test_twenty_cohorts_of_a_thousand_rows_link_offline_within_budget() -> None:
    rng = random.Random(11)
    nodes = [f"CHEBI:{n}" for n in range(1500)]
    cohorts = []
    for c in range(20):
        picks = rng.sample(nodes, 1000)
        cohorts.append(NwayCohort(
            f"c{c:02d}",
            tuple(f"{i}|m{n}" for i, n in enumerate(picks)),
            tuple(mr(f"metabolite {n.split(':')[1]}", n,
                     {"HMDB": [f"HMDB{int(n.split(':')[1]) // 2:07d}"]}) for n in picks),
        ))
    sink_count = 0

    def sink(_pair: object) -> None:
        nonlocal sink_count
        sink_count += 1

    start = time.perf_counter()
    result = group_cohorts(cohorts, link_by_name=True, drilldown_sink=sink)
    elapsed = time.perf_counter() - start
    assert len(result.pair_counts) == 190
    assert all(p.reconciles for p in result.pair_counts)
    assert sink_count == sum(p.cross_node_link_pairs for p in result.pair_counts) > 0
    # First timing on the dev box was ~8.4 s; the budget leaves headroom for slower CI runners.
    assert elapsed < 60, f"20 x 1000 took {elapsed:.1f}s"


def test_pair_counts_cover_every_unordered_pair_in_input_order() -> None:
    cohorts = [cohort(x, mr("glucose", "CHEBI:17234")) for x in ("z", "a", "m")]
    result = group_cohorts(cohorts, link_by_name=False)
    assert [(p.a, p.b) for p in result.pair_counts] == list(combinations(("z", "a", "m"), 2))
