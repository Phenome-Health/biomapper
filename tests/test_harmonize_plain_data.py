"""Every harmonization result has a JSON-serializable form, and ``report.json`` holds it (1.9.0).

biomapper-ui exports links, groups, review queues, unresolved rows and the rest as CSV, TSV and
JSON, so each class must give plain data with stable keys. These tests pin the keys: adding one
is fine (extend the set here), renaming or removing one must raise ``REPORT_SCHEMA_VERSION``.
"""

from __future__ import annotations

import json

import pytest

from biomapper import harmonize_cohorts, harmonize_many
from biomapper.cohorts import (
    NAME_LINKING_CATEGORIES,
    REPORT_FILENAME,
    REPORT_SCHEMA_VERSION,
    name_linking_info,
)
from biomapper.harmonize import harmonize
from biomapper.jobs import load_report
from tests.cohorts_helpers import FakeMapper, by_name, mr

PINS = {"kestrel": {"kestrel_version": "1.0",
                    "kg_build": {"kg_version": "2.3.0", "git_commit": "abc123"}}}

LINK_KEYS = {
    "a_key", "b_key", "basis", "bases", "shared_identifiers", "name_only", "a_partners",
    "b_partners", "one_to_one", "ambiguous", "a_id_confidence", "b_id_confidence",
    "id_confidence_differs", "a_is_mixture", "b_is_mixture", "a_mixture_key", "b_mixture_key",
    "mixture", "needs_review", "review_reasons",
}
RESULT_KEYS = {"labels", "summary", "links", "name_match_withheld", "unresolved", "errors",
               "possible_missed"}
MAPPING_ROW_KEYS = {
    "key", "name", "row", "declared_identifiers", "identifiers_sent", "chosen_kg_id",
    "resolved", "error", "kg_equivalent_ids", "confidence_score", "confidence_tier",
}
NAME_LINKING_KEYS = {"enabled", "category", "decided_by", "enabled_for_categories",
                     "overridable", "reason"}


def _roundtrip(value):  # noqa: ANN001, ANN202
    """json.dumps must take it as is (no default=), and nothing may be lost on the way back."""
    text = json.dumps(value, allow_nan=False)
    assert json.loads(text) == value
    return value


# -- the two cohorts used throughout ----------------------------------------------------------

TABLE = {
    "glucose": mr("glucose", "CHEBI:17234", {"CHEBI": ["17234"], "HMDB": ["HMDB0000122"]}),
    "Glucose": mr("Glucose", "CHEBI:4167", {"CHEBI": ["4167"], "HMDB": ["HMDB0000122"]}),
    "urea": mr("urea", "CHEBI:16199", {"CHEBI": ["16199"]}),
    "cis-aconitate": mr("cis-aconitate", "CHEBI:16383", {"CHEBI": ["16383"]}),
    "trans-aconitate": mr("trans-aconitate", "CHEBI:16383", {"CHEBI": ["16383"]}),
    "aconitate": mr("aconitate", "CHEBI:16383", {"CHEBI": ["16383"]}),
    "butyrate/isobutyrate (4:0)": mr("butyrate/isobutyrate (4:0)", "CHEBI:30772",
                                     {"CHEBI": ["30772"]}),
    "co": mr("co", "CHEBI:27638", {"CHEBI": ["27638"]}),
    "CO": mr("CO", "CHEBI:17245", {"CHEBI": ["17245"]}),
    "mystery": None,
    "broken": mr("broken", error="HTTP 500"),
}
A = [{"id": "a1", "name": "glucose", "hmdb": "HMDB0000122", "loinc": "2345-7"},
     {"id": "a2", "name": "urea", "hmdb": "", "loinc": ""},
     {"id": "a3", "name": "cis-aconitate", "hmdb": "", "loinc": ""},
     {"id": "a4", "name": "trans-aconitate", "hmdb": "", "loinc": ""},
     {"id": "a5", "name": "butyrate/isobutyrate (4:0)", "hmdb": "", "loinc": ""},
     {"id": "a6", "name": "co", "hmdb": "", "loinc": ""},
     {"id": "a7", "name": "mystery", "hmdb": "", "loinc": ""},
     {"id": "a8", "name": "", "hmdb": "", "loinc": ""}]
