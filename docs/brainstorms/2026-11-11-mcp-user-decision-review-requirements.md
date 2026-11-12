---
date: 2026-11-11
topic: mcp-user-decision-review
---

# Review triage for harmonization and entity resolution through the MCP server

## Problem Frame

A researcher harmonizing cohorts with BioMapper ends up with a long list of results that deserve
a second look: weak or unresolved rows, and pairs that might be the same entity. Some of these the
researcher can settle quickly with their agent's help. The rest need domain experts. Today there
is no path from that list to an expert review: `harmonize_cohorts()` (two cohorts) and
`harmonize_many()` (more than two, 1.8.0) are Python API only, so an agent working through the MCP
server cannot run them or read their review lists, and nothing turns a review list into something
experts can work through.

**The goal (Trent, 2026-11-11):** the researcher, helped by their agent, decides which subset of
results needs expert review. That subset becomes a campaign in Expert-in-the-Loop, which the
researcher organizes. Experts in the campaign are blind. Blind, label-grade adjudication belongs
there, not in the MCP server.

So the agent's job is triage support: gather the evidence, propose a sort, and let the researcher
adjust and approve it. The researcher is the organizer and makes the call.

This builds on `docs/brainstorms/2026-11-08-mcp-agent-rerank-requirements.md` (shipped as 1.7.0:
weak-row references, candidates and agent suggestions). That document's endorsement and blind
adjudication flows (its R8 to R11) were never built, and this document replaces them with triage
plus a hand-off to Expert-in-the-Loop.

Two constraints carry over:

1. **The engine's output stays canonical.** Every engine-only table and file stays exactly as it
   is today.
2. **There is no LLM call in the client or the server.** The only LLM is the researcher's own
   agent.

## Where this fits

This feature is the first step of a larger loop (Trent, 2026-11-11):

1. **BioMapper streamlines harmonization.** It resolves and links what it can, so the set of
   entities and matched pairs a person has to look at is as small as possible. This document.
2. **Expert-in-the-Loop gathers expert feedback** on the subset that needs it.
3. **Expert-reviewed harmonized data feeds Kestrel and KRAKEN queries** for knowledge-graph
   insights.

Only step 1 is in scope here. Its obligation to the later steps is that every item sent to
experts can be matched back to its rows and cohorts (R11), so expert verdicts can be joined to
the harmonized data when steps 2 and 3 are built.

## How it flows

```
map cohorts (slow, background job)  ->  link (local, instant)  ->  engine-only table + review items
                                                                          |
                        agent fetches evidence and proposes a sort for the review items
                                                                          |
                          researcher adjusts and approves the sort (in conversation)
                              |                     |                        |
                       settle myself         send to experts            leave as is
                              |                     |
                      reviewed table     Expert-in-the-Loop campaign file
```

## Requirements

**Phase 1: harmonization through the MCP server**

- R1. The MCP server exposes two-cohort and many-cohort harmonization as tools. Each runs as a
  background job with the same lifecycle as file mapping today: submit, check status, resume,
  cancel. Results match the Python API on the same inputs.
- R2. A finished job writes the same report the Python API saves, and returns a compact summary:
  counts, the publication grade where the report has one (many-cohort reports only, today), and
  how many review items there are of each kind. Full review lists are read on demand.
- R3. Many-cohort jobs reuse saved mappings under the existing 1.8.0 rules.
- R4. Everything a run flags for review is presented as one typed list of **review items**, each
  with a server-issued reference. v1 has two kinds:
  - **row items**: a weak, unresolved or refused entity, using the 1.7.0 definition of weak. One
    item per entity, listing every cohort row that shares it;
  - **link items**: two rows that might be the same entity. From two-cohort runs these are the
    links already marked as needing review. From many-cohort runs these come from the cross-node
    review queue.
- R5. Review items are listed by impact, largest first: how many rows and cohorts the item
  touches.
- R6. The agent can fetch evidence for a review item. For a row item this is the existing
  candidate list. For a link item it is both sides together: each side's name, chosen node,
  certificate state, identifiers, and the basis of the possible link.
