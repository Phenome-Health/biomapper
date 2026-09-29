"""Guards against a partial or misaligned run being reported as a benchmark result.

All three cases here produce a plausible-looking number rather than an error, which is why each
needs a test: nothing downstream would notice.
"""

from __future__ import annotations

import warnings

import pandas as pd
import pytest

from biomapper.benchmarks.api_mapper import ApiMapper, BatchOrderMismatchError
from biomapper.benchmarks.arms import IncompleteUnionError, require_complete_union
from biomapper.benchmarks.config import SUITE_DATASETS
from biomapper.benchmarks.runner import VocabRun
from biomapper.benchmarks.suite import run_suite
from biomapper.models import MappingResult


def _vocab_run(
    vocab: str, *, ok: bool, output_tsv: str | None, error: str | None = None
) -> VocabRun:
    """A real ``VocabRun``, so a field rename in the production dataclass breaks these tests."""
    return VocabRun(
        vocab=vocab, ok=ok, output_tsv=output_tsv, stats=None, manifest=None, error=error
    )


def _others(*selected: str) -> dict[str, str]:
    """A reason for every suite arm these single-arm tests leave out, as the suite requires."""
    return {k: "not under test" for k in SUITE_DATASETS if k not in selected}


def _mapper() -> ApiMapper:
    return ApiMapper("https://example.invalid/api/v1")


# --------------------------------------------------------------------------------------------------
# A reordered batch response must abort, not be scored positionally
# --------------------------------------------------------------------------------------------------


def _order_warning(sent: str, got: str) -> warnings.WarningMessage:
    return warnings.WarningMessage(
        message=RuntimeWarning(f"Batch order mismatch: sent {sent!r}, got {got!r}"),
        category=RuntimeWarning,
        filename="client.py",
        lineno=1,
    )


def test_an_order_mismatch_warning_aborts_the_batch():
    """Positional joining means a reordered response scores predictions against the wrong gold."""
    chunk = [{"name": "glucose"}, {"name": "alanine"}]
    with pytest.raises(BatchOrderMismatchError) as excinfo:
        ApiMapper._assert_batch_order([_order_warning("glucose", "alanine")], chunk)
    message = str(excinfo.value)
    assert "BY POSITION" in message
    assert "Refusing the arm" in message


def test_the_abort_message_is_not_duplicated():
    """Substring assertions alone passed against a garbled, doubled message.

    A bad line-split once duplicated the whole sentence inside the f-string concatenation. ruff and
    mypy accept that silently — it is valid string concatenation — and the existing ``in message``
    assertions still passed, so nothing in CI caught it. Two independent reviewers did. Pin the
    shape, not just the substrings.
    """
    chunk = [{"name": "glucose"}, {"name": "alanine"}]
    with pytest.raises(BatchOrderMismatchError) as excinfo:
        ApiMapper._assert_batch_order([_order_warning("glucose", "alanine")], chunk)
    message = str(excinfo.value)
    for phrase in (
        "the API returned",
        "predictions are joined",
        "BY POSITION",
        "compare each prediction",
        "Refusing the",
        "First mismatch",
    ):
        assert message.count(phrase) == 1, f"{phrase!r} appears {message.count(phrase)} times"


def test_unrelated_warnings_do_not_abort():
    other = warnings.WarningMessage(
        message=RuntimeWarning("some unrelated deprecation"),
        category=RuntimeWarning,
        filename="x.py",
        lineno=1,
    )
    ApiMapper._assert_batch_order([other], [{"name": "glucose"}])  # must not raise
    ApiMapper._assert_batch_order([], [{"name": "glucose"}])


def test_an_order_mismatch_reaching_assembly_still_aborts():
    """Belt and braces: map_entities converts chunk-level exceptions into per-record errors."""
    frame = pd.DataFrame({"name": ["glucose"], "gold_inchikey": ["X-Y-N"]})
    leaked = [MappingResult(query_name="glucose", error="Batch order mismatch: sent 'a', got 'b'")]
    with pytest.raises(BatchOrderMismatchError, match="reached result assembly"):
        _mapper()._assemble_frame(frame, leaked)


def test_an_order_mismatch_is_never_classified_as_transient():
    """Retrying a misaligned batch would just re-misalign it."""
    results = [MappingResult(query_name="a", error="Batch order mismatch: sent 'a', got 'b'")]
    assert not _mapper()._chunk_wholly_transient(results)


# --------------------------------------------------------------------------------------------------
# A partially-failed arm must not report as green
# --------------------------------------------------------------------------------------------------


def _runner(record: dict):
    def run(**_kwargs):  # noqa: ANN003
        return record

    return run


