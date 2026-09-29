"""Cross-cohort (Monti/NECS) harmonization arm, run against a deployment.

Ported from the biomapper2 engine's ``studies/external_benchmarks/cross_cohort_run.py``
(``origin/dev``, commit ``1ffb571e54fe028ef0ae4e748fc2e7ec093ee603``), rewired from
``biomapper2.mapper.Mapper`` to :class:`biomapper.benchmarks.api_mapper.ApiMapper` exactly as the
11 suite arms were. Running against a deployment is the point: provenance then pins the graph that
actually served the answers rather than a client checkout.

What this measures, and what it does not:

* **Arm M** is BioMapper, names only. Each cohort panel's names go through the mapping API and the
  identifier-only CURIE sets are intersected NECS<->cohort. Linking is set intersection and nothing
  else. Structure-encoding namespaces (``INCHIKEY`` / ``INCHI`` / ``SMILES``) are excluded from the
  linker, because linking on a structure hash would make any downstream structural certificate
  circular and precision 100% by construction.
* **Arm B** is Monti's own published method, reconstructed on the identical row set so the
  comparison is like for like. It is a controlled variable we compute, so the claim that matters is
  against **Monti published**, which we did not compute.
* **Certification is a separate, later judgement** and is deliberately not what forms a link. Three
  of the four cohorts plus NECS ship names only, with no vendor identifiers, so
  ``CohortPanel.certifiable`` is ``False`` for them: their links are countable but never
  structurally certifiable. BLSA is the one to state explicitly in a table, because a blank
  certification column there is a property of the source panel, not a failed certification.

The batch-order hazard the suite already hit applies here unchanged: predictions are joined to
their input rows BY POSITION, so a reordered batch response would attribute one entity's answer to
another. :class:`ApiMapper` escalates that to ``BatchOrderMismatchError`` and aborts rather than
realigning, and :func:`resolve_panel` re-asserts the alignment on the assembled frame. A name is
not unique across a panel in general, so there is nothing safe to realign by.

Usage (resolution is the long pole, so panels are checkpointed and can run as separate processes)::

    python -m biomapper.benchmarks.cross_cohort --out-dir RUN --panel necs
    python -m biomapper.benchmarks.cross_cohort --out-dir RUN --panel arivale
    ...
    python -m biomapper.benchmarks.cross_cohort --out-dir RUN --link-only
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd

from biomapper.benchmarks.adapters.cohort_panel import (
    ARIVALE,
    SPREADSHEET_COHORTS,
    CohortPanel,
    load_cohort_panel,
)
from biomapper.benchmarks.api_mapper import ApiMapper
from biomapper.benchmarks.provenance import (
    DEFAULT_KESTREL_URL,
    RunProvenance,
    build_run_provenance,
    utc_stamp,
)
from biomapper.benchmarks.scorers.arm_b_baseline import (
    MONTI_PUBLISHED,
    MONTI_PUBLISHED_PROVENANCE,
    MONTI_PUBLISHED_SUPERSEDED,
    MONTI_S03_DERIVED,
    PAIR_METHOD,
    arm_b_overlap,
)
from biomapper.benchmarks.scorers.cross_cohort_overlap import (
    link_by_intersection,
    row_curie_set,
)
from biomapper.client import DEFAULT_BASE_URL

# Settled: the four cohorts linked to NECS (engine ``cross_cohort_run.py:33``).
COHORTS: tuple[str, ...] = ("arivale", "xuetal", "llfs", "blsa")
PANELS: tuple[str, ...] = ("necs", *COHORTS)

# Default local source artifacts. Each is SHA-pinned into the manifest; a missing one fails loudly
# rather than producing a panel of zero rows, which an adapter would otherwise read as success.
DEFAULT_SPREADSHEET = (
    Path.home()
    / ".claude/uploads/2390692a-d27f-5fa2-bb74-9b032f2d5009/99293a9b-datasets_metabolites.xlsx"
)
DEFAULT_ARIVALE_XLSX = (
    Path.home()
    / "external_benchmark_runs/arivale_public_panel_20260804/watanabe2023_supp_data2_analytes.xlsx"
)
DEFAULT_REFMET_CACHE = (
    Path.home() / "external_benchmark_runs/cohort_panels_20260804/necs_refmet_convert_cache.tsv"
)

# Published panel sizes, for an honest denominator. LLFS is the one that matters: the paper profiled
# 408 metabolites (188 lipid + 220 polar) and the 4-cohort spreadsheet ships only the 364
# RefMet-standardizable subset, so the gap is pre-filtering and is reported. The "345" figure that
# has circulated appears nowhere in the paper and is not used here.
PUBLISHED_PANEL_SIZES: dict[str, int] = {
    "necs": 1213,
    "arivale": 626,
    "xuetal": 821,
    "llfs": 408,
    "blsa": 468,
}

# The request shape the engine driver used, reproduced so Arm M is comparable across runs.
REQUEST = {
    "entity_type": "metabolite",
    "vocab": "CHEBI",
    "annotation_mode": "all",
    "provided_id_columns": [],
}


class BackendDriftError(RuntimeError):
    """Panels were answered by different backends, so they must not be intersected.

    A cross-cohort overlap is only meaningful when both sides resolved through the same graph. Two
    checkpoints from different builds produce a number no single backend ever produced, and stamping
    one provenance block on them would present it as pinned.
    """


class PanelAlignmentError(RuntimeError):
    """The mapped frame's query column no longer matches the panel it was built from.

    Same class of failure as ``BatchOrderMismatchError`` and refused for the same reason: every
    downstream count joins a prediction to its input row by position, so a shifted frame silently
    attributes one metabolite's CURIE set to another. Nothing is realigned, because a panel name is
    not guaranteed unique across a cohort.
    """


def sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def client_repo_provenance() -> dict[str, str | bool | None]:
    """Commit and cleanliness of the installed client, best effort.

    ``biomapper_version`` comes from installed package metadata, which does not move when a working
    tree does. A run executed from an editable checkout with uncommitted modules would otherwise be
    attributed to whatever version the metadata last recorded, so the commit and the dirty flag are
    pinned alongside it. ``None`` for a wheel install with no repository, which is honest rather
    than invented.
    """
    import subprocess

    repo = Path(__file__).resolve().parents[3]
    try:
        commit = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
    except Exception:  # noqa: BLE001 - provenance must never abort a run
        return {"repo": str(repo), "commit": None, "dirty": None}
    return {"repo": str(repo), "commit": commit, "dirty": bool(status)}


def load_panels(spreadsheet: Path, arivale_xlsx: Path) -> dict[str, CohortPanel]:
    """Load all five panels through the ported engine adapter, so exclusions are counted alike."""
    for path in (spreadsheet, arivale_xlsx):
        if not path.exists():
            raise FileNotFoundError(f"cross-cohort source artifact is missing: {path}")
    sheet = pd.read_excel(spreadsheet, sheet_name="Sheet 1", dtype=str).fillna("")
    panels = {
        key: load_cohort_panel(sheet, SPREADSHEET_COHORTS[key])
        for key in ("necs", "xuetal", "llfs", "blsa")
    }
    arivale = pd.read_excel(arivale_xlsx, sheet_name="Arivale_Metabolomics", dtype=str).fillna("")
    panels["arivale"] = load_cohort_panel(arivale, ARIVALE)
    return panels


# Provenance fields a checkpoint and the finalizing run must agree on. A change in any of them means
# two panels were answered by different software or a different graph, so intersecting them would
# report a cross-cohort overlap that no single backend ever produced.
PINNED_FIELDS: tuple[str, ...] = (
    "endpoint",
    "kestrel_version",
    "kg_version",
    "biolink_version",
    "build_timestamp",
    "git_commit",
)


def panel_provenance_path(out_dir: Path, label: str) -> Path:
    return out_dir / f"{label}_provenance.json"


def write_panel_provenance(
    out_dir: Path,
    label: str,
    provenance: RunProvenance,
    client_repo: dict[str, str | bool | None] | None = None,
) -> dict[str, Any]:
    """Record which backend answered THIS panel, next to its checkpoint.

    Panels are resolved as separate processes and combined later, so a single provenance probe taken
    at link time would stamp one graph build onto checkpoints that may have been produced by
    another. Each panel therefore carries its own pin, and :func:`check_panel_provenance` refuses to
    combine checkpoints that disagree.

    ``client_repo`` must be captured when the run STARTS, not here. The sidecar is written after a
    panel finishes, which can be an hour later; reading the working tree at that point would
    attribute the panel to whatever the repository happens to be at write time rather than to the
    code that was loaded when the process began. Falling back to reading it here is strictly worse
    than nothing would be, so the fallback is recorded as late-captured.
    """
    if client_repo is None:
        client_repo = {**client_repo_provenance(), "captured": "late (at sidecar write)"}
    else:
        client_repo = {**client_repo, "captured": "at run start"}
    record = {
        "panel": label,
        "endpoint": provenance.api_endpoint,
        "kestrel_url": provenance.kestrel_url,
        "kestrel_version": provenance.kestrel_version,
        "kg_version": provenance.kg_build.kg_version,
        "biolink_version": provenance.kg_build.biolink_version,
        "build_timestamp": provenance.kg_build.build_timestamp,
        "git_commit": provenance.kg_build.git_commit,
        "source_versions": provenance.kg_build.source_versions,
        "resolved_at": provenance.run_timestamp,
        "biomapper_version": provenance.biomapper_version,
        "client_repo": client_repo,
    }
    panel_provenance_path(out_dir, label).write_text(json.dumps(record, indent=2, default=str))
    return record


def check_panel_provenance(
    out_dir: Path, label: str, final: RunProvenance, *, allow_unpinned: bool
) -> dict[str, Any]:
    """Compare a panel's recorded backend against the finalizing probe.

    Returns a status record. A missing sidecar means the checkpoint predates provenance recording
    and cannot be attributed to a build at all, which is worse than a mismatch because it looks
    fine. Both are refused unless ``allow_unpinned`` is set, and in that case the manifest carries
    the fact rather than the run pretending to be pinned.
    """
    path = panel_provenance_path(out_dir, label)
    expected = {
        "endpoint": final.api_endpoint,
        "kestrel_version": final.kestrel_version,
        "kg_version": final.kg_build.kg_version,
        "biolink_version": final.kg_build.biolink_version,
        "build_timestamp": final.kg_build.build_timestamp,
        "git_commit": final.kg_build.git_commit,
    }
    status: dict[str, Any]
    if not path.exists():
        status = {"panel": label, "status": "unpinned", "recorded": None, "expected": expected}
    else:
        recorded = json.loads(path.read_text())
        drift = {
            field: {"panel": recorded.get(field), "finalizing_run": expected[field]}
            for field in PINNED_FIELDS
            if recorded.get(field) != expected[field]
        }
        status = {
            "panel": label,
            "status": "match" if not drift else "drift",
            "drift": drift or None,
            "recorded": {field: recorded.get(field) for field in PINNED_FIELDS},
        }
    if status["status"] != "match" and not allow_unpinned:
        detail = status.get("drift") or "no sidecar was written"
        raise BackendDriftError(
            f"{label}: checkpoint provenance is {status['status']}. Refusing to combine "
            f"panels that cannot be attributed to one backend; {detail}. Re-resolve the panel, "
            "or pass --allow-unpinned-checkpoints to publish the run with the gap recorded "
            "in the manifest. To re-resolve: "
            f"--panel {label} --refresh, which discards the checkpoint and its sidecar."
        )
    return status


def resolve_panel(
    mapper: ApiMapper,
    panel: CohortPanel,
    out_dir: Path,
    label: str,
    provenance: RunProvenance | None = None,
    client_repo: dict[str, str | bool | None] | None = None,
    refresh: bool = False,
) -> pd.DataFrame:
    """Resolve one panel name-only; checkpoint the mapped TSV so a mid-run 5xx loses nothing.

    ``provenance`` is written beside the checkpoint when supplied, which is what lets a later
    ``--link-only`` pass verify that every panel was answered by the same backend.
    """
    dest = out_dir / f"{label}_MAPPED.tsv"
    names = panel.names
    if dest.exists() and refresh:
        # The remediation the drift error advises has to be followable. Without this, a stale or
        # unpinned checkpoint can only be re-resolved by finding and deleting the TSV by hand, and
        # re-running the documented panel command silently reuses the file it was meant to replace.
        # The sidecar goes with it: re-pinning a checkpoint whose backend is unknown, rather than
        # regenerating it, would manufacture provenance instead of recording it.
        print(f"[resolve] {label}: --refresh, discarding {dest.name} and re-resolving", flush=True)
        dest.unlink()
        panel_provenance_path(out_dir, label).unlink(missing_ok=True)
    if dest.exists():
        mapped = pd.read_csv(dest, sep="\t", dtype=str).fillna("")
        assert_alignment(mapped, names, label)
        print(f"[resolve] {label}: reusing checkpoint {dest.name} ({len(mapped)} rows)", flush=True)
        return mapped

    print(f"[resolve] {label}: n={len(names)} against {mapper.endpoint}", flush=True)
    out_tsv, stats = mapper.map_dataset_to_kg(
        dataset=pd.DataFrame({"name": names}),
        entity_type=str(REQUEST["entity_type"]),
        name_column="name",
        provided_id_columns=[],
        vocab=str(REQUEST["vocab"]),
        annotation_mode=str(REQUEST["annotation_mode"]),
        output_dir=out_dir,
        output_prefix=label,
    )
    (out_dir / f"{label}_stats.json").write_text(json.dumps(stats, indent=2, default=str))
    mapped = pd.read_csv(out_tsv, sep="\t", dtype=str).fillna("")
    assert_alignment(mapped, names, label)
    if provenance is not None:
        write_panel_provenance(out_dir, label, provenance, client_repo)
    return mapped


def assert_alignment(mapped: pd.DataFrame, names: list[str], label: str) -> None:
    """Re-assert position alignment between the mapped frame and the panel that produced it."""
    got = [str(v) for v in mapped["name"].tolist()]
    if got != [str(n) for n in names]:
        first = next(
            (i for i, (a, b) in enumerate(zip(got, names, strict=False)) if str(a) != str(b)),
            min(len(got), len(names)),
        )
        raise PanelAlignmentError(
            f"{label}: mapped frame does not match the panel by position "
            f"(n={len(got)} vs {len(names)}, first divergence at row {first}). Refusing to score: "
            "every count here joins a prediction to its input row positionally."
        )


def errored_names(mapped: pd.DataFrame) -> list[str]:
    """Names whose mapping call itself failed, in panel order.

    An errored row is NOT an unresolved row. Both come back with an empty CURIE set, so folding
    them together would report "this metabolite did not resolve" when the truth is "we never got an
    answer", and it would understate resolution by exactly the number of rows the deployment
    dropped. The deployment returned 5xx under concurrent load during this run, so this is a live
    hazard and not a theoretical one.
    """
    if "mapping_error" not in mapped.columns:
        return []
    return [
        str(row["name"])
        for _, row in mapped.iterrows()
        if str(row.get("mapping_error", "")).strip() not in ("", "nan")
    ]


def repair_errored_rows(
    mapper: ApiMapper, mapped: pd.DataFrame, out_dir: Path, label: str
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Re-map only the errored rows and write them back into their original positions.

    Positional write-back is what keeps this safe: the repaired values land on the same row index
    they came from, so the panel alignment the rest of the module depends on is preserved. A row
    that errors again stays errored and is reported.
    """
    failures = errored_names(mapped)
    if not failures:
        return mapped, {"attempted": 0, "recovered": 0, "still_errored": 0}

    print(f"[repair] {label}: re-mapping {len(failures)} errored rows", flush=True)
    indices = [
        i
        for i, (_, row) in enumerate(mapped.iterrows())
        if str(row.get("mapping_error", "")).strip() not in ("", "nan")
    ]
    retry_frame = pd.DataFrame({"name": [str(mapped.iloc[i]["name"]) for i in indices]})
    out_tsv, _ = mapper.map_dataset_to_kg(
        dataset=retry_frame,
        entity_type=str(REQUEST["entity_type"]),
        name_column="name",
        provided_id_columns=[],
        vocab=str(REQUEST["vocab"]),
        annotation_mode=str(REQUEST["annotation_mode"]),
        output_dir=out_dir,
        output_prefix=f"{label}_repair",
    )
    repaired = pd.read_csv(out_tsv, sep="\t", dtype=str).fillna("")
    if len(repaired) != len(indices):
        raise PanelAlignmentError(
            f"{label}: repair pass returned {len(repaired)} rows for {len(indices)} requests; "
            "refusing to write back a misaligned frame."
        )
    shared_columns = [c for c in mapped.columns if c in repaired.columns]
    # Rebuilt column-wise from plain lists rather than through positional cell assignment: the
    # write-back target is an index into the ORIGINAL panel order, and going via lists keeps that
    # explicit instead of relying on a frame's internal column positions staying put.
    columns = {c: mapped[c].tolist() for c in shared_columns}
    for position, index in enumerate(indices):
        source = repaired.iloc[position]
        if str(source["name"]) != str(mapped.iloc[index]["name"]):
            raise PanelAlignmentError(
                f"{label}: repair row {position} is {source['name']!r} but target row {index} is "
                f"{mapped.iloc[index]['name']!r}; refusing to write back."
            )
        for column in shared_columns:
            columns[column][index] = source[column]
    for column in shared_columns:
        mapped[column] = columns[column]
    mapped.to_csv(out_dir / f"{label}_MAPPED.tsv", sep="\t", index=False)
    still = errored_names(mapped)
    summary = {
        "attempted": len(failures),
        "recovered": len(failures) - len(still),
        "still_errored": len(still),
        "still_errored_names": still[:50],
    }
    print(
        f"[repair] {label}: recovered {summary['recovered']}/{summary['attempted']}, "
        f"{summary['still_errored']} still errored",
        flush=True,
    )
    return mapped, summary


