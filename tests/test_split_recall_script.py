"""Unit 5: the split-recall gate script, offline on a planted answer key."""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from tests.cohorts_helpers import FakeMapper, by_name, mr

pytest.importorskip("pandas")

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "harmonize_many_split_recall.py"
spec = importlib.util.spec_from_file_location("harmonize_many_split_recall", SCRIPT)
assert spec is not None and spec.loader is not None
gate = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = gate  # dataclasses resolve their module through sys.modules
spec.loader.exec_module(gate)

PINS = {"kestrel": {"kestrel_version": "1.0",
                    "kg_build": {"kg_version": "2.3.0", "git_commit": "3dd08a5b"}}}
IK = "WQZGKKKJIJFFOK-GASJEMHNSA-N"

NECS = [
    # (CHEMICAL_NAME, HMDB, INCHIKEY, inchi_key (second vintage))
    ("D-glucose", "HMDB0000122", IK, IK),                                # same node, diff name
    ("lactate", "HMDB0000190", "JVTAAEKCZFNVCJ-REOHCLBHSA-N", ""),       # cross node by id
    ("urea", "HMDB0000294", "XSQUKJJJFZCRTK-UHFFFAOYSA-N", ""),          # unlinked
    ("alanine", "HMDB0000161", "QNAYBMKLOCPYGJ-REOHCLBHSA-N", ""),       # same node, same name
    ("bad key", "HMDB0000123", "not-an-inchikey", ""),                    # screened out
    ("vintage clash", "HMDB0000124", IK, "QNAYBMKLOCPYGJ-REOHCLBHSA-N"),  # screened out
    ("no hmdb", "", IK, ""),                                             # screened out
]
ARIVALE = [
    ("1", "glucose", "HMDB00122"),
    ("2", "L-lactate", "HMDB0000190"),
    ("3", "carbamide", "HMDB0000294"),
    ("4", "Alanine", "HMDB0000161"),
    ("5", "X - 12345", ""),
]
TABLE = {
    "D-glucose": mr("D-glucose", "CHEBI:17234"),
    "glucose": mr("glucose", "CHEBI:17234"),
    "lactate": mr("lactate", "CHEBI:24996", {"HMDB": ["HMDB0000190"]}),
    "L-lactate": mr("L-lactate", "CHEBI:422", {"HMDB": ["HMDB0000190"]}),
    "urea": mr("urea", "CHEBI:16199"),
    "alanine": mr("alanine", "CHEBI:16449"),
    "Alanine": mr("Alanine", "CHEBI:16449"),
}


def write_inputs(tmp_path: Path) -> tuple[Path, str, Path]:
    necs = tmp_path / "necs.csv"
    with necs.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["CHEMICAL_NAME", "HMDB", "INCHIKEY", "inchi_key"])
        w.writerows(NECS)
    arivale = tmp_path / "arivale.tsv"
    with arivale.open("w", newline="", encoding="utf-8") as fh:
        fh.write("# ARIVALE RESEARCH DATA SNAPSHOT\n")
        w = csv.writer(fh, delimiter="\t", lineterminator="\n")
        w.writerow(["CHEMICAL_ID", "BIOCHEMICAL_NAME", "HMDB"])
        w.writerows(ARIVALE)
    return necs, hashlib.sha256(necs.read_bytes()).hexdigest(), arivale


def run_gate(tmp_path: Path, **kw) -> tuple[Path, FakeMapper]:  # noqa: ANN003
    necs, sha, arivale = write_inputs(tmp_path)
    mapper = FakeMapper(by_name(TABLE))
    out = gate.run(necs_path=necs, necs_sha256=sha, arivale_path=arivale,
                   out=tmp_path / "out", mapper=mapper, pins=PINS,
                   store_dir=tmp_path / "store", **kw)
    return out, mapper


def test_planted_pairs_land_where_expected_by_stratum(tmp_path) -> None:
    out, mapper = run_gate(tmp_path)
    result = json.loads((out / "split_recall.json").read_text())
    diff = result["strata"]["different_name"]
    assert diff["counts"] == {"same_group": 1, "cross_node_queue": 1, "neither": 1}
    assert diff["split_evidence"] is True
    same = result["strata"]["name_equal"]
    assert same["counts"] == {"same_group": 1, "cross_node_queue": 0, "neither": 0}
    assert same["split_evidence"] is False
    assert result["answer_key"]["n_pairs"] == 4
    # names only: no identifier ever reaches the mapper
    assert all(not ids for _, ids, _ in mapper.sent)


def test_rows_failing_screening_are_excluded_and_counted(tmp_path) -> None:
    out, _ = run_gate(tmp_path)
    key = json.loads((out / "split_recall.json").read_text())["answer_key"]
    assert key["necs_rows"] == 7 and key["necs_certifiable"] == 4
    assert key["necs_screened_out"] == {
        "missing or malformed gold InChIKey": 1,
        "InChIKey vintages disagree (first block)": 1,
        "no gold HMDB": 1,
    }


def test_a_key_naming_a_row_absent_from_the_panel_is_missing_not_counted(tmp_path) -> None:
    pairs = [gate.TruePair("0|D-glucose", "D-glucose", "1|glucose", "glucose", ("x",)),
             gate.TruePair("0|D-glucose", "D-glucose", "999|ghost", "ghost", ("x",))]
    out, _ = run_gate(tmp_path, answer_key=pairs)
    diff = json.loads((out / "split_recall.json").read_text())["strata"]["different_name"]
    assert diff["missing_from_panel"] == 1
    assert diff["n_pairs"] == 1 and diff["shares"]["same_group"] == 1.0


def test_results_are_saved_with_pins_and_checksums_and_the_path_is_printed(
    tmp_path, capsys
) -> None:
    out, _ = run_gate(tmp_path)
    assert str(out) in capsys.readouterr().out
    result = json.loads((out / "split_recall.json").read_text())
    assert result["run"]["kg_build"] == "2.3.0 (3dd08a5b)"
    assert result["run"]["publication_grade"] == "publishable"
    assert len(result["answer_key"]["checksum_sha256"]) == 64
    assert (out / "pairs.tsv").exists()
    assert (out / "harmonize_many" / "summary.json").exists()


def test_results_default_to_a_timestamped_directory(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    necs, sha, arivale = write_inputs(tmp_path)
    out = gate.run(necs_path=necs, necs_sha256=sha, arivale_path=arivale,
                   mapper=FakeMapper(by_name(TABLE)), pins=PINS, store_dir=tmp_path / "store")
    assert out.parent == (tmp_path / "biomapper_runs").resolve()
    assert out.name.startswith("split_recall_")


def test_a_wrong_sha256_is_refused(tmp_path) -> None:
    necs, _, arivale = write_inputs(tmp_path)
    with pytest.raises(SystemExit, match="refusing"):
        gate.run(necs_path=necs, necs_sha256="0" * 64, arivale_path=arivale,
                 mapper=FakeMapper(by_name(TABLE)), pins=PINS)


def test_main_parses_arguments(tmp_path, monkeypatch) -> None:
    necs, sha, arivale = write_inputs(tmp_path)
    seen = {}
    monkeypatch.setattr(gate, "run", lambda **kw: seen.update(kw))
    assert gate.main(["--necs", str(necs), "--necs-sha256", sha, "--arivale", str(arivale)]) == 0
    assert seen["remap_all"] is True and seen["necs_sha256"] == sha