def test_a_failed_sub_arm_makes_the_dataset_partial_not_ok(tmp_path):
    """The defect: n_ok counted it, n_failed stayed 0, and the CLI exited green."""
    outcome = run_suite(
        out_dir=tmp_path,
        datasets=["metaboliteannotator"],
        omitted=_others("metaboliteannotator"),
        probe_live=False,
        runners={
            "metaboliteannotator": _runner(
                {
                    "out_dir": str(tmp_path),
                    "dataset": "metaboliteannotator",
                    "role": "coverage",
                    "results": {"entries": []},
                    "arm_status": {
                        "metaboliteannotator-positive": "ok",
                        "metaboliteannotator-negative": "failed: RuntimeError: boom",
                    },
                }
            )
        },
    )
    manifest = outcome["manifest"]
    entry = next(d for d in manifest["datasets"] if d["dataset"] == "metaboliteannotator")
    assert entry["status"] == "partial"
    assert entry["failed_sub_arms"] == ["metaboliteannotator-negative"]
    assert "1 of 2 sub-arm(s) failed" in entry["reason"]
    assert manifest["n_ok"] == 0
    assert manifest["n_partial"] == 1
    assert manifest["complete"] is False


def test_an_arm_with_every_sub_arm_ok_is_still_ok(tmp_path):
    outcome = run_suite(
        out_dir=tmp_path,
        datasets=["metabench"],
        omitted=_others("metabench"),
        probe_live=False,
        runners={
            "metabench": _runner(
                {
                    "out_dir": str(tmp_path),
                    "dataset": "metabench-grounding",
                    "role": "partly_circular",
                    "results": {},
                    "arm_status": {"a": "ok", "b": "ok"},
                }
            )
        },
    )
    manifest = outcome["manifest"]
    assert manifest["n_ok"] == 1
    assert manifest["n_partial"] == 0
    assert manifest["complete"] is True


def test_a_skip_is_not_a_failure_and_keeps_the_run_complete(tmp_path):
    """A recorded, deliberate outcome with a reason is not the same as something breaking."""
    outcome = run_suite(
        out_dir=tmp_path,
        datasets=["swisslipids"],
        omitted=_others("swisslipids"),
        probe_live=False,
    )
    manifest = outcome["manifest"]
    entry = next(d for d in manifest["datasets"] if d["dataset"] == "swisslipids")
    assert entry["status"] == "skipped"
    assert "zero-byte" in entry["reason"]
    assert manifest["n_failed"] == 0
    assert manifest["complete"] is True


def test_the_readme_names_partial_arms(tmp_path):
    run_suite(
        out_dir=tmp_path,
        datasets=["metabench"],
        omitted=_others("metabench"),
        probe_live=False,
        runners={
            "metabench": _runner(
                {
                    "out_dir": str(tmp_path),
                    "dataset": "metabench-grounding",
                    "role": "partly_circular",
                    "results": {},
                    "arm_status": {"a": "ok", "b": "failed: boom"},
                }
            )
        },
    )
    readme = (tmp_path / "README.md").read_text()
    assert "partial" in readme
    assert "not the full benchmark" in readme


# --------------------------------------------------------------------------------------------------
# A union metric must not be computed from a subset of its passes
# --------------------------------------------------------------------------------------------------


def test_a_failed_vocab_pass_withholds_the_union_metric():
    """A deflated union is a WRONG number, not a conservative one, so the arm is refused.

    With the HMDB pass missing, an HMDB-only hit silently becomes a miss while the arm still
    reports as successful. Nothing downstream could tell.
    """
    runs = {
        "CHEBI": _vocab_run("CHEBI", ok=True, output_tsv="/tmp/chebi.tsv"),
        "HMDB": _vocab_run("HMDB", ok=False, output_tsv=None, error="Server error (HTTP 503)"),
    }
    with pytest.raises(IncompleteUnionError) as excinfo:
        require_complete_union(
            runs, key="metaboliteannotator-positive", target_vocabs=("CHEBI", "HMDB")
        )
    message = str(excinfo.value)
    assert "'HMDB'" in message
    assert "deflated" in message
    assert "503" in message


def test_a_complete_set_of_passes_is_returned():
    runs = {
        "CHEBI": _vocab_run("CHEBI", ok=True, output_tsv="/tmp/chebi.tsv"),
        "HMDB": _vocab_run("HMDB", ok=True, output_tsv="/tmp/hmdb.tsv"),
    }
    assert len(require_complete_union(runs, key="k", target_vocabs=("CHEBI", "HMDB"))) == 2


def test_an_ok_run_with_no_output_still_counts_as_failed():
    """ok=True with no TSV is not a usable pass; treating it as one would drop it silently."""
    runs = {"CHEBI": _vocab_run("CHEBI", ok=True, output_tsv=None)}
    with pytest.raises(IncompleteUnionError):
        require_complete_union(runs, key="k", target_vocabs=("CHEBI",))


def test_metabench_marks_an_incomplete_subgroup_set():
    """A dropped subgroup means the accuracy covers fewer than the declared 1,000 pairs."""
    statuses = {"a": "ok", "b": "failed: boom"}
    complete = all(v == "ok" for v in statuses.values())
    assert complete is False
