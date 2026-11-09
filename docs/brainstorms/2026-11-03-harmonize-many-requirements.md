---
date: 2026-11-03
topic: harmonize-many
---

# harmonize_many(): harmonizing more than two cohorts in one run

## Problem Frame

`harmonize_cohorts()` (two cohorts) and `harmonize()` (two lists of mapping results) only work on pairs. Researchers will want to harmonize three or more cohorts at once. The first planned use is a demo that harmonizes five representative public metabolite cohorts (one per major vendor and naming style, spanning the difficulty tiers; narrowed from 50 on 2026-11-03). The function must still scale to many more cohorts, which is where a pairwise loop breaks down. Calling the pairwise functions in a loop goes wrong in three ways:

1. **Cost.** `harmonize_cohorts()` re-maps both cohorts on every call. With 50 cohorts that is 1,225 pairs, and each cohort is mapped 49 times against a prod API that takes about 4.4 s per entity.
2. **No consistent groups.** Pairwise links don't chain. If A links to B by name and B links to C by identifier, A and C may still not link. Nothing defines what one metabolite measured across N cohorts is.
3. **The identifier rule only works for two.** Today an identifier vocabulary is used as mapping input only when both cohorts declare it. That rule exists because supplying codes for one cohort alone lowered lab matches from 31 to 2. With N cohorts, this rule has no single correct generalization.

## Requirements

**The call**
- R1. Add a new public function, `harmonize_many()`, next to `harmonize_cohorts()`. It takes two or more labelled cohort tables (two is allowed so it can be reconciled against the pairwise functions; the intended use is three or more), read with the same inputs `harmonize_cohorts()` accepts: DataFrame, list of dicts or file path; name column; optional vocabulary-to-column declarations; optional key column. It also takes one entity type per run, resolved with the same aliases as today.
- R2. Each cohort is mapped once per arm. The linking step does not call the API.