- R7. The agent can record a suggestion with a rationale for any review item, as in 1.7.0. For a
  link item the suggestion is "same entity", "different entities", or "cannot tell".

**Phase 2: triage and the expert hand-off**

- R8. Every review item has a **triage status**, set by the researcher: settled by me, send to
  experts, or left as is (the default). The agent can propose statuses for many items at once,
  with a reason for each. Nothing is final until the researcher approves, and they can approve or
  change items in bulk.
- R9. A status is recorded through the agent on the researcher's say-so in conversation. There is
  no separate server prompt. The record says the researcher's choice was relayed by the agent.
- R10. The triage is saved to a plain file next to the run's output. It can be read, compared
  between versions and shared. Reading an edited file back in, or passing it into a later run,
  comes with Phase 3.
- R11. The items marked "send to experts" can be exported as an **expert review set**: a file
  ready to upload as pairs to an Expert-in-the-Loop campaign, with no reshaping by hand.
  - a row item becomes one pair per candidate worth asking about (the query against a candidate
    node);
  - a link item becomes one pair (one side against the other).
  - each pair carries the reference of the review item it came from, so campaign results can be
    matched back later.
- R12. The export states plainly which fields it fills, including whether the agent's suggestion
  and rationale are included. The default leaves them out, so that experts are not shown them.
- R22. The triage flow (R8 to R12) also applies to weak rows from file mapping jobs. Weak rows
  from single-entity and batch calls belong to no run and are not triaged or exported in v1.
- R13. Every tool result in a review states where the triage stands: how many items are flagged,
  settled, sent to experts and left as is. The agent can pass that on accurately.

**Phase 3: the reviewed table**

- R14. For an item marked "settled by me", the researcher's answer is recorded: for a row item,
  keep the engine's choice, another candidate, or no acceptable node; for a link item, same entity
  or different entities.
- R15. Applying the triage file produces a separate **reviewed** output next to the engine-only
  output. The engine-only output is never modified. The reviewed output has a clearly different
  file name and marks each row or group whose result came from the researcher and not the engine.
- R16. The reviewed report states what changed against the engine-only output and how far the
  review got: items flagged, settled, sent to experts, and left as is.
- R17. A triage file from an earlier run can be passed into a later run. An answer is applied only
  when the row can still be found, the KG build is known and unchanged, and the engine's chosen
  node for every row involved is unchanged. Otherwise it is reported as stale and not applied.
- R18. Applying a triage file to weak rows from file mapping produces a reviewed output file
  next to the unchanged main output, under R15 to R17.

**Guidance and protection**

- R19. Tool descriptions guide the agent to: propose a sort and let the researcher decide, say
  what each status means, never describe a reviewed result as BioMapper's resolution, and
  recommend expert review when it cannot tell.
- R20. Text from the knowledge graph, cohort files or the agent that ends up in an export or a
  report is length-capped and stripped of control characters, under the predecessor document's
  rules for candidate text.
- R21. Evidence fetching counts against the existing client protections and caps.

## Success Criteria

- Phase 1: in Claude Code, an agent runs a many-cohort harmonization through the MCP server and
  reads the review items in impact order, with evidence for one row item and one link item.
- Phase 2: for a real cohort set, the agent proposes a sort, the researcher adjusts it in
  conversation, and the exported expert review set uploads to an Expert-in-the-Loop campaign and
  shows as reviewable pairs without hand editing.
- The engine-only outputs of mapping and harmonization are identical with and without this
  feature in use.
- Every item in the expert review set traces back to a review item reference.
- Phase 3: the reviewed report's counts reconcile with the engine-only report plus the settled
  items, and a stale answer is reported and not applied.

## Scope Boundaries

- No blind mode, no label-grade export, and no mode tracking in the MCP server. Blind expert
  judgment happens in Expert-in-the-Loop.
- No server prompts (MCP elicitation). This removes the largest unverified dependency.
- No user-changeable review settings in v1. The behaviour is stated to the researcher up front
  through the agent.