def curies_by_name(mapped: pd.DataFrame, label: str) -> dict[str, frozenset[str]]:
    """``{name: identifier-only CURIE set}`` via the ported cross-cohort scorer."""
    out: dict[str, frozenset[str]] = {}
    for _, row in mapped.iterrows():
        name = str(row.get("name", "")).strip()
        if not name:
            continue
        if name in out:
            raise PanelAlignmentError(
                f"{label}: duplicate panel name {name!r} reached the linker. One of the two rows "
                "would be dropped silently; the adapter is supposed to de-duplicate."
            )
        out[name] = row_curie_set(row)
    return out


def certificate_tally(mapped: pd.DataFrame) -> dict[str, dict[str, int]]:
    """Tallies of the API's resolution-certificate columns for one panel.

    This is the API's ``ResolutionCertificateModel``, which reads the chosen KRAKEN node's own
    InChIKey first and only falls back to an external source. It is therefore NOT fully
    KG-independent and is reported as a resolution property, never as a cross-cohort link
    certificate. The link certificate is
    :func:`biomapper.benchmarks.scorers.link_certificate.certify_link`.
    """
    columns = (
        "certificate_state",
        "certificate_structure_status",
        "certificate_refusal_reason",
        "certificate_tier_b_outcome",
        "certificate_refmet_availability",
        "certificate_lipid_resolution_level",
        "certificate_tier_b_snapshot_version",
        "certificate_independent_source",
        "certificate_independent_of_selection",
        "chosen_kg_id_review",
    )
    tallies: dict[str, dict[str, int]] = {}
    for column in columns:
        if column not in mapped.columns:
            continue
        values = [str(v).strip() or "(blank)" for v in mapped[column].tolist()]
        tallies[column] = dict(sorted(Counter(values).items(), key=lambda kv: -kv[1]))
    return tallies


