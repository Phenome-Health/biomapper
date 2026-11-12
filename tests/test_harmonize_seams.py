"""Core seams for running the harmonize functions as MCP jobs (1.9.0).

``on_message=`` routes the functions' and report writers' stdout messages to a callable, because
the stdio MCP transport owns stdout. ``pins=`` on ``harmonize_cohorts`` mirrors ``harmonize_many``:
the caller supplies an already-taken pin block and no probe is made. Both default to today's
behaviour.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from biomapper import cohorts as cohorts_module
from biomapper.cohorts import harmonize_cohorts
from biomapper.many_cohorts import harmonize_many
from tests.cohorts_helpers import FakeMapper, by_name, mr

PINS = {"kestrel": {"kestrel_version": "1.0",
                    "kg_build": {"kg_version": "2.3.0", "git_commit": "abc123"}}}
TABLE = {"glucose": mr("glucose", "CHEBI:17234", {"CHEBI": ["17234"]}),
         "urea": mr("urea", "CHEBI:16199", {"CHEBI": ["16199"]})}
A = [{"name": "glucose"}, {"name": "urea"}]
B = [{"name": "glucose"}]
C = [{"name": "urea"}]


def _files(directory: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(directory.iterdir()) if p.is_file()}


def test_default_calls_still_print_the_saved_path_to_stdout(tmp_path, capsys) -> None:
    harmonize_cohorts(A, B, entity="metabolites", mapper=FakeMapper(by_name(TABLE)),
                      probe_pins=False, output_dir=tmp_path / "two")
    assert f"harmonize_cohorts: wrote results to {tmp_path / 'two'}" in capsys.readouterr().out
    harmonize_many({"a": A, "b": B, "c": C}, entity="metabolites",
                   mapper=FakeMapper(by_name(TABLE)), pins=PINS, store_dir=tmp_path / "s",
                   output_dir=tmp_path / "many")
    out = capsys.readouterr().out
    assert f"harmonize_many: wrote results to {tmp_path / 'many'}" in out
    assert f"harmonize_many: run directory {tmp_path / 'many'}" in out


def test_on_message_gets_every_message_and_stdout_stays_empty(tmp_path, capsys) -> None:
    two: list[str] = []
    harmonize_cohorts(A, B, entity="metabolites", mapper=FakeMapper(by_name(TABLE)),
                      probe_pins=False, output_dir=tmp_path / "two", on_message=two.append)
    many: list[str] = []
    report = harmonize_many({"a": A, "b": B, "c": C}, entity="metabolites",
                            mapper=FakeMapper(by_name(TABLE)), pins=PINS,
                            store_dir=tmp_path / "s", output_dir=tmp_path / "many",
                            on_message=many.append)
    assert capsys.readouterr().out == ""
    assert two == [f"harmonize_cohorts: wrote results to {tmp_path / 'two'}"]
    assert f"harmonize_many: wrote results to {tmp_path / 'many'}" in many
    assert any(m.startswith("harmonize_many: saved-mapping store ") for m in many)
    # A later write() of the same report stays quiet too when given the callable.
    again: list[str] = []
    report.write(tmp_path / "again", on_message=again.append)
    assert capsys.readouterr().out == ""
    assert again and again[0] == f"harmonize_many: wrote results to {tmp_path / 'again'}"


def test_harmonize_cohorts_with_pins_makes_no_probe(tmp_path, monkeypatch) -> None:
    def boom(**kw):  # noqa: ANN003, ANN202
        raise AssertionError("pins= was given, so nothing may be probed")

    monkeypatch.setattr(cohorts_module, "collect_pins", boom)
    mapper = FakeMapper(by_name(TABLE))
    report = harmonize_cohorts(A, B, entity="metabolites", mapper=mapper, pins=PINS, save=False)
    assert report.pins["kestrel"] == PINS["kestrel"]
    assert report.pins["mapper"] == "FakeMapper"
    assert "mapper_pins" in report.pins


@pytest.mark.parametrize("quiet", [False, True])
def test_report_files_are_identical_with_and_without_the_new_keywords(tmp_path, quiet) -> None:
    kw = {"on_message": (lambda _m: None)} if quiet else {}
    base = tmp_path / ("quiet" if quiet else "loud")
    harmonize_cohorts(A, B, entity="metabolites", mapper=FakeMapper(by_name(TABLE)),
                      pins=PINS, output_dir=base / "two", **kw)
    reference = tmp_path / "reference"
    if not reference.exists():
        harmonize_cohorts(A, B, entity="metabolites", mapper=FakeMapper(by_name(TABLE)),
                          pins=PINS, output_dir=reference)
    assert _files(base / "two") == _files(reference)
