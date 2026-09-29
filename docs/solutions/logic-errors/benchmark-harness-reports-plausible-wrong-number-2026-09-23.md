---
title: "A benchmark harness reported a plausible wrong number instead of failing"
category: logic-errors/
module: biomapper/benchmarks
date: 2026-09-23
problem_type: logic_error
component: tooling
severity: high
symptoms:
  - "A reordered /map/batch response was joined to held-out gold columns by position, scoring each prediction against a different entity's gold, with no error raised"
  - "A union metric (resolved in ANY target vocabulary) was computed from only the vocabulary passes that succeeded, deflating the score while the arm still reported success"
  - "An arm whose sub-arms partly failed kept dataset status 'ok', so n_ok counted it, n_failed stayed 0, and the CLI exited 0"
root_cause: missing_validation
resolution_type: code_fix
related_components:
  - testing_framework
  - development_workflow
tags:
  - benchmark-harness
  - silent-failure
  - held-out-gold
  - batch-order
  - union-metric
  - partial-run
  - eval-integrity
---

# A benchmark harness reported a plausible wrong number instead of failing

## Problem

The external benchmark suite in `src/biomapper/benchmarks/` could produce a syntactically valid,
plausible-looking accuracy figure that was simply wrong. Three independent defects shared one
shape: each turned an incomplete or misaligned run into a number rather than an error. Because the
suite's output feeds preprint figures, a wrong number that looks right is worse than a crash.

## Symptoms

The defining symptom is the **absence** of a symptom. That is the whole problem.

- No exception, no non-zero exit, no red CI. The suite ran to completion and wrote a manifest with
  a number in it.
- Batch reordering surfaced only as a `RuntimeWarning` ("Batch order mismatch") from
  `map_entities`, which then continued matching results to requests positionally. Nothing
  downstream listened for that warning.
- A union-metric arm with one failed vocabulary pass still produced `arm_status="ok"` and a scored
  result, just a deflated one.
- A multi-sub-arm run (MetaBench subgroups, MetaboliteAnnotator ion modes) with one sub-arm failing
  still incremented `n_ok`, left `n_failed` at `0`, and exited `0`.

Nothing in the scorer, the aggregator, or the exit code could distinguish "measured everything"
from "measured part of it and reported the part as the whole."

## What Didn't Work

- **Realigning a reordered batch by the echoed name.** Rejected. The API echoes back a `name`, and
  a name is not unique within these datasets: the MetaboliteAnnotator arms legitimately carry the
  same name across several source accessions, which is exactly why `merge_vocab_runs` keys on
  `(name, accession)` rather than name alone. There is no stable per-row identity on the wire, so a
  name-based realignment can silently produce a *second* wrong alignment that looks repaired.
- **Retrying a misaligned batch.** Rejected. Re-sending the same batch just re-misaligns it, and
  retrying a partially-successful chunk would double-count the rows that did succeed against the
  deployment. `BatchOrderMismatchError` is deliberately excluded from the retry path.
- **Treating a failed vocabulary pass as a conservative partial measurement.** Rejected. A union
  metric's missing pass can only turn real hits into misses, never the reverse, so scoring the
  survivors is not a safe lower bound. It is a wrong number that still reports success.
- **Trusting a first diagnosis of a silent-success failure.** (auto memory [claude]) When a
  SwissLipids arm previously came back empty-but-successful, the first diagnosis blamed a join bug.
  The real cause was the source returning HTTP 200 with a **zero-byte body**, which a streaming
  adapter read as success. `EmptyDatasetError`'s docstring now names that failure mode directly.
  The lesson is that a silent-success bug's proximate symptom (a confusing pandas error further
  downstream) points at the wrong layer.
- **Assuming a separate review pass had covered this.** (session history) A `/code-review` run
  against the immediately preceding commit produced 13 verified findings in this same package,
  with **zero overlap** with the three defects fixed here. Two independent audits of the same code
  found two disjoint sets of silently-wrong-number bugs. See "Known remaining gaps" below.

## Solution

### 1. Escalate batch-order mismatch from warning to fatal

```python
class BatchOrderMismatchError(RuntimeError):
    """The API returned batch results in a different order than they were sent.

    Fatal here, where it is merely a warning in the client. Predictions are joined to the
    held-out gold columns BY POSITION, so a reordered response scores each prediction against a
    different entity's gold.
    """

# inside _map_chunk, per attempt
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    last = await client.map_entities(chunk, ...)
self._assert_batch_order(caught, chunk)
...
except BatchOrderMismatchError:
    raise  # never retried, never downgraded: the batch is unscorable
```

