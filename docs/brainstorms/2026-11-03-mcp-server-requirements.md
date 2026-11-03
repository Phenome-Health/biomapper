---
date: 2026-11-03
topic: mcp-server
---

# BioMapper MCP Server

## Problem Frame

The BioMapper preprint presents the engine (biomapper2) and the Python client (`biomapper`) as tools for researchers. More and more researchers work inside agents (Claude Code, Claude Desktop, other MCP-capable clients), and those agents today can only reach BioMapper by writing ad-hoc HTTP or Python calls. An MCP server gives agents first-class, well-described tools for entity resolution against the public, keyless BioMapper API, and gives the preprint a citable "use it agentically" path alongside the Python client.

The constraint that shapes everything: the public API (`https://biomapper.expertintheloop.io/api/v1`) is a single instance that runs at roughly 4.4 s/entity, degrades superlinearly with batch size, and can wedge under concurrent load. An agent that loops over a tool can take prod down. The server must protect the service as much as it exposes it.

## Requirements

**Distribution**
- R1. Ships inside the `biomapper` PyPI package as a local stdio MCP server, launchable with a single command (e.g. via `uvx`), with no separate install step beyond the package and an optional extra if new dependencies are needed.
- R2. Talks only to the public biomapper2 REST API through the existing client; the API base URL and the Kestrel URL used for KG provenance are both overridable (env vars) so self-hosted deployments pin their own KG. No API key is required by default; an optional key is passed through if set.

**Tools**
- R3. Map one entity: name plus optional identifiers and entity type in; resolved identifiers, confidence/provenance, and unresolved/ambiguous status out, in a compact agent-readable form.
- R4. Map a small batch: a list of entities, hard-capped (default 10, matching the known-safe batch size). Over-cap requests are refused with a tool error response that directs the agent to the file-mapping tool.
- R5. Discovery: list supported entity types, vocabularies, and annotators so an agent can choose valid inputs before mapping.
- R6. Map a local CSV/TSV file as a background job: the call validates the file and the output location (refusing up front if it is not writable) and returns immediately with a job id, row count, and a best-effort ETA. Output defaults to a sibling of the input (never overwriting it) with an optional output-path override. The job submits chunked batch calls of at most 10 rows, persisting each chunk before the next; the single-request dataset-stream endpoint is not used because it holds one request open for the whole file, cannot resume, and omits confidence scores. Every mapped row is cached on disk so the job resumes after a crash or restart.
- R7. Job status: given a job id, report progress (rows done / total), a best-effort ETA updated from the observed rate, output path, and state (running, paused, complete, failed with reason, or stalled).

**Service protection**
- R8. At most one in-flight request to the API per server process. File jobs run one at a time and release the request slot between chunks, so interactive calls interleave with a running job instead of waiting for it to finish. A job is owned by one process at a time (lock), so two concurrent sessions cannot run or resume the same job.
- R9. Before starting a file job, the server checks API health (only when it has no request of its own in flight, with a fixed timeout) and refuses with a clear message if the API is slow or unresponsive. During a job, repeated slow or failed chunks move the job to `stalled` and stop sending requests rather than adding load to a degrading service.
- R16. A rolling-window guard on interactive mapping: after roughly 20 single-entity or batch calls within a few minutes, further calls are refused with an error directing the agent to the file-mapping tool. Descriptions (R10) are advice; this is the control.
- R10. Tool descriptions tell the agent the throughput reality (seconds per entity, prefer discovery first, use file mapping for anything over the batch cap) so agents plan correctly without trial and error.

**Provenance**
- R11. Every output file, and the server's provenance for interactive results, records pins with the same semantics and honesty labels as the client's existing cohort harmonization pins: client version; API health plus its self-reported version labeled as known-stale; Kestrel version and KG build labeled as assumed, not verified, to be what the API used. Provenance fetch errors are recorded, never fatal. The server never presents an unverified KG build as verified.

**Prerequisite: server-side rate limit (biomapper2, separate PR)**
- R15. The biomapper2 API gets a per-client rate limit or request queue so aggregate load from many independent MCP users cannot wedge the single prod instance. Client-side protection (R8, R16) only limits each user; this limits the total. Must be live on prod before the registry listing (R14).

