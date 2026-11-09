---
date: 2026-11-08
topic: mcp-agent-rerank
---

# Agent-in-the-loop candidate review through the MCP server

## Problem Frame

BioMapper's MCP server (`biomapper` 1.6.2) returns one engine choice per entity. When that choice
is weak, the researcher's agent has nothing to work with, even though it often holds context the
engine lacks: the study, species, assay, or what neighbouring rows resolved to. The engine can
already return the top-N raw Kestrel candidates without changing its own selection
(`kestrel_top_n`, biomapper2 #87 / org #116, live on prod), but the MCP server drops them to stay
inside tool-output limits.

Letting the user's agent review weak results against those candidates serves:

- **rescuing misses**, the primary goal;
- **a second opinion** where the engine is unsure;
- **collecting blind human adjudications.** Whether these can feed the gated LLM-reranker ablation
  is to be determined, not assumed;
- **an "agent-in-the-loop" demo story** for the preprint.

It costs us no LLM spend, because the agent is the user's.

Three constraints shape everything here:

1. **The engine's choice stays canonical.** Agent picks and human judgments are recorded
   *alongside* it, never in its place, and never in the main output file. Outputs stay
   reproducible, and preprint claims keep meaning "BioMapper resolved X".
2. **An agent pick is not a label, and neither is a human clicking "confirm" on one.** The agent is
   an LLM. A human approving the agent's pick and rationale is mostly accepting LLM output, and
   the dialog would show the same candidate metadata the rerankers see. That is the 2026-07-06
   ablation review's circularity, passed through a click. Only a **blind** human pick (no agent
   pick, rationale, scores, or engine marker shown) is an adjudication.
3. **Weakness is known only after a result returns.** So candidates are a separate, on-demand
   step, not something attached to the first call.

## Requirements

**Weak results and candidates**

- R1. A result is **weak** when:
  - its status is `unresolved` or `refused`; or
  - its certificate state is `uncorroborated` or `contradicted`. `contradicted` is the engine's own
    "a human should look" signal; or
  - its certificate state is `unavailable` **and** the entity type is a small molecule. This was
    added 2026-11-08 after the Unit 0 spike: all 11 such rows were wrong, the largest error group,
    and the gold answer was in the top 10 for 6 of them.

  `not_applicable` (non-structure entities such as genes and proteins), and `unavailable` for
  non-small-molecule types, are not weak. Mapping results flag weak rows and carry a server-issued **reference** for each
  entity, so the agent knows candidates are available. Mapping calls are otherwise unchanged: no
  extra engine work, and no added output for strong rows.
- R2. A separate **get-candidates** tool takes references and returns a compact candidate list for
  each. It re-maps the entity with candidates requested (`kestrel_top_n`) and checks that the
  engine choice and certificate match the original result. On a mismatch the candidates are
  flagged as from a different run and are not attached to the original choice. It is limited to a
  small number of references per call, so it fits inside the 50 s tool deadline. On a deadline it
  returns what it has, never an error that loses results.
- R3. Each candidate is compact:
  - identifier (CURIE);
  - name;
  - Biolink categories;
  - search score;
  - which Kestrel search endpoint returned it;
  - whether it equals or is equivalent to the engine's choice, computed from the full
    `kg_equivalent_ids`, not the truncated interactive view.

  Candidates are deduplicated across endpoints and capped by a configurable N with a bounded
  maximum. Defaults are set in planning against client output limits.
- R4. Each candidate set has a status:
  - `present`;
  - `none_searched`: the engine ran no Kestrel search, e.g. identifiers were supplied or the
    result was refused before searching;
  - `fetch_failed`, with a reason;
  - `run_mismatch`.

  A fetch failure is never shown as "no candidates". Candidate text comes from external KG data and
  is treated as untrusted: length-capped, with control characters stripped, before it reaches the
  agent or a human.
- R5. Candidates never alter the engine's choice, certificate, or provenance. This matches the
  engine's selection-neutral passthrough guarantee.
- R6. For file jobs, weak rows carry references, so the agent can review a whole file's weak rows
  after the job finishes, using the same get-candidates and record tools.

