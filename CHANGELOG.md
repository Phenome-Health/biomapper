# Changelog

All notable changes to the `biomapper` Python client are recorded here.
This project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.5.4] - 2026-09-28

### Added

- **MetaboliteAnnotator is resumable.** The arm is 4,314 names per ion mode across four vocab
  passes and two modes, 34,512 API entities, measured at 3.31 s/entity (about 31.7 h). The
  2026-09-26 run spent 11.9 h on the positive mode, was terminated, and kept nothing, because
  results reached disk only when a whole vocab pass finished. Each ion mode now keeps an
  `entity_cache.jsonl` in its directory: after every `/map/batch` response the batch's successful
  results are appended and fsynced before the next request, so that line is the durable checkpoint
  for `(ion_mode, vocab, batch)` and a kill loses at most the batch in flight. Re-running into the
  same suite dir (`--out <dir>`) looks every entity up by name + target vocab and sends only the
  misses; a completed pass re-assembles with zero requests and a byte-identical scored frame.
  Committing a key twice is a no-op. Errors are never cached, so a transient outage is retried on
  resume rather than frozen into a miss. The cache is pinned to the backend (endpoint, Kestrel
  service, KG version and commit, entity type, annotation mode) and a resume against a different
  build is refused rather than mixed in; with unpinned provenance the arm runs without a cache,
  because `unknown == unknown` cannot tell two builds apart, and the same holds when the KG
  version is known but the KG commit is not. Request options (entity type, annotation mode,
  annotators, candidate limit, the prefer_* flags) are folded into the cache key when set, so a
  reused cache dir with different options misses. The cache is read as bytes, so a kill that
  splits a multi-byte UTF-8 character is a torn tail, and a torn pin line is re-written before any
  answer is cached. A torn final line from a kill
  mid-write is dropped; a corrupt earlier line is refused. `checkpoint.json` beside the cache
  summarizes per-vocab progress for monitoring; `complete` means every entity is
  answered, not merely that every batch was attempted.

  `ApiMapper` gains optional `cache=` and `pacer=` arguments. Pacing follows PR #9: the pacer is
  waited on immediately before every outgoing request, retries included, and never for an entity
  answered from cache. `request_counters` gains `cache_hits`, kept out of `entities` and `seconds`
  so the throughput figure describes the deployment rather than how much work was already on disk.
  `raw_response` and `kestrel_results` are not cached (neither is read by the scored surface, and
  `raw_response` alone would add hundreds of MB).

### Fixed

- **`all --only` no longer drops arms from the manifest silently.** An arm left out of a subset run
  was absent from `suite_manifest.json` entirely: no entry, no reason, indistinguishable from an arm
  that fell out of the registry by accident. The 2026-09-27 run had to write `excluded_arms.json`
  beside the manifest to explain MetaboliteAnnotator's absence. Every `SUITE_DATASETS` arm not
  selected is now a `status="skipped"` entry with the operator's reason (`skip_origin="operator"`),
  and the command refuses to start (exit 2, before any network call) if any omitted arm has none.
  Reasons come from `--skip ARM=REASON` (repeatable; without `--only` the run covers every arm not
  skipped) or `--exclusions FILE`, which takes the `excluded_arms.json` shape and embeds each
  record's evidence in the entry as `exclusion_record`. `run_suite(omitted=...)` enforces the same
  rule for library callers. The manifest gains `full_suite` and `operator_skipped`, and the README
  states `Scope: SUBSET` when arms were left out: a subset run can be `complete` and still not be
  the full benchmark. `arm NAME` records the invocation itself as the reason for the other arms.
  A skipped arm no longer carries a label in the README (the 2026-09-27 run printed
  `swisslipids | skipped | accuracy_candidate`), a scalar `--exclusions` file is an argparse error
  rather than a traceback, and operator reasons are escaped so a `|` or newline cannot split a
  README row.

