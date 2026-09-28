"""A killed long arm resumes from its last committed batch instead of restarting at zero.

The motivating loss: the 2026-09-26 MetaboliteAnnotator run spent 11.9 hours on the positive ion
mode, was terminated, and kept nothing, because results reached disk only when a whole vocab pass
finished. These tests kill a run mid-pass and prove the resume sends only what was not yet
answered, produces the same scored frame as an uninterrupted run, and paces only real requests.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from biomapper.benchmarks import api_mapper as api_mapper_module
from biomapper.benchmarks.api_mapper import ApiMapper
from biomapper.benchmarks.arms import _resume_cache
from biomapper.benchmarks.checkpoint import (
    CACHE_FILENAME,
    PROGRESS_FILENAME,
    CachePinMismatch,
    EntityCache,
    cache_key,
)
from biomapper.benchmarks.provenance import KgBuildInfo, RunProvenance
from biomapper.models import MappingResult, RawApiResponse

PIN = {
    "api_endpoint": "https://api.invalid/v1",
    "kestrel_version": "0.3.0",
    "kg_version": "2.3.0",
    "kg_git_commit": "3dd08a5b" + "0" * 32,
    "entity_type": "metabolite",
    "annotation_mode": "all",
}
NAMES = ["glucose", "alanine", "citrate", "lactate", "urea"]
# The options ApiMapper folds into every key for the _map() calls below.
OPTS = {"entity_type": "metabolite", "annotation_mode": "all"}


class Killed(BaseException):
    """Stands in for SIGTERM. A BaseException, so no ``except Exception`` absorbs it."""


def _answer(name: str, vocab: str | None) -> MappingResult:
    return MappingResult(
        query_name=name,
        resolved=True,
        primary_curie=f"{vocab}:{name}",
        chosen_kg_id=f"{vocab}:{name}",
        identifiers={str(vocab): [name]},
        raw_response=RawApiResponse(),
    )


class FakeClient:
    """Records every batch sent. Optionally dies after ``die_after`` batches, or errors a name."""

    sent: list[list[str]] = []
    die_after: int | None = None
    error_names: set[str] = set()

    def __init__(self, **_kwargs):  # noqa: ANN003
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):  # noqa: ANN002
        return False

    async def map_entities(self, chunk, *, vocab=None, **_kwargs):  # noqa: ANN001, ANN003
        if FakeClient.die_after is not None and len(FakeClient.sent) >= FakeClient.die_after:
            raise Killed
        FakeClient.sent.append([r["name"] for r in chunk])
        return [
            MappingResult(query_name=r["name"], error="HTTP 404 not found")
            if r["name"] in FakeClient.error_names
            else _answer(r["name"], vocab)
            for r in chunk
        ]


class CountingPacer:
    def __init__(self) -> None:
        self.waits = 0

    def wait(self) -> None:
        self.waits += 1


@pytest.fixture(autouse=True)
def fake_client(monkeypatch):
    FakeClient.sent = []
    FakeClient.die_after = None
    FakeClient.error_names = set()
    monkeypatch.setattr(api_mapper_module, "BioMapperClient", FakeClient)
    return FakeClient


def _frame() -> pd.DataFrame:
    return pd.DataFrame({"name": NAMES, "gold_chebi": [f"CHEBI:{i}" for i in range(len(NAMES))]})


def _map(mapper: ApiMapper, out_dir, vocab: str = "CHEBI"):
    return mapper.map_dataset_to_kg(
        dataset=_frame(),
        entity_type="metabolite",
        name_column="name",
        provided_id_columns=[],
        vocab=vocab,
        annotation_mode="all",
        output_dir=out_dir,
        output_prefix=f"t_{vocab}",
    )


def _mapper(cache_dir=None, pacer=None) -> ApiMapper:
    cache = EntityCache(cache_dir, PIN) if cache_dir is not None else None
    return ApiMapper("https://api.invalid/v1", batch_size=2, cache=cache, pacer=pacer)


# --------------------------------------------------------------------------------------------------
# Kill, then resume
# --------------------------------------------------------------------------------------------------


def test_a_killed_pass_resumes_from_the_last_committed_batch(tmp_path, fake_client):
    fake_client.die_after = 2  # two batches (4 names) land, the third request is killed
    with pytest.raises(Killed):
        _map(_mapper(tmp_path / "mode"), tmp_path / "out")
    assert fake_client.sent == [["glucose", "alanine"], ["citrate", "lactate"]]

    progress = json.loads((tmp_path / "mode" / PROGRESS_FILENAME).read_text())
    assert progress["CHEBI"]["batches_attempted"] == 2
    assert progress["CHEBI"]["complete"] is False

    fake_client.die_after = None
    fake_client.sent = []
    resumed = _mapper(tmp_path / "mode")
    assert resumed.cache.loaded_from_disk == 4
    tsv, stats = _map(resumed, tmp_path / "out")

    # Only the unanswered entity went back to the deployment.
    assert fake_client.sent == [["urea"]]
    assert resumed.counters.cache_hits == 4
    assert resumed.counters.entities == 1
    assert stats["n_rows"] == len(NAMES)
    assert json.loads((tmp_path / "mode" / PROGRESS_FILENAME).read_text())["CHEBI"]["complete"]

    # And the scored frame is identical to a run that was never interrupted.
    fake_client.sent = []
    clean_tsv, _ = _map(_mapper(), tmp_path / "clean")
    pd.testing.assert_frame_equal(pd.read_csv(tsv, sep="\t"), pd.read_csv(clean_tsv, sep="\t"))


def test_a_completed_pass_reassembles_with_zero_requests(tmp_path, fake_client):
    _map(_mapper(tmp_path / "mode"), tmp_path / "out")
    fake_client.sent = []
    again = _mapper(tmp_path / "mode")
    _map(again, tmp_path / "out")
    assert fake_client.sent == []
    assert again.counters.cache_hits == len(NAMES)
    assert again.counters.batches == 0


def test_the_cache_is_keyed_on_name_and_target_vocab(tmp_path, fake_client):
    mapper = _mapper(tmp_path / "mode")
    _map(mapper, tmp_path / "out", vocab="CHEBI")
    fake_client.sent = []
    _map(mapper, tmp_path / "out", vocab="HMDB")
    # A CHEBI answer is never served for an HMDB request.
    assert sum(len(b) for b in fake_client.sent) == len(NAMES)
    assert cache_key("glucose", "CHEBI", {}) != cache_key("glucose", "HMDB", {})


def test_errors_are_never_cached_so_a_resume_retries_them(tmp_path, fake_client):
    fake_client.error_names = {"citrate"}
    _map(_mapper(tmp_path / "mode"), tmp_path / "out")
    fake_client.error_names = set()
    fake_client.sent = []
    resumed = _mapper(tmp_path / "mode")
    tsv, _ = _map(resumed, tmp_path / "out")
    assert fake_client.sent == [["citrate"]]
    assert pd.read_csv(tsv, sep="\t")["mapping_error"].isna().all()


def test_interleaved_hits_keep_input_order(tmp_path, fake_client):
    cache = EntityCache(tmp_path / "mode", PIN)
    for i, name in enumerate(["alanine", "lactate"]):
        cache.commit_batch(
            vocab="CHEBI",
            batch_index=i,
            keyed_results=[(cache_key(name, "CHEBI", {}, OPTS), _answer(name, "CHEBI"))],
        )
    mapper = ApiMapper("https://api.invalid/v1", batch_size=2, cache=cache)
    tsv, _ = _map(mapper, tmp_path / "out")
    assert fake_client.sent == [["glucose", "citrate"], ["urea"]]
    frame = pd.read_csv(tsv, sep="\t")
    # Gold is joined by position, so each row's prediction must be its own name's.
    assert list(frame["chosen_kg_id"]) == [f"CHEBI:{n}" for n in NAMES]


# --------------------------------------------------------------------------------------------------
# Pacing: the request path only (PR #9)
# --------------------------------------------------------------------------------------------------


def test_pacing_fires_once_per_request_and_never_for_a_cache_hit(tmp_path):
    pacer = CountingPacer()
    _map(_mapper(tmp_path / "mode", pacer=pacer), tmp_path / "out")
    assert pacer.waits == 3  # 5 names at batch_size 2 -> 3 requests

    replay = CountingPacer()
    _map(_mapper(tmp_path / "mode", pacer=replay), tmp_path / "out")
    assert replay.waits == 0


# --------------------------------------------------------------------------------------------------
# Durability and pin safety
# --------------------------------------------------------------------------------------------------


def test_committing_the_same_batch_twice_writes_nothing_new(tmp_path):
    cache = EntityCache(tmp_path, PIN)
    keyed = [(cache_key("glucose", "CHEBI", {}), _answer("glucose", "CHEBI"))]
    assert cache.commit_batch(vocab="CHEBI", batch_index=0, keyed_results=keyed) == 1
    size = (tmp_path / CACHE_FILENAME).stat().st_size
    assert cache.commit_batch(vocab="CHEBI", batch_index=0, keyed_results=keyed) == 0
    assert (tmp_path / CACHE_FILENAME).stat().st_size == size


def test_raw_response_is_not_cached_and_hits_agree_before_and_after_a_restart(tmp_path):
    cache = EntityCache(tmp_path, PIN)
    key = cache_key("glucose", "CHEBI", {})
    cache.commit_batch(
        vocab="CHEBI", batch_index=0, keyed_results=[(key, _answer("glucose", "CHEBI"))]
    )
    reloaded = EntityCache(tmp_path, PIN).get(key)
    assert reloaded is not None
    assert reloaded.raw_response is None
    assert cache.get(key) == reloaded


def test_a_resume_against_a_different_build_is_refused(tmp_path):
    EntityCache(tmp_path, PIN)
    with pytest.raises(CachePinMismatch, match="kg_git_commit"):
        EntityCache(tmp_path, {**PIN, "kg_git_commit": "f" * 40})


def test_a_torn_final_line_is_dropped_and_appends_continue_cleanly(tmp_path):
    cache = EntityCache(tmp_path, PIN)
    key = cache_key("glucose", "CHEBI", {})
    cache.commit_batch(
        vocab="CHEBI", batch_index=0, keyed_results=[(key, _answer("glucose", "CHEBI"))]
    )
    with (tmp_path / CACHE_FILENAME).open("a") as fh:
        fh.write('{"type": "batch", "entries": [{"key"')  # killed mid-write

    resumed = EntityCache(tmp_path, PIN)
    assert resumed.get(key) is not None
    key2 = cache_key("urea", "CHEBI", {})
    resumed.commit_batch(
        vocab="CHEBI", batch_index=1, keyed_results=[(key2, _answer("urea", "CHEBI"))]
    )
    assert EntityCache(tmp_path, PIN).get(key2) is not None


def test_a_corrupt_middle_line_is_refused_not_skipped(tmp_path):
    EntityCache(tmp_path, PIN)
    with (tmp_path / CACHE_FILENAME).open("a") as fh:
        fh.write("not json\n")
        fh.write(json.dumps({"type": "batch", "entries": []}) + "\n")
    with pytest.raises(ValueError, match="corrupt rather than torn"):
        EntityCache(tmp_path, PIN)


def _provenance(*, pinned: bool) -> RunProvenance:
    return RunProvenance(
        run_id="test",
        biomapper_version="1.5.3",
        api_endpoint=PIN["api_endpoint"],
        kestrel_url="https://kestrel.invalid/api",
        kestrel_version=PIN["kestrel_version"] if pinned else "unknown",
        kg_build=KgBuildInfo(
            kg_version=PIN["kg_version"] if pinned else "unknown",
            git_commit=PIN["kg_git_commit"],
        ),
    )


def test_the_arm_only_resumes_from_a_pinned_build(tmp_path):
    cache = _resume_cache(tmp_path / "positive", _provenance(pinned=True), entity_type="metabolite")
    assert cache is not None
    assert cache.pin == PIN
    # Unknown == unknown, so an unpinned cache could not tell two builds apart. No cache at all.
    assert (
        _resume_cache(tmp_path / "x", _provenance(pinned=False), entity_type="metabolite") is None
    )


# --------------------------------------------------------------------------------------------------
# Greptile round 1 (PR #19)
# --------------------------------------------------------------------------------------------------


def test_a_resume_with_different_request_options_misses(tmp_path, fake_client):
    cache_dir = tmp_path / "mode"
    _map(_mapper(cache_dir), tmp_path / "out")
    fake_client.sent = []
    mapper = _mapper(cache_dir)
    mapper.map_dataset_to_kg(
        dataset=_frame(),
        entity_type="metabolite",
        name_column="name",
        provided_id_columns=[],
        vocab="CHEBI",
        annotation_mode="all",
        output_dir=tmp_path / "out2",
        output_prefix="t",
        candidate_limit=3,
    )
    assert sum(len(b) for b in fake_client.sent) == len(NAMES)


def test_progress_is_not_complete_while_errors_are_outstanding(tmp_path, fake_client):
    fake_client.error_names = {"citrate"}
    _map(_mapper(tmp_path / "mode"), tmp_path / "out")
    progress = json.loads((tmp_path / "mode" / PROGRESS_FILENAME).read_text())["CHEBI"]
    assert progress["batches_attempted"] == progress["n_batches"]
    assert progress["entities_cached"] == len(NAMES) - 1
    assert progress["entities_outstanding"] == 1
    assert progress["complete"] is False


def test_a_torn_pin_line_is_replaced_before_any_answer_is_cached(tmp_path):
    (tmp_path / CACHE_FILENAME).write_text('{"type": "pin", "pin": {"api_end')
    cache = EntityCache(tmp_path, PIN)
    key = cache_key("urea", "CHEBI", {})
    cache.commit_batch(
        vocab="CHEBI", batch_index=0, keyed_results=[(key, _answer("urea", "CHEBI"))]
    )
    # The re-written pin still protects those answers from a different build.
    with pytest.raises(CachePinMismatch):
        EntityCache(tmp_path, {**PIN, "kg_git_commit": "f" * 40})
    assert EntityCache(tmp_path, PIN).get(key) is not None


def test_answers_with_no_pin_before_them_are_refused(tmp_path):
    line = {"type": "batch", "entries": [{"key": "k", "result": {"query_name": "x"}}]}
    (tmp_path / CACHE_FILENAME).write_text(json.dumps(line) + "\n")
    with pytest.raises(CachePinMismatch, match="precede any build pin"):
        EntityCache(tmp_path, PIN)


def test_a_tail_torn_inside_a_multibyte_character_still_resumes(tmp_path):
    cache = EntityCache(tmp_path, PIN)
    key = cache_key("glucose", "CHEBI", {})
    cache.commit_batch(
        vocab="CHEBI", batch_index=0, keyed_results=[(key, _answer("glucose", "CHEBI"))]
    )
    torn = json.dumps({"type": "batch", "entries": [{"key": "\u03b2-alanine"}]}, ensure_ascii=False)
    raw = torn.encode("utf-8")
    cut = raw.index("\u03b2".encode()) + 1  # split the 2-byte beta in half
    with (tmp_path / CACHE_FILENAME).open("ab") as fh:
        fh.write(raw[:cut])
    resumed = EntityCache(tmp_path, PIN)
    assert resumed.get(key) is not None
    key2 = cache_key("\u03b2-alanine", "CHEBI", {})
    resumed.commit_batch(
        vocab="CHEBI", batch_index=1, keyed_results=[(key2, _answer("\u03b2-alanine", "CHEBI"))]
    )
    assert EntityCache(tmp_path, PIN).get(key2) is not None


def test_a_version_without_a_kg_commit_gets_no_cache(tmp_path):
    provenance = _provenance(pinned=True)
    provenance.kg_build.git_commit = "unknown"
    assert provenance.pinned  # the case: versions known, commit not
    assert _resume_cache(tmp_path, provenance, entity_type="metabolite") is None
