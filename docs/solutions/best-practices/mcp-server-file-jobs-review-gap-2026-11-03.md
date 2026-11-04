---
title: "An MCP server's internal review gate passed with 0 blocking while Greptile found six P1s in the file-job lifecycle"
category: best-practices/
module: biomapper/mcp
date: 2026-11-03
problem_type: best_practice
component: background_job
severity: high
applies_when:
  - "Building an agent-facing server (MCP or similar) that runs background jobs on local files"
  - "A pipeline gates on reviewer-assigned severity (blocking == 0) before opening a PR"
  - "A path allow-list protects some, but not all, of the files a job reads and writes"
symptoms:
  - "ce-review returned 6 findings, all P2/P3, blocking=0, and 161 tests passed, so the Validate stage passed"
  - "Greptile's first review of the same code scored 0/5 with 6 P1s and 2 P2s, 4 of them security-tagged"
root_cause: missing_validation
resolution_type: code_fix
related_components:
  - tooling
  - development_workflow
tags:
  - mcp
  - background-jobs
  - path-allow-list
  - cancellation-race
  - review-severity
  - brainstorm-pipeline
---

# An MCP server's internal review gate passed with 0 blocking while Greptile found six P1s in the file-job lifecycle

PR: https://github.com/trentleslie/biomapper/pull/35 (branch `feat/mcp-server`)

## Context

The `/brainstorm` pipeline ("mcp server for the biomapper2 api") built `biomapper-mcp`, a stdio MCP server
over the biomapper2 API:

