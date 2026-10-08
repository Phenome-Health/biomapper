---
date: 2026-10-08
topic: harmonize-cohorts
---

# harmonize_cohorts(): the harmonization protocol as one call

## Problem Frame

Harmonizing two cohorts with the package today takes several manual steps (map each cohort, call `harmonize()`, decide what to do with identifiers), and the obvious path can quietly make results worse. The UK Biobank × Arivale clinical-lab work (2026-10-05 to 2026-10-08, 16 scenarios) showed why: when only one cohort supplies an identifier type (Arivale's LOINC codes), the default `annotation_mode="missing"` replaces that cohort's name search with the code, so it lands on a more specific entry than the other cohort reaches by name; matched lab pairs fell from 31 to 2. No `annotation_mode` setting recovered the names-only result, and the lab category users would naturally pick (ClinicalFinding) is not where lab tests live in Biolink (ClinicalMeasurement).

Users starting out need the safe protocol built in: names first with the right category, identifiers used as matching input only when both cohorts share them, the other identifiers turned into a review queue, and both versions reported.

## Requirements

**The call**
- R1. A new public function, `harmonize_cohorts()`, in its own module (not in `biomapper.harmonize`, whose offline, no-network contract stays intact; `harmonize()` is unchanged and is reused for linking).
- R2. Inputs: two cohort tables (pandas DataFrame or a file path the package can already read), an entity type, the name column, and optional identifier columns per cohort. Labels for each cohort.
- R3. Entity type accepts a friendly alias as well as a Biolink category: `metabolites` → SmallMolecule, `proteins` → Protein, `genes` → Gene, `labs` → ClinicalMeasurement. The resolved category is recorded in the report and can be overridden.

**Protocol behaviour**
- R4. Names-only arm: both cohorts mapped by name with the resolved category, no identifiers supplied.
- R5. Identifier arm: identifier columns are classified per vocabulary as shared (both cohorts supply that vocabulary) or one-sided. Only shared vocabularies are supplied as mapping input. One-sided vocabularies are not supplied, and a warning names each one and explains why.
- R6. Both arms are linked with the existing `harmonize()` (name linking on for metabolites, as today), and the report gives both, plus a diff: links only in names-only, only in the identifier arm, in both.
- R7. Review queue from one-sided identifiers: for each row with a one-sided identifier, compare the entry its name mapped to with the entry its identifier resolves to; disagreements are listed with both entries, so a user can see cases like "Glucose" mapped by name to a glucagon-challenge entry while its LOINC code says serum glucose.
- R8. Link-basis breakdown per arm (node, identifier, name_exact, name_casefold), plus counts of matched pairs, one-to-one pairs, unresolved and errored rows per cohort.
- R9. If no identifier columns are given, the call runs the names-only arm only and says so.

**Output**
- R10. One report object with: settings used (category, aliases, which vocabularies were shared vs one-sided), both `HarmonizationResult`s, the diff, the review queue, warnings, a `summary()` dict, and a method to write everything to a timestamped output directory by default (tables as TSV, summary as JSON, pinned versions recorded) with an override path.
- R11. Pins in the report: package version, API engine release as reported, KRAKEN build from the API or Kestrel health endpoint, timestamp. No participant data is written beyond what the user passed in.

## Revision 2026-10-08 (feasibility review)

These override anything above that conflicts.

- **Identifier vocabularies are declared by the caller (amends R2, R5).** The client never resolves column names to vocabularies (the server does), so shared vs one-sided cannot be inferred. Callers pass, per cohort, a vocabulary to column(s) mapping, e.g. `{"LOINC": ["Labcorp LOINC ID", "Quest LOINC ID"]}`. Vocabulary keys are normalized with `biomapper.harmonize.curies.canonical_prefix` (KEGG.COMPOUND equals KEGG). Shared = intersection of normalized keys. The normalized vocabulary is what is sent as the identifier key.
- **Inputs are read client-side (amends R2).** A DataFrame (pandas imported lazily, detected by duck typing), a list of dicts, or a TSV/CSV path read with the standard library (skipping `#` comment lines). The server-side dataset stream is not used: it has no row keys. Mapping goes through `map_entities`, which keeps input order and length.
- **Row keys (new R12).** Optional key column per cohort; default key is row position plus name, so repeated names never collide. The same keys are used in both arms, the diff and the review queue, and passed to `harmonize()` via its `key` argument.
- **Requests (new R13).** Small batches (default 10) and a long timeout (default 300 s), both overridable; progress shown. The identifier arm re-maps only rows carrying a shared-vocabulary identifier and reuses the names-only result for the rest. When nothing is shared, the identifier arm is skipped and the report says so. Each arm's results are written to the output directory as soon as it completes, so a failure keeps finished work.
- **Review queue (refines R7).** A code-only pass (`annotation_mode="none"`) over just the rows carrying one-sided codes, one request per (row, vocabulary, value). Agreement = the name result's and the code result's identifier sets intersect (`curie_set` of chosen entry plus equivalents), not raw entry equality. Every row gets a status: agree, disagree, code_unresolved, name_unresolved_code_resolved, errored; all but agree are listed, with both entries. Several codes for one row are listed one line per code.
- **Pins (amends R11).** Package version; API `/health` status and self-reported version, labelled "self-reported, known stale"; Kestrel `/health` `kestrel_version` and `kg_build` (kg version, package version, build commit, Biolink, timestamp) with the Kestrel URL used (overridable); UTC timestamp. The existing `fetch_kg_build_info` moves to a pandas-free internal module so core installs can use it; benchmarks re-exports it. "API engine release" is reported as unavailable.
- **Module and dependencies (amends R1).** Module `biomapper/cohorts.py` exporting `harmonize_cohorts` and its report class; the function may be re-exported at the package root (no name clash with a submodule). `import biomapper` must keep working without pandas; core dependencies stay httpx, pydantic, python-dotenv.
- **One-to-one counts (R8)** use the helper already proven in the SOP notebook (`one_to_one`), ported into the package.
- **Name linking** follows the resolved category: on for SmallMolecule, off otherwise, as today.
- **Reference implementation.** `notebooks/ukbb_arivale_harmonization_sop.ipynb` is the reference: port its readers (comment skipping, `clean_id`, accession splitting), composite keys, `one_to_one` and pin cell. Migrating the notebook to call `harmonize_cohorts()` is a follow-up, not this change. `benchmarks/cross_cohort.py` is out of scope.
- **Success criteria testability.** Add a committed ClinicalMeasurement replay of the UK Biobank × Arivale lab panels (names-only arm and the Arivale LOINC code-only pass), pinned to KRAKEN kg 2.3.0 / 3dd08a5b, and a mapper-injection seam so tests replay it offline. The 37 pairs / 21 one-to-one target holds only at that build.

## Success Criteria
- On the UK Biobank × Arivale lab panels, `harmonize_cohorts(..., entity="labs")` reproduces the names-only ClinicalMeasurement result (S9: 37 pairs, 21 one-to-one), warns that LOINC is one-sided, and does not supply it.
- The review queue for that run lists the coded Arivale labs whose name and code land on different entries.
- On a metabolite pair where both cohorts supply HMDB, HMDB is treated as shared and supplied, and both arms are reported.
- `harmonize()` and the `biomapper.harmonize` subpackage behave exactly as before (existing tests unchanged).

## Scope Boundaries
- No engine changes (name-vs-code tie rule, several categories per request, code-guided candidate selection): those are biomapper2 work.
- No roll-up links; a later version may add roll-up as guarded review suggestions.
- No new name-matching rules; `link_by_name` stays metabolite-only as today.
- Not changing defaults of existing functions (`map_entities` etc. still default to SmallMolecule).

## Key Decisions
- New function, own module, `harmonize()` reused: Trent, 2026-10-08.
- `labs` defaults to ClinicalMeasurement, recorded and overridable: Trent, 2026-10-08. Revisit if a second cohort pair disagrees.
- One-sided identifiers become a review queue, not input: the lab tests showed supplying them lowers matches in every `annotation_mode`.
- Shared identifiers are supplied: symmetric identifiers do not create the specificity mismatch; the identifier arm is still reported beside names-only so the effect is visible.

## Dependencies / Assumptions
- Classification of identifier columns into vocabularies relies on the column-name-to-vocabulary resolution the package already uses for provided identifiers (verify in planning; known trap: vocabulary is resolved from the column name).
- The review queue needs each one-sided identifier resolved on its own; planning decides whether that is a separate code-only mapping pass (`annotation_mode="none"`) or a direct lookup.

## Outstanding Questions

### Deferred to Planning
- [R7][Technical] Cheapest way to resolve one-sided identifiers for the review queue without a third full mapping pass.
- [R2][Technical] Which file formats to accept, reusing the existing dataset readers.
- [R11][Needs research] Where the API exposes the engine release and KRAKEN build for pinning.

## Next Steps
-> /ce:plan