**Release and discoverability**
- R12. Released in a tagged `biomapper` PyPI version that the preprint's Software Availability section can cite.
- R13. README section with copy-paste configuration for Claude Code and Claude Desktop, plus one worked example agent session suitable for the supplement.
- R14. Listed in the official MCP registry, only after R15 is live on prod.

## Success Criteria

- A researcher with only `uv` installed can add BioMapper to Claude Code or Claude Desktop from the README and map an entity in under 5 minutes.
- Recorded live end-to-end runs against prod in both Claude Code and Claude Desktop, covering discovery, single, batch, and a file job through to completion.
- Agent-behavior check: with a fixed prompt ("map this 500-row file"), at least 4 of 5 trials per client (Claude Code, Claude Desktop) start a background job and poll status, with no more than 3 single-entity calls per trial. The R16 guard fires in a deliberate looping test.
- Prod `/health` answers within 15 s when probed between chunks of a running job (the engine's mapping routes block their worker while a request is in flight, so probes during a chunk are not a valid measure).
- The tagged PyPI version and registry entry exist and are referenced in the preprint draft.

## Scope Boundaries

- No hosted/remote MCP endpoint in v1. Deferred until prod has rate limiting (it binds `0.0.0.0` with no limits today).
- No changes to the biomapper2 engine's mapping behavior or API surface; the only engine change is the R15 rate limit, shipped as its own PR.
- No raw Kestrel/KG query passthrough tool; Kestrel has its own MCP.
- No cross-cohort harmonization tools (`biomapper.harmonize`) in v1.
- No parallel or multi-job scheduling; one job at a time is sufficient and safer.

## Key Decisions

- Local stdio in the PyPI client over hosted remote: ships the preprint claim without increasing load-risk on the single prod instance, and makes the version citable.
- Background job + status for files over a synchronous cap: real research files run hours; a blocking tool call would time out, and a row cap would push the most valuable use case out of the agent.
- Conservative concurrency (one in-flight request) baked in: protecting prod matters more than throughput for a public tool.
- Server-side rate limit before public listing: per-process limits cannot bound aggregate load from many researchers, and publicizing via preprint + registry raises traffic.
- Auto-resume on server start over an explicit resume tool: jobs usually outlive a session, and an agent in a new session will not know a job is waiting.

## Dependencies / Assumptions

- The public API remains keyless (verified in prod 2026-08-24; `api/auth.py` opens access when no keys are configured).
- The client already has a pin-checked, fsynced, error-free resumable entity cache (`src/biomapper/benchmarks/checkpoint.py`, `EntityCache` + `cache_key`). It is reused rather than redesigned, but it currently imports through the pandas-dependent benchmarks package, so it must move to a core module the way `_provenance.py` did.
- The MCP server must use the async client directly (the sync helpers call `asyncio.run` and fail inside the server's event loop), and enforce the 10-row cap itself (the client chunks at 1000).
- Background jobs live only as long as the MCP server process runs. Because real jobs usually outlive one agent session, pause-and-resume is the main path, not a crash fallback: when the agent client closes, the job pauses, and any later server start automatically resumes unfinished jobs it can lock (R8), reporting them through job status.

## Outstanding Questions

### Deferred to Planning
- [Affects R1][Technical] MCP SDK choice and whether it goes in a new optional extra versus base dependencies; Python floor is 3.11.
- [Affects R6, R7][Technical] Job registry location and lock mechanism on top of the reused entity cache.
- [Affects R6][Technical] Whether file-job output should reuse the cohort-harmonization output directory / pins layout so both share one provenance shape.
- [Affects R6][Technical] CSV/TSV validation without pandas (stdlib csv may disagree with the engine's parsing on quoting/NaN).
- [Affects R1][Technical] Console-script name and the exact `uvx --from 'biomapper[mcp]' ...` invocation; check MCP SDK resolution against the `httpx ^0.27` pin.
- [Affects R14][Needs research] Current MCP registry submission requirements (`server.json`, namespace verification for a PyPI package).

## Next Steps

-> /ce:plan for structured implementation planning
