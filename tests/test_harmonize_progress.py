"""``on_progress=``: structured per-batch progress from both harmonize functions (1.9.0).

A web backend needs the cohort, the arm and done/total as data, through a callback. The stderr
lines of ``progress=True`` and the stdout messages of ``on_message`` are unchanged.
"""

from __future__ import annotations

import json
import warnings

import pytest

from biomapper import HarmonizeProgress, harmonize_cohorts, harmonize_many
from tests.cohorts_helpers import FakeMapper, by_name, mr

PINS = {"kestrel": {"kestrel_version": "1.0",
                    "kg_build": {"kg_version": "2.3.0", "git_commit": "abc123"}}}
NAMES_A = [f"m{i}" for i in range(7)]
NAMES_B = ["m0", "m1", "x"]
TABLE = {n: mr(n, f"CHEBI:{i}", {"CHEBI": [str(i)]}) for i, n in enumerate([*NAMES_A, "x"])}
A = [{"name": n, "hmdb": f"HMDB{i}", "loinc": f"L{i}"} for i, n in enumerate(NAMES_A)]
B = [{"name": n, "hmdb": f"HMDB{i}"} for i, n in enumerate(NAMES_B)]
C = [{"name": "m0"}, {"name": "y"}]


def _events(seen: list[HarmonizeProgress]) -> list[tuple[str, str | None, int, int]]:
    return [(e.arm, e.cohort, e.entities_done, e.entities_total) for e in seen]


def test_two_cohorts_report_every_arm_cohort_and_batch(tmp_path) -> None:
    seen: list[HarmonizeProgress] = []
    harmonize_cohorts(
        A, B, entity="metabolites", a_label="left", b_label="right",
        a_vocabularies={"HMDB": "hmdb", "LOINC": "loinc"}, b_vocabularies={"HMDB": "hmdb"},
        mapper=FakeMapper(by_name(TABLE)), pins=PINS, batch_size=3, save=False,
        on_progress=seen.append,
    )
    assert all(e.function == "harmonize_cohorts" and e.cohorts == () for e in seen)
    assert _events(seen) == [
        ("names_only", "left", 0, 7), ("names_only", "left", 3, 7),
        ("names_only", "left", 6, 7), ("names_only", "left", 7, 7),
        ("names_only", "right", 0, 3), ("names_only", "right", 3, 3),
        # Shared vocabulary HMDB: every row of both cohorts carries a code.
        ("identifier", "left", 0, 7), ("identifier", "left", 3, 7),
        ("identifier", "left", 6, 7), ("identifier", "left", 7, 7),
        ("identifier", "right", 0, 3), ("identifier", "right", 3, 3),
        # One-sided vocabulary LOINC: one request per code, left cohort only.
        ("review", "left", 0, 7), ("review", "left", 3, 7),
        ("review", "left", 6, 7), ("review", "left", 7, 7),
    ]
    json.dumps([e.to_dict() for e in seen])
    assert seen[1].to_dict() == {
        "function": "harmonize_cohorts", "arm": "names_only", "cohort": "left", "cohorts": [],
        "entities_done": 3, "entities_total": 7,
    }


def test_the_callback_changes_how_the_mapper_is_called_but_not_what_is_sent_or_returned() -> None:
    quiet, loud = FakeMapper(by_name(TABLE)), FakeMapper(by_name(TABLE))
    kw = {"entity": "metabolites", "a_vocabularies": {"HMDB": "hmdb"},
          "b_vocabularies": {"HMDB": "hmdb"}, "pins": PINS, "batch_size": 3, "save": False}
    plain = harmonize_cohorts(A, B, mapper=quiet, **kw)
    reported = harmonize_cohorts(A, B, mapper=loud, on_progress=lambda _e: None, **kw)
    # Without a callback each cohort is one mapper call, as before; with one, a call per batch.
    assert [len(c.records) for c in quiet.calls] == [7, 3, 7, 3]
    assert [len(c.records) for c in loud.calls] == [3, 3, 1, 3, 3, 3, 1, 3]
    assert loud.sent == quiet.sent
    assert reported.summary() == plain.summary()
    assert reported.names_only.links == plain.names_only.links


def test_a_pass_with_nothing_to_map_sends_one_zero_update() -> None:
    seen: list[HarmonizeProgress] = []
    harmonize_cohorts(
        A, [{"name": "m0", "hmdb": ""}], entity="metabolites",
        a_vocabularies={"HMDB": "hmdb"}, b_vocabularies={"HMDB": "hmdb"},
        mapper=FakeMapper(by_name(TABLE)), pins=PINS, save=False, on_progress=seen.append,
    )
    assert ("identifier", "b", 0, 0) in _events(seen)
    assert _events(seen).count(("identifier", "b", 0, 0)) == 1