def chosen_prefix_tally(mapped: pd.DataFrame) -> dict[str, int]:
    """Namespace of the chosen node, per panel. Production answers with ``RM:*`` for most rows."""
    prefixes = [
        str(v).split(":", 1)[0]
        if ":" in str(v)
        else ("(unresolved)" if not str(v).strip() else str(v))
        for v in mapped.get("chosen_kg_id", pd.Series(dtype=str)).tolist()
    ]
    return dict(sorted(Counter(prefixes).items(), key=lambda kv: -kv[1]))


def load_refmet_map(path: Path) -> dict[str, str]:
    if not path.exists():
        raise FileNotFoundError(f"RefMet conversion cache is missing: {path}")
    with path.open() as handle:
        return {row["input"]: row["refmet"] for row in csv.DictReader(handle, delimiter="\t")}


def cross_check_harmonize(
    a_curies: dict[str, frozenset[str]], b_curies: dict[str, frozenset[str]]
) -> dict[str, Any]:
    """Confirm the generic ``biomapper.harmonize`` primitive agrees with the cohort scorer.

    Two implementations of one rule is a real risk: the package ships a pandas-free
    ``harmonize.link_by_intersection`` and this cohort-shaped port. If they ever diverge, a number
    changes for a reason unrelated to the engine, so the agreement is asserted per run rather than
    assumed.
    """
    from biomapper.harmonize import link_by_intersection as generic_link

    engine = link_by_intersection(a_curies, b_curies)
    generic = generic_link(a_curies, b_curies)
    agree = (
        engine.n_links == generic.n_links
        and engine.n_a_linked == generic.n_a_linked
        and engine.n_b_linked == generic.n_b_linked
        and engine.n_a_comparable == generic.n_a_comparable
        and engine.n_b_comparable == generic.n_b_comparable
    )
    return {
        "agree": agree,
        "scorer_n_links": engine.n_links,
        "harmonize_n_links": generic.n_links,
    }


