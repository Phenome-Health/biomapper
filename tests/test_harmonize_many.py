"""Unit 4: harmonize_many() end to end (orchestration, store reuse, identifier arm, status)."""

from __future__ import annotations

import json
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

import biomapper
from biomapper._mapping_store import StoreBuildMismatch
from biomapper.cohorts import harmonize_cohorts
from biomapper.harmonize import harmonize
from biomapper.many_cohorts import ManyCohortReport, harmonize_many
from tests.cohorts_helpers import FakeMapper, by_name, mr
from tests.nway_helpers import NameReplayMapper, write_three_metabolite_cohorts

PINS = {"kestrel": {"kestrel_version": "1.0",
                    "kg_build": {"kg_version": "2.3.0", "git_commit": "3dd08a5b"}}}
NAME_COLUMNS = {"ukbb_nmr": "title", "arivale_a": "BIOCHEMICAL_NAME",
                "arivale_b": "BIOCHEMICAL_NAME"}
KEYS = dict.fromkeys(NAME_COLUMNS, "key")

TABLE = {
    "glucose": mr("glucose", "CHEBI:17234", {"HMDB": ["HMDB0000122"]}),
    "Glucose": mr("Glucose", "CHEBI:17234", {"HMDB": ["HMDB0000122"]}),
    "lactate": mr("lactate", "CHEBI:24996", {"HMDB": ["HMDB0000190"]}),
    "urea": mr("urea", "CHEBI:16199"),
    "alanine": mr("alanine", "CHEBI:16449"),
}


def toy(*names: str, hmdb: list[str | None] | None = None) -> list[dict]:
    rows = []
    for i, n in enumerate(names):
        row = {"name": n}
        if hmdb is not None:
            row["hmdb"] = hmdb[i] or ""
        rows.append(row)
    return rows


def many(cohorts: dict, mapper, tmp_path: Path, **kw) -> ManyCohortReport:  # noqa: ANN001
    kw.setdefault("entity", "metabolites")
    kw.setdefault("pins", PINS)
    kw.setdefault("store_dir", tmp_path / "store")
    kw.setdefault("save", False)
    return harmonize_many(cohorts, mapper=mapper, **kw)


@pytest.fixture(scope="module")
def replay_run(tmp_path_factory) -> tuple[ManyCohortReport, NameReplayMapper, dict]:
    tmp = tmp_path_factory.mktemp("replay")
    files = write_three_metabolite_cohorts(tmp / "in")
    mapper = NameReplayMapper("metabolites")
    report = harmonize_many(
        files, entity="metabolites", name_columns=NAME_COLUMNS, key_columns=KEYS,
        mapper=mapper, pins=PINS, store_dir=tmp / "store", save=False,
    )
    return report, mapper, files


# ---------------------------------------------------------------------------
# End to end on the small-molecule replay (execution note: start here)
# ---------------------------------------------------------------------------


def test_three_cohort_replay_reconciles_every_pair_and_the_tables_agree(replay_run) -> None:
    report, mapper, _ = replay_run
    s = report.summary()
    assert s["status"] == "ok"
    assert s["labels"] == ["ukbb_nmr", "arivale_a", "arivale_b"]
    arm = s["arms"]["names_only"]
    pairs = {(p["a"], p["b"]): p for p in arm["pairs"]}
    assert {k: (p["harmonize_links"], p["same_node_row_pairs"], p["cross_node_link_pairs"])
            for k, p in pairs.items()} == {
        ("ukbb_nmr", "arivale_a"): (15, 15, 0),
        ("ukbb_nmr", "arivale_b"): (19, 19, 0),
        ("arivale_a", "arivale_b"): (37, 37, 0),
    }
    assert all(p["reconciles"] for p in arm["pairs"])
    # Independently: harmonize() on the same results gives the same link counts.
    names_only = report.arms["names_only"]
    for (a, b), p in pairs.items():
        ra, rb = names_only.results[a], names_only.results[b]
        ka = [r.key for r in names_only.records[a]]
        kb = [r.key for r in names_only.records[b]]
        ca, cb = [r.model_copy() for r in ra], [r.model_copy() for r in rb]
        direct = harmonize(
            ca, cb, a_label=a, b_label=b, link_by_name=True,
            key=lambda r, i, ca=ca, ka=ka, kb=kb: ka[i] if i < len(ca) and ca[i] is r else kb[i],
        )
        assert direct.n_links == p["harmonize_links"]
    # overlap matrix == groups containing both cohorts == membership rows
    nway = names_only.nway
    for (a, b), p in pairs.items():
        assert p["overlap_groups"] == sum(1 for g in nway.groups if {a, b} <= set(g.cohorts))
    assert arm["per_cohort"]["ukbb_nmr"]["total"] == 72
    assert sum(c["total"] for c in arm["per_cohort"].values()) == len(nway.rows)
    # every distinct name was sent exactly once
    assert len(mapper.sent) == len(set(mapper.sent))


