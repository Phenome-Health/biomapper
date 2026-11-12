---
title: "Pre-flight for harmonize jobs over MCP: where a node's name comes from, and what was decided at implementation"
category: best-practices/
module: biomapper/mcp
date: 2026-11-11
problem_type: best_practice
component: background_job
severity: medium
applies_when:
  - "Showing a knowledge-graph node to a reviewer by name when the mapping result carries only its CURIE"
  - "Wrapping a synchronous, printing, file-writing function as a background job in a stdio MCP server"
tags:
  - mcp
  - background-jobs
  - harmonize
  - review-items
  - node-names
---

# Pre-flight for harmonize jobs over MCP

Plan: `docs/plans/2026-11-11-001-feat-mcp-review-triage-plan.md`, Unit 0. Phase 1 only.

## Node-name check (needed before Unit 4)

**Question.** Which existing call returns a node's name for a CURIE, and what does it cost?

**Answer, from reading the client (`src/biomapper/client.py`, `src/biomapper/models.py`).**
None does directly. The BioMapper API routes the client uses are `/map/entity`, `/map/batch`, the
dataset routes, the three listings and `/health`. `MappingResult` carries the chosen node's CURIE
(`chosen_kg_id`) and its equivalent ids, and no name. The only place a node name appears is the
Kestrel passthrough: a request with `kestrel_top_n` returns `kestrel_results`, whose rows are
`{id, name, score, ...}`. `get_candidates` (1.7.0) already uses this and marks the row whose `id`
is the engine's choice as `relation: equals`.

**What the capture stage does with that.** To name a node in a link item it re-maps ONE member
row's name with `kestrel_top_n` (the candidates flow) and takes the name of the passthrough row
whose id equals the node's CURIE. Names learned from any re-map in the stage are reused, so a
node that already appeared in a row item's candidates costs nothing.

- Cost: at most one extra engine request per node, the same request `get_candidates` makes
  (measured for 1.7.0 at about 0.5 to 1 s on a warm cache, up to a full map on a cold one).
- When the chosen node is not among the returned rows, the side reads `node_name_status:
  not_found` and shows the CURIE alone. This is the plan's stated fallback. It is never reported
  as "the node has no name".

**Not verified live.** One probe against the production API on 2026-11-11 (`map_entity("glucose",
kestrel_top_n=10)`, keyless) timed out at 60 s. It was not retried, because the deployment wedges
under load. So how often the chosen node is in the top 10 rows on real data is **unmeasured**.
The Phase 1 live check (Unit 5, the operator's step) should record, per link item, how many sides
read `captured` versus `not_found`. If `not_found` is common, raise `capture_top_n` or add a
name-by-CURIE route to the engine before Phase 2 exports node names to experts.

## Expert-in-the-Loop upload check (needed before Unit 7)

**Not done, on purpose.** It is an admin web upload (Trent's step) and belongs to Phase 2, which
this run does not build. The External References section of the plan is still "read from code,
never run".

## Decisions the plan left to implementation

| Question | Decision | Why |
|---|---|---|
| Route stderr progress and warnings too? | No. Only stdout messages go through `on_message`. | stderr is not the transport. Jobs run with `progress=False`. |
| A stale `store.lock` left by a killed server | Reported with its path; never removed by a job. | `StoreLocked` already names the path and the holder's pid. A graceful shutdown releases the lock, because the server now waits for the worker thread. |
| Impact score | Row item: input rows sharing the entity. Link item: row pairs times cohorts touched. Ties: row items first, then by identity. | The plan's starting point; stable, so paging by offset is safe. |
| Capture cap default | 200 requests (`capture_max_entities`), 10 candidates each (`capture_top_n`). | About 3 to 15 minutes at 1 to 4.4 s per request. Cost per entity on prod is still to be measured in the live check. |
| Page size | Default 25, maximum 50 (`review_page_max`). | A compact row is about 300 bytes. |
| Question wording for `mixture` and `id_confidence_differs` links | `get_review_item` returns a `question` per link, with its own wording for those two reasons. | "Same entity?" is not the natural question when one side is a mixture label. |
| Job status model | `HarmonizeStatus` and `JobStatus` share `JobStatusBase`; `resume_job` / `cancel_job` are declared to return the base. | A union return type would wrap the result in `{"result": ...}` and change what a file job returns. The SDK dumps a returned subclass with its own fields. |
| Batch reorder detection | The job records warnings around each request and fails on the client's "Batch order mismatch" warning. | The client only warns and overwrites `query_name` with the request name, so the result itself cannot show a reorder. Only the one job worker does this, one request at a time. |
| Retrying a `failed` harmonize job | Submitting the same call again re-queues it. There is no `rerun`. | A failed job has placed nothing it did not record as its own, and its entity cache is still good. |
| Shutdown | The lifespan flags shutdown, cancels the task group, and releases job locks only after the group (and the worker thread) has exited. | Otherwise the thread could still hold the store lock after the job lock was free. |

## What to watch in the live check

- Count of each item kind, `skipped_no_node`, capture time per entity, total run time (the plan's
  list), plus the `captured` / `not_found` split above.
- One kill-and-resume of the server mid-run: the job should come back `paused`, resume on its
  own, and leave no `store.lock`. A hard kill (SIGKILL) will leave the lock; the next job names it.
