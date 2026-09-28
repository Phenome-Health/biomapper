"""A subset run must account for every suite arm it leaves out.

The defect: ``all --only a b c`` ran the named arms and wrote a manifest in which every other
``SUITE_DATASETS`` arm was simply absent, with no entry and no reason. A reader of that manifest
cannot tell a deliberate exclusion from an arm that fell out of the registry by accident, which is
the exact failure the suite's "skips are recorded, never omitted" rule exists to prevent. The
2026-09-27 run had to write ``excluded_arms.json`` beside the manifest to say why
MetaboliteAnnotator was missing; that record now travels in-band.
"""

from __future__ import annotations

import json

import pytest

from biomapper.benchmarks import cli
from biomapper.benchmarks.config import SUITE_DATASETS, SUITE_SKIPPED
from biomapper.benchmarks.suite import OmittedArmWithoutReason, resolve_omissions, run_suite

ANNOTATOR_RECORD = {
    "arm": "metaboliteannotator",
    "status": "skipped",
    "reason": "measured 3.31 s/entity over 34,512 entities; 31.7 h projected",
    "throughput_evidence": {"measured_s_per_entity": 3.31, "entities_total_both_modes": 34512},
}


def _ok_runner(key: str):
    def run(**_kwargs):  # noqa: ANN003
        return {"out_dir": "", "dataset": key, "role": "coverage", "results": {}}

    return run


def _run(tmp_path, selected: list[str], omitted):
    return run_suite(
        out_dir=tmp_path,
        datasets=selected,
        omitted=omitted,
        probe_live=False,
        runners={k: _ok_runner(k) for k in selected},
    )


def test_an_omitted_arm_without_a_reason_refuses_before_writing_anything(tmp_path):
    out = tmp_path / "suite"
    with pytest.raises(OmittedArmWithoutReason) as excinfo:
        run_suite(out_dir=out, datasets=["hajjar"], probe_live=False, runners={})
    assert "metaboliteannotator" in str(excinfo.value)
    # Refused before the suite dir exists: nothing half-written for a reader to mistake for a run.
    assert not out.exists()


def test_every_omitted_suite_arm_is_a_skipped_entry_with_its_reason(tmp_path):
    selected = [k for k in SUITE_DATASETS if k != "metaboliteannotator"]
    manifest = _run(tmp_path, selected, {"metaboliteannotator": ANNOTATOR_RECORD})["manifest"]

    names = [d["dataset"] for d in manifest["datasets"]]
    # Nothing in SUITE_DATASETS or SUITE_SKIPPED is absent, and nothing is listed twice.
    assert sorted(names) == sorted([*SUITE_DATASETS, *SUITE_SKIPPED])
    entry = next(d for d in manifest["datasets"] if d["dataset"] == "metaboliteannotator")
    assert entry["status"] == "skipped"
    assert entry["skip_origin"] == "operator"
    assert entry["reason"] == ANNOTATOR_RECORD["reason"]
    # The measured evidence is folded in-band, not left in a side file.
    evidence = ANNOTATOR_RECORD["throughput_evidence"]
    assert entry["exclusion_record"] == {"throughput_evidence": evidence}
    assert manifest["full_suite"] is False
    assert manifest["operator_skipped"] == ["metaboliteannotator"]
    # A skip is not a failure, so the attempted arms can still be complete.
    assert manifest["complete"] is True


def test_the_readme_says_the_run_is_a_subset(tmp_path):
    selected = [k for k in SUITE_DATASETS if k != "lmsd"]
    _run(tmp_path, selected, {"lmsd": "operator reason for lmsd"})
    readme = (tmp_path / "README.md").read_text()
    assert "Scope: SUBSET" in readme
    assert "operator reason for lmsd" in readme


def test_a_full_run_needs_no_reasons_and_says_so(tmp_path):
    manifest = _run(tmp_path, list(SUITE_DATASETS), None)["manifest"]
    assert manifest["full_suite"] is True
    assert manifest["operator_skipped"] == []
    assert "Scope: full suite." in (tmp_path / "README.md").read_text()


@pytest.mark.parametrize(
    ("selected", "omitted", "fragment"),
    [
        (
            ["hajjar"],
            {**{k: "x" for k in SUITE_DATASETS if k != "hajjar"}, "hajar": "x"},
            "not suite arms",
        ),
        (list(SUITE_DATASETS), {"hajjar": "x"}, "both selected"),
        (
            [k for k in SUITE_DATASETS if k != "lmsd"],
            {"lmsd": "   "},
            "without a reason",
        ),
        (
            [k for k in SUITE_DATASETS if k != "lmsd"],
            {"lmsd": {"arm": "lmsd", "note": "record with no reason key"}},
            "without a reason",
        ),
    ],
)
def test_ambiguous_or_empty_reasons_are_refused(selected, omitted, fragment):
    with pytest.raises(OmittedArmWithoutReason) as excinfo:
        resolve_omissions(selected, omitted)
    assert fragment in str(excinfo.value)


# --------------------------------------------------------------------------------------------------
# CLI: the command refuses to run, and folds excluded_arms.json in-band
# --------------------------------------------------------------------------------------------------


@pytest.fixture
def captured(monkeypatch, tmp_path):
    """Replace run_suite so the CLI tests exercise selection without network."""
    calls: dict = {}

    def fake_run_suite(**kwargs):  # noqa: ANN003
        calls.update(kwargs)
        selected = kwargs["datasets"]
        return run_suite(
            out_dir=tmp_path,
            datasets=selected,
            omitted=kwargs["omitted"],
            probe_live=False,
            runners={k: _ok_runner(k) for k in selected},
        )

    monkeypatch.setattr(cli, "run_suite", fake_run_suite)
    return calls


def test_cli_only_without_reasons_exits_2_and_names_the_missing_arms(captured, capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["all", "--only", "hajjar", "metabench"])
    assert excinfo.value.code == 2
    assert "metaboliteannotator" in capsys.readouterr().err
    assert captured == {}, "run_suite must not be called when the selection is refused"


def test_cli_exclusions_file_is_folded_into_the_manifest(captured, tmp_path):
    path = tmp_path / "excluded_arms.json"
    path.write_text(json.dumps([ANNOTATOR_RECORD]))
    only = [k for k in SUITE_DATASETS if k != "metaboliteannotator"]
    assert cli.main(["all", "--only", *only, "--exclusions", str(path)]) == 0
    manifest = json.loads((tmp_path / "suite_manifest.json").read_text())
    entry = next(d for d in manifest["datasets"] if d["dataset"] == "metaboliteannotator")
    assert entry["status"] == "skipped"
    assert "throughput_evidence" in entry["exclusion_record"]


def test_cli_skip_without_only_runs_everything_else(captured):
    assert cli.main(["all", "--skip", "swisslipids=source is a dead-but-200 URL"]) == 0
    assert captured["datasets"] == [k for k in SUITE_DATASETS if k != "swisslipids"]
    assert captured["omitted"] == {"swisslipids": "source is a dead-but-200 URL"}


def test_cli_skip_needs_a_reason(captured):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["all", "--skip", "swisslipids="])
    assert excinfo.value.code == 2


def test_cli_single_arm_records_the_invocation_as_the_reason(captured, tmp_path):
    assert cli.main(["arm", "refmet"]) == 0
    manifest = json.loads((tmp_path / "suite_manifest.json").read_text())
    skipped = {d["dataset"]: d for d in manifest["datasets"] if d.get("skip_origin") == "operator"}
    assert set(skipped) == set(SUITE_DATASETS) - {"refmet"}
    assert all("single-arm invocation (`arm refmet`)" in d["reason"] for d in skipped.values())