def arm_b_reconstruction_basis(
    cohort: str,
    necs_names: list[str],
    cohort_names: list[str],
    refmet_map: dict[str, str],
) -> dict[str, Any]:
    """How much of each side the Arm-B reconstruction could even see.

    Arm B is a controlled variable WE compute, so its gap to the published number is only
    interpretable next to the coverage of the inputs it had. The RefMet pairs are the ones that
    matter: a name that does not standardize is dropped before the join, exactly as in the paper, so
    a cache that covers half a panel caps the reconstruction at half the panel. Reporting the gap as
    a bare integer invites reading a cache limit as a disagreement with the paper.

    Returns an empty basis for the CHEMICAL_NAME pairs, where the join is over raw names and
    there is no standardization step to be short of.
    """
    method = PAIR_METHOD[cohort]
    if method[0] != "refmet":
        return {
            "method": "name",
            "note": "raw CHEMICAL_NAME join; no standardization step, so nothing is dropped first",
        }

    def standardizable(names: list[str]) -> int:
        return sum(1 for n in names if refmet_map.get(n.strip(), "").strip())

    cohort_ok = standardizable(cohort_names)
    necs_ok = standardizable(necs_names)
    return {
        "method": "refmet",
        "necs_standardizable": necs_ok,
        "necs_n": len(necs_names),
        "cohort_standardizable": cohort_ok,
        "cohort_n": len(cohort_names),
        "reconstruction_ceiling": min(necs_ok, cohort_ok),
        "note": (
            "a name that does not RefMet-standardize is dropped before the join, so the "
            "reconstruction cannot exceed the smaller standardizable side. Read the gap to the "
            "published number against this ceiling, not as a disagreement with the paper."
        ),
    }


