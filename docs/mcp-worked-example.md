# BioMapper MCP: worked example

A real session from the 2026-11-04 live trials of `biomapper[mcp]` 1.6.0 in Claude Code,
against the public API (`https://biomapper.expertintheloop.io/api/v1`, KRAKEN KG build 2.3.0,
Kestrel 0.5.0), with `uv` 0.6.9. Paths are shortened to `~/mcp-trials`; nothing else is edited
except where a tool result is truncated (marked `…`).

## Setup

```bash
claude mcp add --scope user biomapper -- uvx --from 'biomapper[mcp]' biomapper-mcp
mkdir -p ~/mcp-trials && cd ~/mcp-trials
cp <biomapper2>/data/examples/metabolites_synthetic.tsv ./metabolites.tsv
claude
```

`metabolites.tsv` (30 public metabolites, sha256
`f2879b93b982a2d96093cdcef34d4e5ee58d8142a899f34fcdbe3e7f909d7849`) has a `name` column plus
`INCHIKEY`, `HMDB`, `KEGG`, `PUBCHEM` and `CHEBI` columns. Some cells are blank and some hold two
values (ornithine's KEGG cell is `C00077;D08302`; the API splits on `,` and `;` by default).
Claude Code passes the project folder to the server as an MCP root, so `map_file` may read and
write there and nowhere else.

## The session

**User:**

> Map every metabolite in metabolites.tsv using the biomapper tools, using the HMDB, KEGG and
> CHEBI columns as identifiers. Poll the job until it finishes, then summarize how many
> resolved, show any that failed or were refused, and tell me where the output and provenance
> files are.

**Agent → `map_file`**

```json
{"path": "~/mcp-trials/metabolites.tsv", "name_column": "name",
 "entity_type": "biolink:SmallMolecule",
 "identifier_columns": {"HMDB": "HMDB", "KEGG": "KEGG.COMPOUND", "CHEBI": "CHEBI"}}
```

```
Error executing tool map_file: ~/mcp-trials/metabolites.biomapper.tsv already exists and
map_file never overwrites. Pass a different output_path, or move the existing file away first.
```

An earlier trial had already written the default output, so the tool refused to overwrite it.
The agent picked a new output path on its own:

**Agent → `map_file`** (same arguments plus
`"output_path": "~/mcp-trials/metabolites.hmdb_kegg_chebi.biomapper.tsv"`)

```json
{"job_id": "0af3f7eeab5c5af3", "state": "queued", "rows_total": 30, "entities_total": 30,
 "entities_done": 0, "eta_s": 132, "kg_build": "unknown", "kg_verified": false,
 "partial_path": "~/mcp-trials/metabolites.hmdb_kegg_chebi.biomapper.tsv.partial",
 "provenance_path": "~/mcp-trials/metabolites.hmdb_kegg_chebi.biomapper.provenance.json", …}
```

The call returns at once with a job id. Mapping runs in the background, so no tool call waits on
the API. (`kg_build` reads `unknown` until the job's provenance pins are fetched.)

**Agent → `job_status`** `{"job_id": "0af3f7eeab5c5af3"}`

```json
{"jobs": [{"job_id": "0af3f7eeab5c5af3", "state": "complete", "rows_total": 30,
  "entities_done": 30, "entities_failed": 0, "pass_index": 1,
  "kg_build": "2.3.0 (3dd08a5b)", "kg_verified": true, …}]}
```

The agent then read the output TSV and provenance file with a short script (result only):

```
30 Counter({'resolved': 30})
1 | glucose  | {"CHEBI": "17234", "HMDB": "HMDB0000122", "KEGG.COMPOUND": "C00031"} | HMDB:HMDB0000122 | KEGG.COMPOUND:C00031
2 | lactate  | {"HMDB": "HMDB0000190", "KEGG.COMPOUND": "C00186"}                  | CHEBI:78320      | KEGG.COMPOUND:C00186
5 | alanine  | {"CHEBI": "16449"}                                                   | HMDB:HMDB0062251 | CHEBI:16449
…
```

**Agent (final answer, abridged):**

> All 30 metabolites resolved. None failed, none were refused and none errored.
>
> - Output: `~/mcp-trials/metabolites.hmdb_kegg_chebi.biomapper.tsv`
> - Provenance: `~/mcp-trials/metabolites.hmdb_kegg_chebi.biomapper.provenance.json` (input
>   checksum, options, client version, KG build 2.3.0)
> - The earlier run's output is untouched.
>
> Matches worth checking: **alanine** was mapped from its ChEBI ID alone to
> `HMDB:HMDB0062251`, not the standard L-alanine entry (`HMDB0000161`). **lactate** was mapped
> to `CHEBI:78320`, which may be a single enantiomer rather than generic lactate.

In this session every row read `confidence_tier = unknown`. The identifiers were supplied, so
no annotator ran and the engine produced no score. From 1.6.1 the server
reports these rows as `not_scored`, so agents do not mistake them for weak matches.

## Live trial results (Claude Code, 2026-11-04)

Scored by counting tool calls in the session transcripts, not from the agent's own account.

| Trial | What it checks | Result |
|---|---|---|
| A: routing, 6 fresh sessions | Asked to map a whole file, does the agent use `map_file` + `job_status` instead of looping `map_entity`? | 6/6 used `map_file` then `job_status`, with 0 `map_entity` calls. 5/6 called `list_entity_types` first. |
| B: full run (above) | End-to-end job, output and provenance | 30/30 resolved; output and provenance written; no `.partial` left; refused to overwrite an existing output. |
| C: loop guard | 25 single `map_entity` calls in a row | Calls 1-20 answered; call 21 refused with a pointer to the batch and file tools. |
| D: resume | Start a 300-row name-only job, quit the agent, reopen | The new session's server picked up the same job automatically (same job id, same pinned KG build) and ran it to 290/300 before that session closed too. The first session exited before its first chunk was saved, so reusing already-mapped rows across a restart was not exercised live; it is covered by the unit tests. |

Not measured: time from a clean machine to the first tool call, and `/health` latency between
chunks. Not tested: Claude Desktop and MCP clients other than Claude Code. Name-only lookups ran
at about 5.4 s per entity on the shared deployment; with identifiers supplied, 30 rows finished
in a few seconds.
