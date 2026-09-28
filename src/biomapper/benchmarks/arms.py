"""The 11 suite arms: acquire -> adapt -> run -> score -> persist.

One function per arm, each returning a record the suite manifest aggregates. Ported from the
engine's ``run.py`` orchestrators with three deliberate substitutions:

* **The mapper is the API client.** Everything downstream of it is unchanged.
* **The Phase-0 cost/smoke gate is gone.** It is an engine CI merge gate and stays in
  ``biomapper2`` per the migration scope; it also guarded an in-process run against burning
  external-API budget, which is not the risk profile of a client pointing at a deployment.
* **The structure oracle reads the response.** ``StructureResolver`` is not imported; see
  :mod:`biomapper.benchmarks.oracle` for the equivalence and the one gap (node names).

Every arm persists by default into its own directory. Nothing is behind a flag: the expensive
part is live API traffic, and a forgotten flag that discards hours of it is not an acceptable
failure mode.

Failure discipline, which the suite relies on:

* :class:`~biomapper.benchmarks.sources.SourceUnavailable` propagates and is recorded as
  ``status="skipped"`` with its reason. Never an empty success.
* Anything else propagates and is recorded as ``status="failed"`` with its error. Never dropped.
* An arm that produces no scorable row raises rather than reporting 0%.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd

from biomapper.benchmarks import sources
from biomapper.benchmarks.api_mapper import ApiMapper
from biomapper.benchmarks.checkpoint import EntityCache
from biomapper.benchmarks.config import (
    HAJJAR,
    HGNC,
    LMSD,
    METABENCH,
    METLINKR,
    NAME_HIT_REGISTRY,
    NECS,
    NLMGENE,
    REFMET,
    SRM1950,
    SWISSLIPIDS,
    DatasetConfig,
)
from biomapper.benchmarks.oracle import ApiStructureOracle, NodeNameResolver
from biomapper.benchmarks.provenance import UNKNOWN, RunProvenance
from biomapper.benchmarks.runner import VocabRun, run_all, run_provided_id, run_vocab
from biomapper.benchmarks.scorers.curie_scorer import score_curie, score_curie_per_target_run
from biomapper.benchmarks.scorers.structure_oracle_scorer import (
    neutralize_first_block,
    score_structure_oracle,
)
from biomapper.benchmarks.structure import NameStructureResolver

logger = logging.getLogger(__name__)


class IncompleteUnionError(RuntimeError):
    """A metric defined as the union across every target vocabulary lost one of its passes.

    The MetaboliteAnnotator name-hit rate and the metLinkR link rate are both defined as "resolved
    in ANY target vocabulary". If the HMDB pass fails and the rest are scored anyway, an HMDB-only
    hit silently becomes a miss and the arm reports a DEFLATED number as a successful result. A
    partial union is not a conservative estimate, it is a wrong one, so the arm is refused.
    """


class UnscorableRunError(RuntimeError):
    """An arm completed but produced nothing scorable.

    Raised rather than returning 0%: an accuracy of zero and an accuracy of "we measured nothing"
    are different claims, and only one of them is a result.
    """


# --------------------------------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------------------------------


def _write(path: Path, payload: Any) -> None:  # noqa: ANN401 - an arbitrary JSON-serializable result payload
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str))


def _build_oracle(
    mapper: ApiMapper,
    mapped_df: pd.DataFrame,
    *,
    kestrel_url: str,
    name_fallback: bool = True,
) -> ApiStructureOracle:
    """Build the structure oracle from a mapped frame.

    ``name_fallback`` mirrors the engine's layered path (graph, then Metabolomics Workbench, then
    PubChem, keyed on the chosen node's name). Disabling it makes every structure-absent node
    report as unverifiable, which is a defensible and stricter reading; it is on by default
    because that is what the engine measured.
    """
    return ApiStructureOracle.from_rows(
        mapper.rows(mapped_df),
        name_fallback=NameStructureResolver() if name_fallback else None,
        node_names=NodeNameResolver(kestrel_url) if name_fallback else None,
    )


def require_complete_union(
    runs: dict[str, VocabRun], *, key: str, target_vocabs: tuple[str, ...]
) -> list[VocabRun]:
    """Return every vocab pass, refusing the arm if any of them failed.

    For a metric defined as "resolved in ANY target vocabulary", a missing pass can only turn real
    hits into misses. Scoring the remainder yields a deflated number that still reports as a
    success, so the arm is refused instead. Used by the two union-shaped arms
    (MetaboliteAnnotator's name-hit rate, metLinkR's link rate).
    """
    failed = {vocab: run.error for vocab, run in runs.items() if not (run.ok and run.output_tsv)}
    if failed:
        raise IncompleteUnionError(
            f"{key}: target vocab pass(es) {sorted(failed)} failed, so the union across "
            f"{list(target_vocabs)} is incomplete and the metric would be deflated. Withholding "
            f"it rather than reporting a number computed from a subset. Errors: {failed}"
        )
    return list(runs.values())


def _primary_run(runs: dict[str, Any], config: Any) -> tuple[str, pd.DataFrame]:  # noqa: ANN401 - any of the five heterogeneous dataset-config types
    """The primary vocab's mapped frame, failing loud when it did not produce one.

    Fail-closed on purpose: a failed primary run must halt here with the recorded error, not
    surface downstream as an opaque KeyError that masks the real cause.
    """
    primary = config.target_vocabs[0]
    run = runs.get(primary)
    if run is None or not run.ok or not run.output_tsv:
        error = run.error if run is not None else "no run recorded"
        raise RuntimeError(
            f"{config.key}: primary vocab {primary!r} produced no scored result (mapping failed: "
            f"{error!r}) — refusing to report a partial arm as a result."
        )
    return primary, pd.read_csv(run.output_tsv, sep="\t")


def _assert_scorable(result: dict[str, Any], key: str) -> dict[str, Any]:
    """Refuse a result whose accuracy denominator is empty.

    ``top1_accuracy is None`` means no row carried a gold structure, so there was nothing to be
    right or wrong about. Coercing that to 0.0 would fabricate a measurement from an absent one.
    """
    core = result.get("comparable_core") or {}
    if core.get("scored_denominator", 0) == 0:
        raise UnscorableRunError(
            f"{key}: zero scorable rows (no row carried a held-out gold structure), so there is no "
            f"accuracy to report. This is a broken input, not a score of zero."
        )
    return result


def _structure_arm(
    *,
    config: DatasetConfig,
    bundle: Any,  # noqa: ANN401 - each adapter returns its own bundle dataclass
    dataset_sha: str,
    source_provenance: dict[str, Any],
    mapper: ApiMapper,
    out_dir: Path,
    provenance: RunProvenance,
    kestrel_url: str,
    name_source_column: str | None = None,
) -> dict[str, Any]:
    """The shared body of every structure-oracle arm (Hajjar, NECS, RefMet, SRM1950, LMSD).

    Factored out because the five arms differed only in their adapter and two optional
    parameters; duplicating it five times is how the ``keys[0]`` scorer bug survived in one copy
    and not another.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    _write(out_dir / "dataset_card.json", bundle.card)

    runs = run_all(
        mapper,
        bundle.input_df,
        config,
        out_dir,
        dataset_sha=dataset_sha,
        provenance=provenance,
        source_provenance=source_provenance,
    )
    primary, mapped_df = _primary_run(runs, config)
    oracle = _build_oracle(mapper, mapped_df, kestrel_url=kestrel_url)

    # The charge-normalized variant needs a gold SMILES column to neutralize. Where the source
    # ships none (Hajjar, RefMet) the normalizer is withheld so the variant reports as
    # unavailable, rather than being computed against an empty column and read as a real number.
    gold_normalizer = neutralize_first_block if config.gold_smiles_column else None

    result = score_structure_oracle(
        mapped_df,
        config,
        oracle,
        vocab=primary,
        gold_smiles_normalizer=gold_normalizer,
        name_source_column=name_source_column,
    )
    _assert_scorable(result, config.key)

    result["oracle_integrity"] = oracle.integrity()
    result["oracle_fallback"] = oracle.fallback_report()
    result["role"] = config.role
    if gold_normalizer is None:
        result["comparable_core_charge_normalized_unavailable_reason"] = (
            f"{config.key} ships no gold SMILES column, so the gold side cannot be neutralized. "
            f"Reported as unavailable rather than computed against an absent column."
        )

    _write(out_dir / f"{primary}_results.json", result)
    per_vocab = {v: {"ok": r.ok, "error": r.error, "stats": r.stats} for v, r in runs.items()}
    _write(out_dir / "vocab_runs.json", per_vocab)
    return {
        "out_dir": str(out_dir),
        "dataset": config.key,
        "vocab": primary,
        "role": config.role,
        "results": result,
        "card": bundle.card,
        "vocab_runs": per_vocab,
    }