```python
@staticmethod
def _assert_batch_order(
    caught: list[warnings.WarningMessage], chunk: list[dict[str, Any]]
) -> None:
    mismatches = [str(w.message) for w in caught if "Batch order mismatch" in str(w.message)]
    if not mismatches:
        return
    raise BatchOrderMismatchError(
        f"the API returned {len(mismatches)} of {len(chunk)} batch result(s) out of order,"
        f" and predictions are joined to the held-out gold BY POSITION — so scoring this"
        f" batch would compare each prediction against another entity's gold. Refusing the"
        f" arm. First mismatch: {mismatches[0]}"
    )
```

A second gate at frame assembly catches the case where the marker arrives as a per-record error
instead of an exception. **This path is not reachable today** and is deliberate insurance, not a
live guard: the client emits the mismatch as a warning and still appends a normal result, so the
`_map_chunk` interception always fires first. It exists because that interception depends on
`warnings.catch_warnings`, which mutates global filter state and only holds while chunks are mapped
sequentially.

Reaching it needs **both** of two conditions at once, not either alone — a distinction worth stating
because the first version of this comment implied otherwise. Parallelizing chunk mapping alone is
not enough: under ordinary warning settings the client still appends a normal result and never
populates `error`. Global warnings-as-errors alone is not enough either: the `simplefilter("always")`
inside the harness's own `catch_warnings` block overrides it. It takes parallel mapping *and*
warnings raised as errors, at which point one task's filter state can leak into another's call, the
warning raises inside the client's broad `except`, and it lands as a per-record error:

```python
order_errors = [r.error for r in results if r.error and "Batch order mismatch" in r.error]
if order_errors:
    raise BatchOrderMismatchError(
        f"a batch-order mismatch reached result assembly ({len(order_errors)} row(s)); "
        f"predictions are joined to the held-out gold by position, so this frame cannot be "
        f"scored. First: {order_errors[0]}"
    )
```

### 2. Refuse a union metric computed from a subset of its passes

```python
class IncompleteUnionError(RuntimeError):
    """A metric defined as the union across every target vocabulary lost one of its passes."""


def require_complete_union(
    runs: dict[str, VocabRun], *, key: str, target_vocabs: tuple[str, ...]
) -> list[VocabRun]:
    """Return every vocab pass, refusing the arm if any of them failed."""
    failed = {vocab: run.error for vocab, run in runs.items() if not (run.ok and run.output_tsv)}
    if failed:
        raise IncompleteUnionError(
            f"{key}: target vocab pass(es) {sorted(failed)} failed, so the union across "
            f"{list(target_vocabs)} is incomplete and the metric would be deflated. Withholding "
            f"it rather than reporting a number computed from a subset. Errors: {failed}"
        )
    return list(runs.values())
```

Note `not (run.ok and run.output_tsv)` — a pass that claims success but produced no output is
still not a usable pass.

The signature is typed to the real `VocabRun` dataclass rather than `dict[str, Any]`. Under
`mypy --strict`, `Any` would mean every `run.ok` / `run.output_tsv` / `run.error` access inside the
guard this whole document is about gets zero static checking, so a renamed field would surface only
as a silently mis-scored live run. The tests construct real `VocabRun` instances for the same
reason: a hand-rolled look-alike would not break when the production dataclass changes.

Wired into **both** union arms. The review flagged only MetaboliteAnnotator; the identical defect
was in metLinkR, whose link rate is also "share a canonical id in ANY target vocab":

```python
# run_metaboliteannotator
ok = require_complete_union(runs, key=key, target_vocabs=config.target_vocabs)
# run_metlinkr
ok = require_complete_union(runs, key=METLINKR.key, target_vocabs=METLINKR.target_vocabs)
```

MetaBench is the weaker case: its subgroups may legitimately drop, so instead of refusing it
carries an explicit completeness flag, because its accuracy then covers fewer than the declared
1,000 pairs:

```python
result["complete"] = all(v == "ok" for v in subgroup_status.values())
if not result["complete"]:
    result["incomplete_reason"] = (
        "one or more subgroups failed, so this accuracy covers fewer than the declared "
        "1,000 grounding pairs. See subgroup_status and rows_scored_of_source; do not quote "
        "it as the full benchmark."
    )
```

