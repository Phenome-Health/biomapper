# Migrating to biomapper 1.9.0

One change in 1.9.0 alters a value existing code already reads: `MappingResult.confidence_score`.
Everything else in the release is additive. This note is written for a downstream application
that displays the score or anything derived from it.

## `confidence_score` now describes the chosen node

### What it meant through 1.8.0

The highest score any annotator gave any identifier in the response, on any knowledge-graph
node. It was a property of the whole response, not of the answer. When two nodes described one
molecule and the resolver committed the lower-scored one, or one no annotator scored, the row
still showed the other node's score.

### What it means from 1.9.0

The score of the committed node: the highest score an annotator gave an identifier that lives on
`chosen_kg_id`'s node (the chosen id itself, or any id the response's `kg_ids` lists for it).

It is `None` when no annotator scored the chosen node, and when there is no chosen node.

The old value is still there under a new name, `MappingResult.max_annotator_score`.

### What does not move

- Which entity a row resolves to: `resolved`, `chosen_kg_id`, `primary_curie`, `identifiers`,
  `kg_equivalent_ids`, certificates and refusals are untouched.
- Harmonization: links, link bases, groups, review queues and every count in a harmonization
  report. None of them reads the score.
- Dataset-stream results (`map_dataset_file_sync`), whose `confidence_score` was already always
  `None`.

### Which displayed numbers move

For any one row the new score is equal to the old one, lower, or `None`. It is never higher.

| What you display | How it moves |
|---|---|
| The score itself | Unchanged for most rows. Lower, or blank, for a row whose best-scored identifier was on a node the resolver did not commit. |
| `confidence_tier` (`high` ≥ 2.0, `medium` ≥ 1.0, `low`, `unknown`) | A row can drop a tier, or go to `unknown`. No row rises. |
| Counts and charts by tier | `high` and `medium` can only shrink; `low` and `unknown` can only grow, by the same total. |
| "Unknown" or "not scored" counts | Grow: a resolved row whose chosen node no annotator scored used to borrow another node's tier and now reports `unknown` (`not_scored` in the MCP server's output). |
| Mean or median score | Equal or lower over the rows that still have a score; the number of rows averaged can shrink. |
| A "needs review" flag built on `low` or `unknown` tier | Flags more rows. biomapper-ui's `services/mapper.py` builds `needsReview` this way, so its review count rises with no change in the answers. |
| Sorting or filtering by score | Rows with a blank score move to wherever your sort puts blanks. |
| Resolved, unresolved and error counts, resolution rate | Do not move. |

How many rows move depends on the panel and was not measured for this note. Only rows where the
response scored an identifier on a node other than the chosen one, or left the chosen node
unscored, are affected.

The same change reaches every place the client reports the score: the `confidence_score` and
`confidence_tier` columns of MCP `map_file` output and of the Metabolon export, the MCP
`map_entity` / `map_batch` results, the benchmark mapper's `confidence_score` column, and the
`confidence_score` / `confidence_tier` fields of each row in a harmonization `report.json`.

### Results cached before the upgrade read as unscored

A result saved by 1.8.0 or earlier (a `harmonize_many` saved-mapping store, a resumable MCP or
`biomapper.jobs` job cache, a benchmark arm cache) holds no raw response, so the chosen node's
score cannot be recomputed from it. On load, 1.9.0 moves the stored number to
`max_annotator_score` and reports `confidence_score` as `None`. Those rows show tier `unknown`
until they are mapped again (`remap_all=True` for a `harmonize_many` store). Their resolutions are
reused as before.

### What to do in a UI

1. **Decide which number you mean.** To show how sure the resolver is of the answer it gave, keep
   reading `confidence_score`; that is now what it says. To keep the old number, read
   `max_annotator_score` and label it for what it is: the best annotator score for any candidate,
   not for the answer shown.
2. **Treat `None` as "not scored", not as "low".** A resolved row with no score is a resolution the
   annotators did not score, often one settled by a supplied identifier or by the resolver's
   source weighting. If a review flag counts `unknown` the same as `low`, expect the review count
   to rise, and consider separating the two.
3. **Do not compare tier counts across the upgrade** as if the engine changed. A drop in `high`
   between a 1.8.0 run and a 1.9.0 run of the same panel is this relabelling unless the
   resolutions differ too.
4. **Re-map cached panels** if you need scores for them.