# --------------------------------------------------------------------------------------------------
# Arm 1 — Hajjar-100 (the acceptance-test arm)
# --------------------------------------------------------------------------------------------------


def run_hajjar(
    *, mapper: ApiMapper, out_dir: Path, provenance: RunProvenance, kestrel_url: str
) -> dict[str, Any]:
    """Hajjar-100, name input, independent InChIKey structure oracle.

    This arm is the migration's acceptance test: its strict and KG-equivalence-set numbers are
    what a reader compares against the published reference. Both are reported; the strict figure
    alone must not be presented as a chemistry failure, because the gap between them is a
    ``keys[0]`` representation artifact rather than a resolution error.
    """
    from biomapper.benchmarks.adapters.hajjar import load_hajjar

    raw, source_prov = sources.acquire(
        "hajjar", HAJJAR.source_url, expected_sha256=HAJJAR.expected_source_sha256
    )
    bundle = load_hajjar(raw, HAJJAR, source_provenance=source_prov)
    return _structure_arm(
        config=HAJJAR,
        bundle=bundle,
        dataset_sha=bundle.card["source_sha256"],
        source_provenance=source_prov,
        mapper=mapper,
        out_dir=out_dir,
        provenance=provenance,
        kestrel_url=kestrel_url,
    )


# --------------------------------------------------------------------------------------------------
# Arm 2 — NECS / Metabolon
# --------------------------------------------------------------------------------------------------