B = [{"id": "b1", "name": "Glucose", "hmdb": "HMDB0000122"},
     {"id": "b2", "name": "aconitate", "hmdb": ""},
     {"id": "b3", "name": "CO", "hmdb": ""},
     {"id": "b4", "name": "broken", "hmdb": ""}]
C = [{"id": "c1", "name": "glucose", "hmdb": "HMDB0000122"},
     {"id": "c2", "name": "urea", "hmdb": "HMDB0000294"}]


def _two(**kw):  # noqa: ANN003, ANN202
    return harmonize_cohorts(
        A, B, entity="metabolites", a_label="left", b_label="right",
        a_key_column="id", b_key_column="id",
        a_vocabularies={"HMDB": "hmdb", "LOINC": "loinc"}, b_vocabularies={"HMDB": "hmdb"},
        mapper=FakeMapper(by_name(TABLE)), pins=PINS, **{"save": False, **kw},
    )


def _many(tmp_path, **kw):  # noqa: ANN001, ANN003, ANN202
    return harmonize_many(
        {"left": A, "right": B, "third": C}, entity="metabolites",
        key_columns={"left": "id", "right": "id", "third": "id"},
        vocabularies={"left": {"HMDB": "hmdb"}, "right": {"HMDB": "hmdb"},
                      "third": {"HMDB": "hmdb"}},
        mapper=FakeMapper(by_name(TABLE)), pins=PINS, store_dir=tmp_path / "store",
        on_message=lambda _m: None, **{"save": False, **kw},
    )


# -- harmonize(): Link, PossibleMiss, HarmonizationResult -------------------------------------


def test_harmonization_result_to_dict_covers_links_withheld_unresolved_errors_and_misses() -> None:
    a = [mr("glucose", "CHEBI:17234", {"CHEBI": ["17234"]}), mr("co", "CHEBI:1", {"CHEBI": ["1"]}),
         mr("nothing"), mr("failed", error="boom")]
    b = [mr("glucose", "CHEBI:17234", {"CHEBI": ["17234"]}), mr("CO", "CHEBI:2", {"CHEBI": ["2"]}),
         mr("a or b", "CHEBI:3", {"CHEBI": ["3"]})]
    result = harmonize(a, b, a_label="x", b_label="y", link_by_name=True)
    out = _roundtrip(result.to_dict())
    assert set(out) == RESULT_KEYS
    assert out["labels"] == {"a": "x", "b": "y"}
    assert out["summary"] == result.summary()
    assert [set(link) for link in out["links"]] == [LINK_KEYS]
    assert out["links"][0] == result.links[0].to_dict()
    assert out["links"][0] | {
        "a_key": "glucose", "b_key": "glucose", "basis": "node",
        "bases": ["identifier", "name_exact", "node"], "shared_identifiers": ["CHEBI:17234"],
        "one_to_one": True, "ambiguous": False, "needs_review": False, "review_reasons": [],
    } == out["links"][0]
    assert out["name_match_withheld"] == [{"a_key": "co", "b_key": "CO"}]
    assert out["unresolved"] == {"x": ["nothing"], "y": []}
    assert out["errors"] == {"x": ["failed"], "y": []}
    assert out["possible_missed"] == [{
        "cohort": "y", "key": "a or b", "name": "a or b", "chosen_kg_id": "CHEBI:3",
        "resolved": True, "reasons": ["mixture"],
    }]
    assert out["possible_missed"][0] == result.possible_missed[0].to_dict()


def test_link_to_dict_marks_ambiguous_and_review_links() -> None:
    result = _two().names_only
    by_pair = {(lk.a_key, lk.b_key): lk.to_dict() for lk in result.links}
    shared_node = by_pair[("a3", "b2")]
    assert shared_node["ambiguous"] and not shared_node["one_to_one"]
    assert shared_node["b_partners"] == 2 and shared_node["review_reasons"] == ["ambiguous"]
    assert {k for k, v in by_pair.items() if v["ambiguous"]} == {
        (lk.a_key, lk.b_key) for lk in result.ambiguous_links
    }


# -- harmonize_cohorts(): the report ----------------------------------------------------------