**Grouping (decided: anchored on the chosen KG node)**
- R3. A group is the set of rows, across all cohorts, that resolved to the same chosen KRAKEN node. Groups are never merged because of a link between two different nodes.
- R4. Links between rows on different nodes (shared identifier, `name_exact`, `name_casefold`) are reported in a **cross-node review queue** as "possibly the same". Each entry shows both nodes, the basis of the link and the cohorts involved. They are not counted as matches. Name links follow the same rule as `harmonize_cohorts()`: on only for categories in `NAME_LINKING_CATEGORIES` (small molecules today), with the same short-name casefold withholding (`NAME_CASEFOLD_MIN_LENGTH_EXCLUSIVE`). Withheld name pairs are reported separately and are not cross-node links.
- R5. A group is **clean** when every cohort in it contributes exactly one row. A group where some cohort contributes two or more rows is flagged for review (a within-node multiplicity flag). It is related to `harmonize()`'s `ambiguous_links` but is not the same set, because `ambiguous_links` also counts partners across nodes; the report says so. All groups are still returned.
- R5b. A group whose node is class-level or broad (as reported by the engine's lipid resolution / lipid hint on the mapping result) is flagged as a **broad-node group** and kept out of the clean count, even when each cohort contributes one row, because different molecules (PC(34:1), PC(36:2), "phosphatidylcholine") can share such a node.
- R6. Unresolved and errored rows are listed for each cohort, as candidates for refusal and as errors. They are never dropped silently and never placed in a group.

**Identifiers (decided: used as a check, plus an arm only for vocabularies every cohort declares)**
- R7. The primary arm maps every cohort by name only.
- R8. The identifier arm supplies a vocabulary as mapping input only when **every** cohort declares it, normalized with `canonical_prefix`. When no vocabulary is declared by all cohorts, the arm is skipped and the report says so. When the arm runs, it is reported next to names-only along with a group-level diff: groups only in names-only, groups only in the identifier arm, and groups in both.
- R9. **Identifier check.** For every grouped row, compare the identifiers the submitter declared in the input table's vocabulary columns (normalized as `harmonize_cohorts()` does today: vocabulary via `canonical_prefix`, then `vocabulary:code` as a CURIE) with the identifier set of the node its name chose. These come from the input table, not from `MappingResult.identifiers`, which holds the engine's own assigned IDs and would make the check circular; the declared IDs are saved with the mapping so the check runs offline. Status per row: `confirmed` (the sets intersect), `not_confirmed` (declared IDs exist, none among the node's equivalents), `no_declared_id`, or `errored`. Rows not in a group (R6) are not checked. Only `not_confirmed` rows go into the review list; the others appear as a status column in the matrix and membership table. The report must say that "not confirmed" is not the same as "contradicted", because the graph may simply lack that cross-reference. Planning decides how structure vocabularies (InChIKey, dropped by `curie_set`) and legacy code formats (5-digit HMDB) are handled.

**Run model (decided: resume and add cohorts)**
- R10. Each cohort's mapping for each arm is saved as soon as it finishes, together with its pins and an input checksum.
- R11. A new run reuses a saved mapping when four things match: the cohort's input checksum (computed the same way for files, DataFrames and lists of dicts, over what the mapper actually receives: the ordered key, name and supplied identifiers per row, plus the name, key and vocabulary column declarations; a file's sha256 is kept as provenance only), the resolved category, the arm (including the set of supplied vocabularies) and the KG build. A crashed run resumes, and adding cohort #51 maps only #51. Linking is always recomputed.
- R12. Mappings from different KG builds are never mixed. The KG build is read before any mapping request; when it cannot be read (unknown), saved mappings are not reused. On a mismatch the run refuses and names the cohorts that would need re-mapping. Fixtures can inject the build.
- R12b. Each saved mapping records an engine fingerprint (whatever the API exposes, labelled as such) and its mapping session. A run that reused any saved mapping is labelled iteration-grade, not for publication; a `remap_all` switch re-maps every cohort in one pass, and only such a run is publishable. Published numbers (including the demo's final run) use `remap_all`. (Amended 2026-11-06: the mixed-session flag was dropped because it fires on every resumed run and cannot detect drift.)
- R13. The report records which cohorts were reused and which were freshly mapped.

**Output**
- R14. One report object, saved by default to a timestamped directory (with an override path), containing:
  - **Matrix:** entity × cohort, where each cell lists the row keys from that cohort and the cell's identifier-check status.
  - **Membership table:** one row per (group, cohort, row).
  - **Overlap matrix:** a cohort × cohort count of groups shared by each pair.
  - **Review lists:** the cross-node review queue (R4), the flagged groups (R5) and the identifier-check review list (R9).
  - **Per-cohort counts:** mapped, unresolved, errored, number of groups, number of clean groups.
  - **Run record:** settings, pins (package version, the API's self-reported version labelled "known stale", the Kestrel KG build) and a checksum for each input.
  - A `summary()` dict.
- R15. **Reconciling with the two-cohort functions.** For every cohort pair, the report gives same-node row pairs (for each group, rows from cohort A × rows from cohort B, summed) plus cross-node review links for that pair. On the same mapped results and the same name-linking rule (R4), these two numbers add up to the link count `harmonize()` produces for that pair. The same-node row-pair count differs from the overlap matrix's group count, and both are reported. `harmonize_cohorts()`, `harmonize()` and every number already reported from them stay unchanged.

## Success Criteria
- **Reconciliation:** on the committed UK Biobank × Arivale replay, a two-cohort `harmonize_many()` reconciles exactly with `harmonize()` on the same results (R15), and existing tests for `harmonize()` and `harmonize_cohorts()` pass unchanged.
- **No chaining:** a synthetic three-cohort fixture with a class-level lipid node that has name or identifier links to several species nodes produces no merged group, and the cross-node queue lists every such link.
- **Mapping calls:** a fixture whose mapper counts its calls shows (a) each cohort mapped once per arm, (b) a resumed run maps only the unfinished cohorts, (c) adding a cohort maps only that cohort, and (d) a changed KG build is refused before any request is sent.
- **Broad nodes:** a fixture where species rows from different cohorts land on one class-level node yields a broad-node group that is not counted as clean.
- **Split recall (gate):** on a name-independent answer key (NECS gold InChIKey/HMDB agreeing with Arivale's declared IDs, screened to certifiable rows), the report gives the share of true matches that land in a node group, in the cross-node queue, or in neither, split into name-equal and different-name strata; only the different-name stratum is evidence about split risk. The demo does not build on `harmonize_many()` until this number is known and Trent accepts it.
- **Identifier check:** on a fixture with a row whose submitter ID is a correct equivalent and a row whose ID is not among the node's equivalents, the statuses are confirmed and not confirmed respectively.
- **Demo run:** the five demo cohorts harmonize in one pinned run, end to end.
- **Scale:** linking 20 synthetic cohorts of about 1,000 rows each from saved mappings finishes offline in minutes (exact budget set in planning). This is a fixture test; the demo itself uses five cohorts. (Amended 2026-11-06: v1 links pairwise, which is fine up to about 20 cohorts.)

## Scope Boundaries
- No engine (biomapper2) changes and no structure check inside this function.
- No merging across nodes, no roll-up, no fuzzy name matching. Cross-node links are review items only.
- Scoring against answer keys and difficulty tiers belong to the five-cohort demo, not to the package.
- Fetching and parsing the public datasets is part of the demo, not this feature.
- More than one entity type per run is out of scope.
- Changing `harmonize_cohorts()` to call `harmonize_many()` is out of scope; it would change milestone numbers already reported.

## Key Decisions
- **Grouping anchored on the chosen node, with links between nodes sent to review:** Trent, 2026-11-03. Strict grouping never chains. Lower recall is reported openly through the review queue.
- **Identifiers used as a check, plus an arm that supplies only vocabularies all cohorts declare:** Trent, 2026-11-03. This keeps the protection from the two-cohort protocol (no one-sided specificity) and the single mapping per cohort. It is expected to rarely run at N=50.
- **Resume and add cohorts:** Trent, 2026-11-03. Mapping is the expensive step and researchers add cohorts over time.
- **After plan review, 2026-11-06 (Trent):** v1 links pairwise with `harmonize()` (reconciliation by construction, fine to about 20 cohorts); the store is the existing `EntityCache` plus a lock; reuse makes a run iteration-grade; the gate uses a name-independent, stratified key.
- **Demo narrowed to five representative cohorts:** Trent, 2026-11-03. The 50-dataset list stays in the survey note as the bench for later scale runs.
- **After document review, 2026-11-03 (Trent):** flag broad-node groups; gate the demo on a measured split recall; keep the all-cohorts identifier arm in v1 (the reviewer suggested deferring it); strict reuse (build read first, unknown build means no reuse, engine fingerprint, mixed-session flag, `remap_all` for published numbers).
- **Reconcile without changing existing functions:** Trent, 2026-11-03. Already-reported numbers stay valid, and the stricter N-way count is explained rather than conflicting with them.

## Dependencies / Assumptions
- Mapping results carry the chosen node and its equivalents (`chosen_kg_id`, `kg_equivalent_ids` on `MappingResult`); the submitter's declared IDs come from the cohort records (`CohortRecord.identifiers`), so R9 can run offline once both are saved. "Confirmed" relies on KRAKEN's own equivalents, so it is not independent of the graph: a wrong node with a wrong cross-reference would still be confirmed.
- Repeat runs are known to differ between cold and warm caches, and the engine release is not reliably exposed, so reuse across sessions can still drift on the same build. R12b makes the drift visible per group and `remap_all` removes it for published numbers.

## Outstanding Questions

### Deferred to Planning
- [R4][Technical] Aggregate the cross-node queue by node pair (bases, cohorts, row count) with row pairs as drill-down; a row-pair queue can grow to hundreds of thousands of entries at N=50.
- [R3/R6][Technical] Use the same "unresolved" test as `harmonize()` (empty `curie_set`); decide where rows that resolved but have no chosen node go, and whether a group can be keyed on a structure-namespace node.
- [R14][Technical] Confirm which outputs the demo consumes (overlap matrix is its headline figure); consider one typed review-item list with a `kind` field instead of three shapes.
- [R9][Technical] Should "not confirmed" codes be resolved through the API (as in the two-cohort review queue) to tell "contradicted" apart from "missing cross-reference"? Doing so costs requests; planning decides whether it is opt-in.
- [R10–R12][Technical] How saved mappings are stored and keyed, and how the API's self-reported version (known stale) affects reuse.
- [R2][Technical] Should mapping be refactored out of `harmonize_cohorts()` into a shared per-cohort step, without changing its behaviour?
- [R14][Technical] Matrix format at N=50 (dense TSV vs long format only) and the time budget for the scale criterion.

## Next Steps
→ /ce:plan