def run_necs(
    *, mapper: ApiMapper, out_dir: Path, provenance: RunProvenance, kestrel_url: str
) -> dict[str, Any]:
    """NECS Metabolon metabolite slice, strict plus charge-normalized.

    The gold carries roughly 5% InChIKey errors and an InChIKey first block is not tautomer- or
    charge-invariant, so a disagreement here is not by itself evidence the gold is wrong. Any
    corrected rate derived from this arm must be re-adjudicated against an outside source before
    it is quoted.
    """
    from biomapper.benchmarks.adapters.necs_metabolon import load_necs

    raw, source_prov = sources.acquire("necs", NECS.source_url)
    bundle = load_necs(raw, NECS)
    result = _structure_arm(
        config=NECS,
        bundle=bundle,
        dataset_sha=bundle.card["source_sha256"],
        source_provenance=source_prov,
        mapper=mapper,
        out_dir=out_dir,
        provenance=provenance,
        kestrel_url=kestrel_url,
    )
    result["results"]["gold_caveat"] = (
        "this gold carries roughly 5% InChIKey errors; re-adjudicate any refused or contradicted "
        "small-molecule case against an outside source before quoting a corrected rate."
    )
    return result


# --------------------------------------------------------------------------------------------------
# Arm 3 — HGNC (gene, CURIE equality)
# --------------------------------------------------------------------------------------------------


def _score_hgnc_per_target_run(runs: dict[str, VocabRun]) -> dict[str, Any]:
    """Score each HGNC namespace from its own target-vocab run, recording which file each used."""
    require_complete_union(runs, key=HGNC.key, target_vocabs=HGNC.target_vocabs)
    frames: dict[str, pd.DataFrame] = {}
    run_sources: dict[str, dict[str, Any]] = {}
    for namespace, _column in HGNC.gold_curie_columns:
        run = runs.get(namespace)
        tsv = run.output_tsv if run is not None else None
        if tsv is None:
            # A missing pass is refused downstream too; say which one here.
            raise IncompleteUnionError(f"{HGNC.key}: no mapped output for target {namespace!r}.")
        frames[namespace] = pd.read_csv(tsv, sep="\t")
        run_sources[namespace] = {
            "source_tsv": str(tsv),
            "source_tsv_sha256": hashlib.sha256(Path(tsv).read_bytes()).hexdigest(),
        }
    return score_curie_per_target_run(frames, HGNC, sources=run_sources)