def test_cohort_report_to_dict_is_json_and_has_every_part() -> None:
    report = _two()
    out = _roundtrip(report.to_dict())
    assert set(out) == {
        "schema_version", "kind", "biomapper_version", "entity", "category", "labels",
        "name_linking", "settings", "summary", "arms", "identifier_skipped_reason", "diff",
        "review", "review_queue", "mappings", "excluded", "warnings", "pins",
    }
    assert out["schema_version"] == REPORT_SCHEMA_VERSION == 1
    assert out["kind"] == "harmonize_cohorts"
    assert out["labels"] == {"a": "left", "b": "right"}
    assert out["summary"] == report.summary()
    assert out["pins"] == report.pins and out["pins"]["kestrel"] == PINS["kestrel"]
    assert out["warnings"] and "LOINC is declared only for 'left'" in out["warnings"][0]
    assert out["excluded"] == {"left": [{"row": 7, "reason": "blank name"}], "right": []}

    arm = out["arms"]["names_only"]
    assert set(arm) == RESULT_KEYS | {"unresolved_rows", "errored_rows"}
    assert set(arm["links"][0]) == LINK_KEYS | {"a_name", "b_name", "a_chosen_kg_id",
                                                "b_chosen_kg_id"}
    glucose = next(lk for lk in arm["links"] if lk["a_key"] == "a1")
    assert (glucose["a_name"], glucose["b_name"]) == ("glucose", "Glucose")
    assert (glucose["a_chosen_kg_id"], glucose["b_chosen_kg_id"]) == ("CHEBI:17234", "CHEBI:4167")
    assert glucose["basis"] == "identifier" and glucose["bases"] == ["identifier",
                                                                     "name_casefold"]
    assert arm["name_match_withheld"] == [
        {"a_key": "a6", "b_key": "b3", "a_name": "co", "b_name": "CO"}
    ]
    assert arm["unresolved_rows"] == {"left": [{"key": "a7", "name": "mystery"}], "right": []}
    assert arm["errored_rows"] == {"left": [], "right": [{"key": "b4", "name": "broken"}]}
    assert arm["unresolved"] == {"left": ["a7"], "right": []}
    assert [m["key"] for m in arm["possible_missed"]] == ["a5"]

    assert out["arms"]["identifier"] is not None and out["identifier_skipped_reason"] is None
    assert set(out["diff"]) == {"counts", "names_only_only", "identifier_only", "both"}
    assert out["diff"]["counts"] == report.diff.counts()
    assert {"a_key": "a1", "b_key": "b1"} in out["diff"]["both"]

    assert out["review"] == [i.to_dict() for i in report.review]
    assert out["review_queue"] == [i.to_dict() for i in report.review_queue]
    assert set(out["review"][0]) == {"cohort", "key", "name", "vocabulary", "code",
                                     "name_entry", "code_entry", "status"}

    assert set(out["mappings"]) == {"names_only", "identifier", "review"}
    row = out["mappings"]["identifier"]["left"][0]
    assert set(row) == MAPPING_ROW_KEYS
    assert row["declared_identifiers"] == {"HMDB": ["HMDB0000122"], "LOINC": ["2345-7"]}
    assert row["identifiers_sent"] == {"HMDB": ["HMDB0000122"]}, "the one-sided code is not sent"
    assert [r["resolved"] for r in out["mappings"]["names_only"]["left"]] == [True] * 6 + [False]
    assert out["mappings"]["names_only"]["right"][3]["error"] == "HTTP 500"


def test_a_skipped_identifier_arm_is_null_with_its_reason() -> None:
    report = harmonize_cohorts(A, B, entity="metabolites", mapper=FakeMapper(by_name(TABLE)),
                               pins=PINS, save=False)
    out = _roundtrip(report.to_dict())
    assert out["arms"]["identifier"] is None and out["diff"] is None
    assert out["identifier_skipped_reason"] == "no identifier columns declared; names-only arm only"
    assert out["review"] == [] and out["review_queue"] == []


def test_cohort_report_json_is_written_by_default_and_matches_to_dict(tmp_path) -> None:
    report = _two(save=True, output_dir=tmp_path / "run", on_message=lambda _m: None)
    path = tmp_path / "run" / REPORT_FILENAME
    assert path.name == "report.json" and path.is_file()
    on_disk = json.loads(path.read_text())
    assert on_disk == report.to_dict() == load_report(tmp_path / "run")
    assert str(tmp_path) not in path.read_text(), "no absolute path in the report"
    report.write(tmp_path / "again", on_message=lambda _m: None)
    assert load_report(tmp_path / "again") == on_disk


