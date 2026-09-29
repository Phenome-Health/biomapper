"""Cross-cohort (Monti/NECS) arm: panels, linker, Arm-B baseline, link certificate, driver guards.

Ported from the engine's ``studies/external_benchmarks/tests`` (``test_cohort_panel_adapter``,
``test_arm_b_baseline``, ``test_cross_cohort_overlap``, ``test_link_certificate``,
``test_link_certificate_disjointness``) at ``origin/dev`` commit
``1ffb571e54fe028ef0ae4e748fc2e7ec093ee603``, with the import paths rewritten and new coverage for
what the port changed: the corrected Monti published-overlap table, the driver's positional
alignment guard, and the agreement between the cohort linker and the generic
``biomapper.harmonize`` primitive.

Fully offline. No network, no mapper, no knowledge graph.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from biomapper.benchmarks.adapters.cohort_panel import (
    ARIVALE,
    BLSA,
    CohortPanel,
    CohortPanelConfig,
    load_cohort_panel,
)
from biomapper.benchmarks.cross_cohort import (
    BackendDriftError,
    PanelAlignmentError,
    arm_b_reconstruction_basis,
    assert_alignment,
    check_panel_provenance,
    client_repo_provenance,
    comparator_independence,
    cross_check_harmonize,
    curies_by_name,
    errored_names,
    prepare_panel,
    repair_errored_rows,
    resolve_panel,
    run_links,
    write_panel_provenance,
)
from biomapper.benchmarks.provenance import KgBuildInfo, RunProvenance
from biomapper.benchmarks.scorers.arm_b_baseline import (
    MONTI_PUBLISHED,
    MONTI_PUBLISHED_PROVENANCE,
    MONTI_PUBLISHED_SUPERSEDED,
    MONTI_S03_DERIVED,
    arm_b_overlap,
    name_match_overlap,
    refmet_join_overlap,
)
from biomapper.benchmarks.scorers.cross_cohort_overlap import (
    Link,
    curie_set,
    link_by_intersection,
)
from biomapper.benchmarks.scorers.independent_inchikey import ProvidedBlock
from biomapper.benchmarks.scorers.independent_link_certificate_overlap import (
    certify_links,
    certify_links_tagged,
)
from biomapper.benchmarks.scorers.link_certificate import certificate_key, certify_link

GLUCOSE = "WQZGKKKJIJFFOK-GASJEMHNSA-N"
GLUCOSE_STEREOISOMER = "WQZGKKKJIJFFOK-VFRWLCBQSA-N"  # same connectivity, different stereo layer
WRONG_MOLECULE = "ZZZZZZZZZZZZZZ-YYYYYYYYYY-N"  # different connectivity (block 1)
_IK = "FHQVHHIBKUMWTI-OTMQOFQ-N"


# ==================================================================================================
# Panels
# ==================================================================================================


def _arivale_like() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "BiochemicalName": ["glucose", "citrate", "X - 12345", "", "glucose"],
            "CAS_ID": ["50-99-7", "77-92-9", "", "", "50-99-7"],
            "KEGG_ID": ["C00031", "C00158", "", "", "C00031"],
            "HMDB_ID": ["HMDB0000122", "HMDB0000094", "", "", "HMDB0000122"],
            "PubChem_ID": ["5793", "311", "", "", "5793"],
        }
    )


def test_loads_names_and_ids_certifiable():
    panel = load_cohort_panel(_arivale_like(), ARIVALE)
    assert panel.names == ["glucose", "citrate"]
    assert set(panel.id_columns) == {"cas", "kegg", "hmdb", "pubchem"}
    assert panel.certifiable is True


def test_exclusions_counted_not_silent():
    panel = load_cohort_panel(_arivale_like(), ARIVALE)
    exclusions = panel.card["exclusions"]
    assert exclusions["blank_name"] == 1
    assert exclusions["unidentified_x"] == 1
    assert exclusions["duplicate_name"] == 1
    assert panel.card["n_raw"] == 5 and panel.card["n_rows"] == 2


def test_spreadsheet_cohort_picks_its_column_names_only():
    frame = pd.DataFrame(
        {
            "blsa": ["Alanine", "Arginine", "PC aa C34:2"],
            "llfs": ["1 Methyluric acid", "", ""],
            "necs": ["spermidine", "X - 12345", ""],
            "xuetal": ["palmitate", "", ""],
        }
    )
    panel = load_cohort_panel(frame, BLSA)
    assert panel.names == ["Alanine", "Arginine", "PC aa C34:2"]
    # BLSA is names only: its links are countable, never structurally certifiable.
    assert panel.certifiable is False
    assert panel.id_columns == ()


def test_missing_name_column_fails_loud():
    with pytest.raises(KeyError):
        load_cohort_panel(pd.DataFrame({"WrongHeader": ["a", "b"]}), ARIVALE)


def test_monti_size_gap_surfaced_not_reconciled():
    panel = load_cohort_panel(_arivale_like(), ARIVALE)
    assert panel.card["monti_panel_size"] == 626
    assert panel.card["monti_size_gap"] == 2 - 626


def test_sha_pinned_and_deterministic():
    config = CohortPanelConfig(key="t", name_column="name")
    first = load_cohort_panel(b"glucose\ncitrate\n", config, name_list=True)
    second = load_cohort_panel(b"glucose\ncitrate\n", config, name_list=True)
    assert first.card["source_sha256"] == second.card["source_sha256"]
    assert first.names == ["glucose", "citrate"]


def test_kegg_only_is_not_certifiable():
    config = CohortPanelConfig(key="k", name_column="name", id_columns={"kegg": "KEGG_ID"})
    panel = load_cohort_panel(pd.DataFrame({"name": ["glucose"], "KEGG_ID": ["C00031"]}), config)
    assert panel.certifiable is False


def test_name_list_parsing_strips_and_drops_blanks():
    config = CohortPanelConfig(key="nl", name_column="name")
    panel = load_cohort_panel(b"Alanine\n\n  Arginine  \nAlanine\n", config, name_list=True)
    assert panel.names == ["Alanine", "Arginine"]


# ==================================================================================================
# Linker
# ==================================================================================================


def test_shared_curie_links_disjoint_does_not():
    a = {"glucose": curie_set("CHEBI:17234", {"KEGG": ["C00031"]})}
    b = {"glc": curie_set("KEGG:C00031", None), "urea": curie_set("CHEBI:16199", None)}
    result = link_by_intersection(a, b)
    assert result.n_links == 1
    assert result.links[0].a_name == "glucose" and result.links[0].b_name == "glc"
    assert "KEGG:C00031" in result.links[0].shared


def test_canonicalization_positive_control():
    a = {"m": curie_set("KEGG.COMPOUND:C00031", None)}
    assert link_by_intersection(a, {"n": curie_set("KEGG:C00031", None)}).n_links == 1
    assert link_by_intersection(a, {"n": curie_set("KEGG.GLYCAN:G00031", None)}).n_links == 0


def test_empty_curie_set_never_links():
    result = link_by_intersection(
        {"unresolved": curie_set("", None)}, {"glc": curie_set("KEGG:C00031", None)}
    )
    assert result.n_links == 0
    assert result.n_a_comparable == 0


def test_comparable_denominator_counts_resolved_rows_only():
    result = link_by_intersection(
        {"x": curie_set("CHEBI:1", None), "y": curie_set("", None)},
        {"p": curie_set("CHEBI:1", None), "q": curie_set("", "")},
    )
    assert result.n_a_comparable == 1 and result.n_b_comparable == 1


def test_multiple_shared_curies_yield_one_link_with_all():
    payload = curie_set("CHEBI:17234", {"KEGG": ["C00031"]})
    result = link_by_intersection({"m": payload}, {"n": payload})
    assert result.n_links == 1
    assert result.links[0].shared == frozenset({"CHEBI:17234", "KEGG:C00031"})


def test_one_a_links_multiple_b():
    result = link_by_intersection(
        {"m": curie_set("CHEBI:1", None)},
        {"p": curie_set("CHEBI:1", None), "q": curie_set("CHEBI:1", None)},
    )
    assert result.n_links == 2 and result.n_a_linked == 1 and result.n_b_linked == 2


def test_curie_set_parses_dict_and_excludes_structural_namespaces():
    # INCHIKEY is a STRUCTURE hash. Letting it into the identifier-only linker would make the
    # downstream certificate circular and precision 100% by construction.
    as_dict = curie_set(
        "CHEBI:17234",
        {
            "KEGG": ["C00031"],
            "PUBCHEM.COMPOUND": ["5793", ""],
            "INCHIKEY": [GLUCOSE],
        },
    )
    assert as_dict == frozenset({"CHEBI:17234", "KEGG:C00031", "PUBCHEM:5793"})
    as_str = curie_set("CHEBI:17234", "{'KEGG': ['C00031'], 'PUBCHEM.COMPOUND': ['5793']}")
    assert as_str == frozenset({"CHEBI:17234", "KEGG:C00031", "PUBCHEM:5793"})


def test_no_links_when_both_sides_empty():
    result = link_by_intersection({"a": curie_set("", None)}, {"b": curie_set(None, None)})
    assert result.n_links == 0 and result.n_a_linked == 0 and result.n_b_linked == 0


def test_cohort_linker_and_generic_harmonize_agree():
    # Two implementations of one rule. If they diverge a published number moves for a reason that
    # has nothing to do with the engine, so the driver asserts the agreement per pair.
    a = {
        "glucose": curie_set("CHEBI:17234", {"KEGG": ["C00031"], "INCHIKEY": [GLUCOSE]}),
        "orphan": curie_set("", None),
    }
    b = {"glc": curie_set("KEGG.COMPOUND:C00031", None), "urea": curie_set("CHEBI:16199", None)}
    check = cross_check_harmonize(a, b)
    assert check["agree"] is True
    assert check["scorer_n_links"] == check["harmonize_n_links"] == 1


# ==================================================================================================
# Arm-B baseline, including the corrected published table
# ==================================================================================================


def test_name_match_case_insensitive():
    assert name_match_overlap(
        ["Glucose", "Citrate"], ["glucose", "urea"], case_sensitive=False
    ) == {"glucose"}


def test_name_match_case_sensitive_does_not_fold():
    assert name_match_overlap(["Glucose"], ["glucose"], case_sensitive=True) == set()


def test_refmet_drops_non_standardizing_before_join():
    refmet = {"alpha-D-glucose": "Glucose", "D-glucose": "Glucose"}
    assert refmet_join_overlap(
        ["alpha-D-glucose", "mystery"], ["D-glucose", "mystery"], refmet
    ) == {"glucose"}


def test_refmet_matches_via_shared_standard_name():
    assert refmet_join_overlap(["raw_a"], ["raw_b"], {"raw_a": "Taurine", "raw_b": "Taurine"}) == {
        "taurine"
    }


def test_dispatch_selects_method_per_cohort():
    arivale = arm_b_overlap("arivale", ["Glucose"], ["glucose"])
    xu = arm_b_overlap("xuetal", ["Glucose"], ["glucose"])
    llfs = arm_b_overlap("llfs", ["a"], ["b"], refmet_map={"a": "X", "b": "X"})
    assert arivale.count == 1 and "case-insensitive" in arivale.method
    assert xu.count == 0 and "case-sensitive" in xu.method
    assert llfs.count == 1 and "RefMet" in llfs.method


def test_unknown_cohort_fails_loud():
    with pytest.raises(ValueError):
        arm_b_overlap("mystery_cohort", ["a"], ["a"])


def test_refmet_cohort_without_map_fails_loud():
    with pytest.raises(ValueError):
        arm_b_overlap("llfs", ["a"], ["a"])


def test_gap_to_monti_published_is_recorded():
    result = arm_b_overlap("arivale", ["Glucose"], ["glucose"])
    assert result.published == 615 and result.gap == 1 - 615


def test_blsa_scores_against_99_not_188():
    # The reverted comparator, asserted at the scoring boundary rather than only on the constant.
    result = arm_b_overlap("blsa", ["a"], ["b"], refmet_map={"a": "X", "b": "X"})
    assert result.published == 99


def test_xu_scores_against_432_not_385():
    result = arm_b_overlap("xuetal", ["Glucose"], ["Glucose"])
    assert result.published == 432


def test_monti_published_comes_from_the_supplement_table():
    # Supplement MOESM6, sheet "Table 2. Datasets", '# Overlap' column, NECS as reference. An
    # earlier pass derived these from the Methods prose and moved Xu to 385 and BLSA to 188; the
    # table and Table S03 both say otherwise, and the prose loses to the table.
    assert {"arivale": 615, "xuetal": 432, "llfs": 163, "blsa": 99} == MONTI_PUBLISHED


def test_the_prose_derived_values_are_retained_as_superseded():
    # Kept so the reversal stays traceable rather than looking like the table was always consulted.
    assert {"arivale": 615, "xuetal": 385, "llfs": 163, "blsa": 188} == MONTI_PUBLISHED_SUPERSEDED
    assert MONTI_PUBLISHED_SUPERSEDED["blsa"] != MONTI_PUBLISHED["blsa"]
    assert MONTI_PUBLISHED_SUPERSEDED["xuetal"] != MONTI_PUBLISHED["xuetal"]


def test_s03_derivation_is_recorded_as_independent_corroboration():
    # 1052 metabolite rows, counting any non-null statistic per cohort. Xu lands exactly on the
    # published 432. BLSA derives 88, which is 11 from 99 and 100 from 188, so it rules 188 out.
    assert {"xuetal": 432, "llfs": 162, "blsa": 88} == MONTI_S03_DERIVED
    assert MONTI_S03_DERIVED["xuetal"] == MONTI_PUBLISHED["xuetal"]
    assert abs(MONTI_S03_DERIVED["blsa"] - MONTI_PUBLISHED["blsa"]) < abs(
        MONTI_S03_DERIVED["blsa"] - MONTI_PUBLISHED_SUPERSEDED["blsa"]
    )


def test_the_188_lipid_count_coincidence_stays_visible():
    # 188 is the paper's own count of LIPID metabolites in the LLFS panel. That explains where the
    # prose number came from and why it was the wrong one to reach for, so it must not be dropped.
    resolution = MONTI_PUBLISHED_PROVENANCE["blsa"]["conflict"]["resolution"]
    assert "LIPID" in resolution
    assert "188 lipid and 220 polar" in resolution


def test_every_published_value_names_its_source():
    for cohort, published in MONTI_PUBLISHED.items():
        record = MONTI_PUBLISHED_PROVENANCE[cohort]
        assert record["published"] == published
        assert "MOESM6" in str(record["source"])
    # A conflict record exists only where the paper's PROSE disagrees with its own table.
    assert MONTI_PUBLISHED_PROVENANCE["arivale"]["conflict"] is None
    assert MONTI_PUBLISHED_PROVENANCE["llfs"]["conflict"] is None
    assert MONTI_PUBLISHED_PROVENANCE["xuetal"]["conflict"]["prose_value"] == 385
    assert MONTI_PUBLISHED_PROVENANCE["blsa"]["conflict"]["prose_value"] == 188


# ==================================================================================================
# Link certificate
# ==================================================================================================


def test_certified_when_independent_structures_agree():
    certificate = certify_link(GLUCOSE, GLUCOSE)
    assert certificate.verdict == "certified" and certificate.stereo_checked is True


def test_refuted_on_connectivity_disagreement():
    certificate = certify_link(GLUCOSE, WRONG_MOLECULE)
    assert certificate.verdict == "refuted" and "connectivity" in certificate.reason


def test_refuted_on_stereoisomer():
    certificate = certify_link(GLUCOSE, GLUCOSE_STEREOISOMER)
    assert certificate.verdict == "refuted" and certificate.stereo_checked is True


def test_co_derivation_positive_control():
    # The only thing preventing a false certify is passing the INDEPENDENT structure rather than
    # the KG node that formed the link.
    assert certify_link(GLUCOSE, WRONG_MOLECULE).verdict == "refuted"
    assert certify_link(GLUCOSE, GLUCOSE).verdict == "certified"


def test_refused_when_cohort_has_no_independent_structure():
    certificate = certify_link(GLUCOSE, None)
    assert certificate.verdict == "refused" and "cohort" in certificate.reason


def test_refused_on_lookup_failure_not_crash():
    certificate = certify_link(None, GLUCOSE)
    assert certificate.verdict == "refused" and "NECS" in certificate.reason


def test_first_block_only_certifies_at_connectivity():
    # The independent resolver's granularity is the first block, which is neither tautomer- nor
    # charge-invariant, so stereo is explicitly flagged as unchecked rather than passed silently.
    certificate = certify_link("WQZGKKKJIJFFOK", "WQZGKKKJIJFFOK")
    assert certificate.verdict == "certified" and certificate.stereo_checked is False


def test_certificate_key_parsing():
    full = certificate_key(GLUCOSE)
    assert full is not None and full.connectivity == "WQZGKKKJIJFFOK" and full.stereo8 == "GASJEMHN"
    assert certificate_key("WQZGKKKJIJFFOK").stereo8 is None
    assert certificate_key("") is None and certificate_key(None) is None


def test_kg_tagged_side_refuses_even_when_blocks_match():
    assert (
        certify_link(_IK, _IK, necs_source="kg", cohort_source="provided-hmdb").verdict == "refused"
    )


def test_untagged_side_refuses_in_strict_mode():
    certificate = certify_link(
        _IK, _IK, necs_source="provided-hmdb", cohort_source=None, require_tags=True
    )
    assert certificate.verdict == "refused"


def test_tagged_independent_sides_certify():
    certificate = certify_link(
        _IK, _IK, necs_source="provided-hmdb", cohort_source="provided-pubchem", require_tags=True
    )
    assert certificate.verdict == "certified"


def test_legitimate_same_source_still_certifies():
    certificate = certify_link(
        _IK, _IK, necs_source="provided-hmdb", cohort_source="provided-hmdb", require_tags=True
    )
    assert certificate.verdict == "certified"


def _link(a: str, b: str) -> Link:
    return Link(a_name=a, b_name=b, shared=frozenset())


def test_certify_links_untagged_path():
    overlap = certify_links(
        [_link("n1", "c1"), _link("n2", "c2")],
        {"n1": GLUCOSE, "n2": GLUCOSE},
        {"c1": GLUCOSE, "c2": None},
    )
    assert overlap.certified == 1 and overlap.refused == 1
    assert overlap.adjudicable == 1 and overlap.certified_rate == 1.0


def test_certified_rate_is_none_when_nothing_adjudicable():
    overlap = certify_links([_link("n", "c")], {"n": GLUCOSE}, {})
    assert overlap.adjudicable == 0 and overlap.certified_rate is None


def test_certify_links_tagged_counts_and_canary():
    links = [_link("necs_glucose", "coh_glucose"), _link("necs_x", "coh_missing")]
    a = {
        "necs_glucose": ProvidedBlock(_IK, "provided-hmdb", "success"),
        "necs_x": ProvidedBlock("AAAAAAAAAAAAAA", "provided-hmdb", "success"),
    }
    b = {"coh_glucose": ProvidedBlock(_IK, "provided-pubchem", "success")}
    overlap, untagged = certify_links_tagged(links, a, b)
    assert overlap.certified == 1
    assert overlap.refused == 1
    assert untagged == 1


def test_certify_links_tagged_record_independence():
    same = [_link("glucose", "glucose")]
    a_same = {"glucose": ProvidedBlock(_IK, "provided-hmdb", "success", record_id="gold:glucose")}
    b_same = {"glucose": ProvidedBlock(_IK, "provided-hmdb", "success", record_id="gold:glucose")}
    overlap_same, _ = certify_links_tagged(same, a_same, b_same)
    assert overlap_same.certified == 0 and overlap_same.refused == 1
    b_distinct = {
        "glucose": ProvidedBlock(_IK, "provided-hmdb", "success", record_id="arivale:glucose")
    }
    overlap_distinct, _ = certify_links_tagged(same, a_same, b_distinct)
    assert overlap_distinct.certified == 1


def test_certify_links_tagged_kg_block_refused():
    overlap, _ = certify_links_tagged(
        [_link("n", "c")],
        {"n": ProvidedBlock(_IK, "kg", "success")},
        {"c": ProvidedBlock(_IK, "provided-pubchem", "success")},
    )
    assert overlap.certified == 0 and overlap.refused == 1


# ==================================================================================================
# Driver guards
# ==================================================================================================


def test_assert_alignment_accepts_matching_frame():
    frame = pd.DataFrame({"name": ["a", "b", "c"]})
    assert_alignment(frame, ["a", "b", "c"], "panel")


def test_assert_alignment_rejects_reordered_frame():
    # The batch-order hazard: a reordered response would attribute one metabolite's CURIE set to
    # another, silently. Nothing is realigned, because a panel name is not guaranteed unique.
    frame = pd.DataFrame({"name": ["b", "a", "c"]})
    with pytest.raises(PanelAlignmentError):
        assert_alignment(frame, ["a", "b", "c"], "panel")


def test_assert_alignment_rejects_truncated_frame():
    with pytest.raises(PanelAlignmentError):
        assert_alignment(pd.DataFrame({"name": ["a", "b"]}), ["a", "b", "c"], "panel")


def test_curies_by_name_rejects_duplicate_panel_names():
    frame = pd.DataFrame(
        {
            "name": ["glucose", "glucose"],
            "chosen_kg_id": ["CHEBI:17234", "CHEBI:4167"],
            "kg_equivalent_ids": ["{}", "{}"],
        }
    )
    with pytest.raises(PanelAlignmentError):
        curies_by_name(frame, "panel")


def test_curies_by_name_excludes_structural_namespaces_end_to_end():
    frame = pd.DataFrame(
        {
            "name": ["glucose"],
            "chosen_kg_id": ["RM:0135901"],
            "kg_equivalent_ids": [f'{{"CHEBI": ["17234"], "INCHIKEY": ["{GLUCOSE}"]}}'],
        }
    )
    assert curies_by_name(frame, "panel") == {"glucose": frozenset({"RM:0135901", "CHEBI:17234"})}


# ==================================================================================================
# Error rows are not unresolved rows
# ==================================================================================================


def test_errored_names_lists_only_failed_calls():
    frame = pd.DataFrame(
        {
            "name": ["a", "b", "c"],
            "chosen_kg_id": ["CHEBI:1", "", ""],
            "kg_equivalent_ids": ["{}", "{}", "{}"],
            "mapping_error": ["", "503 Server Error", "nan"],
        }
    )
    # "b" errored. "c" simply did not resolve. Both have an empty CURIE set, and conflating them
    # would report a non-resolution that never happened.
    assert errored_names(frame) == ["b"]
    assert curies_by_name(frame, "panel")["b"] == frozenset()
    assert curies_by_name(frame, "panel")["c"] == frozenset()


def test_errored_names_empty_when_no_error_column():
    frame = pd.DataFrame({"name": ["a"], "chosen_kg_id": ["CHEBI:1"], "kg_equivalent_ids": ["{}"]})
    assert errored_names(frame) == []


class _StubMapper:
    """Minimal ApiMapper stand-in: records what it was asked to re-map and answers from a table."""

    endpoint = "https://stub.invalid/api/v1"

    def __init__(self, answers: dict[str, dict[str, str]]) -> None:
        self.answers = answers
        self.requested: list[list[str]] = []

    def map_dataset_to_kg(self, **kwargs: object) -> tuple[str, dict[str, object]]:
        dataset = kwargs["dataset"]
        out_dir = Path(str(kwargs["output_dir"]))
        prefix = str(kwargs["output_prefix"])
        names = [str(n) for n in dataset["name"].tolist()]  # type: ignore[index]
        self.requested.append(names)
        rows = [
            {
                "name": name,
                "chosen_kg_id": self.answers.get(name, {}).get("chosen_kg_id", ""),
                "kg_equivalent_ids": self.answers.get(name, {}).get("kg_equivalent_ids", "{}"),
                "mapping_error": self.answers.get(name, {}).get("mapping_error", ""),
            }
            for name in names
        ]
        path = out_dir / f"{prefix}_MAPPED.tsv"
        pd.DataFrame(rows).to_csv(path, sep="\t", index=False)
        return str(path), {}


def _errored_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "name": ["a", "b", "c"],
            "chosen_kg_id": ["CHEBI:1", "", ""],
            "kg_equivalent_ids": ["{}", "{}", "{}"],
            "mapping_error": ["", "503 Server Error", ""],
        }
    )


def test_repair_rewrites_only_the_errored_row_in_place(tmp_path):
    mapper = _StubMapper({"b": {"chosen_kg_id": "CHEBI:2", "kg_equivalent_ids": "{}"}})
    frame, summary = repair_errored_rows(mapper, _errored_frame(), tmp_path, "panel")
    assert mapper.requested == [["b"]]  # only the failed row is re-sent
    assert summary == {
        "attempted": 1,
        "recovered": 1,
        "still_errored": 0,
        "still_errored_names": [],
    }
    # The repaired value lands on the row it came from, and its neighbours are untouched.
    assert frame["name"].tolist() == ["a", "b", "c"]
    assert frame["chosen_kg_id"].tolist() == ["CHEBI:1", "CHEBI:2", ""]
    assert (tmp_path / "panel_MAPPED.tsv").exists()


def test_repair_reports_a_row_that_errors_again_rather_than_hiding_it(tmp_path):
    mapper = _StubMapper({"b": {"mapping_error": "503 Server Error"}})
    frame, summary = repair_errored_rows(mapper, _errored_frame(), tmp_path, "panel")
    assert summary["recovered"] == 0 and summary["still_errored"] == 1
    assert summary["still_errored_names"] == ["b"]
    assert errored_names(frame) == ["b"]


def test_repair_is_a_no_op_when_nothing_errored(tmp_path):
    clean = pd.DataFrame(
        {
            "name": ["a"],
            "chosen_kg_id": ["CHEBI:1"],
            "kg_equivalent_ids": ["{}"],
            "mapping_error": [""],
        }
    )
    mapper = _StubMapper({})
    frame, summary = repair_errored_rows(mapper, clean, tmp_path, "panel")
    assert mapper.requested == []
    assert summary == {"attempted": 0, "recovered": 0, "still_errored": 0}
    assert frame["chosen_kg_id"].tolist() == ["CHEBI:1"]


def test_repair_refuses_a_misaligned_response(tmp_path):
    class _WrongOrderMapper(_StubMapper):
        def map_dataset_to_kg(self, **kwargs: object) -> tuple[str, dict[str, object]]:
            out_dir = Path(str(kwargs["output_dir"]))
            path = out_dir / "wrong_MAPPED.tsv"
            pd.DataFrame(
                [
                    {
                        "name": "not-b",
                        "chosen_kg_id": "CHEBI:9",
                        "kg_equivalent_ids": "{}",
                        "mapping_error": "",
                    }
                ]
            ).to_csv(path, sep="\t", index=False)
            return str(path), {}

    with pytest.raises(PanelAlignmentError):
        repair_errored_rows(_WrongOrderMapper({}), _errored_frame(), tmp_path, "panel")


def test_client_repo_provenance_names_the_code_that_ran():
    record = client_repo_provenance()
    assert set(record) == {"repo", "commit", "dirty"}
    # A wheel install has no repository; that is reported as None rather than invented.
    assert record["commit"] is None or len(str(record["commit"])) == 40


# ==================================================================================================
# Checkpoint provenance: panels resolved separately must be attributable to one backend
# ==================================================================================================


def _provenance(
    *, endpoint: str = "https://api.invalid/v1", kg_version: str = "2.1.1", commit: str = "a" * 40
) -> RunProvenance:
    return RunProvenance(
        run_id="test",
        biomapper_version="1.5.0",
        api_endpoint=endpoint,
        kestrel_url="https://kestrel.invalid/api",
        kestrel_version="0.3.0",
        run_timestamp="2026-09-24T00:00:00+00:00",
        kg_build=KgBuildInfo(
            kg_version=kg_version,
            biolink_version="4.2.5",
            build_timestamp="2026-09-17T07:27:41Z",
            git_commit=commit,
        ),
    )


def test_panel_provenance_round_trips_and_matches(tmp_path):
    provenance = _provenance()
    write_panel_provenance(tmp_path, "necs", provenance)
    status = check_panel_provenance(tmp_path, "necs", provenance, allow_unpinned=False)
    assert status["status"] == "match" and status["drift"] is None


def test_a_different_graph_build_between_panels_aborts():
    # This is the failure the sidecar exists for: two panels answered by different builds, then
    # intersected and stamped with one provenance block.
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        out = Path(directory)
        write_panel_provenance(out, "necs", _provenance(kg_version="2.1.0"))
        with pytest.raises(BackendDriftError) as caught:
            check_panel_provenance(
                out, "necs", _provenance(kg_version="2.1.1"), allow_unpinned=False
            )
        assert "kg_version" in str(caught.value)


def test_a_different_endpoint_between_panels_aborts(tmp_path):
    write_panel_provenance(tmp_path, "necs", _provenance(endpoint="https://other.invalid/v1"))
    with pytest.raises(BackendDriftError):
        check_panel_provenance(tmp_path, "necs", _provenance(), allow_unpinned=False)


def test_a_checkpoint_with_no_sidecar_is_refused_not_assumed(tmp_path):
    # Worse than a mismatch, because an unpinned checkpoint looks fine.
    with pytest.raises(BackendDriftError) as caught:
        check_panel_provenance(tmp_path, "necs", _provenance(), allow_unpinned=False)
    assert "no sidecar" in str(caught.value)


def test_drift_can_be_published_but_only_on_the_record(tmp_path):
    write_panel_provenance(tmp_path, "necs", _provenance(kg_version="2.1.0"))
    status = check_panel_provenance(tmp_path, "necs", _provenance(), allow_unpinned=True)
    assert status["status"] == "drift"
    assert status["drift"]["kg_version"] == {"panel": "2.1.0", "finalizing_run": "2.1.1"}


# ==================================================================================================
# run_links: an unrecovered error must not be double-counted as a non-resolution
# ==================================================================================================


def _panel(names: list[str], key: str, certifiable: bool = False) -> CohortPanel:
    config = CohortPanelConfig(key=key, name_column="name")
    return load_cohort_panel(pd.DataFrame({"name": names}), config)


def _link_fixture() -> tuple[dict[str, CohortPanel], dict[str, dict[str, frozenset[str]]]]:
    panels = {
        "necs": _panel(["glucose", "urea", "dropped_by_server"], "necs"),
        "arivale": _panel(["glucose", "creatinine"], "arivale"),
        "xuetal": _panel(["glucose"], "xuetal"),
        "llfs": _panel(["glucose"], "llfs"),
        "blsa": _panel(["glucose"], "blsa"),
    }
    linked = frozenset({"CHEBI:17234"})
    curies = {
        "necs": {"glucose": linked, "urea": frozenset(), "dropped_by_server": frozenset()},
        "arivale": {"glucose": linked, "creatinine": frozenset()},
        "xuetal": {"glucose": linked},
        "llfs": {"glucose": linked},
        "blsa": {"glucose": linked},
    }
    return panels, curies


def test_run_links_excludes_errored_rows_from_unresolved(tmp_path):
    panels, curies = _link_fixture()
    results = run_links(
        panels,
        curies,
        {"glucose": "Glucose"},
        tmp_path,
        errored={"necs": {"dropped_by_server"}},
    )
    arivale = results["arivale"]
    # "urea" genuinely did not resolve. "dropped_by_server" was never answered. Only the first is
    # an unresolved metabolite; counting both would report the deployment failure twice.
    assert arivale["necs_unresolved"] == 1
    assert arivale["necs_errored"] == 1
    assert arivale["necs_comparable"] == 1
    listed = pd.read_csv(tmp_path / "unresolved_necs.csv")["name"].tolist()
    assert listed == ["urea"]
    assert pd.read_csv(tmp_path / "errored_necs.csv")["name"].tolist() == ["dropped_by_server"]


def test_run_links_without_an_error_map_counts_every_empty_row_as_unresolved(tmp_path):
    panels, curies = _link_fixture()
    results = run_links(panels, curies, {"glucose": "Glucose"}, tmp_path)
    assert results["arivale"]["necs_unresolved"] == 2
    assert results["arivale"]["necs_errored"] == 0


def test_run_links_carries_the_published_provenance_and_the_superseded_value(tmp_path):
    panels, curies = _link_fixture()
    results = run_links(panels, curies, {"glucose": "Glucose"}, tmp_path)
    assert results["blsa"]["monti_published"] == 99
    assert results["blsa"]["monti_published_superseded_value"] == 188
    assert results["blsa"]["monti_s03_derived"] == 88
    assert results["blsa"]["monti_published_provenance"]["conflict"]["prose_value"] == 188
    # Names-only cohorts say so in the result, so a table cannot render a blank as a failure.
    assert results["blsa"]["certifiable"] is False
    assert "never structurally certifiable" in results["blsa"]["certifiability_note"]


def test_run_links_asserts_the_two_linkers_agree(tmp_path):
    panels, curies = _link_fixture()
    results = run_links(panels, curies, {"glucose": "Glucose"}, tmp_path)
    for cohort in ("arivale", "xuetal", "llfs", "blsa"):
        assert results[cohort]["harmonize_cross_check"]["agree"] is True


# ==================================================================================================
# Accuracy versus coverage, derived from the build's own source list
# ==================================================================================================


def test_refmet_pairs_are_labelled_coverage_when_refmet_is_in_the_graph():
    # Monti matched NECS to LLFS and BLSA on RefMet names, and RefMet is ingested into KRAKEN and is
    # the resolver's source-weighting target, so the comparison runs through a shared vocabulary.
    for cohort in ("llfs", "blsa"):
        note = comparator_independence(cohort, ["refmet", "kg2", "babel"])
        assert note["label"] == "coverage"
        assert note["comparator_source_in_graph"] == "refmet"


def test_chemical_name_pairs_are_not_labelled_coverage():
    # Metabolon CHEMICAL_NAME is vendor curation, not a graph source.
    for cohort in ("arivale", "xuetal"):
        note = comparator_independence(cohort, ["refmet", "kg2", "babel"])
        assert note["label"] == "accuracy_candidate"
        assert note["comparator_source_in_graph"] is None


def test_the_label_tracks_the_build_rather_than_an_assertion():
    # If a build ever stopped ingesting refmet, the label would move with it.
    assert comparator_independence("llfs", ["kg2", "babel"])["label"] == "accuracy_candidate"


def test_run_links_records_the_label_per_pair(tmp_path):
    panels, curies = _link_fixture()
    results = run_links(
        panels, curies, {"glucose": "Glucose"}, tmp_path, kg_sources=["refmet", "kg2"]
    )
    assert results["llfs"]["comparator_independence"]["label"] == "coverage"
    assert results["arivale"]["comparator_independence"]["label"] == "accuracy_candidate"


# ==================================================================================================
# The Arm-B gap is only readable next to what the reconstruction could see
# ==================================================================================================


def test_refmet_pair_reports_its_reconstruction_ceiling():
    # Half the cohort standardizes, so the reconstruction cannot exceed that half. Reporting the gap
    # to the published number without this invites reading a cache limit as a disagreement.
    basis = arm_b_reconstruction_basis(
        "blsa",
        ["a", "b", "c", "d"],
        ["a", "b", "x", "y"],
        {"a": "A", "b": "B", "c": "C", "d": "D", "x": "X"},
    )
    assert basis["method"] == "refmet"
    assert basis["necs_standardizable"] == 4 and basis["necs_n"] == 4
    assert basis["cohort_standardizable"] == 3 and basis["cohort_n"] == 4
    assert basis["reconstruction_ceiling"] == 3


def test_name_match_pair_has_no_standardization_step_to_be_short_of():
    basis = arm_b_reconstruction_basis("arivale", ["a"], ["a"], {})
    assert basis["method"] == "name"
    assert "no standardization step" in basis["note"]


def test_run_links_carries_the_reconstruction_basis(tmp_path):
    panels, curies = _link_fixture()
    results = run_links(panels, curies, {"glucose": "Glucose"}, tmp_path)
    assert results["llfs"]["arm_b_reconstruction_basis"]["method"] == "refmet"
    assert results["arivale"]["arm_b_reconstruction_basis"]["method"] == "name"


def test_sidecar_records_a_run_start_client_capture(tmp_path):
    captured = {"repo": "/repo", "commit": "b" * 40, "dirty": False}
    record = write_panel_provenance(tmp_path, "necs", _provenance(), captured)
    assert record["client_repo"]["commit"] == "b" * 40
    assert record["client_repo"]["captured"] == "at run start"


def test_sidecar_flags_a_late_client_capture_rather_than_passing_it_off(tmp_path):
    # The sidecar is written after a panel finishes, possibly an hour later. Reading the working
    # tree then would attribute the panel to whatever the repo has become, so the fallback says so.
    record = write_panel_provenance(tmp_path, "necs", _provenance())
    assert record["client_repo"]["captured"] == "late (at sidecar write)"


# ==================================================================================================
# Ordering: a rejected checkpoint must be left exactly as it was found
# ==================================================================================================


def _checkpoint(out_dir: Path, label: str, names: list[str], error_on: str | None = None) -> None:
    pd.DataFrame(
        {
            "name": names,
            "chosen_kg_id": ["CHEBI:1" for _ in names],
            "kg_equivalent_ids": ["{}" for _ in names],
            "mapping_error": ["503 Server Error" if n == error_on else "" for n in names],
        }
    ).to_csv(out_dir / f"{label}_MAPPED.tsv", sep="\t", index=False)


def test_a_drifting_checkpoint_is_rejected_before_the_repair_pass_can_rewrite_it(tmp_path):
    names = ["glucose", "urea"]
    _checkpoint(tmp_path, "necs", names, error_on="urea")
    before = (tmp_path / "necs_MAPPED.tsv").read_bytes()
    write_panel_provenance(tmp_path, "necs", _provenance(kg_version="2.1.0"), {"commit": "a" * 40})
    mapper = _StubMapper({"urea": {"chosen_kg_id": "CHEBI:16199", "kg_equivalent_ids": "{}"}})

    with pytest.raises(BackendDriftError):
        prepare_panel(
            mapper,
            _panel(names, "necs"),
            tmp_path,
            "necs",
            _provenance(kg_version="2.1.1"),
            {"commit": "a" * 40},
            allow_unpinned=False,
        )
    # The repair pass never ran, so the checkpoint still holds only the original backend's rows.
    # Rewriting it first and aborting second would have left a two-backend file for a later retry.
    assert mapper.requested == []
    assert (tmp_path / "necs_MAPPED.tsv").read_bytes() == before


def test_a_matching_checkpoint_is_repaired(tmp_path):
    names = ["glucose", "urea"]
    _checkpoint(tmp_path, "necs", names, error_on="urea")
    provenance = _provenance()
    write_panel_provenance(tmp_path, "necs", provenance, {"commit": "a" * 40})
    mapper = _StubMapper({"urea": {"chosen_kg_id": "CHEBI:16199", "kg_equivalent_ids": "{}"}})
    frame, pin, repair = prepare_panel(
        mapper,
        _panel(names, "necs"),
        tmp_path,
        "necs",
        provenance,
        {"commit": "a" * 40},
        allow_unpinned=False,
    )
    assert pin["status"] == "match"
    assert repair["recovered"] == 1
    assert frame["chosen_kg_id"].tolist() == ["CHEBI:1", "CHEBI:16199"]


# ==================================================================================================
# --refresh makes the drift error's advice followable
# ==================================================================================================


def test_refresh_discards_the_checkpoint_and_its_sidecar_then_re_resolves(tmp_path):
    names = ["glucose"]
    _checkpoint(tmp_path, "necs", names)
    write_panel_provenance(tmp_path, "necs", _provenance(kg_version="2.1.0"), {"commit": "a" * 40})
    mapper = _StubMapper({"glucose": {"chosen_kg_id": "RM:1", "kg_equivalent_ids": "{}"}})
    frame = resolve_panel(
        mapper,
        _panel(names, "necs"),
        tmp_path,
        "necs",
        _provenance(),
        {"commit": "b" * 40},
        refresh=True,
    )
    assert mapper.requested == [["glucose"]]  # actually re-resolved, not reused
    assert frame["chosen_kg_id"].tolist() == ["RM:1"]
    # Re-pinned from the new run rather than inheriting the stale sidecar.
    sidecar = json.loads((tmp_path / "necs_provenance.json").read_text())
    assert sidecar["kg_version"] == "2.1.1"


def test_without_refresh_an_existing_checkpoint_is_reused(tmp_path):
    names = ["glucose"]
    _checkpoint(tmp_path, "necs", names)
    mapper = _StubMapper({})
    frame = resolve_panel(mapper, _panel(names, "necs"), tmp_path, "necs")
    assert mapper.requested == []
    assert frame["chosen_kg_id"].tolist() == ["CHEBI:1"]


def test_repair_is_skipped_for_an_allowed_but_unconfirmed_checkpoint(tmp_path):
    # Under --allow-unpinned-checkpoints the drift check reports instead of raising. Repairing then
    # would remap failed rows against the CURRENT backend while keeping inherited rows from another,
    # producing a per-row mixed file whose overlap would still read as one backend's output.
    names = ["glucose", "urea"]
    _checkpoint(tmp_path, "necs", names, error_on="urea")
    before = (tmp_path / "necs_MAPPED.tsv").read_bytes()
    write_panel_provenance(tmp_path, "necs", _provenance(kg_version="2.1.0"), {"commit": "a" * 40})
    mapper = _StubMapper({"urea": {"chosen_kg_id": "CHEBI:16199", "kg_equivalent_ids": "{}"}})

    frame, pin, repair = prepare_panel(
        mapper,
        _panel(names, "necs"),
        tmp_path,
        "necs",
        _provenance(kg_version="2.1.1"),
        {"commit": "a" * 40},
        allow_unpinned=True,
    )
    assert pin["status"] == "drift"
    assert repair["skipped"] is True and repair["attempted"] == 0
    assert repair["still_errored"] == 1  # reported as unrecovered, not quietly fixed
    assert "unconfirmed one" in repair["skipped_reason"]
    assert mapper.requested == []
    assert (tmp_path / "necs_MAPPED.tsv").read_bytes() == before
    assert errored_names(frame) == ["urea"]


def test_repair_still_runs_for_a_confirmed_checkpoint(tmp_path):
    names = ["glucose", "urea"]
    _checkpoint(tmp_path, "necs", names, error_on="urea")
    provenance = _provenance()
    write_panel_provenance(tmp_path, "necs", provenance, {"commit": "a" * 40})
    mapper = _StubMapper({"urea": {"chosen_kg_id": "CHEBI:16199", "kg_equivalent_ids": "{}"}})
    _frame, pin, repair = prepare_panel(
        mapper,
        _panel(names, "necs"),
        tmp_path,
        "necs",
        provenance,
        {"commit": "a" * 40},
        allow_unpinned=True,
    )
    assert pin["status"] == "match"
    assert repair.get("skipped") is None and repair["recovered"] == 1


def test_the_manifest_source_note_agrees_with_the_per_pair_provenance():
    """A run record must not carry two different accounts of where its comparator came from.

    The manifest-level note once said the overlaps came from the Methods prose and "NOT Table 2",
    while the per-cohort provenance cited the supplement's "Table 2. Datasets". Both appeared in the
    same record, which makes the corrected Xu and BLSA values unauditable.
    """
    import inspect

    import biomapper.benchmarks.cross_cohort as module

    source = inspect.getsource(module.main)
    assert "supplement MOESM6, sheet 'Table 2. Datasets'" in source
    # The prose is named as what the values are NOT read from, and the two Table 2s are kept apart.
    assert "published_overlaps_NOT_read_from" in source
    for cohort in ("arivale", "xuetal", "llfs", "blsa"):
        assert "MOESM6" in str(MONTI_PUBLISHED_PROVENANCE[cohort]["source"])
