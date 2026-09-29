"""Registry for the external benchmark suite: 11 arms, plus the 2 deliberate skips.

Ported from ``studies/external_benchmarks/config.py`` in the engine repo, trimmed to the arms
this suite runs against a deployment. What is deliberately *not* here:

* ``uniprot-idmapping`` / ``ncbi-gene2ensembl`` / the ``provided-id`` family — multi-hundred-MB
  bulk backbones that need a pinned artifact rather than a URL.
* ``pham`` — its source is a MetaNetX FTP path requiring hand reconstruction.
* The engine's CI regression gates and the ``--resolver-mode {weighted,vote}`` A/B, which
  toggles a resolver constructor argument deliberately absent from the API surface.

Held-out-gold invariant, which every config below is built around: the ``gold_*`` columns ride
along beside the query in the input frame, and ``provided_id_columns`` is empty, so BioMapper
never sees them. Only the scorers consume them. Several configs enforce that in
``__post_init__`` rather than trusting it, because a gold-equals-query config scores a trivial
100% and looks like a triumph.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@runtime_checkable
class RunnableConfig(Protocol):
    """The minimal surface the runner consumes.

    Both ``DatasetConfig`` and ``CurieDatasetConfig`` satisfy this structurally, so the runner
    drives either arm unchanged. Members are read-only properties so the frozen dataclasses,
    whose fields are immutable, match structurally.
    """

    @property
    def key(self) -> str: ...
    @property
    def arm(self) -> str: ...
    @property
    def entity_type(self) -> str: ...
    @property
    def input_type(self) -> str: ...
    @property
    def name_column(self) -> str: ...
    @property
    def target_vocabs(self) -> tuple[str, ...]: ...


# ==================================================================================================
# Metabolite (structure-oracle) arms
# ==================================================================================================


@dataclass(frozen=True)
class DatasetConfig:
    """A metabolite benchmark dataset registry entry, scored by the InChIKey structure oracle."""

    key: str
    arm: str
    entity_type: str
    input_type: str
    target_vocabs: tuple[str, ...]
    name_column: str
    gold_chebi_column: str  # "" when the source ships none
    gold_inchikey_column: str  # the independent structure oracle
    gold_smiles_column: str | None  # None when the source ships no SMILES
    source_doi: str
    source_url: str
    license: str
    # (namespace, held-out-column) pairs whose per-column presence is reported on the card.
    gold_coverage_columns: tuple[tuple[str, str], ...] = ()
    # Deterministic reservoir subsample for large reference sets. The source URL is a mutable
    # "current release", so URL+seed+n cannot reconstruct the scored subset; the exact subsample
    # is persisted beside the card.
    subsample_n: int | None = None
    subsample_seed: int = 42
    # Restrict the load to rows carrying a gold InChIKey: the structure oracle REQUIRES a
    # held-out structure, so an unfiltered sample of a sparsely-annotated source would be
    # mostly coverage-only with nothing to score.
    require_gold_structure: bool = False
    # Benchmark ROLE. "accuracy" arms report an independent accuracy number.
    # "capability_regression" arms certify a capability is wired and gate a resolvability
    # FLOOR; they are never reported as an accuracy headline.
    role: str = "accuracy"
    regression_floor: float | None = None
    # Expected SHA of the fetched source, when one is pinned. "" means record whatever arrives.
    expected_source_sha256: str = ""


# Hajjar et al. 2026, Metabolomics, DOI 10.1007/s11306-026-02404-w. Curated 100-metabolite
# human-plasma set with ChEBI ID + InChIKey ground truth.
#
# source_url was empty in the engine registry, which is the only reason this arm sat in
# SUITE_SKIPPED. The publisher's static supplement is live and fetchable without auth, and its
# SHA is pinned below, so the arm is now self-sourcing and runs unattended.
#
# Two things about this source that have bitten before:
#   * A HEAD request against it reports HTTP 200 with 0 bytes. That is a HEAD artifact, not a
#     dead link. ``sources.fetch`` is GET-only and asserts a non-empty body plus this SHA.
#   * Licensing is CC BY-NC-ND 4.0. Fetching from the publisher at run time is the clean
#     design; committing a derived gold CSV would be the ND-clause risk. Fetch, do not
#     redistribute.
#
# gold_smiles_column is None: the supplement's table exposes ChEBI Name / ChEBI Identifier /
# InChIKey / Monoisotopic mass / Polarity / Chemical class and NO SMILES column. The engine
# config declared "gold_smiles", which silently resolved to empty. Per the 2026-09-23 decision
# that the SMILES path is deprecated, the field is dropped rather than reconciled — so the
# charge-normalized variant is reported as unavailable with a reason instead of being computed
# against an absent column.
HAJJAR = DatasetConfig(
    key="hajjar-100",
    arm="metabolite",
    entity_type="metabolite",
    input_type="name",
    # Primary CHEBI; also HMDB/PUBCHEM/KEGG for the vocab-coverage figure. Correctness is
    # always the dataset-anchored InChIKey block, never the target vocab's identity.
    target_vocabs=("CHEBI", "HMDB", "PUBCHEM", "KEGG"),
    name_column="metabolite_name",
    gold_chebi_column="gold_chebi",
    gold_inchikey_column="gold_inchikey",
    gold_smiles_column=None,
    source_doi="10.1007/s11306-026-02404-w",
    source_url=(
        "https://static-content.springer.com/esm/art%3A10.1007%2Fs11306-026-02404-w/"
        "MediaObjects/11306_2026_2404_MOESM1_ESM.docx"
    ),
    expected_source_sha256="a58ca331e27e4f56ba921a3168c7ee3488fb30c7ea42d5d321e323224c807c50",
    license=(
        "CC BY-NC-ND 4.0 (Hajjar et al. 2026 supplement, Springer). Fetched, not redistributed."
    ),
    gold_coverage_columns=(
        ("INCHIKEY", "gold_inchikey"),
        ("CHEBI", "gold_chebi"),
    ),
)


# Monti et al. 2026, GeroScience, DOI 10.1007/s11357-026-02174-2 (New England Centenarian
# Study). 1,495 plasma metabolites from Metabolon; MOESM5 ships CHEMICAL_NAME plus an
# independent curated InChIKey/SMILES column and partial external IDs. The InChIKey column is
# the structure oracle; no ChEBI ground truth ships, so gold_chebi_column is "".
#
# Known gold defect, do not quote a corrected rate without re-adjudicating: this gold carries
# roughly 5% InChIKey errors, and an InChIKey first block is NOT tautomer- or charge-invariant,
# so a first-block disagreement is not by itself evidence the gold is wrong.
NECS = DatasetConfig(
    key="necs-metabolon",
    arm="metabolite",
    entity_type="metabolite",
    input_type="name",
    target_vocabs=("CHEBI",),
    name_column="chemical_name",
    gold_chebi_column="",
    gold_inchikey_column="gold_inchikey",
    gold_smiles_column="gold_smiles",
    source_doi="10.1007/s11357-026-02174-2",
    source_url=(
        "https://static-content.springer.com/esm/art%3A10.1007%2Fs11357-026-02174-2/"
        "MediaObjects/11357_2026_2174_MOESM5_ESM.xlsx"
    ),
    license="See Monti et al. 2026 (GeroScience, Springer) supplement terms.",
    gold_coverage_columns=(
        ("INCHIKEY", "gold_inchikey"),
        ("SMILES", "gold_smiles"),
        ("HMDB", "gold_hmdb"),
        ("KEGG", "gold_kegg"),
        ("PUBCHEM", "gold_pubchem"),
        ("CAS", "gold_cas"),
        ("REFMET", "gold_refmet"),
    ),
)


# RefMet, the Metabolomics Workbench reference nomenclature (Fahy & Subramaniam 2020).
# >200k analytes, so streamed and reservoir-subsampled from the InChIKey-bearing population.
#
# CIRCULARITY: ``refmet`` is an ingested KRAKEN source AND the source-weighting target
# annotator, so this arm measures coverage, not independent accuracy. ``provenance
# .circularity_notes`` labels it per run from the build's own source list.
REFMET = DatasetConfig(
    key="refmet",
    arm="metabolite",
    entity_type="metabolite",
    input_type="name",
    target_vocabs=("CHEBI",),
    name_column="refmet_name",
    gold_chebi_column="",  # RefMet's chebi_id is a coverage crosswalk, not the oracle
    gold_inchikey_column="gold_inchikey",
    gold_smiles_column=None,  # the bulk CSV ships no SMILES
    source_doi="10.1038/s41592-020-01009-y",
    source_url="https://www.metabolomicsworkbench.org/databases/refmet/refmet_download.php",
    license=(
        "RefMet / Metabolomics Workbench data are freely available (metabolomicsworkbench.org)."
    ),
    subsample_n=1500,
    subsample_seed=42,
    require_gold_structure=True,
    gold_coverage_columns=(
        ("INCHIKEY", "gold_inchikey"),
        ("CHEBI", "gold_chebi"),
        ("HMDB", "gold_hmdb"),
        ("PUBCHEM", "gold_pubchem"),
        ("KEGG", "gold_kegg"),
        ("LIPIDMAPS", "gold_lipidmaps"),
    ),
)


# NIST SRM 1950 / SRM1950-DB, 1,058 certified human-plasma metabolites (Mandal et al. 2025).
#
# The delivery's INCHIKEY column is EMPTY, so the oracle InChIKey is DERIVED from the certified
# SMILES with RDKit — deterministic, and sharing no infrastructure with BioMapper's resolver, so
# oracle independence holds.
#
# PINNED SOURCE: upstream's TLS certificate expired 2026-09-15. The bytes are read from a local
# SHA-verified pin (``sources.PINNED_SOURCES``); certificate verification is never disabled.
SRM1950 = DatasetConfig(
    key="srm1950",
    arm="metabolite",
    entity_type="metabolite",
    input_type="name",
    target_vocabs=("CHEBI",),
    name_column="metabolite_name",
    gold_chebi_column="",
    gold_inchikey_column="gold_inchikey",
    gold_smiles_column="gold_smiles",
    source_doi="10.1021/acs.analchem.4c05018",
    source_url="https://srm1950-data.wishartlab.com/metabolites.csv",
    license=(
        "SRM1950-DB data are freely available (wishartlab.com); NIST SRM 1950 certified values."
    ),
    gold_coverage_columns=(
        ("INCHIKEY", "gold_inchikey"),
        ("SMILES", "gold_smiles"),
    ),
)


# LMSD, the LIPID MAPS Structure Database (Liebisch et al. 2020 shorthand nomenclature).
#
# CONTAMINATION CONTROL: the query is a lipid NAME whose structure must be inferred; ``LM_ID``
# is held out — never a query, never the oracle. The KG recognizes the LIPIDMAPS namespace, so
# scoring on LM_IDs would be circular.
#
# ROLE: capability_regression, not accuracy. ``lipidmaps`` is an ingested KRAKEN source, and
# adopting Goslin plus the LIPID MAPS API as the lipid resolution path makes the gold InChIKey
# and BioMapper's answer come from the same place. The July independence audit anticipated
# exactly this ("INDEPENDENT now; BECOMES-CIRCULAR-IF Goslin / LIPID MAPS REST is
# incorporated"). So this arm gates a resolvability floor and is reported as coverage; it must
# never be presented as an accuracy headline.
LMSD = DatasetConfig(
    key="lmsd",
    arm="metabolite",
    entity_type="metabolite",
    input_type="name",
    target_vocabs=("CHEBI",),
    name_column="lipid_name",
    gold_chebi_column="",
    gold_inchikey_column="gold_inchikey",
    gold_smiles_column="gold_smiles",
    source_doi="10.1194/jlr.S120001025",
    source_url="https://www.lipidmaps.org/files/?file=LMSD&ext=sdf.zip",
    license="LMSD structures and annotations are available under CC BY 4.0 (lipidmaps.org).",
    subsample_n=1500,
    subsample_seed=42,
    require_gold_structure=True,
    role="capability_regression",
    regression_floor=0.90,
    gold_coverage_columns=(
        ("INCHIKEY", "gold_inchikey"),
        ("SMILES", "gold_smiles"),
        ("CHEBI", "gold_chebi"),
        ("HMDB", "gold_hmdb"),
        ("PUBCHEM", "gold_pubchem"),
        ("SWISSLIPIDS", "gold_swisslipids"),
    ),
)


# SwissLipids. NOT a KRAKEN ingest source, which would make it a legal accuracy gold — the one
# reportable lipid accuracy arm if it could be sourced.
#
# It currently cannot. The source returns HTTP 200 with a zero-byte body on a full GET
# (verified 2026-09-23, not a HEAD artifact) and no usable local copy exists: the 2026-08-05
# suite run holds only a 120-byte subsample. Registered so the arm reports as SKIPPED WITH A
# REASON (``sources.UNAVAILABLE_SOURCES``) rather than vanishing from the manifest or, worse,
# reporting as an empty success.
SWISSLIPIDS = DatasetConfig(
    key="swisslipids",
    arm="metabolite",
    entity_type="metabolite",
    input_type="name",
    target_vocabs=("CHEBI",),
    name_column="lipid_name",
    gold_chebi_column="",
    gold_inchikey_column="gold_inchikey",
    gold_smiles_column="gold_smiles",
    source_doi="10.1093/nar/gku1179",
    source_url="https://www.swisslipids.org/api/file.php?cast=normal&file=lipids.tsv",
    license="SwissLipids data are freely available for academic use (swisslipids.org).",
    subsample_n=1500,
    subsample_seed=42,
    require_gold_structure=True,
    gold_coverage_columns=(
        ("PUBCHEM", "held_out_pubchem"),
        ("INCHIKEY_SWISSLIPIDS", "gold_inchikey_swisslipids"),
    ),
)


# ==================================================================================================
# Gene / protein (CURIE-equality) arms
# ==================================================================================================


@dataclass(frozen=True)
class CurieDatasetConfig:
    """A gene/protein cross-reference registry entry, scored by CURIE equality.

    There is no structure oracle for genes and proteins; correctness is CURIE equality between
    BioMapper's assigned cross-reference CURIEs and the reference's held-out cross-refs.

    Accuracy for these arms is reported PER TARGET NAMESPACE. An any-namespace roll-up hides
    that the namespaces perform very differently, and the roll-up has been misquoted as a
    single headline before; ``scorers.curie_scorer`` marks it non-quotable for that reason.
    """

    key: str
    arm: str  # "gene" | "protein"
    entity_type: str
    input_type: str
    name_column: str
    target_vocabs: tuple[str, ...]
    # (namespace, held-out-column) — stated explicitly so the gold-column identity is
    # reviewable rather than inferred at run time.
    gold_curie_columns: tuple[tuple[str, str], ...]
    source_label: str
    source_url: str
    license: str
    subsample_n: int = 1500
    subsample_seed: int = 42
    tax_filter: str | None = None


# HGNC complete set (genenames.org). Query = approved gene symbol.
#
# CIRCULARITY: partly-circular but milder than the pure xref arms — the symbol-to-ID step is a
# real resolution step, but the cross-refs come from the same tables the graph ingests.
HGNC = CurieDatasetConfig(
    key="hgnc-complete-set",
    arm="gene",
    entity_type="gene",
    input_type="name",
    name_column="symbol",
    target_vocabs=("ENSEMBL", "NCBIGene", "UniProtKB"),
    gold_curie_columns=(
        ("ENSEMBL", "gold_ensembl"),
        ("NCBIGene", "gold_entrez"),
        ("UniProtKB", "gold_uniprot"),
    ),
    source_label="HGNC complete set",
    source_url="https://storage.googleapis.com/public-download-files/hgnc/tsv/tsv/hgnc_complete_set.txt",
    license="HGNC data are freely available without restriction (genenames.org).",
)


# NLM-Gene (Islamaj Doğan et al. 2021). The INDEPENDENT name-input gene-normalization
# benchmark: its gold NAMESPACE (NCBIGene) is downstream in BioMapper's path, but the gold
# MAPPING was produced by six NLM indexers reading 550 PubMed abstracts, so it is independent
# by construction in a way the xref backbones are not.
#
# Scored ambiguity-partitioned: unambiguous surface forms give accuracy, ambiguous forms give a
# flag-rate. Blending them would let the hard population dilute a number that is supposed to
# measure the easy one.
NLMGENE_AMBIGUOUS_MIN_GENES = 2

NLMGENE = CurieDatasetConfig(
    key="nlm-gene",
    arm="gene",
    entity_type="gene",
    input_type="name",
    name_column="mention",
    target_vocabs=("NCBIGene",),
    gold_curie_columns=(("NCBIGene", "gold_ncbigene"),),
    source_label="NLM-Gene corpus (Islamaj Doğan et al. 2021)",
    source_url="https://ftp.ncbi.nlm.nih.gov/pub/lu/NLMGene",
    license="Public domain (NLM/NCBI); freely available.",
    # The adapter scores the full deduped surface-form set rather than subsampling; set high so
    # nothing is silently dropped.
    subsample_n=1_000_000,
    subsample_seed=42,
)


# ==================================================================================================
# Published-competitor values
# ==================================================================================================


@dataclass(frozen=True)
class CompetitorResult:
    """A published competitor number transcribed from a paper table.

    ``value`` is intentionally ``None`` until transcribed at run time. No number is fabricated
    in source control — this is the discipline that the unverified 96.5% Metabolon figure
    exists to enforce. ``doi`` and ``table_ref`` are load-bearing: a competitor entry missing
    either cannot reach a figure.
    """

    tool: str
    metric: str
    input_type: str
    value: float | None
    doi: str
    table_ref: str


HAJJAR_DOI = HAJJAR.source_doi
HAJJAR_COMPETITOR_TABLE_REF = "Hajjar et al. 2026, conversion-accuracy table"

# The six ID-conversion services Hajjar benchmarks on the same 100-set (a valid same-dataset
# comparison). Values stay None here; transcribe from the paper's table at report time. The
# right source for these is MOESM2 (the Fig-3 competitor comparisons), not MOESM1.
HAJJAR_COMPETITORS: tuple[CompetitorResult, ...] = tuple(
    CompetitorResult(
        tool=tool,
        metric="conversion_accuracy",
        input_type="name",
        value=None,
        doi=HAJJAR_DOI,
        table_ref=HAJJAR_COMPETITOR_TABLE_REF,
    )
    for tool in (
        "CTS",
        "MetaboAnalyst",
        "RaMP",
        "MetabolomicsWorkbench/RefMet",
        "PubChem Identifier Exchange",
        "MetaNetX",
    )
)


# ==================================================================================================
# MetaBench (Lu et al. 2025, arXiv:2510.14944) — the Grounding task
# ==================================================================================================
# The one external dataset with a valid LLM head-to-head: the paper scores 25 LLMs on the SAME
# 1,000 grounding pairs. Pairs are bidirectional cross-database mappings:
#   ID -> ID   (400): HMDB->KEGG, KEGG->HMDB              -> provided-ID mode
#   name -> ID (600): name->KEGG, name->HMDB, name->ChEBI -> name-input mode
# In both regimes the gold is the held-out TARGET id and correctness is CURIE equality, so one
# scorer and one number.
#
# CIRCULARITY: partly-circular. Its equivalence sets are built from the same xrefs the graph
# carries.
METABENCH_DOI = "10.48550/arXiv.2510.14944"
METABENCH_SOURCE_URL = (
    "https://huggingface.co/datasets/LuYuxing/MetaBench/resolve/main/"
    "Grounding%20-%20metabolite_mapping_dataset.csv"
)
METABENCH_LICENSE = "Apache-2.0 (HuggingFace dataset LuYuxing/MetaBench)"
METABENCH_EXPECTED_SHA256 = "5f1955d1053aee39ad7d6fd1a9c833d9221abdcfa8d258deb52f61036df12cd2"

METABENCH_BASELINE_TABLE_REF = (
    "Lu et al. 2025 (arXiv:2510.14944), Grounding-task results table — TRANSCRIBE per model"
)
# Every value is None (needs-verification). The paper's headline figures read from the arXiv
# HTML during acquisition MUST be re-checked against the source table before any is asserted;
# they are deliberately not written here as fact.
METABENCH_BASELINES: tuple[CompetitorResult, ...] = tuple(
    CompetitorResult(
        tool=tool,
        metric=metric,
        input_type="grounding",
        value=None,
        doi=METABENCH_DOI,
        table_ref=METABENCH_BASELINE_TABLE_REF,
    )
    for tool, metric in (
        ("Best LLM (no retrieval)", "grounding_exact_match"),
        ("Median LLM (no retrieval)", "grounding_exact_match"),
        ("Worst LLM (no retrieval)", "grounding_exact_match"),
        ("Best LLM (web-search retrieval)", "grounding_exact_match_with_retrieval"),
    )
)


@dataclass(frozen=True)
class MetaBenchDatasetConfig:
    """MetaBench Grounding — a mixed-regime cross-database ID-mapping benchmark.

    The adapter emits one normalized long frame; the scorer consumes the last three columns.

    ANTI-TRIVIAL-100%: the gold target and its namespace are never handed to the mapper, and
    every ID->ID subgroup has a source namespace disjoint from its target namespace
    (HMDB != KEGG). ``scorers.metabench_scorer.assert_metabench_held_out`` re-checks both,
    fail-loud, before scoring. There is no charge-normalized variant: the target is a database
    identifier, not a structure.
    """

    key: str = "metabench-grounding"
    arm: str = "metabolite"
    entity_type: str = "metabolite"
    input_type: str = "mixed"
    source_doi: str = METABENCH_DOI
    source_url: str = METABENCH_SOURCE_URL
    license: str = METABENCH_LICENSE
    expected_source_sha256: str = METABENCH_EXPECTED_SHA256
    question_column: str = "question"
    name_column: str = "metabolite_name"  # populated for name->ID rows; "" for ID->ID rows
    source_id_column: str = "source_id"  # populated for ID->ID rows; "" for name->ID rows
    source_namespace_column: str = "source_namespace"
    gold_target_column: str = "gold_target"  # HELD OUT
    target_namespace_column: str = "target_namespace"  # HELD OUT
    pair_type_column: str = "pair_type"  # "id2id" | "name2id"
    target_vocabs: tuple[str, ...] = ("CHEBI", "HMDB", "KEGG")
    baseline_competitors: tuple[CompetitorResult, ...] = field(
        default_factory=lambda: METABENCH_BASELINES
    )


METABENCH = MetaBenchDatasetConfig()


# ==================================================================================================
# MetaboliteAnnotator name-hit-rate arm (Lu et al. 2026, DOI 10.1021/acs.jproteome.5c00477)
# ==================================================================================================
# A same-set, NAME-input head-to-head. Reports a per-input NAME-HIT-RATE — the fraction of
# input names for which a target-vocab identifier was produced — computed with the same
# protocol as MetaboliteAnnotator so BioMapper's number lands beside the published 93.2%
# (positive) / 93.5% (negative) and the MetaboAnalyst 6.0 / metaboliteIDmapping baselines.
#
# The headline is a name-hit rate, which is COVERAGE BY CONSTRUCTION. It must not be presented
# as accuracy however favourable it looks.

METABOLITEANNOTATOR_ACCESSIONS: tuple[str, ...] = (
    "MTBLS12997",
    "MTBLS13105",
    "MTBLS12764",
    "MTBLS11733",
    "MTBLS12636",
    "MTBLS13039",
)


@dataclass(frozen=True)
class NameHitDatasetConfig:
    """A NAME-input, name-hit-rate registry entry (one config per ion mode).

    ANTI-TRIVIAL guard: the held-out ``gold_id_column`` must exist and must not be the
    ``name_column``. A gold-equals-query config would let every row self-hit to 100%.
    """

    key: str
    arm: str
    entity_type: str
    mode: str  # "positive" | "negative"
    name_column: str
    gold_id_column: str
    gold_smiles_column: str
    target_vocabs: tuple[str, ...]
    accessions: tuple[str, ...]
    source_url_template: str
    license: str
    input_type: str = "name"
    source_doi: str = "10.1021/acs.jproteome.5c00477"
    source_pmid: str = "41691569"
    # Flipped to "resolved" once real accessions are filled in. The adapter refuses a placeholder
    # before any scoring, so an unresolved arm can never look green; the field records which state
    # the card was built in.
    accessions_status: str = "needs-fetching"
    # MAF bytes come from the EBI public FTP mirror: the web-service /download route returns
    # HTTP 400 for these studies, so listing uses source_url_template and download uses this.
    maf_download_url_template: str = (
        "https://ftp.ebi.ac.uk/pub/databases/metabolights/studies/public/{accession}/{filename}"
    )

    def __post_init__(self) -> None:
        if not (self.gold_id_column and self.gold_id_column.strip()):
            raise ValueError(
                f"{self.key}: anti-trivial violation — a held-out gold_id_column is required to "
                f"adjudicate a name hit against a reference; none was given."
            )
        if self.gold_id_column == self.name_column:
            raise ValueError(
                f"{self.key}: anti-trivial violation — gold_id_column "
                f"{self.gold_id_column!r} equals the query name_column; the gold must be held out, "
                f"not the input. Refusing a config "
                f"that would self-hit to a trivial 100%."
            )


METABOLITEANNOTATOR_POS = NameHitDatasetConfig(
    key="metaboliteannotator-positive",
    arm="metabolite",
    entity_type="metabolite",
    mode="positive",
    name_column="metabolite_identification",
    gold_id_column="gold_database_identifier",
    gold_smiles_column="gold_smiles",
    target_vocabs=("CHEBI", "HMDB", "PUBCHEM", "KEGG"),
    accessions=METABOLITEANNOTATOR_ACCESSIONS,
    accessions_status="resolved",
    source_url_template="https://www.ebi.ac.uk/metabolights/ws/studies/{accession}",
    license="MetaboLights data are available under CC0 (per-study terms apply).",
)

METABOLITEANNOTATOR_NEG = NameHitDatasetConfig(
    key="metaboliteannotator-negative",
    arm="metabolite",
    entity_type="metabolite",
    mode="negative",
    name_column="metabolite_identification",
    gold_id_column="gold_database_identifier",
    gold_smiles_column="gold_smiles",
    target_vocabs=("CHEBI", "HMDB", "PUBCHEM", "KEGG"),
    accessions=METABOLITEANNOTATOR_ACCESSIONS,
    accessions_status="resolved",
    source_url_template="https://www.ebi.ac.uk/metabolights/ws/studies/{accession}",
    license="MetaboLights data are available under CC0 (per-study terms apply).",
)

NAME_HIT_REGISTRY: dict[str, NameHitDatasetConfig] = {
    METABOLITEANNOTATOR_POS.key: METABOLITEANNOTATOR_POS,
    METABOLITEANNOTATOR_NEG.key: METABOLITEANNOTATOR_NEG,
}


# ==================================================================================================
# metLinkR (Patt et al. 2025, DOI 10.1021/acs.jproteome.4c01051) — same-task cross-linking
# ==================================================================================================
METLINKR_DOI = "10.1021/acs.jproteome.4c01051"
METLINKR_PMCID = "PMC12053952"
# Canonical ACS SI URL. Cloudflare-blocked on a direct bot fetch, so it is recorded for
# provenance while the live fetch uses the EuropePMC bundle below.
METLINKR_SI_URL = (
    "https://pubs.acs.org/doi/suppl/10.1021/acs.jproteome.4c01051/suppl_file/pr4c01051_si_003.zip"
)
METLINKR_FETCH_URL = (
    "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC12053952/supplementaryFiles"
)
METLINKR_SI_ZIP_MEMBER = "pr4c01051_si_003.zip"
METLINKR_MANUAL_MAPPINGS_MEMBER = "ManualMappings.csv"
METLINKR_MANUAL_MAPPINGS_SHA256 = "3c94b2d0a6463b7dc446884a873b8a4d0e3d80943ea91de0bf1d599e1183e5ac"


@dataclass(frozen=True)
class MetLinkRDatasetConfig:
    """metLinkR head-to-head — a same-TASK cross-linking benchmark.

    One row per source metabolite across the 5 COMETS datasets. The query is the NAME; the
    curator cross-link group label and the curator-provided reference IDs are held out.

    ANTI-TRIVIAL guard: ``group_label_column`` must exist and must not equal ``name_column``. A
    label-equals-query config would leak the grouping into the input and let every curator pair
    self-link to 100%.
    """

    key: str = "metlinkr-comets"
    arm: str = "metabolite"
    entity_type: str = "metabolite"
    input_type: str = "name"
    name_column: str = "metabolite_name"
    group_label_column: str = "curator_group_label"  # HELD OUT — oracle (a)
    gold_hmdb_column: str = "curator_hmdb"  # HELD OUT — oracle (b) structural anchor
    gold_pubchem_column: str = "curator_pubchem"  # HELD OUT — oracle (b)
    source_file_column: str = "source_file"
    # A link is BioMapper-confirmed iff both members share a canonical id in ANY of these.
    target_vocabs: tuple[str, ...] = ("CHEBI", "HMDB", "PUBCHEM", "KEGG", "REFMET")
    source_doi: str = METLINKR_DOI
    source_pmcid: str = METLINKR_PMCID
    source_url: str = METLINKR_SI_URL
    fetch_url: str = METLINKR_FETCH_URL
    si_zip_member: str = METLINKR_SI_ZIP_MEMBER
    manual_mappings_member: str = METLINKR_MANUAL_MAPPINGS_MEMBER
    expected_manual_mappings_sha256: str = METLINKR_MANUAL_MAPPINGS_SHA256
    license: str = (
        "metLinkR SI (Patt et al. 2025, ACS J. Proteome Res.); COMETS/Metabolon-derived curator "
        "mappings — see ACS supporting-information terms."
    )

    def __post_init__(self) -> None:
        if not (self.group_label_column and self.group_label_column.strip()):
            raise ValueError(
                f"{self.key}: anti-trivial violation — a held-out group_label_column is "
                f"required to "
                f"adjudicate a link against the curator grouping; none was given."
            )
        if self.group_label_column == self.name_column:
            raise ValueError(
                f"{self.key}: anti-trivial violation — group_label_column "
                f"{self.group_label_column!r} equals the query name_column; the curator grouping "
                f"must be held out, not the input. Refusing a config that would self-link to 100%."
            )


METLINKR = MetLinkRDatasetConfig()

# Published same-task baseline, transcribed and verified against the paper's Results text
# (PMC12053952, "MetLinkR vs Manual Annotation"), not asserted from memory:
#   "Among metabolite entities identified across data sets by the curator, metLinkR identified
#    these entities at an 85.3% rate."  -> the oracle-(a) comparator.
#   "When removing identifiers that metLinkR was unable to map ... that number rose to a 90.7%
#    rate."  -> a mapped-only denominator; recorded for context, not the comparator.
METLINKR_BASELINES: tuple[CompetitorResult, ...] = (
    CompetitorResult(
        tool="metLinkR",
        metric="curator_agreement_rate",
        input_type="name",
        value=0.853,
        doi=METLINKR_DOI,
        table_ref="Patt et al. 2025, Results — 'MetLinkR vs Manual Annotation' (85.3% rate)",
    ),
    CompetitorResult(
        tool="metLinkR (mapped-only denominator)",
        metric="curator_agreement_rate_mapped_only",
        input_type="name",
        value=0.907,
        doi=METLINKR_DOI,
        table_ref="Patt et al. 2025, Results — 'MetLinkR vs Manual Annotation' (90.7% rate)",
    ),
)


# --------------------------------------------------------------------------------------------------
# Retained-but-not-suite-members
# --------------------------------------------------------------------------------------------------
# These are NOT in SUITE_DATASETS: their sources are multi-GB bulk downloads that need a pinned
# artifact rather than a URL, so they cannot run unattended (see SUITE_SKIPPED). They are kept
# here because the shared streaming/reservoir-subsample adapter (``adapters.backbones``) is
# parameterized over them, and deleting them would mean editing that adapter's streaming logic —
# a change with real regression risk and no benefit. A reader who finds them should treat their
# presence in the registry as "wired but unsourceable", which is exactly what SUITE_SKIPPED says.

UNIPROT_IDMAPPING = CurieDatasetConfig(
    key="uniprot-idmapping",
    arm="protein",
    entity_type="protein",
    input_type="name",
    name_column="uniprotkb_ac",
    target_vocabs=("RefSeq", "ENSEMBL"),
    gold_curie_columns=(
        ("RefSeq", "gold_refseq"),
        ("ENSEMBL", "gold_ensembl"),
    ),
    source_label="UniProt idmapping_selected.tab",
    source_url=(
        "https://ftp.uniprot.org/pub/databases/uniprot/current_release/knowledgebase/"
        "idmapping/idmapping_selected.tab.gz"
    ),
    license="UniProt data are available under CC BY 4.0.",
    tax_filter="9606",  # human rows only (column 13, NCBI-taxon)
)

NCBI_GENE2ENSEMBL = CurieDatasetConfig(
    key="ncbi-gene2ensembl",
    arm="gene",
    entity_type="gene",
    input_type="name",
    name_column="gene_id",
    target_vocabs=("ENSEMBL",),
    gold_curie_columns=(("ENSEMBL", "gold_ensembl"),),
    source_label="NCBI gene2ensembl",
    source_url="https://ftp.ncbi.nlm.nih.gov/gene/DATA/gene2ensembl.gz",
    license="NCBI Gene data are in the public domain.",
    tax_filter="9606",  # human rows only (tax_id column)
)


@dataclass(frozen=True)
class ProvidedIdDatasetConfig:
    """A provided-ID (identifier-input) registry entry.

    Used inside the MetaBench arm, whose ID->ID subgroups hand the SOURCE identifier to
    BioMapper as a provided id with ``annotation_mode='none'`` — no name resolution, pure
    provided-ID equivalence expansion. The measurement is whether BioMapper's equivalence set
    for the source reaches the held-out TARGET cross-reference.

    ANTI-TRIVIAL-100% INVARIANT, enforced in ``__post_init__`` and fail-loud: the scored TARGET
    must never be a provided column, and the source namespace must be disjoint from every
    target namespace. A target-in-provided config, or a same-namespace round-trip, raises at
    construction rather than silently scoring a trivial 100%.
    """

    key: str
    arm: str
    entity_type: str
    source_id_column: str  # the ONLY provided_id column
    source_namespace: str
    name_column: str  # inert placeholder query column (unused under annotation_mode='none')
    gold_target_columns: tuple[tuple[str, str], ...]  # HELD OUT — scorer-only
    target_vocabs: tuple[str, ...]
    source_label: str
    source_url: str
    license: str
    input_type: str = "provided_id"
    annotation_mode: str = "none"
    backbone_source_key: str | None = None
    backbone_source_column: str | None = None
    # Set True ONLY for a direction with a DOCUMENTED source gap (the provided source id is not a
    # queryable KG node, e.g. MetaBench kegg2hmdb). Then a zero provided-path mapping is a genuine
    # 0/n result rather than a broken run. Never set this to paper over an actually-broken run.
    known_source_gap: bool = False

    def __post_init__(self) -> None:
        provided = {self.source_id_column}
        gold_cols = {col for _, col in self.gold_target_columns}
        overlap = provided & gold_cols
        if overlap:
            raise ValueError(
                f"{self.key}: anti-trivial-100% violation — held-out TARGET column(s) "
                f"{sorted(overlap)} are also in provided_id_columns ({sorted(provided)}). The gold "
                f"target must NEVER be provided; only the source is."
            )
        gold_ns = {ns.upper() for ns, _ in self.gold_target_columns}
        if self.source_namespace.upper() in gold_ns:
            raise ValueError(
                f"{self.key}: anti-trivial-100% violation — source namespace "
                f"{self.source_namespace!r} is also a TARGET namespace ({sorted(gold_ns)}). A "
                f"same-namespace round-trip lets the provided source id self-match the gold."
            )


# Fail-loud sentinels. An accession or SI file that was never resolved must refuse to score
# rather than silently producing an empty arm.
NEEDS_FETCHING_SENTINEL = "MTBLS-NEEDS-FETCHING-"
NEEDS_FETCHING_SENTINEL_METLINKR = "METLINKR-NEEDS-FETCHING-"


# ==================================================================================================
# Registries and the suite membership lists
# ==================================================================================================

REGISTRY: dict[str, DatasetConfig] = {
    HAJJAR.key: HAJJAR,
    NECS.key: NECS,
    REFMET.key: REFMET,
    SRM1950.key: SRM1950,
    LMSD.key: LMSD,
    SWISSLIPIDS.key: SWISSLIPIDS,
}

CURIE_REGISTRY: dict[str, CurieDatasetConfig] = {HGNC.key: HGNC}
NLMGENE_REGISTRY: dict[str, CurieDatasetConfig] = {NLMGENE.key: NLMGENE}
METABENCH_REGISTRY: dict[str, MetaBenchDatasetConfig] = {METABENCH.key: METABENCH}
METLINKR_REGISTRY: dict[str, MetLinkRDatasetConfig] = {METLINKR.key: METLINKR}


# The 11 arms the suite runs unattended. Hajjar joins the 10 that were already self-sourcing,
# now that its supplement URL and SHA are pinned above.
SUITE_DATASETS: list[str] = [
    "hajjar",
    "metabench",
    "necs",
    "hgnc",
    "metaboliteannotator",
    "metlinkr",
    "nlmgene",
    "refmet",
    "srm1950",
    "lmsd",
    "swisslipids",
]

# Everything the CLI can name that the suite does NOT run, with the reason. Written into the
# manifest as ``status="skipped"``, because a deliberate exclusion and a dataset that fell out
# of the registry by accident look identical if skips are simply omitted.
SUITE_SKIPPED: dict[str, str] = {
    "provided-id": (
        "a --dataset family over multi-hundred-MB bulk backbones; needs a pinned artifact, not a "
        "URL. Stays in the engine repo."
    ),
    "pham": (
        "source is a MetaNetX FTP path requiring hand reconstruction into a table, not a "
        "fetchable file."
    ),
}
