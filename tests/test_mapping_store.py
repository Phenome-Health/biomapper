"""Unit 1: the saved-mapping store behind harmonize_many() (per-arm EntityCache, lock, pin)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from biomapper._mapping_store import (
    LOCK_FILENAME,
    MappingStore,
    StoreBuildMismatch,
    StoreLocked,
    map_requests,
    store_pin,
)
from biomapper.cohorts import request_checksum
from biomapper.models import MappingResult
from tests.cohorts_helpers import FakeMapper, by_name, mr

ETYPE = "biolink:SmallMolecule"


def pins(version: str | None = "2.3.0", commit: str = "3dd08a5b") -> dict:
    kg = {"kg_version": version, "git_commit": commit} if version else None
    return {"kestrel": {"kestrel_version": "1.0", "kg_build": kg}}


TABLE = {
    "glucose": mr("glucose", "CHEBI:17234", {"HMDB": ["HMDB0000122"]}),
    "lactate": mr("lactate", "CHEBI:24996"),
    "urea": mr("urea", "CHEBI:16199"),
    "alanine": mr("alanine", "CHEBI:16449"),
}


def reqs(*names: str, ids: dict | None = None) -> list[dict]:
    return [{"name": n, "identifiers": dict(ids or {})} for n in names]


def open_store(tmp_path: Path, **kw) -> MappingStore:
    kw.setdefault("pins", pins())
    return MappingStore(tmp_path / "store", base_url="https://api", entity_type=ETYPE, **kw)


def run(store: MappingStore, mapper: FakeMapper, requests: list[dict], arm: str = "names_only"):
    return map_requests(
        mapper, requests, store=store.arm(arm), entity_type=ETYPE, annotation_mode="missing"
    )


def test_second_run_with_same_inputs_and_build_sends_nothing(tmp_path) -> None:
    mapper = FakeMapper(by_name(TABLE))
    with open_store(tmp_path) as store:
        first = run(store, mapper, reqs("glucose", "lactate", "urea"))
    assert [s[0] for s in mapper.sent] == ["glucose", "lactate", "urea"]
    assert first.n_fresh == 3 and first.n_reused == 0

    mapper2 = FakeMapper(by_name(TABLE))
    with open_store(tmp_path) as store:
        assert store.reuse_enabled
        second = run(store, mapper2, reqs("glucose", "lactate", "urea"))
    assert mapper2.sent == []
    assert second.n_reused == 3 and second.n_fresh == 0
    assert [r.chosen_kg_id for r in second.results] == ["CHEBI:17234", "CHEBI:24996", "CHEBI:16199"]


def test_new_entities_alone_are_mapped_and_duplicates_within_a_run_are_sent_once(tmp_path) -> None:
    mapper = FakeMapper(by_name(TABLE))
    with open_store(tmp_path) as store:
        run(store, mapper, reqs("glucose", "lactate"))
        out = run(store, mapper, reqs("glucose", "alanine", "alanine", "lactate"))
    assert [s[0] for s in mapper.sent] == ["glucose", "lactate", "alanine"]
    assert out.reused == [True, False, False, True]
    # every row gets its own object even when the request repeats
    assert out.results[1] is not out.results[2]


def test_identifiers_are_part_of_the_key(tmp_path) -> None:
    mapper = FakeMapper(by_name(TABLE))
    with open_store(tmp_path) as store:
        run(store, mapper, reqs("glucose"))
        run(store, mapper, reqs("glucose", ids={"HMDB": ["HMDB0000122"]}), arm="identifier")
        run(store, mapper, reqs("glucose", ids={"HMDB": ["HMDB0000122"]}), arm="identifier")
    assert mapper.sent == [
        ("glucose", {}, "missing"),
        ("glucose", {"HMDB": ["HMDB0000122"]}, "missing"),
    ]


def test_torn_final_line_loads_what_is_intact_and_remaps_the_rest(tmp_path) -> None:
    mapper = FakeMapper(by_name(TABLE))
    with open_store(tmp_path, batch_size=1) as store:
        run(store, mapper, reqs("glucose", "lactate"))
        cache_path = store.arm("names_only").cache.path  # type: ignore[union-attr]
    with cache_path.open("ab") as fh:
        fh.write(b'{"type": "batch", "entries": [{"key": "x"')  # killed mid-write
    data = cache_path.read_bytes().split(b"\n")
    # drop lactate's complete line and keep the torn tail to simulate a kill during lactate
    cache_path.write_bytes(b"\n".join([*data[:2], data[-1]]))
    mapper2 = FakeMapper(by_name(TABLE))
    with open_store(tmp_path) as store:
        out = run(store, mapper2, reqs("glucose", "lactate"))
    assert [s[0] for s in mapper2.sent] == ["lactate"]
    assert out.reused == [True, False]


def test_errored_rows_are_never_saved_and_only_they_are_resent(tmp_path) -> None:
    table = {**TABLE, "lactate": mr("lactate", error="timeout")}
    mapper = FakeMapper(by_name(table))
    with open_store(tmp_path) as store:
        out = run(store, mapper, reqs("glucose", "lactate"))
    assert out.n_errored == 1
    mapper2 = FakeMapper(by_name(TABLE))
    with open_store(tmp_path) as store:
        out2 = run(store, mapper2, reqs("glucose", "lactate"))
    assert [s[0] for s in mapper2.sent] == ["lactate"]
    assert out2.n_errored == 0


def test_unknown_build_disables_reuse_and_says_why(tmp_path) -> None:
    mapper = FakeMapper(by_name(TABLE))
    with open_store(tmp_path) as store:
        run(store, mapper, reqs("glucose"))
    mapper2 = FakeMapper(by_name(TABLE))
    with open_store(tmp_path, pins=pins(None)) as store:
        assert not store.reuse_enabled
        assert "unknown" in (store.reason or "")
        out = run(store, mapper2, reqs("glucose"))
    assert [s[0] for s in mapper2.sent] == ["glucose"]
    assert out.n_reused == 0
    # nothing mapped under an unknown build was written to the store
    with open_store(tmp_path) as store:
        assert len(store.arm("names_only").cache) == 1  # type: ignore[arg-type]


def test_build_mismatch_is_refused_before_any_request(tmp_path) -> None:
    mapper = FakeMapper(by_name(TABLE))
    with open_store(tmp_path) as store:
        run(store, mapper, reqs("glucose"))
    with pytest.raises(StoreBuildMismatch, match="remap_all=True"):
        open_store(tmp_path, pins=pins("2.4.0", "aaaaaaaa"))
    # the refused open must not leave a lock behind
    assert not list((tmp_path / "store").rglob(LOCK_FILENAME))


def test_remap_all_maps_everything_and_replaces_the_saved_build(tmp_path) -> None:
    mapper = FakeMapper(by_name(TABLE))
    with open_store(tmp_path) as store:
        run(store, mapper, reqs("glucose", "lactate"))
    mapper2 = FakeMapper(by_name(TABLE))
    with open_store(tmp_path, pins=pins("2.4.0", "bbbbbbbb"), remap_all=True) as store:
        out = run(store, mapper2, reqs("glucose", "lactate"))
        superseded = list(store.arm("names_only").directory.glob("*.superseded-*"))
    assert [s[0] for s in mapper2.sent] == ["glucose", "lactate"]
    assert out.n_reused == 0
    assert superseded, "the old build's cache is kept aside, not deleted"
    mapper3 = FakeMapper(by_name(TABLE))
    with open_store(tmp_path, pins=pins("2.4.0", "bbbbbbbb")) as store:
        run(store, mapper3, reqs("glucose", "lactate"))
    assert mapper3.sent == []


def test_a_held_lock_refuses_a_second_writer_and_names_the_holder(tmp_path) -> None:
    with open_store(tmp_path), pytest.raises(StoreLocked, match=f"pid {os.getpid()}"):
        open_store(tmp_path)
    # released on exit
    with open_store(tmp_path):
        pass


def test_a_stale_lock_is_reported_not_broken(tmp_path) -> None:
    with open_store(tmp_path) as store:
        lock = store.arm("names_only").directory / LOCK_FILENAME
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": 999999999, "started_utc": "2026-01-01T00:00:00+00:00"}))
    with pytest.raises(StoreLocked, match="not running"):
        open_store(tmp_path)
    assert lock.exists()


@pytest.mark.parametrize("content", ["", "not json", "[1, 2]", '{"pid": "x"}'])
def test_an_unreadable_lock_says_how_to_recover_not_to_wait(tmp_path, content) -> None:
    with open_store(tmp_path) as store:
        lock = store.arm("names_only").directory / LOCK_FILENAME
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(content)
    with pytest.raises(StoreLocked, match="names no holder") as excinfo:
        open_store(tmp_path)
    assert "wait for that run" not in str(excinfo.value)
    assert lock.exists()


def test_a_caching_mapper_returning_one_shared_object_yields_distinct_copies(tmp_path) -> None:
    shared = mr("glucose", "CHEBI:17234")

    def mapper(records, *, entity_type, annotation_mode):  # noqa: ANN001, ANN202
        return [shared for _ in records]

    with open_store(tmp_path) as store:
        out = map_requests(
            mapper, reqs("glucose", "Glucose"), store=store.arm("names_only"),
            entity_type=ETYPE, annotation_mode="missing",
        )
    assert out.results[0] is not out.results[1]
    assert out.results[0] is not shared
    with open_store(tmp_path) as store:
        assert len(store.arm("names_only").cache) == 2  # type: ignore[arg-type]


def test_mapper_returning_the_wrong_count_raises(tmp_path) -> None:
    def short(records, *, entity_type, annotation_mode):  # noqa: ANN001, ANN202
        return [MappingResult(query_name="x")]

    with open_store(tmp_path) as store, pytest.raises(RuntimeError, match="one result per"):
        map_requests(short, reqs("a", "b"), store=store.arm("names_only"),
                     entity_type=ETYPE, annotation_mode="missing")


def test_no_store_maps_everything_without_touching_disk(tmp_path) -> None:
    mapper = FakeMapper(by_name(TABLE))
    out = map_requests(mapper, reqs("glucose", "glucose"), store=None, entity_type=ETYPE,
                       annotation_mode="missing")
    assert [s[0] for s in mapper.sent] == ["glucose"]
    assert out.n_fresh == 2 and out.n_reused == 0


def test_store_pin_matches_the_mcp_cache_pin_shape() -> None:
    pin = store_pin(pins(), base_url="https://api", entity_type=ETYPE, annotation_mode="missing")
    assert pin == {
        "api_endpoint": "https://api", "kestrel_version": "1.0", "kg_version": "2.3.0",
        "kg_git_commit": "3dd08a5b", "entity_type": ETYPE, "annotation_mode": "missing",
    }


def test_request_checksum_is_the_same_for_every_input_kind_and_order_sensitive() -> None:
    rows = [("0|glucose", "glucose", {"HMDB": ["b", "a"]}), ("1|urea", "urea", {})]
    same = [("0|glucose", "glucose", {"HMDB": ["a", "b"]}), ("1|urea", "urea", {})]
    swapped = list(reversed(rows))
    c = request_checksum(rows, arm="names_only")
    assert c == request_checksum(same, arm="names_only")
    assert c != request_checksum(swapped, arm="names_only")
    assert c != request_checksum(rows, arm="identifier")