**Agent suggestions, endorsement, and blind adjudication**

- R7. A record tool lets the agent record a **suggestion** for a reference. The suggestion is a
  CURIE from that reference's stored candidate set, the engine's choice, or "none of these", plus
  a rationale.
  - The server validates the choice against the candidate set it stored for that exact reference.
  - The rationale is inert, length-capped text and is never treated as instructions downstream.
  - The tool's input schema has no field for human status.
  - One reference has one current suggestion; recording again replaces it and keeps the history.
- R8. **Endorsement** (optional). After a suggestion is recorded, the server can ask the human,
  through the client's confirmation UI (MCP elicitation), to endorse, reject, or change the
  agent's suggestion.
  - The dialog is built from server-held state for that reference. The rationale is shown clearly
    labelled as the agent's.
  - The outcome is stored as `human_endorsed`, `human_rejected`, or `human_changed`.
  - It is never an adjudication.
- R9. **Blind adjudication**, the only source of label-grade judgments. The server asks the human,
  via elicitation, to pick the right identifier for a reference, showing only the query and the
  candidates. Candidates are shown in shuffled order, by CURIE and name only: no agent suggestion,
  rationale, scores, endpoint, or engine-choice marker.
  - The human can pick a candidate, say "none of these", or type an out-of-list CURIE. An
    out-of-list CURIE is recorded as a retrieval miss.
  - The outcome is stored as a separate `blind` record. It is compared to the agent's suggestion
    and the engine's choice only afterwards.
  - The agent can request a blind adjudication but can't see or influence what the human is shown,
    beyond choosing which reference to ask about.
- R10. Every human judgment (R8 and R9) is saved first as pending, upgraded only on an accepted
  response, and records elicitation provenance:
  - the client name and version;
  - the request id;
  - request and response timestamps, and the response latency;
  - position within the session.

  Decline, cancel, timeout, or a client without elicitation leaves no human judgment. Waiting for a
  human never holds the request gate or runs under the tool deadline.
- R11. **Trust boundary, stated honestly.** Human status can't be set through the MCP tool
  surface. It does *not* stop an agent with shell access from editing local files, or from
  installing a client hook that auto-answers elicitations (Claude Code supports `Elicitation`
  hooks). Provenance (R10) makes that visible after the fact. Whether to add tamper-evidence, such
  as a signature with a key kept outside the workspace, is a planning decision.

**Storage and export**

- R12. Suggestions, endorsements, and blind adjudications are stored locally: `0600` files in the
  existing `0700` job home, with full context for each. That context is:
  - the query;
  - the engine choice and certificate state;
  - the candidate set shown, with candidate depth N;
  - the honest provenance pins (KG build labelled as assumed, not verified, per the existing pin
    labels).
- R13. File jobs write agent suggestions, endorsements, and blind adjudications to a **sidecar**
  file next to the output (e.g. `<stem>.biomapper.choices.tsv`), with an explicit status per row.
  The main output TSV stays engine-only.
- R14. Blind adjudications can be exported. Each row records the population metadata needed to
  judge whether it fits the ablation later:
  - entity type;
  - the weak trigger;
  - candidate depth N;
  - whether the human's answer was in the candidate list;
  - labeller role (self-declared, e.g. curator or researcher);
  - provenance (R10, R12).

  The export does not claim to be ablation input.

**Service protection and guidance**

- R15. Get-candidates counts against the existing client protections (one request in flight, loop
  guard), weighted above a plain lookup, and has its own per-window cap. The prod nginx limit
  doesn't see this load: passthrough makes up to 3 extra Kestrel calls per entity *inside* the
  engine, so the client must bound it.
- R16. Tool descriptions guide the agent to:
  - prefer corroborated engine choices;
  - fetch candidates only for weak rows;
  - always give a rationale;
  - never present a suggestion as BioMapper's resolution;
  - use blind adjudication when the human wants their judgment recorded.

## Success Criteria

- In Claude Code, for a weak result: the agent fetches candidates, records a suggestion with a
  rationale, and the human endorses it, all without touching the main output.