def run_hgnc(
    *, mapper: ApiMapper, out_dir: Path, provenance: RunProvenance, kestrel_url: str
) -> dict[str, Any]:
    """HGNC complete set: gene symbol -> authoritative cross-refs, scored by CURIE equality.

    Reports accuracy PER TARGET NAMESPACE. The any-namespace roll-up is emitted flagged
    non-quotable: the namespaces perform very differently and the roll-up has been quoted as
    though it described all three.
    """
    from biomapper.benchmarks.adapters.backbones import (
        load_backbone,
        persist_subsample,
        resolve_source_version,
    )

    del kestrel_url  # no structure oracle for genes; the parameter keeps the arm signature uniform
    out_dir.mkdir(parents=True, exist_ok=True)
    source_version = resolve_source_version(HGNC.source_url)
    bundle = load_backbone(HGNC.source_url, HGNC, source_version=source_version)
    persist_subsample(bundle, out_dir)
    _write(out_dir / "dataset_card.json", bundle.card)

    runs = run_all(
        mapper,
        bundle.input_df,
        HGNC,
        out_dir,
        dataset_sha=bundle.card["subsample_sha256"],
        provenance=provenance,
        source_provenance={"source_url": HGNC.source_url, "source_version": source_version},
    )
    primary, mapped_df = _primary_run(runs, HGNC)
    # The any-namespace roll-up, coverage and per_row stay on the primary run (non-quotable,
    # kept for continuity). The per-namespace figures are then REPLACED with ones scored from
    # each namespace's own run, because node selection is steered by the target vocab.
    result = score_curie(mapped_df, HGNC, vocab=primary)
    result["rollup_source_vocab_run"] = primary
    result.update(_score_hgnc_per_target_run(runs))
    if not any(
        (entry.get("scored_denominator") or 0) > 0
        for entry in result["per_namespace_accuracy"].values()
    ):
        raise UnscorableRunError(
            f"{HGNC.key}: no target namespace carried a scorable gold cross-ref; nothing to report."
        )
    _write(out_dir / f"{primary}_results.json", result)
    return {
        "out_dir": str(out_dir),
        "dataset": HGNC.key,
        "vocab": primary,
        # ncbigene is an ingested graph source; see provenance.circularity_notes.
        "role": "coverage",
        "results": result,
        "card": bundle.card,
    }


# --------------------------------------------------------------------------------------------------
# Arm 4 — MetaboliteAnnotator name-hit rate (both ion modes)
# --------------------------------------------------------------------------------------------------


def run_metaboliteannotator(
    *, mapper: ApiMapper, out_dir: Path, provenance: RunProvenance, kestrel_url: str
) -> dict[str, Any]:
    """MetaboliteAnnotator name-hit head-to-head, one number per ion mode.

    The headline is a name-hit RATE, which is coverage by construction: it measures whether a
    target-vocab identifier was produced, not whether it was right. It must never be presented as
    accuracy, however favourably it compares to the published 93.2% / 93.5%.

    Every target vocab runs and the passes are unioned — a name is a hit if it resolves in ANY of
    CHEBI/HMDB/PubChem/KEGG — so scoring the CHEBI pass alone would under-count.

    Resumable. This is the suite's longest arm (4,314 names per ion mode x 4 vocab passes x 2
    modes, measured at 3.31 s/entity, about 31.7 h), so each ion mode keeps an
    :class:`~biomapper.benchmarks.checkpoint.EntityCache` in its directory. Every batch is
    committed to disk before the next request, and a re-run into the SAME suite dir
    (``--out <that dir>``) sends only the entities not yet answered. A completed vocab pass
    re-assembles entirely from cache with zero requests. The cache is pinned to the build: a
    resume against a different KG commit is refused, not mixed in.
    """
    from biomapper.benchmarks.adapters.metaboliteannotator import load_metaboliteannotator
    from biomapper.benchmarks.scorers.name_hit_scorer import merge_vocab_runs, score_name_hit

    out_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    arm_status: dict[str, str] = {}
    for key, config in NAME_HIT_REGISTRY.items():
        mode_dir = out_dir / config.mode
        try:
            mapper.cache = _resume_cache(mode_dir, provenance, entity_type=config.entity_type)
            if mapper.cache is not None and mapper.cache.loaded_from_disk:
                logger.info(
                    "MetaboliteAnnotator %s: resuming with %d cached entities from %s",
                    config.mode,
                    mapper.cache.loaded_from_disk,
                    mapper.cache.path,
                )
            bundle = load_metaboliteannotator(config.accessions, config)
            _write(mode_dir / "dataset_card.json", bundle.card)
            runs = run_all(
                mapper,
                bundle.input_df,
                config,
                mode_dir,
                dataset_sha=bundle.card["source_sha256"],
                provenance=provenance,
            )
            # Every configured pass is required: the name-hit rate is the union across them.
            ok = require_complete_union(runs, key=key, target_vocabs=config.target_vocabs)
            merged = merge_vocab_runs(
                [pd.read_csv(str(r.output_tsv), sep="\t") for r in ok], config
            )
            oracle = _build_oracle(mapper, merged, kestrel_url=kestrel_url)
            result = score_name_hit(merged, config, oracle=oracle)
            result["metric_is_coverage_by_construction"] = True
            _write(mode_dir / "results.json", result)
            entries.append({"key": key, "mode": config.mode, "result": result})
            arm_status[key] = "ok"
        except Exception as exc:  # noqa: BLE001 — one ion mode failing must not hide the other
            logger.warning("MetaboliteAnnotator mode %s failed: %s", config.mode, exc)
            arm_status[key] = f"failed: {type(exc).__name__}: {exc}"
        finally:
            # The mapper is per arm, but one mode's cache must never answer the other's names.
            mapper.cache = None
    if not entries:
        raise RuntimeError(f"both MetaboliteAnnotator ion modes failed: {arm_status}")
    _write(out_dir / "results.json", {"entries": entries, "arm_status": arm_status})
    return {
        "out_dir": str(out_dir),
        "dataset": "metaboliteannotator",
        "role": "coverage",
        "results": {"entries": entries},
        "arm_status": arm_status,
    }


