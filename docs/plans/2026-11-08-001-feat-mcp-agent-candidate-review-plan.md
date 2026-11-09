---
title: "feat: agent-in-the-loop candidate review in the MCP server (phased: spike, v1 rescue, gated human flows)"
type: feat
status: active
date: 2026-11-08
origin: docs/brainstorms/2026-11-08-mcp-agent-rerank-requirements.md
---

# feat: agent-in-the-loop candidate review in the MCP server

## Overview

The feature is delivered in three phases, and each phase is gated on the one before it.

- **Phase A, Unit 0 (spike, no code shipped).** Measure whether the premise holds:
  - how often the right answer is in the top-N candidates for weak rows (hit@N, per weak
    trigger);
  - how often re-mapping a weak row changes the engine's choice (the `run_mismatch` rate).

  Go/no-go decides Phase B.
- **Phase B, v1 (`biomapper` 1.7.0, Units 1-3 plus release).** For rescue:
  - mapping flags weak rows with a reference;
  - `get_candidates` returns verified, compact candidates;
  - the agent records suggestions to a sidecar.

  The main output and engine choice are never changed.
- **Phase C (designed here, built only if gated in).** Human judgments via MCP elicitation:
  endorsement, which is never label-grade, and blind adjudication, which is. Plus export with
  population metadata. The gate is the Phase A numbers, Phase B usage, and an explicit check that
  MCP weak-row adjudications could overlap the ablation population.

## Problem Frame

Weak results (unresolved, refused, uncorroborated, contradicted) leave the researcher's agent with
nothing to act on, although the engine can already return candidates without changing its
selection. Two premises have not been tested:

- the right answer is often in the candidate list (rescue);
- re-maps reproduce the original choice often enough to attach candidates to it.

Both are cheap to measure, so the plan does that first (see origin).

## Requirements Trace

| Requirement | Phase / Unit |
|---|---|
| Premise checks (hit@N, mismatch rate) | A / 0 |
| R1 weak definition, flag, reference; R5 engine unchanged; R6 file-job refs | B / 1 |
| R2-R4 get-candidates (re-map, verify, compact, statuses, sanitize); R15 load bounds | B / 2 |
| R7 suggestions; R12 store; R13 sidecar (suggestions only in v1) | B / 3 |
| R16 guidance, release | B / 6 |
| R8-R11 endorsement, blind adjudication, provenance, trust boundary; R14 export | C / 4-5 (gated) |

Origin success criteria 1-3 and 7 (endorse, blind, export) belong to Phase C. Criteria 4-6
(strong rows, main TSV, deadline) are verified in Phase B.

## Scope Boundaries

- No server-side LLM reranker and no change to biomapper2 (the existing passthrough is used
  as-is).
- No claim that any output is ablation input. No upstream submission.
- Nothing from suggestions or judgments reaches the main output TSV or harmonization.
- Phase C is not built in this delivery unless explicitly gated in.

### Deferred to Separate Tasks

- Phase C (Units 4-5), behind the gate above.
- Tamper-evidence for stored judgments (a Phase C decision).
- Wiring exports into the annotation-reranking ablation harness.

## Context & Research

### Relevant Code and Patterns

- `src/biomapper/mcp/server.py`:
  - `_gated()`: `anyio.fail_after(tool_deadline_s)` around one gate hold. It converts timeouts,
    `GateBusy` and `BioMapperError` into a ToolError, so it cannot return partial results.
  - `Resolve(_want_roots)` / `ListRoots`: the capability-gated resolver pattern.
  - The lifespan task group.
- `src/biomapper/mcp/tools.py`:
  - `EntityResult` and `shape_result()`;
  - `entity_status()`, `certificate_state`, `mcp_confidence_tier()`;
  - `_trim_equivalents()`, which truncates. Equivalence must use the full
    `MappingResult.kg_equivalent_ids`.
- `src/biomapper/mcp/gate.py`:
  - `RequestGate` (one in flight; `gate_wait_s` 10);
  - `LoopGuard` (`DEFAULT_LOOP_LIMIT` 20 per 300 s, one timestamp per call; `release()` removes
    one).