def test_many_cohorts_report_distinct_entities_per_arm(tmp_path) -> None:
    seen: list[HarmonizeProgress] = []
    cohorts = {"a": A, "b": B, "c": C}
    harmonize_many(cohorts, entity="metabolites", mapper=FakeMapper(by_name(TABLE)), pins=PINS,
                   batch_size=4, store_dir=tmp_path / "store", output_dir=tmp_path / "run",
                   on_message=lambda _m: None, on_progress=seen.append)
    assert all(e.function == "harmonize_many" and e.cohort is None for e in seen)
    assert all(e.cohorts == ("a", "b", "c") for e in seen)
    # 12 rows, 9 distinct names (m0..m6, x, y): each mapped once for all cohorts.
    assert _events(seen) == [("names_only", None, 0, 9), ("names_only", None, 4, 9),
                             ("names_only", None, 8, 9), ("names_only", None, 9, 9)]


def test_many_cohorts_count_reused_mappings_as_done_from_the_first_update(tmp_path) -> None:
    kw = {"entity": "metabolites", "pins": PINS, "batch_size": 4,
          "store_dir": tmp_path / "store", "on_message": lambda _m: None}
    harmonize_many({"a": A, "b": B}, mapper=FakeMapper(by_name(TABLE)),
                   output_dir=tmp_path / "one", **kw)
    seen: list[HarmonizeProgress] = []
    second = FakeMapper(by_name(TABLE))
    harmonize_many({"a": A, "b": B, "c": C}, mapper=second, output_dir=tmp_path / "two",
                   on_progress=seen.append, **kw)
    assert second.sent == [("y", {}, "missing")], "only the new name is mapped"
    assert _events(seen) == [("names_only", None, 8, 9), ("names_only", None, 9, 9)]


def test_many_cohorts_without_a_store_still_report_per_batch() -> None:
    seen: list[HarmonizeProgress] = []
    mapper = FakeMapper(by_name(TABLE))
    harmonize_many({"a": A, "b": B}, entity="metabolites", mapper=mapper, pins=PINS,
                   batch_size=4, save=False, on_progress=seen.append)
    assert _events(seen) == [("names_only", None, 0, 8), ("names_only", None, 4, 8),
                             ("names_only", None, 8, 8)]
    assert [len(c.records) for c in mapper.calls] == [4, 4]


def test_many_cohorts_without_a_callback_still_map_in_one_call_when_there_is_no_store() -> None:
    mapper = FakeMapper(by_name(TABLE))
    harmonize_many({"a": A, "b": B}, entity="metabolites", mapper=mapper, pins=PINS,
                   batch_size=4, save=False)
    assert [len(c.records) for c in mapper.calls] == [8]


@pytest.mark.parametrize("many", [False, True])
def test_a_callback_that_raises_warns_once_and_the_run_finishes(many) -> None:
    calls: list[HarmonizeProgress] = []

    def boom(event: HarmonizeProgress) -> None:
        calls.append(event)
        raise RuntimeError("client went away")

    kw = {"entity": "metabolites", "mapper": FakeMapper(by_name(TABLE)), "pins": PINS,
          "batch_size": 3, "save": False, "on_progress": boom}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        report = harmonize_many({"a": A, "b": B}, **kw) if many else harmonize_cohorts(A, B, **kw)
    messages = [str(w.message) for w in caught if "on_progress raised" in str(w.message)]
    assert len(messages) == 1 and "RuntimeError: client went away" in messages[0]
    assert len(calls) == 1, "not called again after it raised"
    assert report.summary()["arms"]["names_only"] is not None


def test_on_message_and_stderr_progress_are_unchanged_beside_the_callback(
    tmp_path, capsys
) -> None:
    messages: list[str] = []
    seen: list[HarmonizeProgress] = []
    harmonize_cohorts(A, B, entity="metabolites", mapper=FakeMapper(by_name(TABLE)), pins=PINS,
                      output_dir=tmp_path / "run", progress=True, on_message=messages.append,
                      on_progress=seen.append)
    captured = capsys.readouterr()
    assert messages == [f"harmonize_cohorts: wrote results to {tmp_path / 'run'}"]
    assert captured.out == ""
    assert "harmonize_cohorts: names-only arm, a (7 rows)" in captured.err
    assert seen