def test_replay_run_reports_flags_and_identifier_checks(replay_run) -> None:
    report, _, _ = replay_run
    s = report.summary()
    arm = s["arms"]["names_only"]
    assert arm["groups"] == 119
    assert arm["flagged_groups"] == arm["multiplicity_groups"] + arm["broad_only_groups"]
    idc = arm["id_check"]
    assert idc["independent"] is True
    assert idc["comparison_rule"].endswith("/v1")
    assert sum(idc["by_status"].values()) == arm["per_cohort_grouped_total"]
    assert idc["n_review"] == idc["by_status"]["not_confirmed"]
    review = report.arms["names_only"].id_check_review
    assert all(item.check.status == "not_confirmed" for item in review)
    # ukbb declares nothing, so all of its grouped rows are no_declared_id
    ukbb_statuses = {report.arms["names_only"].id_checks[("ukbb_nmr", r.key)].status
                     for r in report.arms["names_only"].nway.rows
                     if r.cohort == "ukbb_nmr" and r.bucket == "grouped"}
    assert ukbb_statuses == {"no_declared_id"}


def test_identifier_arm_is_skipped_when_not_every_cohort_declares_a_vocabulary(replay_run) -> None:
    report, _, _ = replay_run
    # Arivale slices declare nothing here (no vocabularies passed): names-only only.
    assert report.identifier_skipped_reason == "no identifier columns declared; names-only arm only"
    assert report.summary()["arms"]["identifier"] is None


def test_two_cohort_labs_replay_matches_harmonize_cohorts(tmp_path) -> None:
    from tests.test_cohorts_replay import FIXTURE, ReplayMapper

    kw = dict(entity="labs", mapper=ReplayMapper(), probe_pins=False, save=False)
    with pytest.warns(UserWarning):
        two = harmonize_cohorts(FIXTURE / "ukbb_labs.tsv", FIXTURE / "arivale_labs.tsv",
                                a_key_column="key", b_key_column="key",
                                b_vocabularies={"LOINC": ["Labcorp LOINC ID", "Quest LOINC ID"]},
                                a_label="ukbb", b_label="arivale", **kw)
    mapper = ReplayMapper()
    report = harmonize_many(
        {"ukbb": FIXTURE / "ukbb_labs.tsv", "arivale": FIXTURE / "arivale_labs.tsv"},
        entity="labs", key_columns={"ukbb": "key", "arivale": "key"},
        vocabularies={"arivale": {"LOINC": ["Labcorp LOINC ID", "Quest LOINC ID"]}},
        mapper=mapper, pins=PINS, store_dir=tmp_path / "store", save=False,
    )
    s = report.summary()
    assert s["link_by_name"] is False
    (pair,) = s["arms"]["names_only"]["pairs"]
    assert pair["harmonize_links"] == two.names_only.n_links == 37
    assert pair["reconciles"]
    assert "not declared by every cohort" in (report.identifier_skipped_reason or "")
    # LOINC never travels as mapping input; it is only checked offline
    assert all(not ids for _, ids, _ in mapper.requests)
    assert s["arms"]["names_only"]["id_check"]["by_status"]["confirmed"] + \
        s["arms"]["names_only"]["id_check"]["by_status"]["not_confirmed"] > 0


# ---------------------------------------------------------------------------
# Identifier arm
# ---------------------------------------------------------------------------


def id_resolver(name: str, ids: dict, mode: str, etype: str):  # noqa: ANN201
    if ids.get("HMDB") == ["HMDB0000122"]:
        return mr(name, "CHEBI:4167", {"HMDB": ["HMDB0000122"]})
    return by_name(TABLE)(name, ids, mode, etype)