def _resume_cache(
    directory: Path, provenance: RunProvenance, *, entity_type: str
) -> EntityCache | None:
    """An entity cache pinned to this run's build, or ``None`` when the build is unknown.

    Unpinned provenance records ``unknown`` for the KG commit, and two unknowns compare equal, so a
    cache keyed on them could resume a run across two different builds without noticing. The KG
    commit is checked on its own too: ``pinned`` only requires the service and KG versions, and a
    build that reports a version but no commit is exactly that case. Running
    without a cache is slower; mixing builds is wrong. Take the slower path.
    """
    if not provenance.pinned or provenance.kg_build.git_commit in (UNKNOWN, "", None):
        logger.warning(
            "KG build identity is incomplete (pinned=%s, kg_git_commit=%s, %s); running %s WITHOUT "
            "a resume cache, because a cache cannot verify which build its answers came from.",
            provenance.pinned,
            provenance.kg_build.git_commit,
            provenance.health_error,
            directory,
        )
        return None
    kg = provenance.kg_build
    return EntityCache(
        directory,
        pin={
            "api_endpoint": provenance.api_endpoint,
            "kestrel_version": provenance.kestrel_version,
            "kg_version": kg.kg_version,
            "kg_git_commit": kg.git_commit,
            "entity_type": entity_type,
            # run_all sends every name-hit pass with annotation_mode="all".
            "annotation_mode": "all",
        },
    )


# --------------------------------------------------------------------------------------------------
# Arm 5 — metLinkR (same-task cross-linking)
# --------------------------------------------------------------------------------------------------


def run_metlinkr(
    *, mapper: ApiMapper, out_dir: Path, provenance: RunProvenance, kestrel_url: str
) -> dict[str, Any]:
    """metLinkR COMETS curator cross-linking, against the published 85.3% agreement rate."""
    from biomapper.benchmarks.adapters.metlinkr import load_metlinkr
    from biomapper.benchmarks.scorers.metlinkr_scorer import merge_vocab_runs, score_metlinkr

    out_dir.mkdir(parents=True, exist_ok=True)
    bundle = load_metlinkr("fetch", METLINKR)
    _write(out_dir / "dataset_card.json", bundle.card)

    runs = run_all(
        mapper,
        bundle.input_df,
        METLINKR,
        out_dir,
        dataset_sha=bundle.card["source_sha256"],
        provenance=provenance,
    )
    # A link is confirmed when two members share a canonical id in ANY target vocab, so a missing
    # pass can only turn real links into misses.
    ok = require_complete_union(runs, key=METLINKR.key, target_vocabs=METLINKR.target_vocabs)
    merged = merge_vocab_runs([pd.read_csv(str(r.output_tsv), sep="\t") for r in ok], METLINKR)
    oracle = _build_oracle(mapper, merged, kestrel_url=kestrel_url)
    result = score_metlinkr(merged, METLINKR, oracle=oracle)
    _write(out_dir / "results.json", result)
    return {
        "out_dir": str(out_dir),
        "dataset": METLINKR.key,
        "role": "accuracy_candidate",
        "results": result,
        "card": bundle.card,
    }


# --------------------------------------------------------------------------------------------------
# Arm 6 — NLM-Gene (ambiguity-partitioned)
# --------------------------------------------------------------------------------------------------


