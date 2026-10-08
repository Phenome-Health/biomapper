# biomapper

Python client for the **BioMapper2 API** — map biological entity names to
standardized knowledge-graph identifiers (CHEBI, HMDB, PubChem, RefMet, and more).

```python
from biomapper import map_entity

result = map_entity("L-Histidine")
print(result.primary_curie)     # RM:0129894
print(result.confidence_tier)   # high
print(result.ids_for("CHEBI"))  # ['15971']
print(result.equivalent_ids_for("HMDB"))  # ['HMDB0000177']
```

---

## Installation

```bash
# Core (async HTTP client + Pydantic models)
pip install biomapper

# With the external benchmark suite
pip install 'biomapper[benchmarks]'
```

**Use 1.5.4 or later to call the hosted deployment without a key** (see [API keys](#api-keys-optional)).

**Use 1.5.3 or later for the benchmark suite.** Earlier published releases, up to and including
1.4.0, ship neither `biomapper.benchmarks` nor the `benchmarks` extra, so
`pip install 'biomapper[benchmarks]'` against them warns that the extra is unknown and installs
only the core client. 1.5.2 contains the suite but its extras were published **empty**, so the
same command installs the core client without pandas, requests, rdkit, openpyxl or defusedxml and
the suite fails on first import; install those by hand if you are pinned to 1.5.2. 1.5.3 is the
first release whose `benchmarks` extra actually carries its dependencies. The changelog records
which versions reached the index; 1.5.0 and 1.5.1 are tagged *(not published)*.

---

## Notebooks

- [`notebooks/biomapper_tutorial.ipynb`](notebooks/biomapper_tutorial.ipynb): the harmonization protocol end to end, keyless and build-pinned: resolve two cohort panels, link them by identifier-only CURIE sets, and read a certified and a refused structural certificate.
- [`notebooks/monti_overlap_benchmark.ipynb`](notebooks/monti_overlap_benchmark.ipynb): BioMapper vs Monti et al. 2026 on the NECS cross-cohort overlap, as linked-count coverage for four cohorts plus a structural certification of the NECS to Arivale links, replayed from a pinned run with a small live check.
- [`notebooks/ukbb_arivale_harmonization_sop.ipynb`](notebooks/ukbb_arivale_harmonization_sop.ipynb): a standard operating procedure for harmonizing two cohorts with this package, worked on UK Biobank × Arivale (metabolites, proteins, clinical labs): inclusion rules, map, match, verify, and results asserted against the Activity 2 milestone, replayed offline from shipped panel metadata.

---

## API keys (optional)

No API key is needed for the hosted deployment (`https://biomapper.expertintheloop.io`). From
1.5.4 the client is keyless by default: with no key configured it sends no `X-API-Key` header,
and every entry point, including the synchronous wrappers, works as shown below.

A key is only for a self-hosted or authenticated deployment. Pass it as `api_key=`, or set it in
your environment:
```bash
export BIOMAPPER_API_KEY=your-key-here
```

or in a `.env` file in your project root:
```
BIOMAPPER_API_KEY=your-key-here
```

A configured key is sent unless `anonymous=True` is passed. To force a keyless call while `BIOMAPPER_API_KEY` is set, use
`BioMapperClient(anonymous=True)`. If a deployment does require a key and none was sent, the first
request raises `BioMapperAuthError` saying so.

Releases before 1.5.4 raise `BioMapperConfigError` when no key is configured, and their
synchronous wrappers cannot reach a keyless deployment; upgrade, or use
`BioMapperClient(anonymous=True)` directly.

---

## Quick start

### Single lookup (synchronous)

```python
from biomapper import map_entity

result = map_entity("L-Histidine")

print(result.resolved)          # True
print(result.primary_curie)     # RM:0129894
print(result.chosen_kg_id)      # CHEBI:15971
print(result.chosen_kg_id_review)  # None  (or 'divergent_refmet' when a ChEBI conflict is flagged for review)
print(result.confidence_score)  # 2.489
print(result.confidence_tier)   # high  (≥2.0)
print(result.ids_for("CHEBI"))  # ['15971']
print(result.ids_for("refmet_id"))  # ['RM0129894']

# KG equivalent IDs — all identifiers from the resolved knowledge graph node
print(result.kg_equivalent_ids)            # {'CHEBI': ['15971', '44637'], 'HMDB': ['HMDB0000177'], ...}
print(result.equivalent_ids_for("HMDB"))   # ['HMDB0000177']
```

### Batch mapping (synchronous)

```python
from biomapper import map_entities, summarize

records = [
    {"name": "L-Histidine"},
    {"name": "Glucose", "identifiers": {"HMDB": "HMDB00122"}},
    {"name": "Sphinganine"},
]

results = map_entities(records, progress=True)  # tqdm bar with [notebook]
summary = summarize(results)

print(f"{summary.resolved}/{summary.total_queried} resolved")
print(f"Resolution rate: {summary.resolution_rate:.1%}")
print(summary.vocabulary_coverage)
```

Inputs are auto-chunked at 1000 entities per request against the native
`POST /map/batch` endpoint, so 10,000 records cost 10 round-trips.

### Dataset upload (synchronous)

For larger inputs, hand the server a TSV/CSV file directly and stream
results back. The server processes the file row-by-row over the
`POST /map/dataset/stream` endpoint:

```python
from pathlib import Path
from biomapper import map_dataset_file_sync

result = map_dataset_file_sync(
    Path("compounds.tsv"),
    name_column="name",
    provided_id_columns=["hmdb_id"],
    progress=True,         # tqdm bar
    total_hint=1000,       # optional; enables % progress
)
result.raise_for_error()   # opt-in: raise BioMapperError if the stream truncated
print(f"resolved {sum(1 for r in result.results if r.resolved)} of {len(result.results)}")
```

`name_column` and `provided_id_columns` are required — the server uses
them to map your file's columns to entity names and identifier hints.
For per-result streaming into a UI or custom processing, use the async
`BioMapperClient.map_dataset_file_iter` method (see the tutorial
notebook in `notebooks/`).

### Discovering what the API supports

```python
from biomapper import list_annotators, list_vocabularies, list_entity_types

for a in list_annotators():
    print(f"{a.slug:30s} {a.name}")

# 300+ supported vocabularies (CHEBI, HMDB, PubChem, …)
vocabs = list_vocabularies()
print(f"{len(vocabs)} vocabularies supported")

# Biolink entity types with aliases and default vocabulary prefixes
for et in list_entity_types():
    print(f"{et.type}: {', '.join(et.aliases)}")
    if et.default_prefixes:
        print(f"  prefixes: {', '.join(et.default_prefixes)}")
```

### Tuning resolution

The mapping calls accept the API's resolution options as keyword arguments. An option you do not
pass is **omitted from the request**, so the server's own default applies and the payload is
unchanged for callers who ignore them.

Coverage is uneven, so check this before reaching for one. Passing an option to a call that does
not accept it raises `TypeError` locally, before any request:

| | `vocab` | `prefer_human` | `prefer_canonical` | `candidate_limit` | `kestrel_top_n` | `array_delimiters` |
|---|---|---|---|---|---|---|
| `map_entity`, `map_entities` (sync and async) | yes | yes | yes | yes | yes | yes |
| `BioMapperClient.map_dataset_file_iter` (async) | yes | yes | yes | yes | yes | no |
| `map_dataset_file_sync` | yes | no | no | no | no | no |

`array_delimiters` is absent from the dataset routes because the API does not accept it there. The
four missing from `map_dataset_file_sync` are a gap in that wrapper rather than an API limitation:
the async `map_dataset_file_iter` underneath it does accept them, so use that directly if you need
them on a file-based run.

```python
from biomapper import map_entity  # map_entity / map_entities accept all six

result = map_entity(
    "PC 34:1",
    vocab="refmet",             # restrict to one vocabulary (or a list)
    prefer_human=False,         # gene/protein: allow a non-human ortholog to win
    prefer_canonical=False,     # non-gene: allow a non-canonical-namespace node to win
    candidate_limit=20,         # candidates each Kestrel search annotator retrieves (1..100)
    array_delimiters=["|"],     # how delimited ID strings are split
    kestrel_top_n=5,            # opt in to raw Kestrel passthrough rows (1..100)
)
```

| Option | Applies to | Default | Notes |
|---|---|---|---|
| `vocab` | all | server | `str` or `list[str]`, e.g. `"refmet"` |
| `prefer_human` | gene/protein | `True` (server) | Prefer the human, HGNC-bearing candidate over a wrong-species ortholog |
| `prefer_canonical` | non-gene | `True` (server) | Prefer the canonical-namespace node (CHEBI/HMDB/RefMet) over a same-text node from UMLS/ICD/KEGG |
| `candidate_limit` | all | server | 1..100. Validated client-side, so an out-of-range value raises `ValueError` before the request |
| `kestrel_top_n` | all | off | 1..100. Passthrough only: it **never** changes `chosen_kg_id`, `assigned_ids` or the certificate |
| `array_delimiters` | `map_entity`, `map_entities` | `[",", ";"]` | Not accepted by the dataset routes |

`kestrel_top_n` populates `result.kestrel_results` with the raw rows Kestrel returned, exactly as
returned, for each search endpoint the pipeline used. Those rows are **untrusted external data**:
treat every field as unescaped and unverified.

### Resolution certificates and refusal

A mapping result carries the structural certificate the API computed for `chosen_kg_id`, which is
how you tell "we could not check this" apart from "we checked and disagree".

```python
result = map_entity("cortisone")

cert = result.certificate
if cert is not None:
    print(cert.state)                    # corroborated | uncorroborated | contradicted |
                                         # unavailable | not_applicable
    print(cert.independent_source)       # registry consulted, e.g. "pubchem"
    print(cert.independent_of_selection)  # False => corroboration would be circular
    print(cert.refmet_availability)      # voted | no_match | unavailable | not_queried

# Why the resolver declined, when it did. Distinct from `error`:
# an error means the call failed; a refusal means the pipeline ran and declined to assert.
print(result.refusal_reason)
```

`state="contradicted"` means a human should look, not that the resolver is wrong: a name lookup at
an external registry can itself return a related-but-different compound. `state="unavailable"`
means there was nothing to check against, which is unverifiable rather than wrong.

### Async usage

```python
import asyncio
from biomapper import BioMapperClient

async def main() -> None:
    async with BioMapperClient() as client:
        # Verify connectivity
        health = await client.health_check()
        print(health)  # {'status': 'healthy', ...}

        # Single
        result = await client.map_entity(
            "L-Histidine",
            identifiers={"HMDB": "HMDB00177"},
        )

        # Batch — auto-chunked at 1000 entities per request
        results = await client.map_entities(
            [{"name": "L-Histidine"}, {"name": "Glucose"}],
            progress=True,
        )

        # Stream from a file — per-result as they arrive
        from pathlib import Path
        async for r in client.map_dataset_file_iter(
            Path("compounds.tsv"),
            name_column="name",
            provided_id_columns=["hmdb_id"],
        ):
            print(r.query_name, r.primary_curie)

asyncio.run(main())
```

`map_dataset_file_iter` is the primitive for UIs and custom processing that
want per-result reactivity. Callers needing a blocking, fully-collected
result should use `map_dataset_file_sync` instead (see above).

### Jupyter notebooks

Apply `nest_asyncio` before using sync helpers inside a running event loop:

```python
import nest_asyncio
nest_asyncio.apply()  # required in Jupyter

from biomapper import map_entities
results = map_entities([{"name": "L-Histidine"}], progress=True)
```

The worked notebooks are listed under [Notebooks](#notebooks). Both run against the keyless
hosted deployment, so no API key is needed, and both print the exact KRAKEN build that answered.

### Preprocessing functions

```python
from biomapper.extras.metabolon import clean_compound_name, extract_hmdb_id

# Strip quotes and collision-energy suffixes
clean_compound_name('"1,3-Diphenylguanidine_CE45"')  # '1,3-Diphenylguanidine'
clean_compound_name('"4,6-DIOXOHEPTANOIC ACID"')     # '4,6-DIOXOHEPTANOIC ACID'
clean_compound_name('L-Histidine')                   # 'L-Histidine'  (unchanged)

# Extract HMDB accessions from ms1_compound_name format
extract_hmdb_id('HMDB:HMDB03349-2257 L-Dihydroorotic acid')  # 'HMDB03349'
extract_hmdb_id('HMDB00177')                                  # 'HMDB00177'
extract_hmdb_id(None)                                         # None
```

---

## Harmonization (cross-dataset equivalence)

`biomapper.harmonize` links two **already-resolved** datasets locally. It runs entirely on the
client: no extra requests, no knowledge-graph access, so it works offline and is fully testable
without a network. Two entities, one per cohort, link when any of these holds, and each link
records which one as its `basis` (strongest first):

| basis | rule |
|---|---|
| `node` | their identifier-only CURIE sets intersect and both resolved to the same chosen node |
| `identifier` | their identifier-only CURIE sets intersect, on different nodes |
| `name_exact` | their names are equal after whitespace normalization |
| `name_casefold` | their names are equal only after casefold (see the short-name rule below) |

Name links (from 1.5.5, **opt-in** with `link_by_name=True`) catch entities that resolution put
on different nodes, such as `acetylcarnitine (c2)` and `acetylcarnitine (C2)`. They are currently
intended for small-molecule / metabolite panels; other entity types are pending review of
case-sensitive naming conventions (gene and protein symbols, for example). Rules:

- Only entities that resolved on both sides link by name; an unresolved one stays a refusal
  candidate. Names are compared on `query_name`, even when you pass a custom `key=`; links are
  reported under the keys.
- No fuzzy matching and no punctuation stripping, only whitespace normalization.
- A **casefold-only** match where either name is `NAME_CASEFOLD_MIN_LENGTH_EXCLUSIVE` (4)
  characters or fewer is **withheld**, not linked, and listed in `report.name_match_withheld` for review:
  `Co` (cobalt) and `CO` (carbon monoxide) are different things.
- Pass results of **one entity type** per `harmonize()` call. The results carry no type to check,
  so this is the caller's responsibility; a cross-type collision such as cAMP (metabolite) vs
  CAMP (gene) is out of scope.
- Off by default: `harmonize(...)` links by `node` and `identifier` only, exactly as before 1.5.5.

`report.summary()` counts links by basis (`links_by_basis`) and reports `n_name_only_links` (links
with no identifier evidence) and `n_name_match_withheld`, so a report can say "X linked by
identifier, Y by name only". Every link also carries `bases`, the full set of bases that apply.

```python
from biomapper import map_entities
from biomapper.harmonize import harmonize

ukbb    = map_entities([{"name": "Glucose"},   {"name": "Urea"}])
arivale = map_entities([{"name": "D-glucose"}, {"name": "X-12345"}])

report = harmonize(ukbb, arivale, a_label="ukbb", b_label="arivale")

report.n_links          # 1
report.links[0].basis   # 'node'
report.links[0].shared  # frozenset({'CHEBI:17234', 'KEGG:C00031'}): the identifiers shared
report.b_unresolved     # ('X-12345',) — a refusal candidate, never silently dropped
report.summary()
```

Two rules the linker is built around:

1. **No structure identifiers.** `INCHIKEY`, `INCHI` and `SMILES` are excluded. Linking on a structure
   hash would make any downstream structural certificate circular and would make precision 100%
   by construction.
2. **Prefix synonyms normalize.** `KEGG.COMPOUND:C00031` equals `KEGG:C00031`; genuinely
   different identifier spaces such as `KEGG.GLYCAN` stay distinct.

An entity that resolved to nothing is a **refusal candidate, not a link**. It is named in
`a_unresolved` / `b_unresolved`, counted in `summary()`, and left out of the link-rate
denominator so non-resolution is never scored as non-equivalence. An entity whose mapping call
errored is tracked separately again in `a_errors` / `b_errors`.

Import it as `from biomapper.harmonize import harmonize`. The name is deliberately not bound on
the package root, where it would shadow the submodule.

### Harmonizing two cohorts in one call: `harmonize_cohorts()`

`harmonize_cohorts()` runs the whole protocol: it reads both cohort tables, maps them, links them
with `harmonize()`, and writes a pinned report. It exists because the obvious manual path can
quietly make results worse. When only one cohort publishes an identifier type, supplying it moves
that cohort onto code-specific entries the other cohort never reaches by name: on the UK Biobank x
Arivale clinical labs, supplying Arivale's LOINC codes cut matched pairs from 31 to 2.

```python
from biomapper import harmonize_cohorts

report = harmonize_cohorts(
    "ukbb_labs.tsv",                  # a DataFrame, a list of dicts, or a TSV/CSV path
    "arivale_labs.tsv",               # ('#' comment lines are skipped)
    entity="labs",                    # -> biolink:ClinicalMeasurement
    a_name_column="field_name",
    b_name_column="Display Name",
    b_vocabularies={"LOINC": ["Labcorp LOINC ID", "Quest LOINC ID"]},
    a_label="ukbb",
    b_label="arivale",
)
# UserWarning: LOINC is declared only for 'arivale' ... not supplied as mapping input
report.summary()["arms"]["names_only"]   # pairs, one-to-one, link bases, unresolved, errors
report.review_queue                      # codes whose entry disagrees with the name's entry
# harmonize_cohorts: wrote results to .../biomapper_runs/harmonize_cohorts_20261008T190000Z
```

What it does:

1. **Names-only arm.** Both cohorts are mapped by name with the resolved category and no
   identifiers. `entity` takes `metabolites` (SmallMolecule), `proteins` (Protein), `genes` (Gene),
   `labs` (ClinicalMeasurement, where lab tests live in Biolink) or any raw `biolink:` category;
   `category=` overrides an alias. Name linking is on for SmallMolecule only, as in `harmonize()`.
2. **Identifier arm, shared vocabularies only.** You declare identifier columns per vocabulary
   and cohort (`{"LOINC": [...]}`); names are normalized, so `KEGG.COMPOUND` equals `KEGG`. Only
   vocabularies **both** cohorts declare are supplied as mapping input, and only rows carrying
   one are re-mapped; the rest reuse their names-only result. With nothing shared the arm is
   skipped and the report says why; with no identifier columns only the names-only arm runs.
3. **Review queue for one-sided codes.** Each code of a vocabulary only one cohort declares is
   resolved on its own (`annotation_mode="none"`, one request per row and code) and compared with
   the row's names-only entry by identifier-set intersection. Every code gets a status (`agree`,
   `disagree`, `code_unresolved`, `name_unresolved_code_resolved`, `errored`);
   `report.review_queue` lists everything but `agree`, with both entries. On the labs this lists
   Arivale "Glucose", whose name lands on one entry while its LOINC code 2345-7 says another.
4. **Report.** Both `HarmonizationResult`s (`report.names_only`, `report.identifier`), their
   `diff` (pairs found only by names, only with identifiers, by both), one-to-one counts, link
   bases, warnings, excluded rows (blank names), and pins.

**Output by default.** Results are written to `biomapper_runs/harmonize_cohorts_<UTC stamp>/`
under the current directory (override with `output_dir=`; `save=False` to skip;
`report.write(path)` any time): mapping results per arm and cohort, written as each arm finishes
so a failure keeps finished work, link tables per arm, `diff.tsv`, `review_queue.tsv`,
`settings.json` and `summary.json`. Nothing beyond what you passed in is written, and input files
are recorded by name and SHA-256, never by absolute path.

**Pins.** The package version; the API's `/health` status and self-reported version (labelled
"self-reported, known stale": the API does not expose its engine release, which is recorded as
`unavailable`); Kestrel's `/health` `kestrel_version` and `kg_build` together with the Kestrel URL
used (`kestrel_url=`); and a UTC timestamp. `probe_pins=False` skips the two reads.

**Requests.** The default mapper sends batches of 10 with a 300 s timeout (`batch_size=`,
`timeout=`, `progress=True` to see them); a failed batch becomes errored rows that are counted,
not an exception. Pass `mapper=` (any `(records, *, entity_type, annotation_mode) ->
list[MappingResult]` callable) to replay recorded results offline; the tests replay the UK Biobank
x Arivale labs this way (`tests/fixtures/cohorts_labs_cm/`, KRAKEN kg 2.3.0 / 3dd08a5b: 37 pairs,
21 one-to-one by name).

---

## API reference

### `MappingResult`

| Attribute | Type | Description |
|---|---|---|
| `query_name` | `str` | Name submitted to the API |
| `resolved` | `bool` | Whether any identifier was returned |
| `primary_curie` | `str \| None` | First CURIE in the response |
| `chosen_kg_id` | `str \| None` | Resolver-selected knowledge graph ID |
| `chosen_kg_id_review` | `str \| None` | Review flag for source-weighted small-molecule ChEBI conflicts: `"divergent_refmet"`, `"conflict_no_structure"`, or `None` |
| `confidence_score` | `float \| None` | Highest score across annotators |
| `confidence_tier` | `str` | `"high"` (≥2.0) / `"medium"` (1–2) / `"low"` (<1) / `"unknown"` |
| `identifiers` | `dict[str, list[str]]` | Vocabulary → IDs, e.g. `{"CHEBI": ["15971"]}` |
| `kg_equivalent_ids` | `dict[str, list[str]]` | All equivalent IDs from the resolved KG node, by CURIE prefix |
| `certificate` | `ResolutionCertificate \| None` | Structural certificate for `chosen_kg_id` (state, independent source, RefMet provenance) |
| `refusal_reason` | `str \| None` | Why the resolver declined, read from the certificate. `None` when there is no certificate |
| `lipid_resolution` | `LipidResolution \| None` | Lipid hierarchy detail (goslin parse, `mapping_relation`); `None` for non-lipid rows |
| `refmet_availability` | `str` | `"voted"` / `"no_match"` / `"unavailable"` / `"not_queried"` |
| `refmet_source` | `str` | `"local_snapshot"` / `"not_in_snapshot"` / `"live_api"` / `"unavailable"` / `"not_queried"` |
| `refmet_snapshot_version` | `str \| None` | Pinned RefMet freeze that served the row |
| `tier_b_snapshot_version` | `str \| None` | Tier B freeze that produced a frozen independent result |
| `kestrel_results` | `list[KestrelSearchResult] \| None` | Raw passthrough rows; only when `kestrel_top_n` was set |
| `hmdb_hint` | `str \| None` | HMDB hint passed in the request |
| `error` | `str \| None` | Error message if mapping failed |

```python
result.ids_for("CHEBI")        # ['15971']
result.ids_for("refmet_id")    # ['RM0129894']
result.ids_for("PUBCHEM.COMPOUND")  # []

# KG equivalent IDs — all identifiers from the resolved knowledge graph node
result.equivalent_ids_for("HMDB")  # ['HMDB0000177']
result.equivalent_ids_for("LM")   # ['ST01010001', 'ST01010093']
```

### `DatasetMappingResult`

Return type of `map_dataset_file_sync`. Captures per-row results plus an
opt-in error signal for partial runs.

| Attribute | Type | Description |
|---|---|---|
| `results` | `list[MappingResult]` | Per-row mapping outcomes in server-emitted order |
| `stats` | `dict[str, Any]` | Server-provided summary. Empty unless the stream emits a terminal summary line |
| `metadata` | `ApiMetadata` | Request metadata; stays at defaults when the stream truncates before completion |
| `error` | `str \| None` | Mid-stream transport failure text. `None` on clean runs |

```python
result.raise_for_error()   # raises BioMapperError if .error is set; else no-op
```

`raise_for_error` mirrors `httpx.Response.raise_for_status` and turns the
partial-result contract into an explicit caller opt-in — silent consumption
of a truncated run (using `.results` without checking `.error`) is the
footgun this model is designed to prevent.

> **Note:** `confidence_score` on dataset-stream results is always `None` —
> the `/map/dataset/stream` endpoint emits a slimmer per-row payload than
> `/map/batch` and does not include the annotator `assigned_ids` block.
> Use `map_entity` / `map_entities` if you need confidence tiers.

### Confidence tiers

| Score | Tier | Recommended action |
|---|---|---|
| ≥ 2.0 | `high` | Accept without review |
| 1.0–2.0 | `medium` | Quick sanity check |
| < 1.0 | `low` | Manual review recommended |
| `None` | `unknown` | No score returned (e.g. HMDB-hint resolved) |

### Error handling

```python
from biomapper import (
    BioMapperError,       # base class
    BioMapperAuthError,   # 401/403: a key is required, or the key was rejected
    BioMapperRateLimitError,  # 429 — throttled
    BioMapperServerError,     # 5xx
    BioMapperTimeoutError,    # request timeout
    BioMapperConfigError,     # invalid config, e.g. api_key= together with anonymous=True
)

try:
    result = map_entity("Glucose")
except BioMapperAuthError as e:
    # Only for a deployment that requires a key; the message says whether one was
    # missing or rejected.
    print(e)
else:
    if result.error:
        print(f"mapping failed: {result.error}")
```

An authentication failure (401/403) raises `BioMapperAuthError` from every entry point, sync or
async, because it is systemic: every later request would fail the same way. The synchronous
`map_entity` and `map_entities` go through the batch endpoint, where **transient** failures (429,
5xx, timeouts, a malformed response) and per-entity problems do not raise: each affected record
comes back as `MappingResult(error=...)` rather than aborting the batch. Check `.error` on every
result. The single-entity call on the async client, `await client.map_entity(...)`, raises the
typed exceptions (`BioMapperRateLimitError`, `BioMapperServerError`, `BioMapperTimeoutError`)
directly.

Dataset streaming (`map_dataset_file_sync`) uses a two-tier contract:

- **Initial-request errors** (401, 422, 500, connect timeout) raise as typed
  exceptions — these happen before any row is processed, so partial results
  don't exist to preserve.
- **Mid-stream transport failures** are captured into
  `DatasetMappingResult.error` with the partial results preserved in
  `.results`. Call `.raise_for_error()` to get exception semantics, or
  inspect `.error` directly for "accept partial, log the rest" workflows.

Callback exceptions raised from `on_result` propagate unwrapped and
**replace the return value** — partial results collected up to that point
are lost. For UI consumers with failure-prone callbacks, wrap the callback
body in your own try/except if you want partial data to survive.

---

## External benchmark suite

An API-only reproduction harness for the benchmarks the BioMapper preprint reports. It runs
against a **deployment**, not a local engine checkout, which matters for two reasons: running
in-process against a checkout measures a library rather than the deployed service, and pointing at
a deployment pins provenance to the backend that actually served the answers.

```bash
pip install 'biomapper[benchmarks]'

python -m biomapper.benchmarks list                 # the 11 arms and the 2 deliberate skips
python -m biomapper.benchmarks all                  # all arms, production endpoint
python -m biomapper.benchmarks arm hajjar           # a single arm
python -m biomapper.benchmarks --endpoint dev all   # dev, for testing
```

The public KRAKEN endpoint is keyless, and a BioMapper2 deployment with no keys configured is
open, so the suite runs unauthenticated by default. Set `BIOMAPPER_API_KEY` if your deployment
requires one; the key is never accepted on the command line, because argv is visible to other
processes and lands in shell history.

### The arms

| Arm | Input | Metric | Reportable as |
|---|---|---|---|
| `hajjar` | metabolite name | InChIKey structure oracle, strict + KG-equivalence-set | accuracy candidate |
| `necs` | metabolite name | structure oracle, strict + charge-normalized | accuracy, with the gold caveat |
| `srm1950` | metabolite name | structure oracle (gold derived from certified SMILES) | accuracy candidate |
| `metlinkr` | metabolite name | curator cross-link agreement + structural concordance | accuracy candidate |
| `metabench` | mixed ID→ID and name→ID | CURIE equality over 1,000 grounding pairs | partly circular |
| `metaboliteannotator` | metabolite name | name-hit rate, per ion mode | coverage by construction |
| `refmet` | metabolite name | structure oracle | coverage |
| `lmsd` | lipid name | shorthand resolvability, floor-gated | coverage (capability regression) |
| `hgnc` | gene symbol | CURIE equality **per target namespace** | coverage |
| `nlmgene` | gene mention | accuracy (unambiguous) + flag rate (ambiguous) | accuracy candidate |
| `swisslipids` | lipid name | — | currently unsourceable, reports as skipped |

**Read the `Reportable as` column before quoting anything.** Four of the graph's ingested sources
(`lipidmaps`, `refmet`, `loinc`, `ncbigene`) are gold sources for arms above, so those arms measure
coverage, not independent accuracy: the gold identifier and BioMapper's answer come from the same
place. Each run labels every arm from the build's own source list rather than from a static note,
so the label tracks the graph.

Two further reporting rules the suite enforces rather than documents:

- **Gene arms report accuracy per target namespace.** The any-namespace roll-up is emitted flagged
  `quotable: false`, because the namespaces perform very differently and the roll-up has been
  quoted as though it described all of them.
- **A skipped arm is not a zero and not a pass.** It carries a reason into the manifest.

### Provenance

Every run records what actually served it, read from Kestrel `/health` (keyless) rather than
hardcoded: `kestrel_version`, `kg_version`, `biolink_version`, `build_timestamp`, `git_commit`, the
full `source_versions` map, the package version, each dataset's source SHA, and a run id. The build
is read once before the arms start and re-read afterwards; if it moved mid-suite, the manifest says
so, because the pins would no longer describe every number.

Results are saved by default to a timestamped directory (override with `--out`). There is no flag
that discards them: the expensive part of a run is live API traffic.

### What stayed in the engine repo

The CI regression gates (`gate.py`, `conflation_gate.py`, `test_regression_gate.py`,
`kg-regression.yml`) guard merges and need engine internals. The `--resolver-mode {weighted,vote}`
A/B toggles a resolver constructor argument that is deliberately not on the API surface.

---

## Development

```bash
git clone https://github.com/Phenome-Health/biomapper
cd biomapper
poetry install --with dev --extras all

make check          # format → lint → type-check → test
make test           # tests only
make coverage       # HTML coverage report
```

`docs/solutions/` holds documented solutions to past problems — bugs, best practices, and workflow
patterns — organized by category with YAML frontmatter (`module`, `tags`, `problem_type`). Relevant
when implementing or debugging in an area something has already been written about.

---

## License

MIT — see [LICENSE](LICENSE).

---

## Related

- **BioMapper2 API**: `https://biomapper.expertintheloop.io`