- `src/biomapper/mcp/jobs.py`:
  - `OUTPUT_COLUMNS` and `_output_row()` stay unchanged;
  - `Job.provenance` naming (output name minus `.tsv`);
  - `_within()` (input, output, partial, provenance);
  - `_check_output_free()` and `_place_without_overwrite()`;
  - `_write_private()` (tmp + `os.replace`, `0600`);
  - `JobLock`;
  - `_map_chunk()` uses the job client's `map_entities` (`/map/batch`, 300 s).
- `src/biomapper/_checkpoint.py`: `cache_key()` folds options. `_read_rows` request options are
  only `entity_type` and `annotation_mode`. `CACHE_EXCLUDED_FIELDS` already drops
  `kestrel_results`.
- `src/biomapper/_pins.py`: the `KESTREL_LABEL` honesty label.
- `src/biomapper/client.py`: `map_entity` (`/map/entity`) and `map_entities` (`/map/batch`), both
  accepting `kestrel_top_n`.
- Engine (read-only): `KestrelSearchResult` rows are UNTRUSTED (`id`, `score`, `name`, `synonyms`,
  `prefixes`, `categories`; `error` enum). Passthrough runs after the batch deadline is disarmed,
  with a 180 s Kestrel timeout.
- Gold sets for Unit 0, available locally from the benchmark suite: Hajjar-100 and NECS (Monti).

### Institutional Learnings

- `docs/solutions/best-practices/mcp-server-file-jobs-review-gap-2026-11-03.md`: give the new
  state explicit tests, and check folder and owner scope on every new tool.
- `docs/solutions/best-practices/mcp-registry-pypi-launch-contract-2026-11-04.md`: no new
  dependencies; everything must work under `uvx biomapper`.
- Vault memory, "Tier3 determinism cache confound": cold and warm runs disagreed. Unit 0 measures
  mismatch on cold and warm re-maps separately.
- Vault memory, "Lipid verifiability ceiling": sum-composition lipids are irreducible. Expect
  refused lipid rows to have no rescue value, and measure it.
- Vault memory, "Keep live benchmarks behind their own gate": Unit 0 runs outside the generic
  pipeline, with a compute budget.

### External References

- MCP Python SDK 2.3 negotiates protocol `2026-07-28`, where server-initiated requests are
  forbidden (`NoBackChannelError`). Elicitation must use `Annotated[..., Resolve(fn)]` returning
  `Elicit(message, schema)`. The SDK sends an `InputRequiredResult` and resumes on the client's
  retry, and resolvers may re-run each round, so they must render identically. Older protocols get
  a standalone mid-call request. Per-call enums via `pydantic.create_model` with `Literal[...]` are
  valid; accept-content that fails the schema raises `ValueError` (verified 2026-11-08).
- Claude Code 2.1.319 implements elicitation and exposes `Elicitation` hooks (the R11 caveat).

### Unit 0 spike result (2026-11-09, prod): GO

Run: `spike_results/weak_candidates_20261109T020458Z/` (`summary.json`, `rows.jsonl`, pins, git
commit). It covered Hajjar-100 plus 100 seeded NECS rows (200 total) in 232 s, and found 41 weak
or probed rows.

| Trigger | n | hit@5/10/25 strict | Engine already right | hit@10 when engine wrong | Warm choice change |
|---|---|---|---|---|---|
| uncorroborated | 24 | 0.67 / 0.79 / 0.83 | 0.75 | 5/6 | 0 |
| contradicted | 6 | 0.83 / 1.00 / 1.00 | 0.50 | 3/3 | 0 |
| unavailable (probe) | 11 | 0.45 / 0.55 / 0.64 | 0.00 | 6/11 | 0 |

Median re-map took about 0.5 s. **Decisions taken from this:**
- `unavailable` counts as weak for small molecules.
- `refused` and `unresolved` stay weak per R1, though the sample had none, so their value is
  unmeasured.
- The tool guidance must keep "the engine is usually right on uncorroborated rows" (75%).
- 8 of the 28 engine errors were not weak (confidently wrong), so they are out of this feature's
  reach.

