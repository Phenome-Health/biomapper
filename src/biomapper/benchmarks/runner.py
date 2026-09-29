"""Batch runner over :class:`ApiMapper`, one run per target vocab.

Ported from ``studies/external_benchmarks/runner.py``. The structure is unchanged — name query
only (``provided_id_columns=[]``, ``annotation_mode='all'``), each run into a timestamped dir
with a fully-pinned manifest, save-by-default — with two substitutions:

* the mapper is the API client rather than an in-process ``Mapper``;
* provenance comes from the deployment's Kestrel ``/health`` rather than a local git SHA, so a
  manifest names the backend that actually served the answers.

The anti-trivial-100% guard is carried over intact: with name-only input every mapping must come
through the annotate path, so zero *assigned* mappings means the gold leaked in as a provided id
and the run is refused.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from biomapper.benchmarks.api_mapper import (
    ApiMapper,
    EmptyDatasetError,
    NoProvidedMappingError,
    TrivialMappingError,
    assigned_stats_nonnull,
    mapped_provided_nonnull,
)
from biomapper.benchmarks.config import RunnableConfig
from biomapper.benchmarks.provenance import RunProvenance


def default_run_dir(config: RunnableConfig, base: Path) -> Path:
    """Timestamped, save-by-default output dir.

    Saving is never behind a flag: the expensive part of a run is the live API traffic, and a
    forgotten flag that discards it is not an acceptable failure mode. ``out_dir`` is an
    override, not the only way to persist.
    """
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    return base / f"{config.key}_{stamp}"


def build_manifest(
    *,
    vocab: str,
    config: RunnableConfig,
    dataset_sha: str,
    output_tsv: str,
    provenance: RunProvenance,
    stats: dict[str, Any] | None = None,
    source_provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The per-vocab manifest. Everything a reader needs to attribute the number.

    Deliberately records the full ``source_versions`` map rather than a summary: four of the
    graph's ingested sources are gold sources for arms in this suite, so the per-source versions
    are what let a reader decide whether an arm measured accuracy or coverage.
    """
    kg = provenance.kg_build
    return {
        "dataset": config.key,
        "vocab": vocab,
        "entity_type": config.entity_type,
        "input_type": config.input_type,
        "annotation_mode": "all",
        "provided_id_columns": [],
        "run_id": provenance.run_id,
        "biomapper_version": provenance.biomapper_version,
        "client_git_commit": provenance.client_git_commit,
        "client_git_dirty": provenance.client_git_dirty,
        "api_endpoint": provenance.api_endpoint,
        "kestrel_url": provenance.kestrel_url,
        "kestrel_version": provenance.kestrel_version,
        "kg_version": kg.kg_version,
        "kraken_package_version": kg.kraken_package_version,
        "biolink_version": kg.biolink_version,
        "kg_build_timestamp": kg.build_timestamp,
        "kg_git_commit": kg.git_commit,
        "kg_sources": list(kg.sources),
        "source_versions": dict(kg.source_versions),
        "provenance_pinned": provenance.pinned,
        "provenance_error": provenance.health_error,
        "dataset_source_sha256": dataset_sha,
        "dataset_source_provenance": source_provenance or {},
        "output_tsv": output_tsv,
        "stats": stats or {},
        "timestamp_utc": dt.datetime.now(dt.UTC).isoformat(),
    }


@dataclass
class VocabRun:
    vocab: str
    ok: bool
    output_tsv: str | None
    stats: dict[str, Any] | None
    manifest: dict[str, Any] | None
    error: str | None = None


def _assert_dataset_nonempty(input_df: pd.DataFrame, config: RunnableConfig) -> None:
    """Fail fast, and name the likely culprit, when an adapter produced no rows.

    Checked before any mapper call so the error costs nothing and arrives while the cause is
    still obvious. The message names the dataset and the pinned source, because an empty dataset
    almost always means the source stopped serving data rather than that the harness broke.
    """
    if len(input_df) > 0:
        return
    source = getattr(config, "source_url", "") or ""
    where = f" Pinned source: {source}" if source else ""
    raise EmptyDatasetError(
        f"{config.key}: the adapter produced 0 rows, so there is nothing to map. This is a broken "
        f"run, not a score of zero.{where} Check that the source still serves data — an HTTP 200 "
        f"with an empty body reads as success to a streaming adapter and yields exactly this."
    )


def run_vocab(
    mapper: ApiMapper,
    input_df: pd.DataFrame,
    config: RunnableConfig,
    vocab: str,
    out_dir: Path,
    *,
    dataset_sha: str,
    provenance: RunProvenance,
    enforce_assigned: bool = True,
    source_provenance: dict[str, Any] | None = None,
    annotation_mode: str = "all",
) -> VocabRun:
    """Run one vocab and write its manifest beside the outputs."""
    _assert_dataset_nonempty(input_df, config)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    output_tsv, stats = mapper.map_dataset_to_kg(
        dataset=input_df,
        entity_type=config.entity_type,
        name_column=config.name_column,
        provided_id_columns=[],
        vocab=vocab,
        annotation_mode=annotation_mode,
        output_dir=out_dir,
        output_prefix=f"{config.key}_{vocab}",
    )
    if enforce_assigned and not assigned_stats_nonnull(stats):
        raise TrivialMappingError(
            f"vocab {vocab}: assigned KG mappings are zero — the gold likely leaked in as a "
            f"provided id (trivial-100% trap). Stats: mapped_to_kg_assigned="
            f"{stats.get('mapped_to_kg_assigned')}, mapped_to_kg={stats.get('mapped_to_kg')}"
        )
    manifest = build_manifest(
        vocab=vocab,
        config=config,
        dataset_sha=dataset_sha,
        output_tsv=str(output_tsv),
        provenance=provenance,
        stats=stats,
        source_provenance=source_provenance,
    )
    (out_dir / f"{config.key}_{vocab}_manifest.json").write_text(json.dumps(manifest, indent=2))
    return VocabRun(
        vocab=vocab, ok=True, output_tsv=str(output_tsv), stats=stats, manifest=manifest
    )