def test_load_report_says_when_there_is_no_report(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        load_report(tmp_path)
    (tmp_path / REPORT_FILENAME).write_text("[]")
    with pytest.raises(ValueError, match="does not hold a report object"):
        load_report(tmp_path)


# -- harmonize_many(): the report ---------------------------------------------------------------


def test_many_report_to_dict_is_json_and_has_every_part(tmp_path) -> None:
    report = _many(tmp_path)
    out = _roundtrip(report.to_dict())
    assert set(out) == {
        "schema_version", "kind", "biomapper_version", "entity", "category", "labels",
        "name_linking", "settings", "summary", "status", "grade", "arms",
        "identifier_skipped_reason", "diff", "store", "mapping", "excluded", "warnings", "pins",
        "session_id",
    }
    assert out["kind"] == "harmonize_many" and out["labels"] == ["left", "right", "third"]
    assert out["status"] == report.status == "partial"  # one row errored
    assert out["grade"] == report.grade == "iteration-grade, not for publication"
    assert out["summary"] == report.summary()
    assert out["pins"] == report.pins
    assert out["excluded"]["left"] == [{"row": 7, "reason": "blank name"}]
    assert set(out["arms"]) == {"names_only", "identifier"}
    assert set(out["diff"]) == {"counts", "names_only_only", "identifier_only", "both",
                                "membership_changed"}
    assert out["diff"]["counts"] == report.diff.counts()
    assert out["mapping"]["names_only"]["left"]["n_rows"] == 7

    arm = out["arms"]["names_only"]
    assert set(arm) == {
        "arm", "summary", "groups", "rows", "not_grouped", "cross_node", "cross_node_pairs",
        "cross_node_pairs_file", "withheld", "pair_counts", "id_check_independent",
        "id_check_review", "mappings",
    }
    assert arm["summary"] == report.names_only.summary()
    groups = {g["node"]: g for g in arm["groups"]}
    assert set(groups["CHEBI:17234"]) == {"node", "cohorts", "n_cohorts", "rows_per_cohort",
                                          "clean", "multiplicity", "broad", "members"}
    assert groups["CHEBI:17234"]["cohorts"] == ["left", "third"]
    assert groups["CHEBI:17234"]["members"] == [
        {"cohort": "left", "key": "a1", "name": "glucose", "broad": False},
        {"cohort": "third", "key": "c1", "name": "glucose", "broad": False},
    ]
    aconitate = groups["CHEBI:16383"]
    assert aconitate["multiplicity"] and not aconitate["clean"]
    assert aconitate["rows_per_cohort"] == {"left": 2, "right": 1}

    assert set(arm["rows"][0]) == {"cohort", "key", "name", "bucket", "node", "chosen_kg_id",
                                   "broad", "error", "id_check"}
    assert arm["rows"][0]["id_check"] == {
        "status": "confirmed", "declared": ["HMDB:HMDB0000122"], "matched": ["HMDB:HMDB0000122"],
        "reason": None, "rule": report.names_only.id_checks[("left", "a1")].rule,
    }
    assert [(r["key"], r["bucket"]) for r in arm["not_grouped"]["left"]] == [("a7", "unresolved")]
    assert [(r["key"], r["bucket"], r["error"]) for r in arm["not_grouped"]["right"]] == [
        ("b4", "errored", "HTTP 500")
    ]
    assert arm["not_grouped"]["third"] == []
    assert arm["rows"][6]["id_check"] is None, "a row outside every group has no identifier check"

    # glucose (left, CHEBI:17234) and Glucose (right, CHEBI:4167) share HMDB: a cross-node link.
    cross = next(e for e in arm["cross_node"]
                 if {e["node_a"], e["node_b"]} == {"CHEBI:17234", "CHEBI:4167"})
    assert set(cross) == {"node_a", "node_b", "bases", "cohorts", "n_row_pairs"}
    assert arm["cross_node_pairs_file"] is None, "held in memory when nothing is saved"
    pair = next(p for p in arm["cross_node_pairs"] if p["a_key"] == "a1" and p["b_key"] == "b1")
    assert set(pair) == {"a_cohort", "a_key", "a_name", "a_node", "b_cohort", "b_key", "b_name",
                         "b_node", "basis", "bases"}
    assert arm["withheld"] == [{"a_cohort": "left", "a_key": "a6", "b_cohort": "right",
                                "b_key": "b3"}]
    assert set(arm["pair_counts"][0]) == {"a", "b", "overlap_groups", "same_node_row_pairs",
                                          "cross_node_link_pairs", "harmonize_links",
                                          "reconciles"}
    assert all(p["reconciles"] for p in arm["pair_counts"])
    assert arm["id_check_independent"] is True
    assert out["arms"]["identifier"]["id_check_independent"] is False
    # third's urea declares HMDB0000294, which its node does not carry: listed for review.
    assert arm["id_check_review"] == [i.to_dict() for i in report.names_only.id_check_review]
    assert [(i["cohort"], i["key"], i["check"]["status"]) for i in arm["id_check_review"]] == [
        ("third", "c2", "not_confirmed")
    ]
    assert set(arm["mappings"]["left"][0]) == MAPPING_ROW_KEYS


def test_many_report_json_is_written_by_default_and_names_the_streamed_drilldown(
    tmp_path,
) -> None:
    report = _many(tmp_path, save=True, output_dir=tmp_path / "run")
    on_disk = load_report(tmp_path / "run")
    assert on_disk == _roundtrip(report.to_dict())
    arm = on_disk["arms"]["names_only"]
    # A saved run streams the row pairs to a table instead of holding them in memory.
    assert arm["cross_node_pairs"] is None
    assert arm["cross_node_pairs_file"] == "cross_node_pairs_names_only.tsv"
    assert (tmp_path / "run" / arm["cross_node_pairs_file"]).is_file()
    assert str(tmp_path) not in (tmp_path / "run" / REPORT_FILENAME).read_text()


# -- name linking: reported, not overridable ------------------------------------------------------


def test_name_linking_is_reported_with_its_reason_everywhere(tmp_path) -> None:
    for report in (_two(), _many(tmp_path)):
        info = report.to_dict()["name_linking"]
        assert set(info) == NAME_LINKING_KEYS
        assert info == report.summary()["name_linking"] == report.settings["name_linking"]
        assert info["enabled"] is True and info["enabled"] == report.settings["link_by_name"]
        assert info["category"] == "biolink:SmallMolecule" and info["decided_by"] == "category"
        assert info["overridable"] is False
        assert info["reason"].startswith("Name links are on because the category is")


@pytest.mark.parametrize("entity,category", [("proteins", "biolink:Protein"),
                                             ("genes", "biolink:Gene"),
                                             ("labs", "biolink:ClinicalMeasurement")])
def test_name_linking_is_off_and_says_why_for_other_categories(entity, category) -> None:
    report = harmonize_cohorts([{"name": "CAMP"}], [{"name": "camp"}], entity=entity,
                               mapper=FakeMapper(by_name({
                                   "CAMP": mr("CAMP", "X:1", {"X": ["1"]}),
                                   "camp": mr("camp", "X:2", {"X": ["2"]})})),
                               pins=PINS, save=False)
    info = report.to_dict()["name_linking"]
    assert info == name_linking_info(category)
    assert info["enabled"] is False and report.settings["link_by_name"] is False
    assert category in info["reason"] and "biolink:SmallMolecule only" in info["reason"]
    assert report.names_only.n_links == 0, "the same name on two nodes is not linked"


def test_name_linking_follows_a_category_override_and_nothing_else() -> None:
    assert name_linking_info("biolink:SmallMolecule")["enabled_for_categories"] == sorted(
        NAME_LINKING_CATEGORIES
    )
    report = harmonize_cohorts([{"name": "glucose"}], [{"name": "glucose"}], entity="labs",
                               category="biolink:SmallMolecule",
                               mapper=FakeMapper(by_name(TABLE)), pins=PINS, save=False)
    assert report.to_dict()["name_linking"]["enabled"] is True
    with pytest.raises(TypeError):
        harmonize_cohorts([{"name": "g"}], [{"name": "g"}], entity="labs",
                          link_by_name=True)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        harmonize_many({"a": [{"name": "g"}], "b": [{"name": "g"}]}, entity="labs",
                       link_by_name=True)  # type: ignore[call-arg]