**Caveats:**
- metabolites only;
- n=41, one run;
- warm path only, since the client cannot force a cold cache;
- hit@N is an upper bound on rescue.

### Measured (2026-11-08, prod, 3 requests)

`map_entity` with `kestrel_top_n` 5 or 10 took **0.7-0.8 s**, used one endpoint
(`hybrid-search`), and returned 5 or 10 rows. This is a lower bound, since the cache was likely
warm. Cholesterol came back with certificate `unavailable`, which is not weak under R1.

## Key Technical Decisions

**Phase A**

- **The spike runs outside the pipeline, behind a compute budget.** It reuses the benchmark
  suite's gold sets and the client's `map_entities`/`map_entity`, and persists results by default.

**Phase B**

- **References are opaque, server-issued, one file per record.** Interactive refs live at
  `refs/<id>.json` under the MCP home. Job refs live at `jobs/<job_id>/refs/<id>.json`, so they
  share the job's lifecycle. All writes go through `_write_private`. Records hold:
  - the original call's context (query, entity type and options);
  - the engine choice, certificate and status;
  - the full equivalent ids;
  - the weak trigger;
  - pins (KG build string plus label);
  - the source endpoint (`entity`, `batch` or `job`), and the original timestamp and age.

  Interactive refs are usable by any server process with the same MCP home and uid. Job refs
  follow `_within()`. Interactive refs expire after 30 days (lazy GC). Ids are random.
- **Weak rows only get a ref.** This deliberately narrows origin R1's "each entity". Strong rows
  serialize identically to 1.6.3: the new `weak`/`ref` fields use
  `Field(default=None, exclude_if=lambda v: v is None)`. The pydantic floor rises to the version
  that supports `exclude_if`. The golden test asserts both `structuredContent` and the text
  content. The tool `outputSchema` gains two optional properties, which is documented.
- **`get_candidates` re-maps through the same endpoint family as the original.** A batch or job
  ref goes through `map_entities` with a one-entity batch; an entity ref goes through
  `map_entity`. Both use the stored options plus `kestrel_top_n=N` (default 10, max 25), at most 3
  refs per call. A new `_gated_partial` helper uses one outer `anyio.move_on_after(deadline)` and a
  per-ref gate hold. It maps each exception class to a reason:
  `fetch_failed(deadline | busy | timeout | http_<code> | transport)`.
  - **Verification:** compare pins first. A different build gives
    `run_mismatch(build_changed)`. Then a different choice or certificate gives
    `run_mismatch(choice_changed)`.
  - **Mismatch recovery:** issue a fresh ref anchored to the re-map, linked as `supersedes` to the
    original, so the agent can carry on deliberately. The original stays untouched.
- **Load bounds:** candidate refs get their own budget of 15 refs per 5 minutes, separate from
  `LoopGuard`, so lookups aren't locked out. A ref is charged when attempted.
- **Suggestions (v1):** `record_suggestion(ref, choice, rationale)`. The choice is validated
  against that ref's latest usable stored set, or the engine's choice, or `none`. The schema has no
  status field. One current suggestion per ref, with superseded history.
- **Sidecars follow the provenance naming rule:** output name minus `.tsv`, plus `.weak.tsv` or
  `.choices.tsv`.
  - `weak.tsv` is checked free at submit (in `_check_output_free`), included in `_within`, and
    placed via `_place_without_overwrite` before the main TSV.
  - `choices.tsv` is server-owned. It carries a header marker and is replaced atomically. It is
    refused if an existing file lacks the marker.
- **Sanitizing:** candidate `name` and synonyms (capped at 120) and the rationale (capped at 500)
  have control characters and newlines stripped and are rendered as quoted data.

**Phase C (designed now, built only if gated in)**

- **Elicitation via `Resolve`/`Elicit`.** The resolver loads the ref and creates or reuses the
  pending record idempotently, keyed by ref plus flow. It stores the shuffle seed before round 1
  and returns `Elicit` (or a ToolError if the capability is absent). The tool body finalizes.
  Latency is measured from the pending record's timestamp. Tests run under both `2025-11-25` and
  `2026-07-28` clients. Cancellation writes `cancelled_by_client` in a shielded `finally`.
  `clientInfo` falls back to `unknown` plus the protocol version.