def run_all(
    mapper: ApiMapper,
    input_df: pd.DataFrame,
    config: RunnableConfig,
    out_dir: Path,
    *,
    dataset_sha: str,
    provenance: RunProvenance,
    vocabs: tuple[str, ...] | None = None,
    enforce_assigned: bool = True,
    source_provenance: dict[str, Any] | None = None,
) -> dict[str, VocabRun]:
    """Run every target vocab.

    A transport error on one vocab is recorded, not fatal: the remaining vocabs still run.
    ``TrivialMappingError`` and ``EmptyDatasetError`` are NOT swallowed — both condemn the whole
    run, and filing either as one vocab's error would turn a loud stop into a quiet partial
    result.
    """
    vocabs = vocabs or config.target_vocabs
    _assert_dataset_nonempty(input_df, config)
    results: dict[str, VocabRun] = {}
    for vocab in vocabs:
        try:
            results[vocab] = run_vocab(
                mapper,
                input_df,
                config,
                vocab,
                out_dir,
                dataset_sha=dataset_sha,
                provenance=provenance,
                enforce_assigned=enforce_assigned,
                source_provenance=source_provenance,
            )
        except (TrivialMappingError, EmptyDatasetError):
            raise
        except Exception as exc:  # noqa: BLE001 — per-vocab isolation
            results[vocab] = VocabRun(
                vocab=vocab,
                ok=False,
                output_tsv=None,
                stats=None,
                manifest=None,
                error=f"{type(exc).__name__}: {exc}",
            )
    return results


# --------------------------------------------------------------------------------------------------
# Provided-ID run mode. Used by the MetaBench arm's ID->ID subgroups.
#
# Distinct from the name-input path in three ways: the source id IS provided
# (``provided_id_columns=[source]``), ``annotation_mode='none'`` so nothing is annotated, and the
# anti-trivial guard inverts to ``mapped_to_kg_provided > 0``. One run per dataset — equivalence
# expansion is not vocab-steered, so there is no target restriction.
# --------------------------------------------------------------------------------------------------


@dataclass
class ProvidedRun:
    ok: bool
    output_tsv: str | None
    stats: dict[str, Any] | None
    manifest: dict[str, Any] | None
    error: str | None = None


def run_provided_id(
    mapper: ApiMapper,
    input_df: pd.DataFrame,
    config: Any,  # noqa: ANN401 - ProvidedIdDatasetConfig or a MetaBench subgroup config
    out_dir: Path,
    *,
    dataset_sha: str,
    provenance: RunProvenance,
    enforce_mapped: bool = True,
    source_provenance: dict[str, Any] | None = None,
) -> ProvidedRun:
    """Run one provided-ID dataset: source id provided, target held out.

    The held-out invariant is re-checked by the config's own ``__post_init__`` at construction, so
    a config whose scored target is a provided column cannot reach here. The guard this function
    adds is the complementary one: that the source id actually linked.

    ``config.known_source_gap`` suppresses that guard for a direction with a DOCUMENTED source gap
    (MetaBench's ``kegg2hmdb``, whose provided source id is not a queryable KG node). There a zero
    provided-path mapping is a genuine 0/n result, scored as all-misses. It must never be set to
    paper over an actually-broken run.
    """
    _assert_dataset_nonempty(input_df, config)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    output_tsv, stats = mapper.map_dataset_to_kg(
        dataset=input_df,
        entity_type=config.entity_type,
        name_column=config.name_column,
        provided_id_columns=[config.source_id_column],
        vocab=None,  # equivalence expansion is not vocab-steered
        annotation_mode=config.annotation_mode,  # 'none' — pure provided-ID expansion
        output_dir=out_dir,
        output_prefix=f"{config.key}_provided",
    )
    if (
        enforce_mapped
        and not getattr(config, "known_source_gap", False)
        and not mapped_provided_nonnull(stats)
    ):
        raise NoProvidedMappingError(
            f"{config.key}: provided-ID run produced zero KG mappings via the provided path "
            f"(mapped_to_kg_provided={stats.get('mapped_to_kg_provided')}). The source id never "
            f"linked — refusing to score a broken run."
        )
    manifest = build_manifest(
        vocab="provided",
        config=config,
        dataset_sha=dataset_sha,
        output_tsv=str(output_tsv),
        provenance=provenance,
        stats=stats,
        source_provenance=source_provenance,
    )
    # The load-bearing anti-trivial record: source PROVIDED, target HELD OUT.
    manifest["mode"] = "provided_id"
    manifest["annotation_mode"] = config.annotation_mode
    manifest["provided_id_columns"] = [config.source_id_column]
    manifest["source_namespace"] = config.source_namespace
    manifest["held_out_target_columns"] = {ns: col for ns, col in config.gold_target_columns}
    (out_dir / f"{config.key}_provided_manifest.json").write_text(json.dumps(manifest, indent=2))
    return ProvidedRun(ok=True, output_tsv=str(output_tsv), stats=stats, manifest=manifest)