- No pushing to Expert-in-the-Loop over its API in v1. The researcher uploads the file.
- No importing campaign results back into the reviewed table in v1 (see Outstanding Questions).
- No change to the engine, and no server-side LLM.
- Not review items in v1: within-node multiplicity groups, broad-node groups, identifier-check
  "not confirmed" rows, and cross-node entries where a side has no node. They stay as report
  flags.
- No claim that reviewed results measure BioMapper's accuracy.

## Key Decisions

- **The purpose is triage toward an expert campaign (Trent, 2026-11-11).** This replaced an
  earlier same-day design in which the researcher's decisions were collected through blind and
  anchored server prompts. Six-reviewer document review had found that design's blind mode was
  not actually blind, that it rested on a prompt channel never run live, and that it bundled a
  certain need (harmonization through MCP) with a speculative one.
- **Blindness and fine-grained provenance are deprioritized here (Trent, 2026-11-11).** They are
  Expert-in-the-Loop's job. The MCP server records only whether a result came from the engine or
  from the researcher.
- **The reviewed output is an overlay (Trent, 2026-11-11).** A separate, labelled table, with
  the engine-only table untouched. Linking is local and instant, so applying answers costs no
  mapping requests.
- **The agent records triage on the researcher's say-so, with no server prompt (Trent,
  2026-11-11).** The judgment that needs integrity happens in Expert-in-the-Loop. The record says
  the choice was relayed by the agent.
- **Plan Phases 1 and 2 now; Phase 3 is a follow-on (Trent, 2026-11-11).** R14 to R18 stay in
  this document as the agreed direction and are not planned yet.
- **Campaign results do not come back into the reviewed table in v1 (Trent, 2026-11-11).** Each
  exported pair carries its review item reference so the return path can be added.
- **Three phases, each shippable alone.** Phase 1 closes a verified gap and needs no new
  interaction pattern. Phase 2 delivers the stated goal. Phase 3 is the researcher's own table.
- **The review field is `engine-only` or `reviewed`, separate from the publication grade
  (Trent, 2026-11-11).** `publication_grade` is not renamed.

## Dependencies / Assumptions

- Verified on `main` at 434a24f (1.8.0): linking in `src/biomapper/harmonize/` makes no network
  calls; `harmonize_cohorts()` and `harmonize_many()` are not MCP tools; `publication_grade`
  exists only in `src/biomapper/many_cohorts.py`.
- Expert-in-the-Loop, checked in a local checkout at e9a91d5 (2026-07-27, may be behind): a
  campaign holds pairs with source and target text, dataset and id, optional metadata, and
  optional LLM confidence, model and reasoning. Pairs are uploaded as a file by an admin through
  the web interface.
- Unverified: whether Expert-in-the-Loop hides the LLM fields from reviewers, and what its
  results export contains.

## Outstanding Questions

### Resolve Before Planning

(none)

### Deferred to Planning

- [Affects R11][Needs research] The exact pairs file columns and import behaviour in the current
  Expert-in-the-Loop, checked against its latest code and one real upload.
- [Affects R11][Technical] How many candidates per row item become pairs.
- [Affects R4, R6][Technical] How link-item references are stored and tied to both sides, and
  which arm (names-only or identifier) review items come from. Proposed: names-only.
- [Affects R1, R3][Technical] Mapping inside a job must go through the request gate without
  being classed as a custom mapper (which would stop reuse of saved mappings); how two-cohort
  runs resume; how cancel interrupts a synchronous run.
- [Affects R6, R21][Technical] The existing cap of 15 candidate fetches per 5 minutes limits row
  evidence to roughly 180 rows an hour. Keep it, or capture candidates for weak rows during the
  mapping job.
- [Affects R14, R15][Needs research] Which call returns a node's full identifier set by CURIE,
  needed when the researcher picks another candidate.
- [Affects R15][Technical] How "same entity" answers combine in many-cohort output, where each
  row sits in exactly one group, and what happens when answers conflict.

## Next Steps

-> /ce:plan for Phases 1 and 2 (R1 to R13, R19 to R22)
