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

The core install includes the MCP server for agents (Claude Code, Claude Desktop, other MCP
clients); launch it with `uvx biomapper` (see [Use from an agent (MCP)](#use-from-an-agent-mcp)).

**Use 1.6.2 or later for `uvx biomapper`.** 1.6.0 and 1.6.1 shipped the server behind an `mcp`
extra (`uvx --from 'biomapper[mcp]' biomapper-mcp`); that line still works on 1.6.2.

**Use 1.5.6 or later for `harmonize_cohorts()` and ambiguous-link flags** (see [Harmonization](#harmonization-cross-dataset-equivalence)).

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
print(result.chosen_kg_id)      # RM:0129894
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

**Ambiguous links** (from 1.5.6). Every link records `a_partners` and `b_partners`, how many
entities each side links to. `link.ambiguous` is true when either is above one: several distinct
entities landed on one entry, so the pair is a review candidate rather than a confident match.
`report.one_to_one_links` and `report.ambiguous_links` split the links, and `summary()` adds
`n_one_to_one_links` and `n_ambiguous_links`. All links are still returned; nothing is dropped.
On NECS x Xu et al. 2022 scored against Metabolon compound IDs, one-to-one links were 397 of 398
correct (99.7%) versus 439 of 575 (76.3%) for all links.

**Identification-confidence markers** (unreleased). Metabolon marks how a compound was identified
with asterisks on its name: no asterisk means confirmed with an authentic chemical standard, `*`
means identified with confidence but not confirmed with a standard, and `**` means reasonably
identified without an available standard. Every link records each side's marker as
`a_id_confidence` / `b_id_confidence` (0, 1 or 2, read from each result's `query_name`; see
`biomapper.harmonize.id_confidence_marker`), and `link.id_confidence_differs` is true when they
differ, as in `glyco-beta-muricholate**` vs `glyco-beta-muricholate`. A marker is a run of one or
two asterisks that directly follows a non-space character and either ends the name or comes right
before ` (` (`1-palmitoyl-gpi* (16:0)`); any other asterisk is ignored, and when a name carries
two markers the longer one counts (`1-arachidonoyl-GPC* (20:4)*` is `*`). Names without markers
are always 0, so panels that do not use Metabolon names are unaffected; a marked name linked to an
unmarked one is flagged. `link.needs_review` is `ambiguous` or `id_confidence_differs`;
`ambiguous` and `one_to_one_links` keep their meaning. `report.review_links` lists the links that
need review, every other link is a confident match, and `summary()` adds `n_id_confidence_differs`
and `n_needs_review`. Name linking compares names with their asterisks intact, so a pair that
differs only in its marker links by identifier, never by name. On NECS x Xu et al. 2022 (engine
v1.2.1, 2026-11-05), 390 links need no review and 389 of them share a Metabolon compound ID;
before this flag the confident set was 399 links, 9 of them with differing markers.

**Mixture names** (unreleased). A name that lists alternative compounds, such as
`butyrate/isobutyrate (4:0)`, describes a measurement that may be either compound. Linking it to
a name that does not list the same alternatives is a review candidate even when the link is
one-to-one. Every link records `a_is_mixture` / `b_is_mixture`.
`biomapper.harmonize.is_mixture_name` applies this rule to the whitespace-normalized, casefolded
name; any one of these makes it a mixture:

1. `or` as a word between spaces: `linolenate [alpha or gamma; (18:3n3 or 6)]`,
   `dihomo-linolenate (20:3n3 or n6)`.
2. `+` with a space on each side: `dimethylarginine (sdma + adma)`,
   `(n(1) + n(8))-acetylspermidine` (but not `NAD+`).
3. `/` with at least three letters right before it and a letter or a locant (`2-`, `5alpha-`)
   right after it: `butyrate/isobutyrate (4:0)`, `2-hydroxybutyrate/2-hydroxyisobutyrate`. Lipid
   chain notation (`PC 16:0/18:1`, `d18:1/17:0`) has a digit or `)` before the slash and never
   counts; neither does a slash before a unit word (`mmol/mol`) or any slash in a name containing
   the word `ratio`.
4. A bracketed group that is a comma-separated list of two or more lipid chains, Metabolon's
   multi-isomer names: `sphingomyelin (d18:1/17:0, d17:1/18:0, d19:1/16:0)`. Other comma lists
   (`androstenediol (3alpha, 17alpha) monosulfate`, `bilirubin (z,z)`) do not count.

`link.mixture` (the `mixture` review reason) is true when the two sides differ as mixtures:

- exactly one side is a mixture (`butyrate/isobutyrate (4:0)` vs `butyrate (4:0)`), or
- both are, and they list different alternatives (`dihomo-linolenate (20:3n3 or n6)` vs
  `dihomolinolenate (20:3n3 or 3n6)`).

Two mixture names listing the same alternatives in any order are not flagged:
`dimethylarginine (sdma + adma)` vs `dimethylarginine (adma + sdma)`, or the same Metabolon
multi-isomer string on both sides. The comparison uses `biomapper.harmonize.mixture_alternatives_key`
(stored on the link as `a_mixture_key` / `b_mixture_key`): the casefolded name without its
identification-confidence markers, with the alternatives inside each bracket level (`(...)` and
`[...]` alike) sorted. Alternatives are only reordered within their own bracket level, so moving a
shared qualifier between levels (`butyrate/isobutyrate (4:0)` vs `isobutyrate (4:0)/butyrate`)
still counts as different, which errs toward review.

The engine's own mixture flag is honoured too: engine v1.3.1 reports
`lipid_resolution.name_is_mixture` on lipid names (a top-level `name_is_mixture` is also read if a
server sends one). A `True` marks the name a mixture; a `False` does not override the name rule,
and a server without the field changes nothing. `link.needs_review` is now `ambiguous`,
`id_confidence_differs` or `mixture`; `link.review_reasons` names which, `report.mixture_links`
lists the mixture links, and `summary()` adds `n_mixture_links`. The rule is written against
Metabolon-style metabolite names.

**Possible missed matches** (unreleased). `report.possible_missed` lists every entity that ended
up in no link although its mapping already carries a review signal, so a reviewer sees it next to
the review links instead of it disappearing. Each entry (`PossibleMiss`) has the cohort label, key,
name, `chosen_kg_id`, whether it resolved, and its `reasons`: the mapping's `chosen_kg_id_review`
value (`divergent_refmet`, `conflict_no_structure`), `certificate_contradicted`, and/or
`mixture` (per entity: the entity's own name is a mixture). Errored entities are never listed. `report.a_possible_missed` / `b_possible_missed` split
it by side, and `summary()` adds `n_possible_missed` plus a `possible_missed` count in each cohort
block. Entries are listed per cohort; nothing pairs them across cohorts.

On NECS x Xu et al. 2022 (engine v1.3.1, offline re-score 2026-11-06), 6 links are mixture
reviews and 2 of them are flagged for that reason alone (`butyrate/isobutyrate (4:0)` with
`butyrate (4:0)`, and the two linolenate spellings; both share a Metabolon compound ID). 401 links
need no review and 400 of them share a compound ID; 99 links need review. 77 entities are listed
as possible misses (68 NECS, 9 Xu), including both sides of the one Monti et al. pair BioMapper
does not link (`dihomo-linolenate (20:3n3 or n6)` with `dihomolinolenate (20:3n3 or 3n6)`).

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
report.summary()["arms"]["names_only"]   # pairs, one-to-one/ambiguous, link bases, ...
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
   `diff` (pairs found only by names, only with identifiers, by both), one-to-one and ambiguous
   link counts per arm (`n_one_to_one_links` / `n_ambiguous_links`, as in `harmonize()`), link
   bases, warnings, excluded rows (blank names), and pins. Each arm's link table carries
   `a_partners`, `b_partners` and a `one_to_one` flag, so ambiguous links can be reviewed, plus
   `a_id_confidence`, `b_id_confidence`, `id_confidence_differs`, `needs_review`,
   `a_is_mixture`, `b_is_mixture`, `mixture` and `review_reasons`; each arm's summary adds
   `n_id_confidence_differs`, `n_mixture_links`, `n_needs_review` and `n_possible_missed`. Each
   arm also writes `possible_missed_<arm>.tsv` (cohort, key, name, chosen_kg_id, resolved,
   reasons).

**Output by default.** Results are written to `biomapper_runs/harmonize_cohorts_<UTC stamp>/`
under the current directory (override with `output_dir=`; `save=False` to skip;
`report.write(path)` any time): mapping results per arm and cohort, written as each arm finishes
so a failure keeps finished work, link and possible-missed tables per arm, `diff.tsv`,
`review_queue.tsv`,
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

### Harmonizing more than two cohorts: `harmonize_many()`

`harmonize_many()` harmonizes two or more cohorts in one pinned run. `harmonize_cohorts()` cannot
simply be looped: it re-maps both cohorts on every call, and pairwise links do not chain into
consistent groups. `harmonize_many()` maps each cohort once, saves the mappings so later runs
reuse them, and groups rows across all cohorts by the KRAKEN node each one resolved to.

A worked example on three toy cohorts (the `mapper=` here stands in for the hosted API; drop it
to map live):

```python
from biomapper import MappingResult, harmonize_many

ENTRIES = {  # name -> (chosen node, equivalent identifiers)
    "glucose": ("CHEBI:17234", {"HMDB": ["HMDB0000122"]}),
    "Glucose": ("CHEBI:17234", {"HMDB": ["HMDB0000122"]}),
    "fatty acids": ("CHEBI:35366", {"LM": ["FA0101"]}),
    "palmitate": ("LM:FA0101", {}),
}

def toy_mapper(records, *, entity_type, annotation_mode):
    out = []
    for r in records:
        node, eq = ENTRIES.get(r["name"], (None, {}))
        out.append(MappingResult(query_name=r["name"], resolved=node is not None,
                                 chosen_kg_id=node, kg_equivalent_ids=eq))
    return out

report = harmonize_many(
    {
        "qmdiab": [{"name": "glucose", "hmdb": "HMDB00122"}, {"name": "fatty acids", "hmdb": ""}],
        "chear": [{"name": "Glucose", "hmdb": "HMDB0000122"}, {"name": "palmitate", "hmdb": ""}],
        "tartu": [{"name": "glucose"}, {"name": "unknown thing"}],
    },
    entity="metabolites",
    vocabularies={"qmdiab": {"HMDB": "hmdb"}, "chear": {"HMDB": "hmdb"}},
    mapper=toy_mapper,
)
# harmonize_many: run directory .../biomapper_runs/harmonize_many_20261108T190000Z
s = report.summary()
s["arms"]["names_only"]["groups"]               # 3: CHEBI:17234 (all three), CHEBI:35366, LM:FA0101
s["arms"]["names_only"]["cross_node_entries"]   # 1: fatty acids (CHEBI:35366) <-> palmitate (LM:FA0101)
s["arms"]["names_only"]["id_check"]["by_status"]  # qmdiab and chear glucose: confirmed
s["identifier_arm_skipped"]                      # HMDB is not declared by tartu
s["publication_grade"]                           # iteration-grade, not for publication
```

A custom `mapper=` keeps its saved mappings apart from the hosted API's, keyed by its identity: a
plain module-level function is identified by its name, and a `functools.partial` by the wrapped
function plus its arguments. A mapper whose settings biomapper cannot see (a callable object, a
bound method, a lambda or closure, or a partial with non-JSON arguments) is mapped fresh on every
run, with a warning, unless it has a `store_id` attribute naming its configuration.

What it reports, per arm (names-only always; the identifier arm when a vocabulary is shared):

- **Node groups** (`matrix_<arm>.tsv`, one row per node and one column per cohort, and
  `membership_<arm>.tsv`, one row per input row). Rows are grouped by their raw `chosen_kg_id`,
  the value `harmonize()`'s `node` basis compares. A group is *clean* when each cohort in it
  contributes one row and no member resolved to a class-level entry.
- **Flagged groups** (`flagged_groups_<arm>.tsv`): *multiplicity* when one cohort contributes two
  or more rows to a node, *broad* when any member's `lipid_resolution.mapping_relation` is
  `"broad"` (a class node such as a lipid class, not one species).
- **Cross-node queue** (`cross_node_<arm>.tsv` by node pair, `cross_node_pairs_<arm>.tsv` row by
  row). An identifier or name link between rows on different nodes is never merged into one group,
  so a class node linked to five species stays six groups and five review entries rather than
  one chained group. Short names equal only after casefold (Co vs CO) are listed in
  `withheld_<arm>.tsv` and never linked.
- **Rows that are not grouped** (`not_grouped_<arm>.tsv`): `unresolved` (no identifiers),
  `errored` (the mapping call failed; the run's status is then `partial`), and `no_node` (has
  identifiers but no usable chosen node, for example a structure-namespace `chosen_kg_id`; its
  links still go to the cross-node queue).
- **Identifier check** (`id_check` column in membership, `id_check_review_<arm>.tsv`). Each grouped
  row's declared identifiers are compared with its node's identifiers (legacy 5-digit HMDB codes
  are zero-padded; InChIKeys are compared in full against the node's InChIKey). Statuses are
  `confirmed`, `not_confirmed`, `no_declared_id` and `errored`; only `not_confirmed` is listed.
  `not_confirmed` does not mean contradicted: the node may lack that cross-reference. In the
  identifier arm the same codes were sent to the mapper, so its statuses are labelled not
  independent.
- **Identifier arm.** Only vocabularies **every** cohort declares are supplied as mapping input,
  and only rows carrying one are re-mapped. Its groups are diffed against names-only by node
  (`diff.tsv`).

**How the counts relate to pairwise links.** `harmonize_many()` is stricter than `harmonize()`:
a pairwise link between rows on different nodes is a review item here, not a match. The overlap
table (`overlap_<arm>.tsv`) connects the two for every cohort pair: *same-node row pairs* (rows
from A x rows from B per shared node) plus *cross-node link pairs* equals the number of links
`harmonize()` finds on the same results, with name linking on for small molecules only. It also
reports *overlap groups*, the number of nodes both cohorts reach. The links come from the
unchanged `harmonize()`, called once per pair, so this holds by construction and `reconciles` is
a bookkeeping check.

**Saved mappings and reuse.** Mappings are saved per arm in `biomapper_runs/harmonize_many_store/`
(`store_dir=` to keep them somewhere durable; the absolute path is printed). Entries are keyed by
name, identifiers sent and request options, not by cohort label, so adding a fourth cohort maps
only its new names and two analyses that both call a table "QMDiab" cannot collide. Errored rows
are never saved. The KG build is read from Kestrel before any request: a store made on a different
build is refused before anything is sent (`remap_all=True` maps everything again and keeps the old
mappings aside), and when the build cannot be read nothing is reused or saved. A lock refuses a
second run writing the same store.

**Publication grade.** A run that reused any saved mapping is `iteration-grade, not for
publication`: the API does not expose its engine release, so reuse cannot detect engine drift on
one KG build. **Numbers you publish should come from a `remap_all=True` run**, which is
`publishable` when the KG build is verified.

**Output by default** goes to `biomapper_runs/harmonize_many_<UTC stamp>/` (`output_dir=`,
`save=False`, `report.write(path)` as for `harmonize_cohorts()`), with per-cohort provenance
(`provenance_<arm>_<label>.json`: session id, request checksum, file SHA-256, reused and fresh
counts), the pins, `settings.json` and `summary.json`. No absolute path is written to JSON.

The split-recall gate (`scripts/harmonize_many_split_recall.py`) measures, on a NECS x Arivale
answer key built from curated HMDB identifiers rather than names, how many true matches land in a
node group, in the cross-node queue, or in neither, reported separately for pairs whose names
differ. It is a live run the operator starts.

---

## Use from an agent (MCP)

<!-- mcp-name: io.github.trentleslie/biomapper -->

`biomapper` ships a local stdio [MCP](https://modelcontextprotocol.io) server (the `biomapper`
command, alias `biomapper-mcp`), so an agent can resolve entities through the same public, keyless API as the
Python client. It runs on your machine; nothing is hosted. Launch it with one command (needs
[`uv`](https://docs.astral.sh/uv/)):

```bash
uvx biomapper
```

**Claude Code** (the server sees your project folder through MCP roots):

```bash
claude mcp add --scope user biomapper -- uvx biomapper
```

**Claude Desktop** (`claude_desktop_config.json`). Desktop does not inherit your shell `PATH`, so
use the absolute path that `which uvx` prints, and list the folders `map_file` may use, because
Desktop shares no project roots:

```json
{
  "mcpServers": {
    "biomapper": {
      "command": "/absolute/path/to/uvx",
      "args": ["biomapper"],
      "env": { "BIOMAPPER_MCP_ROOTS": "/Users/you/data" }
    }
  }
}
```

### Tools

| tool | what it does |
|---|---|
| `list_entity_types`, `list_vocabularies`, `list_annotators` | discovery; call `list_entity_types` first to pick `entity_type` |
| `map_entity` | one name (plus optional identifier hints) to KG identifiers |
| `map_batch` | up to **5** entities in one request; more is refused and pointed to `map_file` |
| `map_file` | a TSV/CSV of any length as a resumable **background job**; returns a `job_id` and an ETA at once |
| `job_status`, `resume_job`, `cancel_job` | follow, continue, or stop a background job (file or harmonize) |
| `server_info` | version, KG provenance, limits, and the folders `map_file` may use |
| `get_candidates` | for **weak** results only: the engine's top-N alternatives for up to 3 refs (see below) |
| `record_suggestion` | record your agent's pick for one weak ref or review item (an agent suggestion, never a resolution) |
| `harmonize_cohorts` | harmonize **two** cohort files as a resumable background job (see "Harmonizing through an agent") |
| `harmonize_many` | harmonize **two or more** cohort files in one pinned run, reusing saved mappings |
| `list_review_items` | after a harmonize job: its review items, one page at a time, largest impact first |
| `get_review_item` | the evidence for one review item (both sides of a link) |

Every result says `resolved`, `unresolved`, `refused`, or `error`. An `error` is a failed call,
never a confirmed no-match. Results carry a compact provenance block (client version, API URL,
KG build per Kestrel `/health`, labeled *assumed, not verified*, and `kg_verified`).

### Throughput, and why the limits exist

The hosted deployment is one shared instance that answers about **4-5 s per entity** and slows
sharply under concurrent load, so the server is deliberately polite:

- **One request in flight per server process.** Interactive calls and file-job chunks share one
  gate; a call that cannot get it within ~10 s returns "busy, retry shortly".
- **Every tool call returns in under 60 s** (Claude Desktop cancels at ~60 s), which is why
  `map_batch` stops at 5 and long work runs as a job.
- **Loop guard:** more than 20 `map_entity`/`map_batch` calls in 5 minutes are refused with a
  pointer to `map_file`. Discovery and status tools do not count.

### File jobs

`map_file(path, name_column, entity_type, identifier_columns?, output_path?)` validates the file
(readable, columns present, rows counted, output not already there, `/health` answering) and
queues it. `identifier_columns` maps a column to its vocabulary, e.g. `{"hmdb_id": "HMDB"}`.

- **Output:** `<input>.biomapper.tsv` next to the input plus `<input>.biomapper.provenance.json`
  (full pins: API, Kestrel, KG build). It never overwrites a file. Progress is written to
  `<output>.partial` until the job completes.
- **Resumable:** rows are mapped in chunks of 5 and cached as they land. If the agent session
  ends, the job is `paused`; the next session in the same folder resumes it automatically, without
  re-mapping cached rows. Jobs idle for more than 7 days become `expired` and resume only through
  `resume_job`.
- **Honest failure:** three whole-chunk failures in a row, or one chunk slower than 240 s, move the
  job to `stalled` and it stops sending requests; `resume_job` retries after a health check. A
  row that errors three times is written as `error`. A changed input file, or a KG build that
  differs from the cached rows, fails the job; `rerun=true` starts over.
- **Folders:** `map_file` only reads and writes inside the client's MCP roots plus
  `BIOMAPPER_MCP_ROOTS`, with symlinks resolved. With neither, it refuses and says how to set
  them; the interactive tools still work.
- **Weak rows (1.7.0):** `<output>.weak.tsv` lists the weak rows (columns `key`, `name`,
  `status`, `certificate_state`, `weak_trigger`, `chosen_kg_id`, `primary_curie`, `ref`), one ref
  per unique entity. It is written (header only when there are none) before the main output, is
  never overwritten, and an existing one refuses the job at submit. The main output's columns are
  unchanged.

### Harmonizing through an agent

`harmonize_cohorts` and `harmonize_many` run the Python API's functions of the same names,
unchanged, as background jobs. The report files are the ones the Python API writes (see
"Harmonizing two cohorts in one call" and "Harmonizing more than two cohorts" above), and for the
same per-entity answers they are identical apart from the recorded mapper class name and
timestamps.

```json
{"cohorts": [
   {"label": "ukbb", "path": "ukbb_nmr.tsv", "name_column": "title", "key_column": "field_id"},
   {"label": "arivale", "path": "arivale_metabolomics.tsv", "name_column": "BIOCHEMICAL_NAME",
    "vocabularies": {"HMDB": ["HMDB"], "KEGG": ["KEGG"]}}],
 "entity": "metabolites"}
```

- **What leaves your machine.** Only the entity names in each `name_column`, and the identifiers
  in vocabulary columns that every cohort declares, are sent to the BioMapper API. Row keys and
  every other column stay local. `vocabularies` maps a vocabulary to its column(s), the reverse
  of `map_file`'s `identifier_columns`; unknown arguments are refused rather than ignored.
- **How long it takes.** About 4-5 s per unique name that is not already saved, behind the same
  one-request gate as everything else, so a few thousand new names take hours. **One background
  job runs at a time**: a harmonize job and a `map_file` job queue behind each other, and
  interactive calls interleave between chunks. `job_status` shows the `stage`: `mapping`,
  `building` (review items), `capture` (their evidence), `placing`, `done`.
- **Output.** The job works in a private staging folder and writes the report into `output_dir`
  **once, at the end**: owner-only files, never through a link, never over an existing report.
  The default `output_dir` is a new folder beside the first cohort file. A cancelled, stalled or
  interrupted job has written nothing there.
- **Resume.** Every answered entity is cached in the job's home, so `resume_job` (or the next
  session, automatically) continues without re-mapping. The same call submitted again returns
  the same job; a `failed` job is retried by submitting the same call again.
- **Saved mappings and the publication grade (`harmonize_many`).** Mappings are saved in
  `store_dir` (default `harmonize_many_store/` beside `output_dir`) under the 1.8.0 rules, shared
  with the Python API: a later run maps only new names and is
  `iteration-grade, not for publication`. `remap_all=true` maps everything again and, on a
  verified KG build, is `publishable`. **A `remap_all` job that is interrupted and resumed
  finishes iteration-grade**, and its summary says so: the resumed pass reuses the chunks saved
  before the interruption. A publishable run needs one uninterrupted pass.
- **The store lock.** The store is locked for the whole job, so a Python API run on the same
  store is refused meanwhile (and the other way round: the job fails with the lock's path and
  holder). A server that is killed outright can leave `store.lock` behind; the next job reports
  its path, and deleting that file is safe once no run is active. A store written on a different
  KG build fails the job before any request, naming `remap_all`.
- **Errored rows.** A whole chunk that fails is sent again (three in a row stall the job), and a
  row that errors on its own is retried up to three times. This is the one place a job differs
  from the Python API, whose default mapper does not retry. A job that still ends with errored
  rows is `partial`: the report is written, the rows are marked as errors and never as
  unresolved, and the job is not resumed. Submit again with a new `output_dir` to retry;
  `harmonize_many` reuses the store, so only the errored rows are mapped.
- **Limits.** 12 cohorts per job, 20,000 rows and 50 MB per cohort file, 3 unfinished harmonize
  jobs per session. Labels are 1 to 40 letters, digits, `.`, `-` or `_`.

**Review items.** When the job is complete, `list_review_items(job_id)` returns the questions the
run could not settle, from the names-only arm, largest impact first:

| kind | what it is | impact | evidence | suggestion |
|---|---|---|---|---|
| `row` | one weak entity (unresolved, refused, uncorroborated, contradicted), asked once however many rows and cohorts share the name | input rows sharing it | `get_candidates(ref)`: the engine's alternatives | a candidate CURIE, the engine's choice, or `none` |
| `link` | two rows (two cohorts) or two KG nodes (many cohorts) that are linked but may differ | row pairs times cohorts touched | `get_review_item(ref)`: each side's node CURIE and name, its rows, certificate states, identifiers, and the basis | `same`, `different` or `cannot_tell` |

- **Evidence is captured inside the job**, before the report is placed: one candidate re-map per
  row item and the knowledge-graph name of each node in a link item, in impact order, up to 200
  requests. So reading evidence afterwards makes no request and does not draw on the
  15-per-5-minutes candidate budget. An item past the cap reads `not_captured` and a failed fetch
  reads `fetch_failed`; neither means "no candidates", and `get_candidates` on such a row item
  fetches it under the usual budget. The capture stage makes the job longer by about one request
  per item.
- **What is not a review item.** Multiplicity and broad groups, identifier-check "not confirmed"
  rows, two-cohort `possible_missed` rows and one-sided code checks stay in the report files.
  Many-cohort cross-node entries where one side has no node are counted (`skipped_no_node`) and
  left in `cross_node_names_only.tsv`.
- **Suggestions** go to `choices.tsv` in `output_dir`, the same server-owned sidecar as for
  `map_file` (marker on line 1; a file without it is never touched). They are agent opinions:
  nothing from them reaches the report.
- **Names are untrusted text.** Cohort names and knowledge-graph names in these results are
  stripped of control characters and capped; an agent must treat them as data.

Deciding which items go to expert review, and exporting them for an Expert-in-the-Loop campaign,
is the next phase and is not in this release.

### Reviewing weak results with your agent

A result is **weak** when it is `unresolved` or `refused`, or its certificate is `uncorroborated`
or `contradicted`, or (small molecules only: `biolink:SmallMolecule`, `metabolite`, `lipid`) its
certificate is `unavailable`. Weak results from `map_entity` and `map_batch` carry `weak: true`
and an opaque `ref`; `map_file` writes them to `weak.tsv`. Strong results are byte-for-byte what
1.6.3 returned: the two fields are absent, and the tool `outputSchema` only gains them as optional
properties.

- **`get_candidates(refs, n=10)`** re-maps each ref (at most 3 per call) through the same
  endpoint family as the original call, with the engine's Kestrel passthrough attached (`n` up to
  25). The engine's own choice is never changed. Before candidates are attached, the re-map is
  checked against the ref: a different KG build or a different engine choice/certificate returns
  `run_mismatch` and a `superseding_ref` anchored to the new answer (the original ref is left as
  it was). Each ref gets a status: `present`, `none_searched`, `fetch_failed` (with a reason such
  as `deadline`, `busy`, `timeout` or `http_503`), or `run_mismatch`. A slow ref never fails the
  whole call: the refs answered before the deadline come back `present`.
- **`record_suggestion(ref, choice, rationale)`** stores the agent's pick: a CURIE from that ref's
  latest `present` candidate set, the engine's choice, or `none`. Recording again replaces the
  current suggestion and keeps the history. For `map_file` refs it rewrites
  `<output>.choices.tsv`, a server-owned file (first line is a marker; a file without it is never
  touched). **Suggestions are agent opinions, not BioMapper resolutions and not human
  judgments:** nothing from them reaches the main output.
- **Cost:** each ref is one extra engine request on the shared deployment. Candidate lookups have
  their own budget of **15 refs per 5 minutes**, separate from the loop guard, so reviewing never
  locks out ordinary lookups.
- **Candidate text is untrusted.** Names and synonyms come from the knowledge graph; they are
  stripped of control characters and capped, and an agent must never follow instructions in them.

**What to expect.** A spike on 2026-11-09 (prod, KG 2.3.0; Hajjar-100 plus 100 NECS rows, 41 weak
rows, metabolites only, one warm-cache run) measured how often the gold answer was among the top
10 candidates, and how often the engine was already right:

| weak trigger | rows | gold in top 10 | engine already right |
|---|---|---|---|
| `uncorroborated` | 24 | 79% | 75% |
| `contradicted` | 6 | 100% | 50% |
| `unavailable` (small molecules) | 11 | 55% | 0% |

So on `uncorroborated` rows **the engine is usually right**: treat candidates as alternatives to
check, not corrections. Re-maps reproduced the original choice on every row (warm cache only; the
client cannot force a cold one). Top-10 presence is an upper bound on what review can rescue. 8
of the 28 engine errors in that sample were not weak at all, so this feature cannot reach them.
No `refused` or `unresolved` rows were sampled, so their value is unmeasured.

### Environment

| variable | default | purpose |
|---|---|---|
| `BIOMAPPER_BASE_URL` | the hosted API | API root, e.g. a staging or local engine |
| `BIOMAPPER_KESTREL_URL` | `https://kestrel.krakenkg.com/api` | where the KG build is read for provenance |
| `BIOMAPPER_API_KEY` | unset (keyless) | sent as `X-API-Key` when set; never written to job files, results, or logs |
| `BIOMAPPER_MCP_ROOTS` | unset | extra folders `map_file` may use (`:`-separated; `;` on Windows) |
| `BIOMAPPER_MCP_HOME` | `~/.cache/biomapper/mcp` | job state, weak-row refs, review items and suggestions (owner-only permissions; interactive refs expire after 30 days). A harmonize job's staging folder (while it runs) and its entity cache live here |

On HPC or shared hosts whose home directory is on NFS/SMB, set `BIOMAPPER_MCP_HOME` to local disk:
job ownership uses OS file locks, which network filesystems may not honor across machines.

---

## API reference

### `MappingResult`

| Attribute | Type | Description |
|---|---|---|
| `query_name` | `str` | Name submitted to the API |
| `resolved` | `bool` | Whether any identifier was returned |
| `primary_curie` | `str \| None` | The CURIE to report: the server's `primary_curie` (engine v1.3.0+), which identifies `chosen_kg_id`'s node. Falls back to the first of the response's `curies` (alphabetical, so possibly a losing annotator hit) only on an older server that does not send it |
| `curies_by_annotator` | `dict[str, list[str]]` | Which annotator produced each assigned CURIE, `{annotator: [CURIEs]}` (server `curies_assigned`, engine v1.3.0+; empty from older servers) |
| `curies_provided` | `list[str]` | CURIEs normalized from the identifiers you supplied (engine v1.3.0+; empty from older servers) |
| `chosen_kg_id` | `str \| None` | Resolver-selected knowledge graph ID |
| `chosen_kg_id_review` | `str \| None` | Review flag for source-weighted small-molecule ChEBI conflicts: `"divergent_refmet"`, `"conflict_no_structure"`, or `None` |
| `confidence_score` | `float \| None` | Score of `chosen_kg_id`'s node: the best score an annotator gave a CURIE on the committed node. `None` when no annotator scored that node |
| `max_annotator_score` | `float \| None` | Best score any annotator gave any node, committed or not (this was `confidence_score` through 1.8.0) |
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