- Interactive tools (`map_entity`, `map_batch` up to 5, vocab/annotator listings, `server_info`) behind one
  per-process request gate: 10 s wait budget, 50 s deadline (under Claude Desktop's ~60 s cancel), and a
  20-calls/5-min loop guard that redirects the agent to `map_file`.
- Background file jobs (`map_file` / `job_status` / `resume_job` / `cancel_job`): chunks of 5, fsynced
  `EntityCache`, atomic `job.json`, an advisory lock per job, stall detection, auto-resume scoped to the session's
  roots, a path allow-list (MCP roots plus `BIOMAPPER_MCP_ROOTS`), and output that never overwrites.
- Packaging: `mcp>=2.3,<3` optional extra and the `biomapper-mcp` script. `EntityCache` moved to pandas-free
  `biomapper._checkpoint`, and pin collection was factored into `biomapper._pins.collect_pins`.

The SDK surface (MCP 2.3 `MCPServer`, `Resolve(ListRoots)`, how roots behave when the client doesn't support them)
was checked with throwaway scripts against the real wheel before the design depended on it. That step was worth
it: roots handling is the base of the allow-list.

The Validate stage ran `ce-review` and passed: **6 findings, all P2/P3, 0 blocking, 161 tests green**. The PR
opened. Greptile's first review then scored it **0/5**.

## What each reviewer caught

| Finding | ce-review | Greptile |
|---|---|---|
| Cancel accepted during the pin fetch / cache load was overwritten by `state="running"`, so the job ran to completion | P2 | P1 |
| `rerun=true` on a job this process was still running `rmtree`'d its directory under the live worker | P2 | P1 |
| `job_id` used unvalidated as a path component (`..`, absolute paths) | P3 | P2 (security) |
| Provenance JSON `os.replace`'d over an existing file, unlike the never-overwrite TSV | P3 | part of a P1 |
| A `JobError` in `_write_final` led to `_finish` raising `_Stop` outside its handler. That escaped `run_worker` and **tore down the server's lifespan task group**: the agent disconnected and every queued job stopped | missed | P1 |
| `resume` / `auto_resume` re-checked only `input_path` against the current roots, so a job could still write into an output folder the new session no longer allowed | missed | P1 (security) |
| `<output>.partial` opened with `open('a')`, which follows a planted symlink out of the allowed folders | missed | P1 (security) |
| `cancel_job` took no roots: any session that knew a job ID could cancel it or read its absolute paths | missed | P1 (security) |
| The `/health` probe ran outside the request gate, breaking the advertised one-request limit | missed | P2 |
| Same-session resume across a known/unknown KG pin flip raised `CachePinMismatch`, so the job ended as failed | P2 | -- |
| A whole-chunk 5xx caused by one bad row is never bisected, so the job stalls and the other rows in that chunk are never mapped | P2 (manual) | -- |

The fix commit `fe1988b` (`fix(mcp): harden file jobs per Greptile review`, +375/-69, test_jobs.py +168) dealt
with all eight Greptile findings:

- A job failure is recorded without letting `_Stop` escape `run_worker`.
- An active rerun is refused.
- Cancellation is checked again after each await, before the job moves to running.
- Every job path (input, output, partial, provenance) is resolved again and checked against the current roots,
  in status, resume, auto-resume and cancel, and again in the worker before each read and write.
- Partial and provenance files get the same no-overwrite check as the TSV. The partial is appended with
  `O_NOFOLLOW`, and temp files are created with `O_EXCL`.
- `cancel_job` takes the client's roots.
- A job ID must be 16 lowercase hex characters and must match the loaded record.
- The health probe holds the request gate.

**Still open when this was written:**

- Poison-row bisection: no bisect logic is in `jobs.py`.
- Greptile's re-review of `fe1988b`: the last summary edit (18:59Z) came before the push (19:06Z). Don't read
  the 0/5 as the final score, and don't read silence as approval.
- The live prod run and `mcp-publisher` validation of `server.json`, both unchecked in the PR test plan.

## Guidance

1. **Treat a background job as a lifecycle, and review each state change for the three things that broke here:**
   - An await where a concurrent `cancel` / `rerun` can change the state. Check again after every await, before
     you write the state.
   - Control-flow exceptions (`_Stop`, sentinels) raised from inside an `except` branch. They escape the handler
     next to them and can kill the server's task group.
   - Destroying a directory that a live worker still holds handles into. Refuse, or stop and await first.
2. **An allow-list has to cover every path a job touches, at every entry point.** List them: input, final output,
   `.partial`, provenance, temp files, the job dir (from `job_id`). Check each one on every tool that can reach a
   job (status, resume, auto-resume, cancel) and again in the worker right before each I/O. Roots belong to a
   session, not to a job: re-resolve them on resume.
3. **Never follow a path you didn't create.** Use `O_NOFOLLOW` for appends, `O_EXCL` for temp files, and
   no-overwrite for every artifact (not only the main one). Check the format of any ID that becomes a path
   component, and check that the loaded record's ID matches the requested one.
4. **Don't let one reviewer's severity labels gate on their own.** ce-review found 4 of Greptile's 8 issues but
   rated them P2/P3, so `blocking == 0` let them through. For agent-facing servers that write files, raise any
   race, path-escape, or "server dies" finding to blocking, whatever label the reviewer gave it.

## Why This Matters

An MCP server runs inside the agent's process tree, with whatever filesystem reach the user granted. One escaped
exception disconnects the agent in the middle of a task. One unchecked `.partial` symlink writes outside the
folders the user approved. The design was careful in the right places: gate, deadlines, loop guard, fsync, atomic
records. The gaps were in the edges between states and between paths, which a happy-path test suite (161 green)
doesn't reach.

## When to Apply

- Any new MCP tool that starts, resumes, cancels, or reports on long-running local work.
- Any `/brainstorm` run whose Validate stage passes on `blocking == 0` for code that touches the filesystem on an
  agent's behalf. Expect the external review to find more, and plan the Greptile loop as real work, not a
  formality.

## Examples

Cancellation race (before: a cancel during the awaits was overwritten):

```python
pins = await collect_pins_gated(...)            # a cancel can land here
cache = await anyio.to_thread.run_sync(EntityCache, jdir, new_pin)
job.state = "running"                           # clobbers "cancelled"
```

After (`fe1988b`):

```python
cache = await anyio.to_thread.run_sync(EntityCache, jdir, new_pin)
# A cancel accepted during the awaits above must not be overwritten by "running".
self._stop_if_cancelled(job)
```

Related: `docs/solutions/logic-errors/benchmark-harness-reports-plausible-wrong-number-2026-09-23.md`, the same
pattern of a green suite hiding a silent failure, in a different module.