def run_nlmgene(
    *, mapper: ApiMapper, out_dir: Path, provenance: RunProvenance, kestrel_url: str
) -> dict[str, Any]:
    """NLM-Gene, scored ambiguity-partitioned.

    Unambiguous surface forms give ACCURACY; ambiguous forms give a FLAG-RATE, because a bare
    context-stripped form denoting two or more genes has no single correct answer and the right
    behaviour is to abstain. The two are never blended: doing so would let the hard population
    dilute a number that is supposed to measure the easy one.

    Accuracy is reported per target namespace, as for every gene arm.
    """
    from biomapper.benchmarks.adapters import nlmgene as nlmgene_adapter
    from biomapper.benchmarks.scorers.nlmgene_scorer import score_nlmgene_ambiguity

    del kestrel_url  # no structure oracle for genes
    out_dir.mkdir(parents=True, exist_ok=True)
    bundle = nlmgene_adapter.load_nlmgene(nlmgene_adapter.fetch_corpus(NLMGENE), NLMGENE)
    _write(out_dir / "dataset_card.json", bundle.card)
    nlmgene_adapter.persist_input_df(bundle, out_dir)

    gold_column = nlmgene_adapter.GOLD_COLUMN
    partition_column = nlmgene_adapter.PARTITION_COLUMN
    # Feed ONLY the query and its gold to the mapper; the ambiguity label is held out and
    # re-attached after, so the split never depends on the mapper passing a column through.
    mapper_input = bundle.input_df[[NLMGENE.name_column, gold_column]]
    runs = run_all(
        mapper,
        mapper_input,
        NLMGENE,
        out_dir,
        dataset_sha=bundle.card["subsample_sha256"],
        provenance=provenance,
    )
    primary, mapped_df = _primary_run(runs, NLMGENE)
    labels = bundle.input_df[[NLMGENE.name_column, partition_column]]
    mapped_df = mapped_df.merge(labels, on=NLMGENE.name_column, how="left")

    unambiguous = mapped_df[mapped_df[partition_column] == nlmgene_adapter.UNAMBIGUOUS]
    ambiguous = mapped_df[mapped_df[partition_column] == nlmgene_adapter.AMBIGUOUS]
    accuracy = score_curie(unambiguous, NLMGENE, vocab=primary)
    flagging = score_nlmgene_ambiguity(ambiguous, NLMGENE, vocab=primary)
    _write(out_dir / "unambiguous_accuracy.json", accuracy)
    _write(out_dir / "ambiguous_flagrate.json", flagging)
    return {
        "out_dir": str(out_dir),
        "dataset": NLMGENE.key,
        "vocab": primary,
        # The gold MAPPING is human-curated, so independent by construction even though the gold
        # NAMESPACE (NCBIGene) is an ingested graph source.
        "role": "accuracy_candidate",
        "results": {"unambiguous_accuracy": accuracy, "ambiguous_flagrate": flagging},
        "card": bundle.card,
    }


# --------------------------------------------------------------------------------------------------
# Arm 7 — RefMet
# --------------------------------------------------------------------------------------------------


def run_refmet(
    *, mapper: ApiMapper, out_dir: Path, provenance: RunProvenance, kestrel_url: str
) -> dict[str, Any]:
    """RefMet name -> structure, streamed and reservoir-subsampled.

    ``refmet`` is both an ingested graph source and the source-weighting target annotator, so
    this arm measures COVERAGE. The exact scored subsample is persisted beside the card: the
    download URL is a mutable current release, so URL plus seed plus n cannot reconstruct it.
    """
    from biomapper.benchmarks.adapters import refmet as refmet_adapter

    out_dir.mkdir(parents=True, exist_ok=True)
    # RefMet's download endpoint exposes no version, so the persisted subsample's SHA is the pin.
    source_version = None
    bundle = refmet_adapter.load_refmet(REFMET.source_url, REFMET, source_version=source_version)
    refmet_adapter.persist_subsample(bundle, out_dir)
    return _structure_arm(
        config=REFMET,
        bundle=bundle,
        dataset_sha=bundle.card["subsample_sha256"],
        source_provenance={"source_url": REFMET.source_url, "source_version": source_version},
        mapper=mapper,
        out_dir=out_dir,
        provenance=provenance,
        kestrel_url=kestrel_url,
    )


# --------------------------------------------------------------------------------------------------
# Arm 8 — NIST SRM 1950 (pinned source)
# --------------------------------------------------------------------------------------------------


def run_srm1950(
    *, mapper: ApiMapper, out_dir: Path, provenance: RunProvenance, kestrel_url: str
) -> dict[str, Any]:
    """NIST SRM 1950 certified plasma metabolites.

    Read from a SHA-verified LOCAL PIN, not the live URL: upstream's TLS certificate expired
    2026-09-15. Certificate verification is never disabled anywhere in this path — the pinned SHA
    is what makes the copy trustworthy, and disabling verification would silently accept any
    future substitution.

    The delivery's InChIKey column is empty, so the oracle structure is derived from the
    certified SMILES with RDKit, which shares no infrastructure with BioMapper's resolver.
    """
    from biomapper.benchmarks.adapters.srm1950 import load_srm1950

    raw, source_prov = sources.acquire("srm1950", SRM1950.source_url)
    bundle = load_srm1950(raw, SRM1950)
    return _structure_arm(
        config=SRM1950,
        bundle=bundle,
        dataset_sha=bundle.card["source_sha256"],
        source_provenance=source_prov,
        mapper=mapper,
        out_dir=out_dir,
        provenance=provenance,
        kestrel_url=kestrel_url,
    )