# Which KRAKEN-ingested source, if any, the pair's PUBLISHED comparator was built from. Monti
# matched NECS to Arivale and to Xu on Metabolon CHEMICAL_NAME, which is vendor curation and not a
# graph source. NECS to LLFS and to BLSA were matched on RefMet standardized names, and RefMet is
# both ingested into KRAKEN and the resolver's source-weighting target.
COMPARATOR_SOURCE: dict[str, str | None] = {
    "arivale": None,
    "xuetal": None,
    "llfs": "refmet",
    "blsa": "refmet",
}


def comparator_independence(cohort: str, kg_sources: list[str]) -> dict[str, Any]:
    """Label a pair accuracy-candidate or coverage, against the build's own ``sources`` list.

    The guardrail this implements: a benchmark whose gold or comparator comes from a vocabulary
    baked into the graph is measuring coverage, not accuracy. Derived from the live source list per
    run, so the label tracks the build rather than a note that goes stale.

    Note what is and is not being claimed. The NECS gold itself is Metabolon vendor curation and is
    not a graph source, so the NAMES are never circular. The question here is about the COMPARATOR.
    On the two RefMet pairs, BioMapper answers predominantly with ``RM:*`` nodes while the baseline
    is a RefMet name join, so "BioMapper recovers more links than the published method" on those
    pairs compares two readings of one vocabulary. That is a coverage statement. On the two
    CHEMICAL_NAME pairs the comparator sits outside the graph, so the comparison is cleaner.
    """
    present = {s.lower() for s in kg_sources}
    source = COMPARATOR_SOURCE[cohort]
    if source and source in present:
        return {
            "label": "coverage",
            "comparator_source_in_graph": source,
            "reason": (
                f"the published comparator for this pair is a {source} name join, and {source!r} "
                "is ingested into the graph being measured and is the resolver's source-weighting "
                "target. BioMapper's answer and the comparator therefore share a vocabulary; "
                "report the difference as coverage, not accuracy."
            ),
        }
    return {
        "label": "accuracy_candidate",
        "comparator_source_in_graph": None,
        "reason": (
            "the published comparator for this pair is Metabolon CHEMICAL_NAME vendor curation, "
            "which is not a KRAKEN-ingested source, so the comparison does not run through a "
            "shared vocabulary. Not a full independence verdict: this arm measures links "
            "recovered, not structural correctness, and certification is reported separately."
        ),
    }