- In Claude Code, a blind adjudication shows the human no agent pick, rationale, scores, or engine
  marker. A test asserts this from the elicitation payload.
- Human status can only come from an accepted elicitation response. A test proves the record tool
  cannot set it.
- Mapping results for strong rows are content-identical to 1.6.2 apart from the weak flag and
  reference. The engine choice is unchanged in every case, including a `run_mismatch`.
- A file job's main TSV is byte-identical in columns to 1.6.2. Suggestions and judgments appear
  only in the sidecar.
- Get-candidates for its maximum batch returns within the tool deadline on prod, or returns
  partial results with statuses rather than an error.
- An export of blind adjudications carries population metadata on every row.

## Scope Boundaries

- No server-side LLM reranker and no change to the engine's selection. The July gate on an
  LLM-reranker tier stands.
- No claim in v1 that adjudications feed the ablation. Whether they fit its population is decided
  later, from the exported metadata.
- No upstream submission of adjudications to Phenome Health in v1. Local storage and export only.
- No change to `biomapper2` engine behaviour. The existing passthrough is used as-is.
- No deterministic reranker in the client (the engine's `rm_anchor` work is separate).
- No automatic use of suggestions or judgments in any downstream harmonization, and none in the
  main output.

## Key Decisions

- **Weak-row candidates fetched by a separate call** that re-maps and verifies the engine choice.
  Only weak rows pay the extra engine and Kestrel cost. The first result can never lose its
  deadline to candidate fetching. A changed engine run is detected rather than mixed in.
- **Suggestion, endorsement, and blind adjudication are three different things.** Only blind
  adjudications are label-grade. Endorsement is kept for usability but never counts as a label.
- **Sidecar output.** The main TSV stays engine-only, so agent picks don't leak downstream as
  if BioMapper chose them.
- **The label goal is "adjudications whose fitness is TBD."** The ablation's population is
  metabolite ChEBI disagreements with top-200 candidates and expert adjudication. Weak-row MCP
  judgments differ on entity type, candidate depth, and labeller, so they are only collected with
  the metadata needed to judge that later.
- **Human judgments via MCP elicitation.** It is the only channel where the server, not the agent,
  shows the human what to judge. Its limits are stated in R11.

## Dependencies / Assumptions

- **Verified 2026-11-08:**
  - prod's OpenAPI exposes `kestrel_top_n`;
  - MCP SDK 2.3 supports tool elicitation (`Context.elicit`, flat primitive schemas only, with
    string enums allowed; candidate detail goes in the message text, and an out-of-list CURIE
    needs a free-text field);
  - Claude Code 2.1.319 implements elicitation.

  Elicitation is gated on the client advertising the capability, the same way roots are. Still
  unverified: behaviour in headless `-p` runs and in Claude Desktop.
- Passthrough runs after the engine's batch deadline is disarmed, with a 180 s Kestrel timeout per
  call. The get-candidates batch size and deadline handling must account for this.
- Re-mapping can return a different engine choice (the known cache and determinism confound).
  R2's verification handles this.

## Outstanding Questions

### Resolve Before Planning

(none)

### Deferred to Planning

- [Affects R2, R15][Needs research] Measure the latency of a `kestrel_top_n` re-map on prod to set
  the get-candidates batch size and N.
- [Affects R3][Technical] Default and maximum N, and the field set, checked against
  `MAX_MCP_OUTPUT_TOKENS` (Claude Code warns at 10k and caps at 25k).
- [Affects R8, R9][Technical] Elicitation form design within flat primitive schemas: an enum of
  shuffled candidate labels plus "none" plus a free-text CURIE field.
- [Affects R11][Technical] Whether to add tamper-evidence for stored judgments.
- [Affects R12, R13][Technical] Store layout (per job and global), how references persist across
  server restarts, and whether references from a previous session can still be adjudicated.
- [Affects R6, R12][Technical] Keep candidates out of the resumable entity-cache key, so existing
  job caches are not invalidated.
- [Affects R14][Technical] Export format and which provenance fields to include.

## Next Steps

-> /ce:plan for structured implementation planning
