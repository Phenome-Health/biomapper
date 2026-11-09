"""Unit 4: the ManyCohortReport's saved outputs and summary."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from biomapper.many_cohorts import harmonize_many
from biomapper.models import LipidResolution
from tests.cohorts_helpers import FakeMapper, by_name, mr

PINS = {"kestrel": {"kestrel_version": "1.0",
                    "kg_build": {"kg_version": "2.3.0", "git_commit": "3dd08a5b"}}}

TABLE = {
    "glucose": mr("glucose", "CHEBI:17234", {"HMDB": ["HMDB0000122"]}),
    "Glucose": mr("Glucose", "CHEBI:17234", {"HMDB": ["HMDB0000122"]}),
    "fatty acids": mr("fatty acids", "CHEBI:35366", {"LM": ["S1", "S2"]}),
    "palmitate": mr("palmitate", "LM:S1"),
    "stearate": mr("stearate", "LM:S2"),
    "PC 34:1": mr("PC 34:1", "LM:PC").model_copy(
        update={"lipid_resolution": LipidResolution(mapping_relation="broad")}),
    "PC(16:0/18:1)": mr("PC(16:0/18:1)", "LM:PC"),
    "Co": mr("Co", "CHEBI:27638"),
    "CO": mr("CO", "CHEBI:17245"),
    "nothing": mr("nothing"),
    "broken": mr("broken", error="timeout"),
}

COHORTS = {
    "qmdiab": [{"name": "glucose", "hmdb": "HMDB00122"}, {"name": "fatty acids", "hmdb": ""},
               {"name": "PC 34:1", "hmdb": ""}, {"name": "Co", "hmdb": ""},
               {"name": "nothing", "hmdb": ""}],
    "chear": [{"name": "Glucose", "hmdb": "HMDB0000190"}, {"name": "palmitate", "hmdb": ""},
              {"name": "PC(16:0/18:1)", "hmdb": ""}, {"name": "CO", "hmdb": ""},
              {"name": "broken", "hmdb": ""}],
    "tartu": [{"name": "glucose"}, {"name": "glucose"}, {"name": "stearate"}],
}


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def run(tmp_path: Path, **kw):  # noqa: ANN201
    return harmonize_many(
        COHORTS, entity="metabolites",
        vocabularies={"qmdiab": {"HMDB": "hmdb"}, "chear": {"HMDB": "hmdb"}},
        mapper=FakeMapper(by_name(TABLE)), pins=PINS, store_dir=tmp_path / "store",
        output_dir=tmp_path / "run", **kw,
    )


def test_every_table_is_written_and_agrees_with_the_summary(tmp_path) -> None:
    report = run(tmp_path)
    d = report.output_dir
    assert d == (tmp_path / "run").resolve()
    s = json.loads((d / "summary.json").read_text())
    assert s == json.loads(json.dumps(report.summary()))
    assert s["status"] == "partial"  # chear's "broken" errored
    arm = s["arms"]["names_only"]

    membership = read_tsv(d / "membership_names_only.tsv")
    assert len(membership) == 13
    assert {r["bucket"] for r in membership} == {"grouped", "unresolved", "errored"}

    matrix = read_tsv(d / "matrix_names_only.tsv")
    assert len(matrix) == arm["groups"]
    glucose = next(r for r in matrix if r["node"] == "CHEBI:17234")
    assert glucose["tartu"] == "0|glucose;1|glucose"
    assert glucose["multiplicity"] == "True" and glucose["clean"] == "False"
    pc = next(r for r in matrix if r["node"] == "LM:PC")
    assert pc["broad"] == "True"

    overlap = read_tsv(d / "overlap_names_only.tsv")
    assert [(r["cohort_a"], r["cohort_b"]) for r in overlap] == [
        ("qmdiab", "chear"), ("qmdiab", "tartu"), ("chear", "tartu")]
    assert all(r["reconciles"] == "True" for r in overlap)

    cross = read_tsv(d / "cross_node_names_only.tsv")
    assert {(r["node_a"], r["node_b"]) for r in cross} == {
        ("CHEBI:35366", "LM:S1"), ("CHEBI:35366", "LM:S2")}
    pairs = read_tsv(d / "cross_node_pairs_names_only.tsv")
    assert len(pairs) == arm["cross_node_row_pairs"] == 2

    withheld = read_tsv(d / "withheld_names_only.tsv")
    assert [(r["a_key"], r["b_key"]) for r in withheld] == [("3|Co", "3|CO")]

    flagged = {r["node"] for r in read_tsv(d / "flagged_groups_names_only.tsv")}
    assert flagged == {"CHEBI:17234", "LM:PC"}

    not_grouped = {(r["cohort"], r["bucket"]) for r in read_tsv(d / "not_grouped_names_only.tsv")}
    assert not_grouped == {("qmdiab", "unresolved"), ("chear", "errored")}

    review = read_tsv(d / "id_check_review_names_only.tsv")
    assert [(r["cohort"], r["key"], r["declared"]) for r in review] == [
        ("chear", "0|Glucose", "HMDB:HMDB0000190")]
    statuses = {(r["cohort"], r["key"]): r["id_check"] for r in membership}
    assert statuses[("qmdiab", "0|glucose")] == "confirmed"
    assert statuses[("chear", "0|Glucose")] == "not_confirmed"
    assert statuses[("tartu", "0|glucose")] == "no_declared_id"
    assert statuses[("chear", "4|broken")] == ""  # not grouped, not checked

    for label in COHORTS:
        assert (d / f"mapping_names_only_{label}.tsv").exists()
        prov = json.loads((d / f"provenance_names_only_{label}.json").read_text())
        assert prov["session_id"] == report.session_id
        assert len(prov["request_checksum"]) == 64


def test_the_identifier_arm_runs_on_hmdb_shared_by_qmdiab_and_chear_only_when_all_declare(
    tmp_path,
) -> None:
    report = run(tmp_path)
    # tartu declares nothing, so HMDB is not shared by every cohort
    assert report.identifier is None
    assert not (report.output_dir / "membership_identifier.tsv").exists()


def test_summary_and_settings_hold_no_absolute_paths(tmp_path) -> None:
    report = run(tmp_path)
    for name in ("summary.json", "settings.json"):
        text = (report.output_dir / name).read_text()
        assert str(tmp_path) not in text, name
    assert report.summary()["store"]["dir_name"] == "store"


def test_writing_again_elsewhere_copies_the_streamed_drilldown(tmp_path) -> None:
    report = run(tmp_path)
    assert report.names_only.nway.drilldown is None  # streamed, not held
    other = report.write(tmp_path / "copy")
    assert read_tsv(other / "cross_node_pairs_names_only.tsv") == read_tsv(
        tmp_path / "run" / "cross_node_pairs_names_only.tsv")


def test_an_unsaved_report_holds_its_drilldown_and_can_be_written_later(tmp_path) -> None:
    report = harmonize_many(COHORTS, entity="metabolites", mapper=FakeMapper(by_name(TABLE)),
                            pins=PINS, save=False)
    assert report.output_dir is None
    assert len(report.names_only.nway.drilldown or ()) == 2
    d = report.write(tmp_path / "later")
    assert len(read_tsv(d / "cross_node_pairs_names_only.tsv")) == 2
    assert report.summary()["store"]["used"] is False


def test_the_summary_records_pins_grade_and_mapping_counts(tmp_path) -> None:
    s = run(tmp_path).summary()
    assert s["kg_build"] == "2.3.0 (3dd08a5b)" and s["kg_verified"] is True
    assert s["publication_grade"] == "iteration-grade, not for publication"
    assert s["mapping"]["names_only"]["tartu"]["n_rows"] == 3
    assert s["pins"]["mapper"] == "FakeMapper"
    assert s["arms"]["names_only"]["id_check"]["note"].startswith("not_confirmed means")