### 3. Make `partial` a first-class status

```python
arm_status = record.get("arm_status")
failed_sub_arms = sorted(k for k, v in arm_status.items() if v != "ok") if arm_status else []
entry = {
    "dataset": key,
    "status": "partial" if failed_sub_arms else "ok",
    ...
}
if failed_sub_arms:
    entry["failed_sub_arms"] = failed_sub_arms
    entry["reason"] = (
        f"{len(failed_sub_arms)} of {len(arm_status)} sub-arm(s) failed "
        f"({', '.join(failed_sub_arms)}); the reported numbers cover only the "
        f"sub-arms that completed."
    )
```

```python
manifest["n_partial"] = sum(1 for r in results if r["status"] == "partial")
manifest["complete"] = not any(r["status"] in ("failed", "partial") for r in results)
```

```python
# cli.py — a partial run is not a green run
return 1 if (manifest["n_failed"] or manifest["n_partial"]) else 0
```

`skipped` is deliberately carved out. A `SourceUnavailable` (SwissLipids' zero-byte source) is
caught separately, recorded with `status="skipped"` and a reason, does not touch
`n_failed`/`n_partial`, and leaves `complete: True`. A skip is not a zero and not a failure.

## Why This Works

Each fix performs the same conversion: **a silent wrong answer becomes a loud refusal.** A warning
becomes an exception. A deflated union becomes a raised error rather than a smaller-but-reported
number. A part-failed arm becomes a status the aggregator and the exit code can see.

The unifying design move is making **"incomplete" a first-class, distinguishable state** instead
of something a reader has to infer from a number being present. Before, `"ok"` meant only "did not
crash," and a partially-run arm produced output identically shaped to a clean one. After, there are
four states — `ok`, `partial`, `failed`, `skipped` — each with its own meaning, its own effect on
`complete`, and its own effect on the exit code. `skipped` is explicitly not folded into
`failed`/`partial` because a deliberate, reasoned exclusion is a different claim from "this broke,"
and conflating them destroys the signal of both.

The value is not that arms fail more often. It is that a reader, or a scheduled job, can now tell
whether a number covers what it claims to cover without reading the error log.

## Prevention

Transferable to any harness that scores predictions against held-out labels:

1. **Never join predictions to gold by position across a network boundary.** If the service may
   legally reorder, reject the batch rather than trusting alignment. A label-based realignment key
   is only safe if it is provably unique per request, which it usually is not.
2. **Escalate alignment and ordering warnings to exceptions in the harness**, even when the client
   under test only warns. A warning is invisible to anything not specifically watching; watching is
   the harness's job. Corollary: never retry a misalignment. Keep the retryable-error set narrow and
   explicit (a marker allowlist for 5xx / 429 / timeouts) so a semantic error cannot slip into the
   retry path by matching an over-broad check.
3. **For a metric defined as a union or aggregate across passes, refuse the whole metric when a
   pass is missing.** A partial union deflates, it does not approximate. Write one shared guard
   rather than re-implementing it per metric: duplicated logic is exactly how one copy gets the
   check and its sibling does not, which is what happened here (the review caught one arm, not
   both) and what happened previously with the `keys[0]` scorer bug.
4. **Build a status taxonomy with more than two buckets: `ok` / `partial` / `skipped` / `failed`,
   and collapse none of them into `ok` or `failed`.** An `n_ok`/`n_failed` pair cannot represent
   "some sub-units succeeded and some did not," so add the `partial` bucket, add a `complete`
   boolean, and make the exit code reflect sub-unit coverage rather than only top-level exceptions.
   Keep a deliberate skip in its own bucket, carrying a reason and outside the completeness
   calculation — conflating "we chose not to run this" with "this broke" destroys the signal of both.
5. **Refuse to report 0% when nothing was scorable.** Zero accuracy and "we measured nothing" are
   different claims; only one of them is a result.
6. **Name the defect in the test, not just the behaviour.** The test names here encode what
   regressing would mean, so a future failure is legible in the failure message:

```python
def test_a_failed_sub_arm_makes_the_dataset_partial_not_ok(tmp_path):
    """The defect: n_ok counted it, n_failed stayed 0, and the CLI exited green."""
    ...
    assert entry["status"] == "partial"
    assert manifest["n_ok"] == 0
    assert manifest["complete"] is False


def test_a_failed_vocab_pass_withholds_the_union_metric():
    runs = {
        "CHEBI": _VocabRun(ok=True, output_tsv="/tmp/chebi.tsv", error=None),
        "HMDB": _VocabRun(ok=False, output_tsv=None, error="Server error (HTTP 503)"),
    }
    with pytest.raises(IncompleteUnionError) as excinfo:
        require_complete_union(
            runs, key="metaboliteannotator-positive", target_vocabs=("CHEBI", "HMDB")
        )
    assert "deflated" in str(excinfo.value)


def test_a_skip_is_not_a_failure_and_keeps_the_run_complete(tmp_path):
    outcome = run_suite(out_dir=tmp_path, datasets=["swisslipids"], probe_live=False)
    entry = next(d for d in outcome["manifest"]["datasets"] if d["dataset"] == "swisslipids")
    assert entry["status"] == "skipped"
    assert outcome["manifest"]["complete"] is True
```

7. **Assert on message shape, not only on substrings.** The first version of these tests checked
   `"BY POSITION" in message`, which passed against a genuinely garbled exception message: a bad
   line-split had duplicated the whole sentence inside an f-string concatenation. ruff and mypy
   accept that silently because it is valid string concatenation, and the substring assertion was
   true of both. Two independent reviewers caught it; CI did not. `message.count(phrase) == 1` does.

A process note rather than a design rule: **expect more than one disjoint set of these bugs.** Two
independent audits of this package found two non-overlapping sets of silently-wrong-number defects,
and a third pass over the fix itself found a defect in the fix. A single clean review is weak
evidence that a harness reports honestly.

## Known remaining gaps

Not fixed by this change. Both verified by direct check on 2026-09-23, not taken on report:

- **The corrupt-gold predicate is defined but never called.** `gold_structure.has_gold_structure`
  exists specifically to reject the documented corrupt `4000` sentinel and blank keys, and nothing
  in the scoring path calls it. `structure_oracle_scorer.first_block("4000")` returns `"4000"`,
  so a NECS row carrying that sentinel enters the accuracy denominator as a guaranteed miss.
  Confirmed: `grep` finds no caller outside the adapters' unrelated `HAS_STRUCTURE_COL` constant,
  and `first_block("4000") == "4000"` while `has_gold_structure("4000") is False`. This depresses
  the NECS number, so fixing it changes a published figure.
- **RefMet carries two contradictory labels in the same manifest.** `config.REFMET.role` is
  `"accuracy"` (the default, never overridden) while `provenance.circularity_notes` correctly
  labels it `"coverage"` because `refmet` is an ingested graph source. Confirmed by running both.
  LMSD sets `role="capability_regression"` explicitly; RefMet does not.

A further 11 findings from the parallel review (session history) are unverified here and are listed
in that session's output, including a NaN-to-`"nan"` coercion in `metlinkr_scorer._norm` that would
collapse label-less rows into one curator group, HMDB zero-padding in `curie_scorer`, and
`run_suite()` hardcoding `anonymous=True` so an exported `BIOMAPPER_API_KEY` is ignored on
programmatic runs.

## Related Issues

This is a recurring failure shape in this project's benchmark work. Prior siblings, all in the
vault's knowledge store under the `biomapper2` engine's `studies/external_benchmarks`:

- `docs/solutions/logic-errors/keying-a-filtered-subset-renumbers-it-2026-09-23.md` — a resolved
  entity inherited the positional key of a failed one because per-partition indices were reused.
  The same shape as defect 1.
- `docs/solutions/logic-errors/hardening-a-benchmark-gate-against-false-clean-holes-2026-09-01.md`
  — a gate could PASS while a coverage mask was absent or partial. The same shape as defects 2
  and 3.
- `docs/solutions/logic-errors/gated-decision-population-mismatch-and-cache-resumption-2026-08-31.md`
  — a gate compared filtered deltas against an unfiltered noise floor.
- `docs/solutions/best-practices/fail-closed-guards-must-not-no-op-on-absent-input-2026-07-13.md`
  — the prevention-rule precedent, same harness family.
- `docs/solutions/security-issues/llm-publish-gate-validated-class-not-identity-2026-08-06.md` —
  status and exit-code semantics precedent for defect 3.

No GitHub issue documents these three defects; searches of `trentleslie/biomapper`,
`trentleslie/biomapper2` and `Phenome-Health/biomapper2` returned nothing on point.