- **A repeated arm runs once and is recorded once (#21).** `--only hgnc hgnc`, or any repeated name
  passed to `run_suite`, ran the arm twice, wrote two manifest entries for it, and counted it twice
  in `n_ok`. The selection is de-duplicated in `run_suite` (first occurrence wins, order kept), which
  covers the library and the CLI. An arm named in both `--only` and `--skip` was already refused
  before any network call; a test now pins that.

- **Each HGNC namespace is scored from its own target-vocab run (#20).** `run_hgnc` maps once per
  target vocab (ENSEMBL, NCBIGene, UniProtKB) but scored all three namespaces from the ENSEMBL run.
  Since node selection became vocab-steered, the three runs agree on only 585 to 628 of 1500 rows,
  so reading NCBIGene cross-refs off the Ensembl-steered pass produced the apparent NCBIGene
  "inversion". New `score_curie_per_target_run` scores each namespace only from its own run and
  records `source_vocab_run`, `source_tsv` and `source_tsv_sha256` per entry; a missing run is
  refused (`MissingTargetRunError`), never borrowed. `per_namespace_scoring: "own_target_run"` marks
  the new figures. The any-namespace roll-up stays on the primary run, still flagged non-quotable,
  and is labelled `rollup_source_vocab_run`. A diagnostic-only `forced_hit_diagnostic` counts rows
  where a namespace with no gold ID still returned one. Re-scored offline on
  `suite_20260927T180639Z` (kg 2.3.0, git `3dd08a5b`): ENSEMBL 1343/1413 (95.0%), NCBIGene
  1453/1475 (98.5%), UniProtKB 617/666 (92.6%).

- **Cross-cohort PubChem lookups reach PubChem again, and legacy HMDB accessions resolve (#17).**
  PubChem rejected urllib3's default TLS context with `503 PUGREST.ServerBusy` on every
  certify/readjudicate request, while the stdlib context got 200. Three certification passes had
  returned `blocks_resolved 0` and were misdiagnosed as an outage. Both PubChem callers now default
  to `pubchem_session()`, which uses `ssl.create_default_context()`. Separately, 5-digit HMDB
  accessions (`HMDB13127`) 404 in PubChem's RegistryID index and became false clean misses; the
  certify route now tries the 7-digit form first, then the original. `cross_cohort_certify` gains
  `--min-interval-s`. **This changes a published number:** on the kg 2.3.0 NECS/Arivale run,
  certified over adjudicable moves from 74.3% (398/536) to 61.4% (452/736). All 146 newly refuted
  links are lipid pairs linked across distinct species.

### Documentation

- The harmonization tutorial notebook was rewritten around `harmonize.link_by_intersection`,
  keyless and build-pinned, and now declares a kernel that has biomapper installed, so a reader's
  run cannot execute unpinned while printing pinned provenance (#16). Notebooks are not part of
  the wheel.

## [1.5.3] - 2026-09-25

### Fixed

- **The `benchmarks` extra shipped empty in 1.5.2 and now carries its dependencies.**
  `pip install 'biomapper[benchmarks]==1.5.2'` installed the core client and nothing else, so
  `python -m biomapper.benchmarks all` failed on the first `import pandas`. All four extras were
  affected (`metabolon`, `notebook`, `benchmarks`, `all`).

  Cause: the extras were declared correctly in `[tool.poetry.extras]`, but the packages they name
  were declared only in `[tool.poetry.group.*.dependencies]`. Poetry groups are a local development
  concept and are never written into wheel metadata, so the published 1.5.2 wheel carried
  `Provides-Extra: benchmarks` with **no** `Requires-Dist` gated on any extra. The optional
  dependencies now live in `[tool.poetry.dependencies]` with `optional = true`, which is the only
  place Poetry reads them from when building extras, and the three redundant feature groups are
  removed.

  Verified against the built wheel rather than the local environment: 1.5.3 metadata carries
  `pandas`, `openpyxl`, `requests`, `rdkit` and `defusedxml` each gated on
  `extra == "benchmarks" or extra == "all"`, and a clean `--target` install of
  `biomapper-1.5.3-py3-none-any.whl[benchmarks]` resolves all five from the install tree.

  **Why CI did not catch it, which is the part worth remembering.** `poetry install --all-extras`
  also installs every non-optional group, so the group declarations satisfied the test suite while
  the published artifact was broken. Green CI was actively misleading: it exercised a dependency
  shape that no installing user could obtain. `tests/test_packaging_extras.py` now reads
  `pyproject.toml` directly, which is the artifact that governs publishing, and fails if an extra
  names a package that is not an optional main dependency. Those tests fail 8 of 12 against the
  1.5.2 configuration.

  A source-level check is still not sufficient on its own, so CI now also inspects the BUILT wheel
  via `scripts/check_wheel_extras.py`. A future change to the build backend or to Poetry could
  reproduce the same symptom from a different cause and leave a config-only test green. Run against
  the real 1.5.2 wheel the checker reports all four extras as gating no requirements and exits 1.

- **The run README no longer calls a coverage arm "accuracy".** The arm table resolved its label as
  `role or circularity_label`, and `role` is a static config field that DEFAULTS to `"accuracy"`.
  The table therefore called RefMet accuracy while the same run's `circularity` register called it
  coverage, on the one arm the July independence audit singled out as the at-risk case. The
  per-run circularity label now wins, the declared role is shown beside it, and a disagreement is
  flagged in the row rather than resolved silently in favour of the more flattering reading.

- **`MONTI_PUBLISHED` reverted to the tabulated values.** 1.5.0 changed the published comparator from
  the paper's prose, moving Xu to 385 and BLSA to 188. Both moves were wrong. Two tabulated sources
  agree with the original values and were not consulted at the time:

  - Supplement **MOESM6, sheet "Table 2. Datasets"**, carries an explicit `# Overlap` column with NECS
    as the reference row: LLFS 163, Arivale 615, **BLSA 99**, **Xu 432**.
  - Supplement **Table S03** (signatures, 1052 metabolite rows), counting rows with any non-null
    statistic per cohort, derives **Xu 432 exactly**, LLFS 162, and **BLSA 88**. 88 sits 11 from 99 and
    100 from 188, which rules 188 out.

  The prose sentence that produced 188 appears to have carried the paper's **LLFS lipid count**
  ("408 metabolites (188 lipid and 220 polar)"). That coincidence is retained in
  `MONTI_PUBLISHED_PROVENANCE` because it is the evidence, not a footnote.

  A partial correction to an earlier claim as well: the MAIN TEXT's Table 2 is "Age-only markers" and
  is not an overlap table, but the SUPPLEMENT's "Table 2. Datasets" is. The earlier statement was half
  right.

  `MONTI_PUBLISHED_SUPERSEDED` now holds the prose-derived values, `MONTI_S03_DERIVED` records the S03
  counts, and every provenance entry cites the table rather than a quote.

### Changed

- **The suite now names which structure figure is the published "strict" one.** Hajjar-100 scores
  92/100 on the KG record alone and 95/100 when an external Metabolomics Workbench or PubChem
  lookup on the node's NAME is allowed to fill in a structure-less node. The suite reported the
  second as `comparable_core`, its headline, while the independence audit defined published strict
  as the first. Both were emitted under the metric name `top1_accuracy`, so nothing in the artifact
  told a reader which definition they were holding, and quoting the headline as "strict" silently
  changed the metric between documents.

  `comparable_core_strict_kg_only` is new and is the published strict figure (decided 2026-09-23).
  It is computed in-run rather than derived by hand from `per_row`. Verified against the real
  2026-09-24 Hajjar artifact: 92/100, against `comparable_core` 95/100 and equivalence-set 95/100.

  Every variant now carries a `definition` string and an `is_published_strict` boolean, the metric
  names are distinct (`top1_accuracy_strict_kg_only` against `top1_accuracy_with_name_fallback`),
  and a test asserts exactly one variant claims to be the published strict figure. Nothing is
  removed: `comparable_core` keeps its key and its value, so existing readers are unaffected.

  For an artifact written before that field existed, the campaign report RECOMPUTES the strict
  figure from `per_row` (correct and not `needed_fallback`) rather than substituting
  `comparable_core`. Substituting it would print the name-fallback number under a "Top-1 (strict)"
  heading, which is the confusion being fixed. Where neither the field nor `per_row` is available
  the cell reads `n/a (strict not in artifact)`: a blank cell reads as zero, and a wrong number
  under a strict heading is unrecoverable. Verified on the real 2026-09-25 Hajjar artifact, which
  predates the field: recomputation returns 92/100, matching the hand-derived figure, against a
  stored `comparable_core` of 95/100.

- Run manifests record `monti_s03_derived` per pair alongside the published overlap.

**Published to PyPI:** 0.1.0 through 1.4.0, 1.5.2, and 1.5.3. Entries tagged *(not published)* were
version bumps that landed in this repository but were never uploaded to the release index, so
`pip install biomapper==<that version>` will not resolve. This matters for any claim about which
release first contained a module: the source tree and the PyPI index diverge across the 1.5.x
series.

## [1.5.2] - 2026-09-24

### Fixed

- **PubChem pacing now fires on the request path, not on cache hits.** `cross_cohort_certify` called
  `Pacer.wait()` at the call site, immediately before `_cached_resolve`, so it slept and advanced the
  pacing clock even when the resolver answered from cache. The cohort panel de-duplicates on NAME,
  not on identifier, so distinct names can share a PubChem CID or HMDB accession; each repeat cost up
  to a full interval without issuing a request and delayed the next real lookup on top.

  `PubChemInChIKeyResolver` now takes an optional pacer and consults it inside `_resolve_txt`, which
  runs only after `_cached_resolve` has missed. Default `None` leaves the suite unchanged. This is
  what `Pacer`'s own docstring already prescribed ("only on a cache miss") and what the
  re-adjudication resolver already did.

  Performance only: no count, verdict or published number changes.

## [1.5.1] - 2026-09-24 *(not published)*

### Fixed

- **The package version has one source of truth again, and every run records the client commit.**
  `pyproject.toml` said `1.5.1` while `src/biomapper/__init__.py` carried a hardcoded
  `__version__ = "1.4.0"`, and the newest tag was `v1.4.0`. Three answers to "what version is
  this?" meant no run manifest could name its own client unambiguously, which makes every number
  the suite produces unauditable after the fact.

  `__version__` is now derived from the installed distribution metadata that Poetry builds from
  `pyproject.toml`, so `pyproject.toml` is the only place a version literal may appear.
  `provenance.package_version()` reads the same source, so the manifest field and the importable
  attribute cannot disagree. `tests/test_version.py` fails the build if a literal reappears
  anywhere under `src/`, because a convention nobody checks is one that drifts back.

  A version string alone was never sufficient, so the manifest now also records
  `client_git_commit` and `client_git_dirty`, captured at run start. The installed metadata does
  not move when the working tree does: a 17-hour suite run on an editable checkout absorbed 20+
  commits mid-flight while still reporting one confident-looking version. The commit is what
  closes that gap. Both fields read `unknown` / `null` for an installed wheel, which is correct
  rather than a failure, since a wheel's version is its identity. Capture never raises; provenance
  must not be able to abort a run that is otherwise fine.

- **The corrupt `4000` gold sentinel no longer becomes a comparable InChIKey block.**
  `cross_cohort_certify.necs_gold_blocks` called `first_block` on the MOESM5 gold cells without
  screening them, so the documented corrupt `4000` placeholder became a 4-character "block". A
  4-character block can never equal a real 14-character one, so every link through that row returned
  REFUTED, and a refuted verdict reads as a wrong molecule rather than as a broken gold cell.

  Found from `docs/solutions/logic-errors/benchmark-harness-reports-plausible-wrong-number-2026-09-23.md`,
  which records that `gold_structure.has_gold_structure` exists precisely to reject this sentinel and
  that nothing in the scoring path called it. That gap applied to this module too.

  Every candidate key is now screened with `has_gold_structure` before `first_block`, and the rejects
  are counted on the card as `rejected_gold_values` / `n_rows_rejected_for_corrupt_gold` rather than
  silently dropped.

  The card carries three separate counts, because one row can carry a corrupt cell in either
  vintage or both: `n_corrupt_gold_cells` (cells), `n_rows_with_any_corrupt_gold` (rows touched),
  and `n_rows_excluded_by_screen` (rows that actually stopped contributing a block, listed by name).
  Naming a cell count after rows overstates the damage wherever both vintages are corrupt.

  Measured on the real supplement: `4000` appears on 10 rows. Nine also carry a usable standard-vintage
  key, so exactly one row (`1-lignoceroyl-gpc (24:0)`) previously reached the block set unscreened and
  would have produced one spurious refutation. Screened block count 944 to 943.

  **This changes a previously reported number.** The two-vintage first-block disagreement rate was
  reported as 48 of 691 (6.9%); screened, it is **39 of 682 (5.7%)**. The corrupt rows were inflating
  both the numerator and the denominator.

## [1.5.0] - 2026-09-24 *(not published)*

### Added

- **`biomapper.benchmarks.cross_cohort` and `cross_cohort_certify` - the Monti/NECS cross-cohort
  arm, finished rather than stubbed.** Phase 2b migrated the 11 external benchmark suite arms but
  not the cross-cohort machinery, so the arm still had to be run from an engine checkout, which
  would have pinned its numbers to a different backend than the suite's. These modules close that
  gap: the whole arm now runs through the package against a deployment, so one provenance story
  covers the suite, the cohort analyses, and the cross-cohort comparison.

  Ported from the biomapper2 engine at `origin/dev`, commit
  `1ffb571e54fe028ef0ae4e748fc2e7ec093ee603`:

  - `benchmarks.adapters.cohort_panel` - the arivale / xuetal / llfs / blsa panels, with the
    exclusion accounting (blank name, unnamed vendor feature, de-duplication) and the
    `certifiable` determination unchanged.
  - `benchmarks.scorers.cross_cohort_overlap` - the cohort-shaped linker. It deliberately
    EXCLUDES `INCHIKEY`, `INCHI` and `SMILES` from the linker: linking on a structure hash would
    make the downstream structural certificate circular and precision 100% by construction. The
    comments explaining that are preserved verbatim.
  - `benchmarks.scorers.link_certificate` and
    `benchmarks.scorers.independent_link_certificate_overlap` - the KG-independent link
    certificate, which refuses rather than certifying off a KG-derived structure.
  - `benchmarks.scorers.arm_b_baseline` - Monti's own published method, reconstructed on the
    identical row set.

  The driver is rewired from `biomapper2.mapper.Mapper` to
  `benchmarks.api_mapper.ApiMapper`, exactly as the suite arms were, so provenance names the graph
  that served the answers instead of a client checkout.

### Changed

- **The Monti published-overlap table is corrected.** `arm_b_baseline.MONTI_PUBLISHED` read
  `{"arivale": 615, "xuetal": 432, "llfs": 163, "blsa": 99}` and attributed all four values to
  "Monti Table 2". Re-read against the paper (DOI `10.1007/s11357-026-02174-2`), the citation and
  two of the four values were wrong:

  - Table 2 is "Age-only markers", not an overlap table. The overlaps are in the Methods section
    "Datasets harmonization" and in the per-cohort descriptions.
  - NECS to Xu is **385** in the harmonization methods. The Xu cohort description separately says
    432, so the paper contradicts itself on this one pair. 385 is used because it comes from the
    sentence describing the procedure Arm B reconstructs, and that sentence's Arivale value agrees
    with the Arivale cohort description.
  - NECS to BLSA is **188**. The old 99 was the BLSA to LLFS overlap ("Ninety-nine metabolites
    were in common with the LLFS"), a different pair entirely.

  Both the corrected and the superseded tables ship, as `MONTI_PUBLISHED` and
  `MONTI_PUBLISHED_SUPERSEDED`, with a per-pair quote and section in
  `MONTI_PUBLISHED_PROVENANCE`, so a changed number is always traceable to a reason.

### Fixed

- **An errored row is no longer indistinguishable from an unresolved one in the cross-cohort
  path.** Both come back with an empty CURIE set, so folding them together reports a
  non-resolution that never happened and understates resolution by however many rows the
  deployment dropped. `cross_cohort.errored_names` counts them apart, `repair_errored_rows`
  re-maps only the failed rows and writes them back into their original positions, and any row
  that still errors is reported in the manifest with the affected panel's counts labelled a floor.
  The deployment did return 5xx under concurrent load during this work, so this is a live hazard.

### Fixed (review round 2)

- **Checkpoints now carry the backend that answered them.** Panels resolve as separate processes and
  are combined by `--link-only`, which previously fetched `/health` once at finalization and stamped
  it on every checkpoint. A deployment or graph-build change between panel runs would then present a
  mixed-backend result as one pinned run. Each panel writes a provenance sidecar at resolve time, and
  finalization compares `endpoint`, `kestrel_version`, `kg_version`, `biolink_version`,
  `build_timestamp` and `git_commit` per panel. A mismatch, or a checkpoint with no sidecar at all,
  raises `BackendDriftError`. `--allow-unpinned-checkpoints` is an explicit escape that records the
  gap in the manifest instead of letting the run read as pinned.
- **A missing per-pair link artifact no longer reads as zero links.** `manifest.json` existing does
  not imply the artifacts do. `read_links` raises `MissingLinkArtifactError`, and each link file's
  row count is cross-checked against the manifest's `arm_m_links`.
- **A transient PubChem failure is no longer overwritten by a clean fallback miss.** A `lookup_failed`
  on the CID route followed by a `clean_miss` on the HMDB route previously reported a retryable run
  artifact as a genuine coverage gap. The failure is sticky unless a fallback actually resolves.

### Added (review round 2)

- **`biomapper.benchmarks.cross_cohort_readjudicate`.** Every non-certified case is checked against
  PubChem's name index, which is a different lookup than either side of the certificate used. A
  first-block mismatch is classified as a tautomer, charge or salt artifact only when the outside
  source corroborates BOTH sides and their heavy-atom compositions match with a mass gap consistent
  with the hydrogen-count difference. Outcomes separate `necs_gold_suspect` from `cohort_id_suspect`
  from `genuine_structural_disagreement` from `outside_source_unresolved`, so a refusal that could
  not be checked is never folded into one that was.

### Fixed (review round 3, from the first live panel)

- **The client commit is captured at run start, not at sidecar write.** The first live panel exposed
  this: a sidecar is written when its panel finishes, 29 minutes after launch here, and the working
  tree was being read at that moment. The sidecar therefore named a commit that was not the code the
  process had loaded. A provenance record naming the wrong code is worse than one naming none,
  because it looks authoritative. The late-capture path remains as a fallback but labels itself.
- **An identical molecular formula is no longer called a tautomer artifact.** `composition_relation`
  replaces a boolean that treated same-formula-different-first-block as an artifact. It is not
  decidable that way: Pro-Leu against Leu-Pro, and leucine against isoleucine, share a formula and a
  mass while being genuinely different molecules, and keto-enol tautomers also share a hydrogen
  count. Since the first block IS the connectivity hash, that case is equally a tautomer and a
  constitutional isomer. Only a hydrogen-count difference confirmed by the mass gap is now called
  `charge_or_protonation_artifact`; the ambiguous case becomes
  `same_formula_different_connectivity` and says it must not be counted in either direction.
- **`outside_source_hit_a_derivative` is a named outcome.** Observed live: production resolved the
  LLFS row "Prolylleucine" to `RM:0137550` (block `ZKQOUHVVXABNDG`, CID 3527720, C11H20N2O3, the free
  Pro-Leu dipeptide) and the API certificate marked it `contradicted` because its own external
  fallback returned `YCYXUKRYYSXSLJ` (CID 3584406, Cbz-protected Z-Pro-Leu, C19H26N2O5). BioMapper
  was right and the certificate's independent source was wrong, because PubChem's name index ranks
  protected forms above free peptides. Detected by a containing formula plus a mass gap over 50 Da.

### Notes

- Cohorts that ship names only (NECS, Xu, LLFS, BLSA have no vendor identifier column) are
  `certifiable=False`. Their links are countable but never structurally certifiable, and
  `cross_cohort_certify` reports that as refused **by construction**, not as an uncertified
  failure. Only Arivale carries structure-resolvable vendor identifiers in this arm.
- Every certificate verdict here is an InChIKey **first block** comparison, which is neither
  tautomer- nor charge-invariant: it over-flags tautomers as refuted and silently accepts a
  stereoisomer error whenever a side lacks the stereo layer. `stereo_checked` records which
  happened. The NECS curated gold also carries roughly 5% InChIKey errors, so a refuted verdict is
  a candidate for re-adjudication against an outside source, not a finding on its own.

## [1.4.0] - 2026-09-23

### Added

- **`biomapper.harmonize` — local, client-side cross-dataset harmonization.**
  Two entities from two cohorts are equivalent when they resolve to the same canonical KRAKEN
  node. The linker is an identifier-set intersection, never string matching, and it runs entirely
  on the client over results you already have. It issues no requests and needs no knowledge-graph
  access, so it is fully testable offline.

  - `harmonize(a_results, b_results)` -> `HarmonizationResult` over two lists of `MappingResult`.
  - `link_by_intersection(a_curies, b_curies)` -> `OverlapResult` for raw CURIE-set inputs.
  - `curie_set()`, `normalize_curie()`, `canonical_prefix()`, `predicted_curies()`.

  Two invariants are carried over from the engine's own linking code, along with the comments
  that explain them:

  1. **The linker is identifier-only.** `INCHIKEY`, `INCHI` and `SMILES` are excluded. Linking on
     a structure hash would make any downstream structural certificate circular and would make
     precision 100% by construction.
  2. **CURIE prefix synonyms normalize.** `KEGG.COMPOUND:C00031` equals `KEGG:C00031`, while
     genuinely different identifier spaces such as `KEGG.GLYCAN` stay distinct.

  An entity that resolved to no identifier is a **refusal candidate, not a link**. It is reported
  by name in `a_unresolved` / `b_unresolved` and counted in `summary()`, never silently dropped,
  and it is excluded from the link-rate denominator so non-resolution is never scored as
  non-equivalence. An entity whose mapping call errored is counted separately again, in
  `a_errors` / `b_errors`: "we do not know" is a different claim from "it did not resolve".

  Keys are derived once per input row, against its position in the **original** list, so an
  index-based custom `key` cannot hand the same string to an errored row and a later resolved
  one. Uniqueness is enforced across errored and resolved rows together. Cohort labels key
  `summary()`, so labels that are equal to each other or to a reserved field are rejected rather
  than allowed to silently overwrite a sibling entry.

  `harmonize` is deliberately **not** re-exported at the package root. Binding that name on
  `biomapper` would shadow the `biomapper.harmonize` submodule and break
  `biomapper.harmonize.curie_set` for anyone who reaches for it the obvious way. Import it as
  `from biomapper.harmonize import harmonize`.

- **Mapping options the client could not previously send.** `map_entity`, `map_entities` (async
  and sync) and `map_dataset_file_iter` now accept `vocab`, `prefer_human`, `prefer_canonical`,
  `candidate_limit` and `kestrel_top_n`; the entity and batch paths also accept
  `array_delimiters` (the dataset routes do not take it). Any option left as `None` is omitted
  from the request, so the server's own default applies and the payload is byte-unchanged for
  callers who do not use them. `candidate_limit` and `kestrel_top_n` are validated against the
  API's 1..100 bound locally, turning a wasted round trip into an immediate error.

- **Response fields the client was discarding.** `MappingResult` and `RawApiResult` now carry the
  full `resolution_certificate` (as `MappingResult.certificate`), `lipid_resolution`,
  `chosen_kg_id_lipid_hint`, `refmet_availability`, `refmet_source`, `refmet_snapshot_version`,
  `tier_b_snapshot_version` and the opt-in `kestrel_results` passthrough rows. New models
  `ResolutionCertificate`, `LipidResolution`, `KestrelSearchResult` and `KestrelRequestParams`
  are exported from the package root. `MappingResult.refusal_reason` reads the certificate's
  refusal reason, which is distinct from `error`: an error means the call failed, a refusal means
  the pipeline ran and declined to assert an answer.

- `identifiers` now accepts a list per vocabulary (`{"KEGG": ["C00031", "C00267"]}`), matching
  the API, which types it as `dict[str, str | list[str]]`.

### Fixed

- **`map_entities` failed against the current API.** `BatchMappingResponse.summary` was typed
  `dict[str, int]`, but the API returns a nested `refmet_source_counts` map alongside the scalar
  tallies (it types the field as `{str: int | {str: int}}`). Validation rejected the whole batch
  response, and the batch loop's chunk-level error handling then converted that into a per-entity
  error for **every record in the chunk**. The repo's own test fixture omitted
  `refmet_source_counts`, which is why the suite stayed green while live calls did not.

### Notes

- **Not added: `POST /api/v1/map/dataset`** (the non-streaming dataset route). It returns a
  path on the server's filesystem rather than per-entity JSON, so a remote client cannot read its
  output. `map_dataset_file_iter` / `map_dataset_file_sync` (the streaming route) remain the way
  to map a file. Recorded here so the omission is a decision rather than an oversight.
- No new runtime dependencies. `biomapper.harmonize` is pure Python and does **not** require
  pandas, which is an optional extra of this package.

## [1.3.0]

- Surface `chosen_kg_id_review` from the mapping API.
- Support the new `EntityType[]` response shape with `defaultPrefixes`.

## [1.1.0]

- Add `kg_equivalent_ids` support.

## [1.0.0]

- Initial release of `biomapper` following the rename from `ddharmon`.
