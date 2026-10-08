---
title: "feat: harmonize_cohorts(), the harmonization protocol as one call"
type: feat
status: active
date: 2026-10-08
origin: docs/brainstorms/2026-10-08-harmonize-cohorts-requirements.md
---

# feat: harmonize_cohorts()

**Target repo:** biomapper (dev fork `trentleslie/biomapper`, branch `feat/harmonize-cohorts` off `origin/main`; PR to the fork's `main`, never straight to `Phenome-Health/biomapper`).

## Overview

Add `harmonize_cohorts()` in a new `src/biomapper/cohorts.py`: read two cohort tables client-side, map both by name with the right category, classify caller-declared identifier vocabularies as shared or one-sided, run an identifier arm only for shared vocabularies, build a review queue from one-sided codes, link each arm with the existing pure `harmonize()`, and return one report that can write itself to a timestamped directory. See origin for the evidence (UK Biobank × Arivale labs, 16 scenarios).

## Requirements Trace

- R1, module/exports, pandas-free core -> Unit 1
- R2 inputs, R12 keys -> Unit 2
- R3 aliases and categories, name-linking rule -> Unit 2
- R4, R5, R9, R13 arms, shared vs one-sided, batching, skip rules -> Unit 3
- R7 review queue -> Unit 4
- R6, R8 diff, link bases, one-to-one -> Unit 5
- R10, R11 report, output dir, pins -> Unit 5 (pins helper in Unit 1)
- Success criteria (S9 replay, HMDB shared case, `harmonize()` unchanged) -> Unit 6

## Scope Boundaries

- No engine (biomapper2) changes; no roll-up; no new name-matching rules; existing defaults unchanged.
- `harmonize()` and `biomapper.harmonize` untouched (existing tests unchanged).

### Deferred to Separate Tasks
- Migrate `notebooks/ukbb_arivale_harmonization_sop.ipynb` to call `harmonize_cohorts()`: follow-up PR.
- Release (version bump, PyPI): manual release chain, Trent's call.

## Context & Research

- `src/biomapper/harmonize/linking.py`: `harmonize(a, b, *, a_label, b_label, key, link_by_name)` pure; raises on duplicate keys and on reserved labels (`n_links`, `links_by_basis`, `n_name_only_links`, `n_name_match_withheld`); `HarmonizationResult.summary()`; `BASIS_ORDER`.
- `src/biomapper/harmonize/curies.py`: `canonical_prefix`, `curie_set` (identifier-only, structure namespaces excluded).
- `src/biomapper/client.py`: `map_entities(..., entity_type, annotation_mode, ...)`, `DEFAULT_MAX_BATCH_SIZE = 1000`, chunk errors become per-row errored results, order and length preserved; `health_check()` returns a stale self-reported version.
- `src/biomapper/models.py` `MappingResult`: `query_name`, `resolved`, `chosen_kg_id`, `kg_equivalent_ids`, `identifiers`, `error`; no provided-id field.
- `src/biomapper/benchmarks/provenance.py`: `fetch_kg_build_info` / `KgBuildInfo` (reads Kestrel `/health`); `benchmarks/__init__` imports pandas transitively.
- `notebooks/ukbb_arivale_harmonization_sop.ipynb`: readers (`clean_id`, accession splitting, `#` comment skipping), composite keys, `one_to_one()`, batched mapping (batches of 10, timeout 300), pin cell, timestamped outputs. Reference implementation to port.
- Tests: respx mocks and conftest helpers (`make_batch_entry`, `make_ndjson_body`, `HEALTH_RESPONSE`); `test_packaging_extras.test_core_install_stays_light`; coverage gate 80%.
- Replay data: `notebooks/data/ukbb_arivale/replay/*.json.gz` (ClinicalFinding). Lab runs with ClinicalMeasurement exist outside the repo (`harmonization_runs/labs_edge_rollup_20261008T171112Z`, S9 and S16 mappings) and can seed a new replay.

## Key Technical Decisions

- **Mapper-injection seam:** `harmonize_cohorts(..., mapper=None)`; default wraps `map_entities` with the batching and timeout policy; tests pass a replay or fake mapper. Keeps network out of unit tests.
- **Vocabulary declaration** `{vocab: [columns]}` per cohort, normalized with `canonical_prefix` plus upper-casing.
- **Agreement** in the review queue by `curie_set` intersection of chosen entry plus equivalents.
- **Pins** via a new pandas-free `src/biomapper/_provenance.py` holding `fetch_kg_build_info`/`KgBuildInfo` (moved; `benchmarks.provenance` re-exports for compatibility).
- **Output writing** is a report method with a default timestamped directory under the current working directory's `biomapper_runs/` and an override; per-arm results written as each arm completes.

## Implementation Units

- [ ] **Unit 1: Module skeleton, exports, pandas-free provenance**

**Goal:** `src/biomapper/cohorts.py` with the public function signature and report dataclass stubs; move `fetch_kg_build_info` to `src/biomapper/_provenance.py`.
**Requirements:** R1, R11
**Dependencies:** none
**Files:** Create `src/biomapper/cohorts.py`, `src/biomapper/_provenance.py`; Modify `src/biomapper/__init__.py` (export `harmonize_cohorts`, `CohortHarmonizationReport`), `src/biomapper/benchmarks/provenance.py` (re-export); Test `tests/test_cohorts_imports.py`.
**Test scenarios:**
- `import biomapper` works with pandas absent (simulate by blocking the import); `biomapper.harmonize_cohorts` exists.
- `biomapper.benchmarks.provenance.fetch_kg_build_info` still importable and identical.
- `test_core_install_stays_light` still passes.
**Verification:** existing test suite green.

- [ ] **Unit 2: Inputs, keys, categories**

**Goal:** Normalize inputs into records with stable keys; resolve entity aliases.
**Requirements:** R2, R3, R12
**Dependencies:** Unit 1
**Files:** Modify `src/biomapper/cohorts.py`; Test `tests/test_cohorts_inputs.py`.
**Approach:** accept DataFrame (duck-typed), list of dicts, or TSV/CSV path (stdlib csv, skip `#` lines); port `clean_id` and accession splitting; keys from `key_column` or `"{row}|{name}"`; aliases `metabolites`/`proteins`/`genes`/`labs` to Biolink categories, raw `biolink:` strings pass through; name linking on only for SmallMolecule.
**Test scenarios:**
- Duplicate names get distinct default keys; explicit key column respected; duplicate explicit keys raise naming the column.
- `#`-prefixed lines skipped; empty name rows excluded and reported.
- `labs` resolves to `biolink:ClinicalMeasurement`; override honoured; unknown alias raises listing valid ones.
- Vocabulary declaration normalizes `KEGG.COMPOUND` and `KEGG` to one key; a column named in the declaration but missing from the table raises.
**Verification:** records and keys deterministic across calls.

- [ ] **Unit 3: Arms and identifier classification**

**Goal:** Names-only arm for both cohorts; identifier arm for shared vocabularies only.
**Requirements:** R4, R5, R9, R13
**Dependencies:** Unit 2
**Files:** Modify `src/biomapper/cohorts.py`; Test `tests/test_cohorts_arms.py`.
**Approach:** shared = intersection of normalized vocab keys; warnings name each one-sided vocab; identifier arm re-maps only rows with a shared-vocab value and reuses names-only results for others; skipped with a recorded reason when nothing is shared or no vocab given; batching (default 10) and timeout (default 300 s) overridable; progress optional.
**Test scenarios:**
- Both cohorts declare HMDB: HMDB shared, identifier arm runs, only rows with HMDB values re-mapped (fake mapper call log).
- Only one cohort declares LOINC: warning emitted, identifier arm skipped, reason recorded.
- No vocab declared: names-only only (R9).
- A chunk error in the fake mapper produces errored rows, not an exception; they are counted.
**Verification:** mapper call log shows no requests carrying one-sided identifiers.

- [ ] **Unit 4: Review queue from one-sided identifiers**

**Goal:** Statused list comparing name entry and code entry per one-sided code.
**Requirements:** R7
**Dependencies:** Unit 3
**Files:** Modify `src/biomapper/cohorts.py`; Test `tests/test_cohorts_review.py`.
**Approach:** code-only pass (`annotation_mode="none"`) over rows carrying one-sided codes, one request per (row, vocab, value); statuses agree / disagree / code_unresolved / name_unresolved_code_resolved / errored by `curie_set` intersection; queue lists all non-agree rows with key, name, vocab, code, name entry, code entry, status.
**Test scenarios:**
- Glucose-style case: name entry `UMLS:C5781949`, code entry `LOINC:2345-7`, no shared identifiers -> disagree.
- Equivalent entries (different chosen ids, overlapping equivalents) -> agree, not listed.
- Code unresolved (e.g. placeholder "SOLOINC") -> code_unresolved.
- Two LOINC columns on one row -> two lines.
**Verification:** statuses cover every coded row exactly once per code.

- [ ] **Unit 5: Report, diff, link bases, output, pins**

**Goal:** `CohortHarmonizationReport` with summary and writer.
**Requirements:** R6, R8, R10, R11
**Dependencies:** Units 3, 4
**Files:** Modify `src/biomapper/cohorts.py`; Test `tests/test_cohorts_report.py`.
**Approach:** link each arm with `harmonize(..., key=...)`; diff = links only in names-only / only in identifier arm / both (by key pair); per arm: pairs, one-to-one (ported `one_to_one`), link-basis counts, unresolved and errored per cohort; settings, warnings, pins; `write(path=None)` creates `biomapper_runs/harmonize_cohorts_<UTC timestamp>/` with TSVs (links per arm, diff, review queue), `summary.json`, `settings.json`; arms written as they complete; prints the output path.
**Test scenarios:**
- Diff correct on a constructed pair of arms.
- one_to_one matches the notebook's definition on a fixture with a 3 × 2 grid (6 pairs, 0 one-to-one).
- Labels colliding with reserved summary keys raise before any mapping.
- `write()` creates files; no absolute paths inside the JSON beyond the output path itself; pins present with "self-reported, known stale" label on the API version.
**Verification:** `summary()` round-trips through JSON.

- [ ] **Unit 6: Replay fixture and success-criteria tests; docs**

**Goal:** Offline proof on real panels; user docs.
**Requirements:** success criteria
**Dependencies:** Units 1 to 5
**Files:** Create `tests/fixtures/cohorts_labs_cm/` (replay of UK Biobank × Arivale labs: names-only ClinicalMeasurement both cohorts, plus Arivale LOINC code-only results; pinned kg 2.3.0 / 3dd08a5b; panel metadata only), `tests/test_cohorts_replay.py`; Modify `README.md` (new section), `CHANGELOG.md` if present.
**Approach:** build the replay from the existing lab runs if their stored results carry the `MappingResult` fields; otherwise re-map once live with the pinned client and commit the result with its pins. A replay mapper serves results by (name, identifiers, annotation_mode, entity type).
**Test scenarios:**
- Labs replay: names-only arm gives 37 pairs, 21 one-to-one; LOINC flagged one-sided with a warning; identifier arm skipped; review queue non-empty and includes glucose.
- Metabolite fixture with HMDB on both sides: HMDB shared; both arms reported.
- Existing `test_harmonize_*` suites unchanged and passing.
**Verification:** full suite passes offline with coverage at or above 80%.

## Review resolutions (2026-10-08)

- **Identifier key sent:** the normalized vocabulary key (after `canonical_prefix` and upper-casing) is the key sent in `identifiers` to `map_entities`, never the declared column-form string.
- **Pins:** API engine release is recorded as `unavailable`; graph version comes from Kestrel `kg_build`. The Kestrel URL actually used (default or override) is recorded. Unit 5 tests assert both.
- **Replay is mandatory:** if stored lab runs lack the needed `MappingResult` fields, do one live re-map with the pinned client against kg 2.3.0 / 3dd08a5b (verify the build from Kestrel `/health` before mapping; stop if it differs) and commit the result. Unit 6 is not done without a committed replay.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Replay cannot be built from stored lab runs | One live re-map with pins, committed |
| Production API slow or wedging | Small batches, long timeout, per-arm persistence |
| Kestrel pin describes a different graph than the API used | Record the Kestrel URL; label the pin as Kestrel's |
| Alias table drifts from server aliases | Aliases documented; raw Biolink categories always accepted |

## Sources & References

- Origin: docs/brainstorms/2026-10-08-harmonize-cohorts-requirements.md
- Lab scenario runs: harmonization_runs/labs_edge_rollup_20261008T171112Z (outside repo)
- SOP notebook: notebooks/ukbb_arivale_harmonization_sop.ipynb