def run_links(
    panels: dict[str, CohortPanel],
    curies: dict[str, dict[str, frozenset[str]]],
    refmet_map: dict[str, str],
    out_dir: Path,
    errored: dict[str, set[str]] | None = None,
    kg_sources: list[str] | None = None,
) -> dict[str, Any]:
    """Link every NECS<->cohort pair, reconstruct Arm B, and write the per-pair artifacts.

    ``errored`` names the rows the deployment never answered, per panel. They are excluded from the
    unresolved counts and from the unresolved CSVs: an errored row already carries an empty CURIE
    set,
    so leaving it in would report the same deployment failure twice, once as an error and again as a
    metabolite that genuinely did not resolve, inflating the published unresolved total.
    """
    errored = errored or {}
    kg_sources = kg_sources if kg_sources is not None else []
    necs_names = panels["necs"].names
    results: dict[str, Any] = {}

    def unresolved_names(label: str) -> list[str]:
        failed = errored.get(label, set())
        return [name for name, curie in curies[label].items() if not curie and name not in failed]

    for cohort in COHORTS:
        overlap = link_by_intersection(curies["necs"], curies[cohort])
        derived = arm_b_overlap(cohort, necs_names, panels[cohort].names, refmet_map=refmet_map)
        published = MONTI_PUBLISHED[cohort]
        results[cohort] = {
            "certifiable": panels[cohort].certifiable,
            "certifiability_note": (
                "names only, no vendor identifiers: links are countable but never structurally "
                "certifiable"
                if not panels[cohort].certifiable
                else f"vendor id namespaces present: {list(panels[cohort].id_columns)}"
            ),
            "panel_n_loaded": len(panels[cohort].names),
            "panel_n_published": PUBLISHED_PANEL_SIZES[cohort],
            "panel_card": panels[cohort].card,
            "arm_m_links": overlap.n_links,
            "arm_m_necs_linked": overlap.n_a_linked,
            "arm_m_cohort_linked": overlap.n_b_linked,
            "necs_comparable": overlap.n_a_comparable,
            "cohort_comparable": overlap.n_b_comparable,
            "necs_unresolved": len(unresolved_names("necs")),
            "cohort_unresolved": len(unresolved_names(cohort)),
            "necs_errored": len(errored.get("necs", set())),
            "cohort_errored": len(errored.get(cohort, set())),
            "arm_b_rederived": derived.count,
            "arm_b_method": derived.method,
            "monti_published": published,
            "monti_published_provenance": MONTI_PUBLISHED_PROVENANCE[cohort],
            "monti_published_superseded_value": MONTI_PUBLISHED_SUPERSEDED[cohort],
            "monti_s03_derived": MONTI_S03_DERIVED.get(cohort),
            "arm_b_gap_to_published": derived.count - published,
            "arm_m_vs_arm_b": overlap.n_a_linked - derived.count,
            "arm_m_vs_published": overlap.n_a_linked - published,
            "harmonize_cross_check": cross_check_harmonize(curies["necs"], curies[cohort]),
            "comparator_independence": comparator_independence(cohort, kg_sources),
            "arm_b_reconstruction_basis": arm_b_reconstruction_basis(
                cohort, necs_names, panels[cohort].names, refmet_map
            ),
        }
        print(
            f"[pair] NECS<->{cohort}: Arm-M necs-linked={overlap.n_a_linked} "
            f"cohort-linked={overlap.n_b_linked} links={overlap.n_links} "
            f"Arm-B={derived.count} published={published}",
            flush=True,
        )
        pd.DataFrame(
            [
                {
                    "necs_name": link.a_name,
                    f"{cohort}_name": link.b_name,
                    "shared_curies": "|".join(sorted(link.shared)),
                }
                for link in overlap.links
            ]
        ).to_csv(out_dir / f"links_necs_{cohort}.csv", index=False)
        pd.DataFrame({"name": unresolved_names(cohort)}).to_csv(
            out_dir / f"unresolved_{cohort}.csv", index=False
        )
        pd.DataFrame({"name": sorted(errored.get(cohort, set()))}).to_csv(
            out_dir / f"errored_{cohort}.csv", index=False
        )
    pd.DataFrame({"name": unresolved_names("necs")}).to_csv(
        out_dir / "unresolved_necs.csv", index=False
    )
    pd.DataFrame({"name": sorted(errored.get("necs", set()))}).to_csv(
        out_dir / "errored_necs.csv", index=False
    )
    return results


