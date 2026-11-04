# BioMapper MCP: worked example

Status: **to be filled from the live verification run** (operator checklist step 3, after the
1.6.0 release). This file is the supplement-ready transcript for the preprint's Software
Availability section; it must come from a real session against the hosted API, not be written
by hand.

What to record here:

1. Setup: the exact `claude mcp add` line (or Desktop config), `uv` version, and the time from a
   clean machine to the first successful tool call (target: under 5 minutes).
2. The fixed prompt: "map this 500-row file" with the file's name, row count, and sha256.
3. The transcript: the agent's `list_entity_types` call, the `map_file` call and its returned
   `job_id` / ETA, the `job_status` polls, and the final status.
4. The outputs: the first rows of `<input>.biomapper.tsv` and the `pins` block of
   `<input>.biomapper.provenance.json` (KG build, Kestrel version, client version).
5. The `/health` probe timings taken between chunks during the run (target: under 15 s).