- **Blind-first:**
  - blind adjudication is refused on refs that already have a suggestion or endorsement;
  - a `blind_first` option locks `record_suggestion` on a ref until its blind record is final;
  - `request_blind_adjudication(job_id=...)` lets the server choose a uniformly random weak ref
    (`selection_mode=server_random`, versus `agent`);
  - one accepted blind adjudication per ref per labeller; repeats are separate, non-superseding
    records with `repeat_index`;
  - prior-suggestion and prior-endorsement flags are recorded.
- **Blind list grouped by equivalence class** (one option per entity, using the full ids). The
  engine's choice is always included, unmarked. The namespace mix shown is recorded. Candidates
  are shuffled with a stored seed. The free-text `other_curie` stays a plain string, so the server
  decides `out_of_list` versus `invalid_entry`.
- **Outcome names:**
  - endorsement: `human_endorsed`, `human_rejected`, `human_changed`;
  - adjudication: `picked`, `none`, `out_of_list`, `invalid_entry`;
  - non-answers: `declined` and `cancelled_by_client`. These are recorded for audit and are never
    accepted judgments (this supersedes origin R10's "leaves no human judgment" wording).
- **Export:**
  - **Contents:** all adjudication attempts, including declined ones.
  - **Denominators per job or session:** weak refs issued, candidates fetched, blind requested,
    accepted, declined.
  - **Population metadata:** entity type, weak trigger, N, `answer_in_list`, labeller role,
    selection mode, prior flags, provenance.
  - **Scope:** the session's allowed folders.
  - **Header:** "not validated as ablation input".

## Open Questions

### Resolved During Planning

- **Protocol path:** use `Resolve`/`Elicit` (Phase C).
- **Re-map endpoint:** match the original family.
- **Store layout:** one file per record.
- **Ref scope:** home plus uid; jobs via `_within`.
- **Limits:** a separate candidate budget.
- **Outcome names and decline recording:** as above.

### Deferred to Implementation

- Which protocol Claude Code 2.1.319 negotiates with an SDK 2.3 stdio server. Both paths are
  tested; the live check confirms which one runs.
- How Claude Code renders long elicitation messages (Phase C live check).
- Exact Unit 0 thresholds are set in Unit 0 from the measured distribution. The default go
  criteria are below.

## Phased Delivery

### Phase A: Unit 0 spike (go/no-go)

### Phase B: v1, Units 1-3 and 6 (`biomapper` 1.7.0)

Phase B starts only on a Phase A **go**.

### Phase C: Units 4-5

Phase C starts only on an explicit decision after Phase B. The decision looks at Phase A
numbers, Phase B usage, and an overlap check against the ablation population.

## Implementation Units

- [ ] **Unit 0 (Phase A): Premise spike: hit@N and run_mismatch rate**

**Goal:** Decide whether candidates can rescue weak rows, and whether re-maps are stable enough to
attach candidates to.

**Requirements:** premise checks for R2 and rescue.

**Dependencies:** None. Runs outside the Workflow pipeline.

**Files:**
- Create: `scripts/spike_weak_candidates.py`, which reuses the benchmark gold-set loaders and
  the client.
- Output: `spike_results/weak_candidates_<UTC>/` holding a per-row JSONL, `summary.json` and the
  pins. Saved by default.
- Test: `tests/test_spike_weak_candidates.py`, offline only, with a respx fixture for the row
  classification and hit@N arithmetic.

**Approach:**
1. Map Hajjar-100 and NECS (subsample to 150 rows total) through `map_entities` (batch 5).
2. Classify weak rows by trigger.
3. For each weak row:
   - re-map warm with `kestrel_top_n=25` (immediately);
   - re-map cold with `kestrel_top_n=25` (after the warm pass, with the cache bypassed where the
     client allows);
   - record:
     - whether the gold CURIE, or an equivalent, is in the top 5/10/25;
     - whether the choice or certificate changed against the original;
     - latency;
     - endpoints used;
     - the candidate status.
4. Summarise hit@N and mismatch rate per weak trigger and per entity type.

**Compute budget:** a timing run on 10 rows first, projected before the full run. Expected full
run is about 150 maps plus about 2 × weak-count re-maps, roughly 25-40 min at about 4.4 s per
entity, under the 1 r/s rate limit. **Stop if the projection exceeds 60 min**; subsample instead.

**Default go criteria:** proceed to Phase B if, for at least one major weak trigger:
- hit@10 ≥ 30%; and
- the warm-path mismatch rate is ≤ 20%.

If refused rows show near-zero hit@N (the irreducible lipids), they are excluded from "weak" for
candidate purposes in v1.

**Test scenarios:**
- Happy path (offline): fixture rows with known gold and candidate lists compute hit@5/10/25
  correctly, including a match through an equivalent id.
- Edge case: a row with candidate status `none_searched` counts as a miss with reason, not an
  error.
- Edge case: a mismatch is detected and categorised by build versus choice.
- Integration: results are persisted under a timestamped dir by default, with pins.

**Verification:** `summary.json` holds hit@N and mismatch per trigger, the go/no-go verdict, and
the run pins. Results are reported to Trent before Phase B dispatch.

- [ ] **Unit 1 (Phase B): Weak detection and references**

**Goal:** Flag weak rows and issue persisted, scoped refs; strong rows are unchanged.

**Requirements:** R1 (weak rows only), R5, R6, R16.

**Dependencies:** Unit 0 go.

**Files:**
- Create: `src/biomapper/mcp/references.py`.
- Modify:
  - `src/biomapper/mcp/tools.py` (`is_weak()`, plus `weak`/`ref` fields with `exclude_if`);
  - `src/biomapper/mcp/server.py` (issue refs in the `map_entity`/`map_batch` paths);
  - `src/biomapper/mcp/jobs.py` (`Job.weak_path`, `_within`, `_check_output_free`, place
    `weak.tsv` before the main TSV, and job refs under the job dir);
  - `pyproject.toml` (pydantic floor).
- Test: `tests/mcp_server/test_references.py`, plus extensions to `test_server_tools.py` and
  `test_jobs.py`.

**Approach:**
- Weak means:
  - status in {unresolved, refused}; or
  - certificate in {uncorroborated, contradicted}; or
  - certificate `unavailable` when the entity type is a small molecule (the Unit 0 result).

  The small-molecule check uses the request's entity type, normalised to Biolink (e.g.
  `biolink:SmallMolecule`, `metabolite`, `lipid`).