def prepare_panel(
    mapper: ApiMapper,
    panel: CohortPanel,
    out_dir: Path,
    label: str,
    provenance: RunProvenance,
    client_repo: dict[str, str | bool | None],
    *,
    allow_unpinned: bool,
) -> tuple[pd.DataFrame, dict[str, Any], dict[str, Any]]:
    """Validate, resolve and repair one panel, in that order. Returns (frame, pin, repair).

    The order is the substance. An inherited checkpoint is validated BEFORE anything writes to it,
    because the repair pass rewrites the TSV with rows from the CURRENT backend. Repairing first and
    validating second would leave a file holding rows from two backends on disk even though the run
    then aborts, and a later retry would reuse that mixed file as if it were one backend's output.
    Validating first means a rejected checkpoint is left exactly as it was found.
    """
    existed_before = (out_dir / f"{label}_MAPPED.tsv").exists()
    if existed_before:
        pin = check_panel_provenance(out_dir, label, provenance, allow_unpinned=allow_unpinned)
    else:
        pin = {"panel": label, "status": "match", "drift": None}
    frame = resolve_panel(mapper, panel, out_dir, label, provenance, client_repo)

    # Repair is SKIPPED for a checkpoint whose backend was not confirmed. Under
    # --allow-unpinned-checkpoints the drift check returns a status instead of raising, and a
    # repair would remap the failed rows against the CURRENT backend while keeping inherited rows
    # from an unknown or different one. That yields a genuinely per-row mixed file whose overlap
    # would still read as one coherent backend's output, since the manifest carries only the pin
    # status and aggregate repair counts. Leaving it unrepaired keeps the file homogeneous and the
    # errors reported as unrecovered, which is the honest shape.
    if pin["status"] != "match":
        repair: dict[str, Any] = {
            "attempted": 0,
            "recovered": 0,
            "still_errored": len(errored_names(frame)),
            "skipped": True,
            "skipped_reason": (
                f"checkpoint provenance is {pin['status']}, so a repair would mix rows from the "
                "current backend into a checkpoint produced by an unconfirmed one"
            ),
        }
        if repair["still_errored"]:
            print(
                f"[warn] {label}: repair skipped ({pin['status']} provenance); "
                f"{repair['still_errored']} errored rows left as found",
                flush=True,
            )
        return frame, pin, repair

    frame, repair = repair_errored_rows(mapper, frame, out_dir, label)
    return frame, pin, repair


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m biomapper.benchmarks.cross_cohort",
        description="Cross-cohort (Monti/NECS) harmonization arm against a deployment.",
    )
    parser.add_argument(
        "--out-dir", type=Path, default=None, help="Run directory (default: timestamped)"
    )
    parser.add_argument("--endpoint", default=DEFAULT_BASE_URL, help="BioMapper API root")
    parser.add_argument(
        "--kestrel-url", default=DEFAULT_KESTREL_URL, help="Kestrel root for /health"
    )
    parser.add_argument("--api-key", default=None, help="Omit for a keyless deployment")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--spreadsheet", type=Path, default=DEFAULT_SPREADSHEET)
    parser.add_argument("--arivale-xlsx", type=Path, default=DEFAULT_ARIVALE_XLSX)
    parser.add_argument("--refmet-cache", type=Path, default=DEFAULT_REFMET_CACHE)
    parser.add_argument(
        "--panel",
        choices=PANELS,
        default=None,
        help="Resolve only this panel and exit (checkpointed; run one process per panel)",
    )
    parser.add_argument(
        "--link-only",
        action="store_true",
        help="Skip resolution; link and score from the existing per-panel checkpoints",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="With --panel, discard an existing checkpoint and its sidecar and re-resolve. This is "
        "the follow-up the drift error advises; without it a stale or unpinned checkpoint can only "
        "be replaced by deleting the TSV by hand.",
    )
    parser.add_argument(
        "--allow-unpinned-checkpoints",
        action="store_true",
        help="Combine checkpoints whose backend cannot be confirmed against the finalizing probe. "
        "Off by default: a cross-cohort overlap spanning two graph builds is a number no single "
        "backend produced. When on, the gap is recorded in the manifest rather than hidden.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ts = utc_stamp()
    out_dir = args.out_dir or (Path.home() / f"external_benchmark_runs/cross_cohort_{ts}")
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[run] {out_dir}", flush=True)

    panels = load_panels(args.spreadsheet, args.arivale_xlsx)
    for key, panel in panels.items():
        print(
            f"[panel] {key}: n={len(panel.names)} certifiable={panel.certifiable} "
            f"exclusions={panel.card['exclusions']}",
            flush=True,
        )

    # Probed BEFORE the panel branch, because the panel-only path is the documented workflow and it
    # is the one whose checkpoints have to carry a backend pin. Probing only at finalization would
    # leave every sidecar unwritten and the drift check with nothing to compare against.
    provenance = build_run_provenance(
        api_endpoint=args.endpoint, kestrel_url=args.kestrel_url, run_id=f"cross_cohort_{ts}"
    )
    if not provenance.pinned:
        print(
            f"[fatal] provenance is not pinned ({provenance.health_error}). Refusing to emit "
            "numbers that cannot be attributed to a graph build.",
            file=sys.stderr,
        )
        return 2

    # Captured here, at run start, so a sidecar written an hour later still names the code that was
    # loaded when the process began rather than whatever the tree looks like at write time.
    client_repo = client_repo_provenance()

    if args.panel is not None and not args.link_only:
        mapper = ApiMapper(
            args.endpoint,
            api_key=args.api_key,
            batch_size=args.batch_size,
            timeout=args.timeout,
        )
        resolve_panel(
            mapper,
            panels[args.panel],
            out_dir,
            args.panel,
            provenance,
            client_repo,
            refresh=args.refresh,
        )
        (out_dir / f"{args.panel}_counters.json").write_text(
            json.dumps(mapper.counters.snapshot(), indent=2, default=str)
        )
        print(
            f"[done] panel {args.panel} -> {out_dir}/{args.panel}_MAPPED.tsv "
            f"(kg={provenance.kg_build.kg_version} commit={provenance.kg_build.git_commit[:8]})",
            flush=True,
        )
        return 0
    print(
        f"[kg] kestrel={provenance.kestrel_version} kg={provenance.kg_build.kg_version} "
        f"biolink={provenance.kg_build.biolink_version} "
        f"commit={provenance.kg_build.git_commit[:8]} built={provenance.kg_build.build_timestamp}",
        flush=True,
    )

    mapper = ApiMapper(
        args.endpoint, api_key=args.api_key, batch_size=args.batch_size, timeout=args.timeout
    )
    mapped: dict[str, pd.DataFrame] = {}
    curies: dict[str, dict[str, frozenset[str]]] = {}
    repairs: dict[str, dict[str, Any]] = {}
    errored: dict[str, set[str]] = {}
    pins: dict[str, dict[str, Any]] = {}
    for label in PANELS:
        checkpoint = out_dir / f"{label}_MAPPED.tsv"
        if args.link_only and not checkpoint.exists():
            print(f"[fatal] --link-only but {checkpoint} is missing", file=sys.stderr)
            return 2
        frame, pins[label], repairs[label] = prepare_panel(
            mapper,
            panels[label],
            out_dir,
            label,
            provenance,
            client_repo,
            allow_unpinned=args.allow_unpinned_checkpoints,
        )
        mapped[label] = frame
        curies[label] = curies_by_name(frame, label)
        errored[label] = set(errored_names(frame))
        print(
            f"[resolve] {label}: {sum(1 for s in curies[label].values() if s)}/"
            f"{len(curies[label])} with a non-empty identifier-only CURIE set, "
            f"{repairs[label]['still_errored']} errored, provenance={pins[label]['status']}",
            flush=True,
        )

    skipped_repairs = {k: v["skipped_reason"] for k, v in repairs.items() if v.get("skipped")}
    unpinned = {k: v["status"] for k, v in pins.items() if v["status"] != "match"}
    if unpinned:
        print(
            f"[warn] panels whose backend could not be confirmed against the finalizing probe: "
            f"{unpinned}. The manifest records this; these numbers are NOT single-backend pinned.",
            file=sys.stderr,
        )

    unrecovered = {k: v["still_errored"] for k, v in repairs.items() if v["still_errored"]}
    if unrecovered:
        print(
            f"[warn] rows the deployment never answered, after the repair pass: {unrecovered}. "
            "These are recorded as errors, NOT as unresolved metabolites, and the affected panels' "
            "resolution counts are a floor. Re-run those panels before publishing.",
            file=sys.stderr,
        )

    refmet_map = load_refmet_map(args.refmet_cache)
    results = run_links(panels, curies, refmet_map, out_dir, errored, provenance.kg_build.sources)

    manifest = {
        "arm": "M (BioMapper, names only) vs B (Monti method, re-derived) vs Monti published",
        "run_id": provenance.run_id,
        "created_utc": ts,
        "package": {
            "name": "biomapper",
            "version": provenance.biomapper_version,
            "client_repo": client_repo_provenance(),
            "linker": "biomapper.benchmarks.scorers.cross_cohort_overlap.link_by_intersection",
            "linker_generic": "biomapper.harmonize.link_by_intersection (cross-checked per pair)",
            "baseline": "biomapper.benchmarks.scorers.arm_b_baseline.arm_b_overlap",
            "panels": "biomapper.benchmarks.adapters.cohort_panel",
            "mapper": "biomapper.benchmarks.api_mapper.ApiMapper",
            "provenance": "biomapper.benchmarks.provenance.build_run_provenance",
        },
        "ported_from": {
            "repo": "biomapper2",
            "ref": "origin/dev",
            "commit": "1ffb571e54fe028ef0ae4e748fc2e7ec093ee603",
            "modules": [
                "studies/external_benchmarks/adapters/cohort_panel.py",
                "studies/external_benchmarks/scorers/arm_b_baseline.py",
                "studies/external_benchmarks/scorers/cross_cohort_overlap.py",
                "studies/external_benchmarks/scorers/link_certificate.py",
                "studies/external_benchmarks/cross_cohort_run.py (driver, reference)",
            ],
        },
        "provenance": json.loads(provenance.model_dump_json()),
        "request": {
            **REQUEST,
            "batch_size": args.batch_size,
            "endpoint": args.endpoint,
            "api_key_sent": args.api_key is not None,
        },
        "sources": {
            "spreadsheet": {
                "path": str(args.spreadsheet),
                "sha256": sha256_path(args.spreadsheet),
            },
            "arivale_xlsx": {
                "path": str(args.arivale_xlsx),
                "sha256": sha256_path(args.arivale_xlsx),
            },
            "refmet_cache": {
                "path": str(args.refmet_cache),
                "sha256": sha256_path(args.refmet_cache),
            },
            "monti_reference": {
                "doi": "10.1007/s11357-026-02174-2",
                "gold": "MOESM5 (Supplementary Table 5), 1495 metabolites",
                # Must agree with arm_b_baseline.MONTI_PUBLISHED_PROVENANCE. A run record carrying
                # two different accounts of where its comparator came from cannot be audited, and an
                # earlier version of this note said the opposite of the per-cohort provenance.
                "published_overlaps_read_from": (
                    "supplement MOESM6, sheet 'Table 2. Datasets', '# Overlap' column, with "
                    "NECS as the reference row. Corroborated by supplement Table S03 over its 1052 "
                    "metabolite rows (see monti_s03_derived per pair)."
                ),
                "published_overlaps_NOT_read_from": (
                    "the Methods 'Datasets harmonization' prose, which disagrees with the table on "
                    "two pairs (Xu 385, BLSA 188) and is superseded by it. The MAIN TEXT's Table 2 "
                    "is 'Age-only markers' and is a different table from the supplement's "
                    "'Table 2. Datasets'; conflating the two is what made the prose look "
                    "authoritative."
                ),
                "per_pair_provenance": "results.<cohort>.monti_published_provenance",
            },
        },
        "panel_sizes_loaded": {k: len(v.names) for k, v in panels.items()},
        "panel_sizes_published": PUBLISHED_PANEL_SIZES,
        "certifiability": {
            k: {"certifiable": v.certifiable, "id_namespaces": list(v.id_columns)}
            for k, v in panels.items()
        },
        "certificate_tallies": {k: certificate_tally(v) for k, v in mapped.items()},
        "chosen_namespace_tallies": {k: chosen_prefix_tally(v) for k, v in mapped.items()},
        "checkpoint_provenance": {
            "per_panel": pins,
            "unconfirmed": unpinned,
            "allow_unpinned_checkpoints": args.allow_unpinned_checkpoints,
            "note": (
                "Panels are resolved as separate processes and combined here, so each checkpoint "
                "records the backend that answered it and is checked against the finalizing probe. "
                "A cross-cohort overlap built from two different graph builds is a number no "
                "single "
                "backend produced, which is why a mismatch aborts unless it is explicitly allowed "
                "and recorded."
            ),
        },
        "mapping_errors": {
            "repair_pass": repairs,
            "repairs_skipped_for_unconfirmed_provenance": skipped_repairs,
            "unrecovered_by_panel": unrecovered,
            "note": (
                "An errored row is a row the deployment never answered. It is counted apart "
                "from an unresolved row, because both carry an empty CURIE set and merging them "
                "would "
                "report a non-resolution that never happened. A panel with unrecovered errors "
                "has a resolution count that is a floor, not a measurement."
            ),
        },
        "circularity_register": {
            "kraken_sources": provenance.kg_build.sources,
            "kraken_source_versions": provenance.kg_build.source_versions,
            "necs_gold_source": (
                "Metabolon CHEMICAL_NAME vendor curation (MOESM5). Not a KRAKEN-ingested "
                "vocabulary, so the NECS name gold is not circular through the graph."
            ),
            "refmet_ingested": "refmet" in {s.lower() for s in provenance.kg_build.sources},
            "refmet_caveat": (
                "RefMet is both ingested into KRAKEN and the resolver's source-weighting target, "
                "and production answers this arm predominantly with RM:* nodes. Any number whose "
                "gold is a RefMet name is coverage, not accuracy."
            ),
        },
        "results": results,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    print(f"[done] {out_dir}/manifest.json", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