def test_identifier_arm_runs_when_every_cohort_declares_hmdb_and_remaps_coded_rows_only(
    tmp_path,
) -> None:
    cohorts = {
        "a": toy("glucose", "lactate", "urea", hmdb=["HMDB00122", None, None]),
        "b": toy("Glucose", "lactate", hmdb=["HMDB0000122", "HMDB0000190"]),
        "c": toy("glucose", "alanine", hmdb=[None, None]),
    }
    vocab = {label: {"HMDB": "hmdb"} for label in cohorts}
    mapper = FakeMapper(id_resolver)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        report = many(cohorts, mapper, tmp_path, vocabularies=vocab)
    supplied = [(n, ids) for n, ids, _ in mapper.sent if ids]
    assert supplied == [("glucose", {"HMDB": ["HMDB00122"]}),
                        ("Glucose", {"HMDB": ["HMDB0000122"]}),
                        ("lactate", {"HMDB": ["HMDB0000190"]})]
    s = report.summary()
    assert s["vocabularies"]["shared"] == ["HMDB"]
    assert s["arms"]["identifier"] is not None
    assert s["arms"]["identifier"]["id_check"]["independent"] is False
    diff = s["diff"]
    # names-only: glucose on CHEBI:17234 in a, b, c. identifier arm: a's and b's glucose moved to
    # CHEBI:4167, c's stayed.
    assert diff["names_only_only"] == 0
    assert diff["identifier_only"] == 1
    assert diff["membership_changed"] == 1
    assert report.diff is not None and "CHEBI:4167" in report.diff.identifier_only
    # names-only id check: a's legacy HMDB code is confirmed against the names-only node
    check = report.arms["names_only"].id_checks[("a", "0|glucose")]
    assert check.status == "confirmed"


def test_identifier_arm_is_skipped_when_two_of_three_declare_hmdb(tmp_path) -> None:
    cohorts = {"a": toy("glucose", hmdb=["HMDB0000122"]), "b": toy("glucose", hmdb=[None]),
               "c": toy("glucose")}
    report = many(cohorts, FakeMapper(id_resolver), tmp_path,
                  vocabularies={"a": {"HMDB": "hmdb"}, "b": {"HMDB": "hmdb"}})
    reason = report.identifier_skipped_reason or ""
    assert "not declared by every cohort" in reason and "HMDB" in reason and "'c'" in reason
    assert report.identifier is None


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def test_errored_rows_make_the_run_partial_and_are_listed(tmp_path) -> None:
    table = {**TABLE, "urea": mr("urea", error="HTTP 503")}
    report = many({"a": toy("glucose", "urea"), "b": toy("glucose")},
                  FakeMapper(by_name(table)), tmp_path)
    assert report.status == "partial"
    assert report.arms["names_only"].nway.bucket_keys("a", "errored") == ("1|urea",)


def test_a_run_where_every_row_errored_is_failed(tmp_path) -> None:
    report = many({"a": toy("x"), "b": toy("y")},
                  FakeMapper(lambda n, i, m, e: mr(n, error="down")), tmp_path)
    assert report.status == "failed"


# ---------------------------------------------------------------------------
# Validation before any request
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cohorts", "kw", "match"),
    [
        ({"a": toy("x")}, {}, "at least two"),
        ({"a b": toy("x"), "a_b": toy("y")}, {}, "both save as"),
        ({"n_links": toy("x"), "b": toy("y")}, {}, "reserved"),
        ({"a": toy("x"), "b": toy("y")}, {"entity": "nonsense"}, "unknown entity"),
        ({"a": toy("x"), "b": toy("y")}, {"name_columns": {"a": "title"}}, "name column 'title'"),
        ({"a": toy("x"), "b": toy("y")}, {"key_columns": {"zzz": "key"}}, "not a cohort label"),
        ({"a": toy("x"), "": toy("y")}, {}, "non-empty"),
    ],
)
def test_invalid_input_raises_before_any_request(tmp_path, cohorts, kw, match) -> None:
    mapper = FakeMapper(by_name(TABLE))
    with pytest.raises(ValueError, match=match):
        many(cohorts, mapper, tmp_path, **kw)
    assert mapper.calls == []


def test_a_build_mismatch_is_refused_before_any_request(tmp_path) -> None:
    cohorts = {"a": toy("glucose"), "b": toy("glucose")}
    many(cohorts, FakeMapper(by_name(TABLE)), tmp_path)
    other = {"kestrel": {"kestrel_version": "1.0",
                         "kg_build": {"kg_version": "2.4.0", "git_commit": "ffffffff"}}}
    mapper = FakeMapper(by_name(TABLE))
    with pytest.raises(StoreBuildMismatch, match="remap_all=True"):
        many(cohorts, mapper, tmp_path, pins=other)
    assert mapper.calls == []


# ---------------------------------------------------------------------------
# Reuse across runs (Unit 1 through the public call)
# ---------------------------------------------------------------------------


def test_a_second_identical_run_maps_nothing_and_is_iteration_grade(tmp_path) -> None:
    cohorts = {"a": toy("glucose", "lactate"), "b": toy("Glucose"), "c": toy("urea")}
    first = many(cohorts, FakeMapper(by_name(TABLE)), tmp_path)
    assert first.summary()["mapping"]["names_only"]["a"]["n_fresh"] == 2
    mapper = FakeMapper(by_name(TABLE))
    second = many(cohorts, mapper, tmp_path)
    assert mapper.sent == []
    s = second.summary()
    assert all(m["n_reused"] == m["n_rows"] for m in s["mapping"]["names_only"].values())
    assert s["publication_grade"] == "iteration-grade, not for publication"
    assert s["arms"]["names_only"] == first.summary()["arms"]["names_only"]