- Refs are stored as one file per record with random ids. Interactive refs: home plus uid scope
  and a 30-day TTL. Job refs: under the job dir and `_within`.

**Test scenarios:**
- Happy path: an unresolved result gets `weak: true` and a `ref`, and the record holds the full
  context, pins, source endpoint and timestamp.
- Edge case: for a strong row, `structuredContent` and the text content equal the 1.6.3 golden,
  with no `weak`/`ref` keys.
- Edge case: `contradicted` is weak. `unavailable` is weak for `biolink:SmallMolecule` and its
  aliases, but not for `biolink:Gene` or `biolink:Protein`. `not_applicable` is never weak.
- Edge case: a job's `weak.tsv` is named `<output minus .tsv>.weak.tsv`. An existing `weak.tsv`
  refuses the job at submit, and the main TSV columns are unchanged.
- Error path: unknown, malformed or traversal ref ids are rejected without file access.
- Integration: a job ref outside the allowed folders is refused, and an interactive ref resolves
  in a new server process (restart).
- Integration: an interactive ref older than 30 days is GC'd lazily, and a job ref follows its
  job.

**Verification:** existing tests pass, and the strong-row golden test passes on both content
channels.

- [ ] **Unit 2 (Phase B): `get_candidates`**

**Goal:** Fetch, verify, compact and sanitize candidates. Deadline-safe and budgeted.

**Requirements:** R2, R3, R4, R5, R15, R16.

**Dependencies:** Unit 1.