# --------------------------------------------------------------------------------------------------
# Arm 9 — LMSD (capability regression, not accuracy)
# --------------------------------------------------------------------------------------------------


def run_lmsd(
    *, mapper: ApiMapper, out_dir: Path, provenance: RunProvenance, kestrel_url: str
) -> dict[str, Any]:
    """LMSD lipid names. A CAPABILITY REGRESSION arm, never an accuracy headline.

    ``lipidmaps`` is an ingested graph source, and once Goslin plus the LIPID MAPS API are in the
    resolution path the gold InChIKey and BioMapper's answer descend from the same place. The
    July independence audit anticipated exactly this. So this arm certifies that the lipid
    grammar capability is wired and gates a shorthand resolvability FLOOR.

    The floor is enforced BEFORE the result is filed: a declared floor that is never checked is
    dead config.
    """
    from biomapper.benchmarks.adapters import lmsd as lmsd_adapter
    from biomapper.benchmarks.scorers.regression import (
        assert_capability_floor,
        capability_resolvability,
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    # Same as RefMet: the LMSD bulk download exposes no version, so the subsample SHA is the pin.
    source_version = None
    bundle = lmsd_adapter.load_lmsd(LMSD.source_url, LMSD, source_version=source_version)
    lmsd_adapter.persist_subsample(bundle, out_dir)

    record = _structure_arm(
        config=LMSD,
        bundle=bundle,
        dataset_sha=bundle.card["subsample_sha256"],
        source_provenance={"source_url": LMSD.source_url, "source_version": source_version},
        mapper=mapper,
        out_dir=out_dir,
        provenance=provenance,
        kestrel_url=kestrel_url,
        name_source_column=lmsd_adapter.QUERY_SOURCE_COL,
    )
    result = record["results"]
    measured = capability_resolvability(result, regime="shorthand")
    result["capability_gate"] = {
        "role": LMSD.role,
        "regime": "shorthand",
        "regression_floor": LMSD.regression_floor,
        "measured_resolvability": measured,
        "note": (
            "LMSD is reported as coverage, not accuracy: its gold is LIPID MAPS, which is an "
            "ingested graph source, so a high resolvability certifies only that the lipid grammar "
            "capability is present."
        ),
    }
    _write(Path(record["out_dir"]) / f"{record['vocab']}_results.json", result)
    if LMSD.regression_floor is not None:
        assert_capability_floor(result, LMSD.regression_floor, regime="shorthand")
    record["role"] = "coverage"
    return record


# --------------------------------------------------------------------------------------------------
# Arm 10 — SwissLipids (currently unsourceable)
# --------------------------------------------------------------------------------------------------


def run_swisslipids(
    *, mapper: ApiMapper, out_dir: Path, provenance: RunProvenance, kestrel_url: str
) -> dict[str, Any]:
    """SwissLipids. Registered so it reports as SKIPPED WITH A REASON.

    This would be the one reportable lipid ACCURACY arm — SwissLipids is not a KRAKEN ingest
    source, so its gold is legally independent. It cannot run: the source returns HTTP 200 with a
    zero-byte body on a full GET and no usable local copy exists.

    ``sources.acquire`` raises :class:`SourceUnavailable` before any network call, which the suite
    records as a skip with this reason. The alternative — letting a zero-byte body reach a
    streaming adapter — is exactly the failure that produced an empty-but-successful arm on
    2026-08-05.
    """
    from biomapper.benchmarks.adapters import swisslipids as swisslipids_adapter

    del mapper, provenance, kestrel_url
    out_dir.mkdir(parents=True, exist_ok=True)
    # Raises SourceUnavailable. Referenced so the adapter is importable and the arm is not
    # silently unwired: when a distribution reappears, only ``sources`` needs the edit.
    _ = swisslipids_adapter
    raw, _prov = sources.acquire("swisslipids", SWISSLIPIDS.source_url)
    raise AssertionError(  # pragma: no cover - unreachable while the source is unavailable
        f"SwissLipids unexpectedly returned {len(raw)} bytes. Remove it from "
        f"sources.UNAVAILABLE_SOURCES and wire the arm deliberately."
    )


# --------------------------------------------------------------------------------------------------
# Arm 11 — MetaBench Grounding (mixed regime, LLM head-to-head)
# --------------------------------------------------------------------------------------------------


def run_metabench(
    *, mapper: ApiMapper, out_dir: Path, provenance: RunProvenance, kestrel_url: str
) -> dict[str, Any]:
    """MetaBench Grounding: 1,000 cross-database pairs, one accuracy.

    Mixed regime — ID->ID subgroups run in provided-ID mode, name->ID subgroups in name-input
    mode — but scoring is identical in both, so the subgroup outputs are concatenated and scored
    once. Partly-circular: its equivalence sets are built from the same xrefs the graph carries.
    """
    from biomapper.benchmarks.adapters import metabench as metabench_adapter
    from biomapper.benchmarks.scorers.metabench_scorer import score_metabench

    del kestrel_url  # the target is a database identifier, not a structure; no oracle needed
    out_dir.mkdir(parents=True, exist_ok=True)
    raw, source_prov = sources.acquire(
        "metabench", METABENCH.source_url, expected_sha256=METABENCH.expected_source_sha256
    )
    bundle = metabench_adapter.load_metabench(raw, METABENCH)
    _write(out_dir / "dataset_card.json", bundle.card)
    dataset_sha = bundle.card["source_sha256"]

    frames: list[pd.DataFrame] = []
    subgroup_status: dict[str, str] = {}
    for subgroup in metabench_adapter.build_subgroups(bundle.long_df, METABENCH):
        sub_dir = out_dir / subgroup.key
        try:
            if subgroup.pair_type == "id2id":
                provided_config = metabench_adapter.provided_config_for_subgroup(
                    subgroup, METABENCH
                )
                run = run_provided_id(
                    mapper,
                    subgroup.input_df,
                    provided_config,
                    sub_dir,
                    dataset_sha=dataset_sha,
                    provenance=provenance,
                    source_provenance=source_prov,
                )
                output = run.output_tsv
            else:
                vocab_run = run_vocab(
                    mapper,
                    subgroup.input_df,
                    METABENCH,
                    subgroup.vocab,
                    sub_dir,
                    dataset_sha=dataset_sha,
                    provenance=provenance,
                    source_provenance=source_prov,
                )
                output = vocab_run.output_tsv
            if not output:
                raise RuntimeError("subgroup produced no output")
            frames.append(pd.read_csv(output, sep="\t"))
            subgroup_status[subgroup.key] = "ok"
        except Exception as exc:  # noqa: BLE001 — a failed subgroup must be named, not dropped
            logger.warning("MetaBench subgroup %s failed: %s", subgroup.key, exc)
            subgroup_status[subgroup.key] = f"failed: {type(exc).__name__}: {exc}"

    if not frames:
        raise RuntimeError(f"every MetaBench subgroup failed: {subgroup_status}")
    mapped_df = pd.concat(frames, ignore_index=True)
    result = score_metabench(mapped_df, METABENCH)
    if (result["comparable_core"].get("scored_denominator") or 0) == 0:
        raise UnscorableRunError(
            f"{METABENCH.key}: no row carried a held-out gold target; nothing to score."
        )
    # A partial run's accuracy is computed over the subgroups that succeeded, so the coverage of
    # the 1,000-pair set has to travel with the number or it reads as the full benchmark.
    result["subgroup_status"] = subgroup_status
    result["rows_scored_of_source"] = {"scored": len(mapped_df), "source_rows": len(bundle.long_df)}
    result["complete"] = all(v == "ok" for v in subgroup_status.values())
    if not result["complete"]:
        result["incomplete_reason"] = (
            "one or more subgroups failed, so this accuracy covers fewer than the declared "
            "1,000 grounding pairs. See subgroup_status and rows_scored_of_source; do not quote "
            "it as the full benchmark."
        )
    _write(out_dir / "results.json", result)
    return {
        "out_dir": str(out_dir),
        "dataset": METABENCH.key,
        "role": "partly_circular",
        "results": result,
        "card": bundle.card,
        "arm_status": subgroup_status,
    }


# --------------------------------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------------------------------

ARM_RUNNERS: dict[str, Any] = {
    "hajjar": run_hajjar,
    "metabench": run_metabench,
    "necs": run_necs,
    "hgnc": run_hgnc,
    "metaboliteannotator": run_metaboliteannotator,
    "metlinkr": run_metlinkr,
    "nlmgene": run_nlmgene,
    "refmet": run_refmet,
    "srm1950": run_srm1950,
    "lmsd": run_lmsd,
    "swisslipids": run_swisslipids,
}