def test_adding_a_fourth_cohort_maps_only_its_unseen_names(tmp_path) -> None:
    cohorts = {"a": toy("glucose", "lactate"), "b": toy("Glucose"), "c": toy("urea")}
    many(cohorts, FakeMapper(by_name(TABLE)), tmp_path)
    mapper = FakeMapper(by_name(TABLE))
    many({**cohorts, "d": toy("urea", "alanine")}, mapper, tmp_path)
    assert [n for n, _, _ in mapper.sent] == ["alanine"]


def test_two_analyses_labelling_different_tables_qmdiab_do_not_collide(tmp_path) -> None:
    many({"QMDiab": toy("glucose"), "other": toy("urea")}, FakeMapper(by_name(TABLE)), tmp_path)
    mapper = FakeMapper(by_name(TABLE))
    report = many({"QMDiab": toy("lactate"), "other": toy("urea")}, mapper, tmp_path)
    assert [n for n, _, _ in mapper.sent] == ["lactate"]
    assert report.arms["names_only"].results["QMDiab"][0].chosen_kg_id == "CHEBI:24996"


def test_remap_all_maps_everything_and_can_be_publishable(tmp_path) -> None:
    cohorts = {"a": toy("glucose"), "b": toy("Glucose")}
    many(cohorts, FakeMapper(by_name(TABLE)), tmp_path)
    mapper = FakeMapper(by_name(TABLE))
    report = many(cohorts, mapper, tmp_path, remap_all=True)
    assert sorted(n for n, _, _ in mapper.sent) == ["Glucose", "glucose"]
    assert report.grade == "publishable"


def test_an_unknown_build_disables_reuse_and_says_why(tmp_path) -> None:
    cohorts = {"a": toy("glucose"), "b": toy("Glucose")}
    many(cohorts, FakeMapper(by_name(TABLE)), tmp_path)
    mapper = FakeMapper(by_name(TABLE))
    with pytest.warns(UserWarning, match="KG build unknown"):
        report = many(cohorts, mapper, tmp_path, pins=None, probe_pins=False)
    assert len(mapper.sent) == 2
    assert "KG build unknown" in (report.summary()["store"]["reason"] or "")
    assert report.grade == "iteration-grade, not for publication"


def test_without_a_store_every_entity_is_still_sent_once_per_run(tmp_path) -> None:
    mapper = FakeMapper(by_name(TABLE))
    harmonize_many({"a": toy("glucose", "urea"), "b": toy("glucose")}, entity="metabolites",
                   mapper=mapper, pins=PINS, save=False)
    assert sorted(n for n, _, _ in mapper.sent) == ["glucose", "urea"]


# ---------------------------------------------------------------------------
# Saving and the public surface
# ---------------------------------------------------------------------------


def test_save_by_default_claims_distinct_run_dirs_and_prints_absolute_paths(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    cohorts = {"a": toy("glucose"), "b": toy("Glucose")}
    first = harmonize_many(cohorts, entity="metabolites", mapper=FakeMapper(by_name(TABLE)),
                           pins=PINS)
    second = harmonize_many(cohorts, entity="metabolites", mapper=FakeMapper(by_name(TABLE)),
                            pins=PINS)
    assert first.output_dir is not None and second.output_dir is not None
    assert first.output_dir != second.output_dir
    assert first.output_dir.parent == (tmp_path / "biomapper_runs").resolve()
    assert first.output_dir.name.startswith("harmonize_many_")
    assert (tmp_path / "biomapper_runs" / "harmonize_many_store").is_dir()
    out = capsys.readouterr().out
    assert str(first.output_dir) in out
    assert str((tmp_path / "biomapper_runs" / "harmonize_many_store").resolve()) in out
    for name in ("summary.json", "settings.json"):
        text = (first.output_dir / name).read_text()
        assert str(tmp_path) not in text
        json.loads(text)


def test_harmonize_many_is_exported_at_the_package_root() -> None:
    assert biomapper.harmonize_many is harmonize_many
    assert biomapper.ManyCohortReport is ManyCohortReport
    assert {"harmonize_many", "ManyCohortReport"} <= set(biomapper.__all__)


def test_import_works_without_pandas() -> None:
    code = (
        "import sys\n"
        "sys.modules['pandas'] = None\n"
        "import biomapper\n"
        "import biomapper.many_cohorts, biomapper.harmonize.nway, biomapper._mapping_store\n"
        "assert callable(biomapper.harmonize_many)\n"
        "assert 'pandas' not in [m for m in sys.modules if sys.modules[m] is not None]\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