**Files:**
- Create: `src/biomapper/mcp/candidates.py`.
- Modify:
  - `src/biomapper/mcp/server.py` (the tool, plus `_gated_partial`);
  - `src/biomapper/mcp/gate.py` (a `CandidateBudget`);
  - `src/biomapper/mcp/config.py` (N default 10 / max 25, 3 refs per call, budget 15 per 5 min).
- Test: `tests/mcp_server/test_candidates.py`, plus an extension to `test_gate.py`.

**Approach:**
- Re-map through the original endpoint family with `kestrel_top_n`.
- Pins-first verification; `run_mismatch(build_changed | choice_changed)` issues a superseding
  ref.
- Compact the rows: dedupe by CURIE (max score, list of endpoints); equivalence from the full ids;
  categories as a list.
- Statuses: `present`, `none_searched`, `fetch_failed(reason)`, `run_mismatch(reason)`.
- Store the compact set (N, timestamp, raw hash) on the ref.

**Execution note:** test-first on compaction, statuses, verification and `_gated_partial`.

**Test scenarios:**
- Happy path: rows from 2 endpoints with overlap are deduped with the max score, and the set is
  stored on the ref.
- Happy path: candidates are marked `equals` and `equivalent` (beyond the truncated view) from the
  full ids.
- Edge case: `none_searched` versus `fetch_failed(timeout)` are told apart correctly.
- Edge case: a different build gives `run_mismatch(build_changed)`, and a different choice gives
  `choice_changed`. Each issues a superseding ref, and the original is untouched.
- Edge case: a job or batch ref re-maps via `map_entities` (batch of one); an entity ref via
  `map_entity`.
- Edge case: names and synonyms are sanitized and capped.
- Error path: 4 refs gives a ToolError naming the limit, and a strong ref is refused with
  guidance.
- Error path: a deadline after ref 1 returns ref 1 `present` and the rest
  `fetch_failed(deadline)`. A gate held by a job chunk longer than `gate_wait_s` gives
  `fetch_failed(busy)`. Neither is a ToolError.
- Integration: the 16th candidate ref in 5 minutes is refused, and plain `map_entity` calls are
  unaffected by candidate use.
- Integration: job `cache_key` request options contain only `entity_type` and `annotation_mode`,
  and the job client is never called with `kestrel_top_n`.

**Verification:** a payload of 3 refs × N=25 serializes under 10k tokens (asserted on serialized
length as a proxy).

- [ ] **Unit 3 (Phase B): `record_suggestion` and the v1 sidecar**

**Goal:** Record agent suggestions against the stored set, and write the server-owned
`choices.tsv` for jobs.

**Requirements:** R7, R12, R13 (suggestions only), R16.

**Dependencies:** Units 1, 2.

**Files:**
- Create: `src/biomapper/mcp/judgments.py` (one file per record; suggestion model;
  supersession).
- Modify: `src/biomapper/mcp/server.py` (the tool), `src/biomapper/mcp/jobs.py` (`choices.tsv`
  regeneration with the header marker; `Job.choices_path`; `_within`).
- Test: `tests/mcp_server/test_judgments.py`, plus an extension to `test_jobs.py`.

**Test scenarios:**
- Happy path: a valid CURIE from the stored set produces a suggestion record with full context.
- Edge case: a CURIE from another ref's set is rejected. A `run_mismatch` set can't be used.
  Without any fetch, only the engine's choice or `none` is accepted.
- Edge case: re-recording supersedes, and history is kept.
- Error path: extra keys such as `status` or `human_confirmed` are rejected by the schema.
- Integration: `choices.tsv` is regenerated with only current suggestions plus the marker, and an
  existing `choices.tsv` without the marker is refused. The main TSV is byte-identical.
- Integration: store files are `0600` and dirs `0700`, and the API key value is absent from all
  stored files.

**Verification:** the tool input schema has no status-like fields, and the job main TSV golden
test is unchanged.

- [ ] **Unit 4 (Phase C, gated): Endorsement and blind adjudication**

**Goal, requirements, design:** R8-R11 as specified in the Phase C key decisions above
(`Resolve`/`Elicit`, blind-first, server-random sampling, one adjudication per ref, equivalence
grouping, outcome names, cancellation shielding, clientInfo fallback).

**Files (if built):** `src/biomapper/mcp/elicit.py` plus server tools.

**Test scenarios (if built):**
- Blind-payload exclusion for every field.
- Blind refused after a suggestion or endorsement exists.
- Server-random selection is uniform over un-adjudicated weak refs.
- A repeat blind request creates `repeat_index`, not supersession.
- Both protocol versions, using a mocked client for each.
- Cancellation during the elicit await writes `cancelled_by_client`.
- A full fetch → suggest → endorse flow (origin success criterion 1).

- [ ] **Unit 5 (Phase C, gated): Adjudication export and sidecar extension**

**Design:** the Phase C export decision above (all attempts, denominators, population metadata,
scoped to allowed folders, header disclaimer). `choices.tsv` gains endorsement and adjudication
rows.

**Test scenarios (if built):**
- Export holds every attempt, including declined ones, with denominators.
- Out-of-scope job adjudications are excluded.
- An existing file is refused.
- Labeller role defaults to `unspecified`.

- [ ] **Unit 6 (Phase B): Docs, release 1.7.0, live check**

**Goal:** Document v1 honestly, release it, and verify live.

**Requirements:** R16, plus Phase B success criteria. The README states the spike numbers with
their caveats, and says that the engine is usually right on uncorroborated rows.

**Dependencies:** Units 1-3.

**Files:** `README.md` (a "Reviewing weak results with your agent" section stating that
suggestions are not BioMapper resolutions, plus the candidate budget and that it costs extra
engine work); `CHANGELOG.md` (1.7.0, noting the `outputSchema` additions and the pydantic floor);
`pyproject.toml` and `server.json` versions.

**Operator checklist (Trent):** in Claude Code on the trial folder:
1. Map the file.
2. Run `get_candidates` for 2 weak refs.
3. Record a suggestion.
4. Check `weak.tsv` and `choices.tsv`, and that the main TSV is unchanged.

**Test expectation:** packaging and version tests cover the release.

**Verification:** 1.7.0 is on PyPI and the registry is updated (`mcp-publisher publish`), and the
live checklist passes.

## System-Wide Impact

- **Interaction graph:**
  - mapping tools gain optional fields, on weak rows only;
  - jobs gain `weak.tsv`, plus `choices.tsv` on demand;
  - the engine is unchanged.
- **Error propagation:** candidate failures are per-ref statuses and never alter or fail a mapping
  result.
- **State lifecycle:** one file per record with atomic writes. Interactive refs have a TTL, and
  job refs follow the job.
- **API surface parity:** the client API is unchanged. MCP `outputSchema` gains two optional
  fields (documented).
- **Unchanged invariants:** main TSV columns, entity-cache keys, engine choice, and 1.6.3
  serialization for strong rows.

## Risks & Dependencies

| Risk | Mitigation |
|---|---|
| Premise fails (low hit@N or high mismatch) | Unit 0 go/no-go before any product code |
| Re-map changes choice or build | Pins-first verification; superseding ref so the agent can still proceed |
| Gate contention with running file jobs | `fetch_failed(busy)` per ref, never a ToolError |
| Cold passthrough slower than measured | 3 refs per call, partial results; Unit 0 measures cold latency |
| Anchoring and selection bias in human labels | Phase C blind-first, server-random sampling, one adjudication per ref, denominators |
| Protocol-dependent elicitation | Phase C on `Resolve`/`Elicit`, both protocol versions tested |
| Store growth | Interactive TTL; job refs under the job dir |

## Sources & References

- **Origin document:** [docs/brainstorms/2026-11-08-mcp-agent-rerank-requirements.md](../brainstorms/2026-11-08-mcp-agent-rerank-requirements.md)
- Engine passthrough: biomapper2 #87 / org #116.
- Prior learnings: `docs/solutions/best-practices/mcp-server-file-jobs-review-gap-2026-11-03.md`,
  `mcp-registry-pypi-launch-contract-2026-11-04.md`.
