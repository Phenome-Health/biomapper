"""Drive every arm into ONE timestamped suite dir with ONE aggregate manifest.

Ported from ``run.run_suite``. Three properties carried over deliberately:

* **One bad arm never aborts the suite.** A failing arm is recorded ``status="failed"`` and the
  run continues, so a suite always produces a complete account of what passed and what broke.
* **Skips are recorded, never omitted.** A deliberate exclusion and an arm that fell out of the
  registry by accident look identical if skips are simply dropped. That holds for an arm the
  operator leaves out of a subset run too: every ``SUITE_DATASETS`` arm not selected must carry an
  operator reason, and the run refuses to start without one.
* **The backend is pinned BEFORE any arm runs, and re-read after.** Sampling provenance at the
  end would attribute every result to whatever build happened to be serving when the last arm
  finished. Re-reading catches a build that moved mid-suite, which would mean the pins no longer
  describe every number — silence there is the failure this exists to remove.

The endpoint defaults to PRODUCTION. ``--endpoint`` switches to dev for future testing.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any

from biomapper.benchmarks.api_mapper import ApiMapper
from biomapper.benchmarks.arms import ARM_RUNNERS
from biomapper.benchmarks.config import SUITE_DATASETS, SUITE_SKIPPED
from biomapper.benchmarks.provenance import (
    DEFAULT_KESTREL_URL,
    build_run_provenance,
    circularity_notes,
    fetch_kg_build_info,
    new_run_id,
    utc_stamp,
)
from biomapper.benchmarks.sources import SourceUnavailable

# The deployment the paper describes. Production by default: a benchmark that measures a
# dev checkout is not measuring the service a reader can call.
PRODUCTION_ENDPOINT = "https://biomapper.expertintheloop.io/api/v1"
DEV_ENDPOINT = "https://biomapper-dev.expertintheloop.io/api/v1"

ENDPOINTS: dict[str, str] = {"production": PRODUCTION_ENDPOINT, "dev": DEV_ENDPOINT}

DEFAULT_SUITE_ROOT = Path.home() / "external_benchmark_runs"

logger = logging.getLogger(__name__)


def resolve_endpoint(endpoint: str) -> str:
    """Map an alias (``production`` / ``dev``) to a URL, or pass a URL through.

    A URL is accepted so a local or staging instance can be measured without editing this table,
    but the aliases exist so the common case cannot be typo'd into pointing at the wrong backend.
    """
    if endpoint in ENDPOINTS:
        return ENDPOINTS[endpoint]
    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        return endpoint.rstrip("/")
    raise ValueError(
        f"unknown endpoint {endpoint!r}; use one of {sorted(ENDPOINTS)} or a full http(s) URL"
    )


class OmittedArmWithoutReason(ValueError):
    """A subset run left out a suite arm without saying why.

    Raised before any network traffic or output directory, so the refusal costs nothing.
    """


def resolve_omissions(
    datasets: list[str], omitted: dict[str, str | dict[str, Any]] | None
) -> dict[str, dict[str, Any]]:
    """Validate the operator's reasons for every ``SUITE_DATASETS`` arm not in ``datasets``.

    Returns one ``status="skipped"`` manifest entry per omitted arm. A reason is either a string or
    a record carrying a ``reason`` key (the shape of an ``excluded_arms.json`` entry); a record's
    other fields are kept verbatim under ``exclusion_record`` so measured evidence travels with the
    skip instead of living in a side file a reader may never open.

    Refuses three things: an omitted arm with no reason (the silent omission this exists to stop),
    a reason for an arm that IS selected (a contradiction that means the operator's intent is
    unclear), and a reason for a name that is not a suite arm (a typo that would otherwise leave
    the real arm unexplained).
    """
    omitted = dict(omitted or {})
    unknown = sorted(k for k in omitted if k not in SUITE_DATASETS)
    if unknown:
        raise OmittedArmWithoutReason(
            f"skip reason(s) given for {unknown}, which are not suite arms. "
            f"Suite arms: {SUITE_DATASETS}."
        )
    contradictory = sorted(k for k in omitted if k in datasets)
    if contradictory:
        raise OmittedArmWithoutReason(
            f"arm(s) {contradictory} are both selected to run and given a skip reason. "
            f"Drop one or the other."
        )

    entries: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for key in SUITE_DATASETS:
        if key in datasets:
            continue
        given = omitted.get(key)
        record = dict(given) if isinstance(given, dict) else {}
        reason = record.pop("reason", None) if isinstance(given, dict) else given
        if not isinstance(reason, str) or not reason.strip():
            missing.append(key)
            continue
        entry: dict[str, Any] = {
            "dataset": key,
            "status": "skipped",
            "reason": reason.strip(),
            "skip_origin": "operator",
        }
        # The record's own arm/status restate the entry; everything else is evidence.
        for restated in ("arm", "dataset", "status"):
            record.pop(restated, None)
        if record:
            entry["exclusion_record"] = record
        entries[key] = entry
    if missing:
        raise OmittedArmWithoutReason(
            f"this run omits suite arm(s) {missing} without a reason. An arm left out of a subset "
            f"run is recorded in the manifest as skipped WITH A REASON, never dropped silently. "
            f"Give one per arm (CLI: --skip ARM='why' or --exclusions FILE)."
        )
    return entries


def run_suite(
    out_dir: Path | str | None = None,
    *,
    datasets: list[str] | None = None,
    omitted: dict[str, str | dict[str, Any]] | None = None,
    endpoint: str = "production",
    api_key: str | None = None,
    kestrel_url: str = DEFAULT_KESTREL_URL,
    runners: dict[str, Any] | None = None,
    probe_live: bool = True,
    batch_size: int = 20,
) -> dict[str, Any]:
    """Run the suite and return ``{"out_dir", "manifest", "results"}``.

    ``runners`` is injectable so the aggregation logic is testable offline without any network.

    ``omitted`` maps every ``SUITE_DATASETS`` arm NOT in ``datasets`` to the operator's reason
    (see ``resolve_omissions``). A subset run without a reason for each omitted arm raises
    ``OmittedArmWithoutReason`` before anything is fetched or written.
    """
    resolved_endpoint = resolve_endpoint(endpoint)
    runners = ARM_RUNNERS if runners is None else runners
    requested = list(SUITE_DATASETS if datasets is None else datasets)
    # First occurrence wins, order kept. A repeated name (``--only hgnc hgnc``) otherwise ran the
    # arm twice and wrote two manifest entries for it, double-counting it in n_ok.
    datasets = list(dict.fromkeys(requested))
    if len(datasets) != len(requested):
        logger.warning(
            "arm(s) %s named more than once; each runs once.",
            sorted({k for k in requested if requested.count(k) > 1}),
        )
    operator_skips = resolve_omissions(datasets, omitted)

    run_id = new_run_id("suite")
    suite_dir = (
        Path(out_dir) if out_dir is not None else DEFAULT_SUITE_ROOT / f"suite_{utc_stamp()}"
    )
    suite_dir.mkdir(parents=True, exist_ok=True)

    # Pin the backend BEFORE any arm runs.
    provenance = build_run_provenance(
        api_endpoint=resolved_endpoint,
        kestrel_url=kestrel_url,
        run_id=run_id,
        probe_live=probe_live,
    )
    if probe_live and not provenance.pinned:
        # Not fatal — the arms can still run — but a suite whose numbers cannot be attributed to a
        # graph is worse than one with no pins, because "unknown" still looks like provenance.
        logger.warning(
            "Kestrel provenance is UNPINNED for this suite (%s). Every number will record "
            "kestrel_version/kg_version as 'unknown'.",
            provenance.health_error,
        )

    results: list[dict[str, Any]] = []
    for key in datasets:
        runner = runners.get(key)
        if runner is None:
            results.append({"dataset": key, "status": "skipped", "reason": "no runner registered"})
            continue
        mapper = ApiMapper(resolved_endpoint, api_key=api_key, batch_size=batch_size)
        try:
            record = runner(
                mapper=mapper,
                out_dir=suite_dir / key,
                provenance=provenance,
                kestrel_url=kestrel_url,
            )
            arm_status = record.get("arm_status")
            # An arm with several sub-arms (MetaBench subgroups, MetaboliteAnnotator ion modes)
            # can complete one and fail another. Reporting that as "ok" made n_ok count it, left
            # n_failed at zero and let the CLI exit green while part of the declared benchmark was
            # missing. "partial" is its own status for exactly that case: it is not a success, and
            # it is not the same as an arm that produced nothing.
            failed_sub_arms = (
                sorted(k for k, v in arm_status.items() if v != "ok") if arm_status else []
            )
            entry = {
                "dataset": key,
                "status": "partial" if failed_sub_arms else "ok",
                "out_dir": record.get("out_dir", ""),
                "role": record.get("role"),
                "headline": _headline(record),
            }
            if arm_status is not None:
                entry["arm_status"] = arm_status
            if failed_sub_arms:
                entry["failed_sub_arms"] = failed_sub_arms
                entry["reason"] = (
                    f"{len(failed_sub_arms)} of {len(arm_status)} sub-arm(s) failed "
                    f"({', '.join(failed_sub_arms)}); the reported numbers cover only the "
                    f"sub-arms that completed."
                )
            entry["request_counters"] = mapper.counters.snapshot()
            results.append(entry)
        except SourceUnavailable as exc:
            # The load-bearing distinction: unsourceable is a SKIP WITH A REASON, never an empty
            # success and never a failure that reads as a bug in the harness.
            results.append(
                {
                    "dataset": key,
                    "status": "skipped",
                    "reason": exc.reason,
                    "request_counters": mapper.counters.snapshot(),
                }
            )
        except Exception as exc:  # noqa: BLE001 — a single failing arm must not abort the suite
            logger.exception("Arm %s failed", key)
            results.append(
                {
                    "dataset": key,
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "request_counters": mapper.counters.snapshot(),
                }
            )

    results.extend(operator_skips.values())
    for key, reason in SUITE_SKIPPED.items():
        if key not in datasets:
            results.append(
                {"dataset": key, "status": "skipped", "reason": reason, "skip_origin": "registry"}
            )

    kg = provenance.kg_build
    manifest: dict[str, Any] = {
        "run_id": run_id,
        "suite_out_dir": str(suite_dir),
        "created": utc_stamp(),
        "pins": {
            "api_endpoint": resolved_endpoint,
            "endpoint_alias": endpoint if endpoint in ENDPOINTS else None,
            "authenticated": bool(api_key),
            "biomapper_version": provenance.biomapper_version,
            "client_git_commit": provenance.client_git_commit,
            "client_git_dirty": provenance.client_git_dirty,
            "kestrel_url": kestrel_url,
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
        },
        # Which arms may be quoted as accuracy and which are coverage, derived from the build's
        # own ingested-source list rather than asserted from memory.
        "circularity": circularity_notes(kg, datasets),
        "datasets": results,
        "n_ok": sum(1 for r in results if r["status"] == "ok"),
        "n_partial": sum(1 for r in results if r["status"] == "partial"),
        "n_failed": sum(1 for r in results if r["status"] == "failed"),
        "n_skipped": sum(1 for r in results if r["status"] == "skipped"),
        # True only when every attempted arm completed every sub-arm. A scheduled run should read
        # this rather than n_failed alone.
        "complete": not any(r["status"] in ("failed", "partial") for r in results),
        # False when the operator left suite arms out. A subset run can be complete (nothing it
        # attempted broke) and still not be the full benchmark; the two are different questions.
        "full_suite": not operator_skips,
        "operator_skipped": sorted(operator_skips),
    }

    if probe_live:
        # Re-read the build now the arms are done. If it moved, the pins above no longer describe
        # every result and a reader must know that before treating the suite as one measurement.
        end_version, end_kg, _ = fetch_kg_build_info(kestrel_url)
        before = (provenance.kestrel_version, kg.kg_version, kg.git_commit)
        after = (end_version, end_kg.kg_version, end_kg.git_commit)
        manifest["kg_stable_during_run"] = before == after
        if before != after:
            manifest["kg_at_end"] = {
                "kestrel_version": end_version,
                "kg_version": end_kg.kg_version,
                "kg_git_commit": end_kg.git_commit,
            }

    (suite_dir / "suite_manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    (suite_dir / "README.md").write_text(_suite_readme(manifest))
    return {"out_dir": str(suite_dir), "manifest": manifest, "results": results}


# How strong a claim each label makes, weakest first. Used to reconcile the two label sources.
CLAIM_STRENGTH: dict[str, int] = {
    "coverage": 0,
    "capability_regression": 0,
    "partly_circular": 1,
    "accuracy_candidate": 2,
    "accuracy": 3,
}


def weakest_claim(declared: str, circularity: str) -> str:
    """Reconcile the arm's declared role with the per-run circularity verdict, conservatively.

    Two sources disagree for opposite reasons and neither can simply win.

    ``role`` is a static config field that DEFAULTS to ``"accuracy"``, so it overstates whenever an
    arm's author did not set it: that is how the README called RefMet accuracy while the same run's
    circularity register called it coverage.

    But ``circularity`` only asks whether the arm's gold source is ingested into the graph. It
    cannot see an arm that is coverage *by construction* regardless of provenance, such as
    MetaboliteAnnotator, whose headline is a name-hit rate that measures whether an identifier was
    produced rather than whether it was right. For that arm circularity reports
    ``accuracy_candidate`` while the runner correctly declares ``coverage``.

    So take the WEAKER claim. Overstating a coverage number as accuracy is the error that actually
    misleads a reader; understating an accuracy number is merely conservative. Unknown labels sort
    as the weakest, because an unrecognized label is not evidence for a strong claim.
    """
    candidates = [label for label in (declared, circularity) if label]
    if not candidates:
        return ""
    return min(candidates, key=lambda label: CLAIM_STRENGTH.get(label, -1))


def label_basis(declared: str, circularity: str) -> str:
    """WHICH source decided the label, per arm. Deliberately not why that source decided it.

    The label has two possible origins and they mean different things, so a single generalization
    in the report prose is guaranteed to be wrong for some arm. MetaboliteAnnotator is labelled
    coverage because its headline is a NAME-HIT RATE, while RefMet is labelled coverage because its
    gold source is INGESTED into the graph. Telling every reader "coverage means the gold source was
    ingested" is false for the first.

    This names the deciding source and stops there. An earlier version tried to state the
    underlying reason too and got it wrong in both directions: it reported "gold source is ingested"
    for Hajjar, whose circularity verdict is ``accuracy_candidate`` precisely because NO gold source
    of that arm is in the build, and "metric is coverage by construction" for MetaBench, which is
    ``partly_circular`` on xref provenance rather than on being a coverage metric. The per-arm
    reason already exists, verbatim and derived from the build, in the manifest's ``circularity``
    register; duplicating it in prose only creates a second copy that can be wrong.
    """
    if not declared and not circularity:
        return ""
    if declared and circularity and declared != circularity:
        return (
            "per-run circularity verdict"
            if weakest_claim(declared, circularity) == circularity
            else "arm's declared role"
        )
    if circularity and not declared:
        return "per-run circularity verdict"
    if declared and not circularity:
        return "arm's declared role"
    return "both sources agree"


def _headline(record: dict[str, Any]) -> dict[str, Any]:
    """The arm's quotable numbers, extracted for the aggregate manifest.

    Deliberately carries the role alongside the number. A coverage arm's figure and an accuracy
    arm's figure are not comparable, and a manifest that lists them in one column without the
    label invites exactly that comparison.
    """
    result = record.get("results") or {}
    out: dict[str, Any] = {"role": record.get("role")}
    # The published strict figure comes FIRST and is carried unconditionally. Omitting it here
    # while the adjacent README advertises it would leave a reader of the aggregate manifest
    # unable to retrieve the one number the report tells them to quote.
    strict = result.get("comparable_core_strict_kg_only")
    if isinstance(strict, dict):
        out["comparable_core_strict_kg_only"] = strict
    core = result.get("comparable_core")
    if isinstance(core, dict):
        out["comparable_core"] = core
    for extra in ("comparable_core_kg_equivalence_set", "comparable_core_charge_normalized"):
        if isinstance(result.get(extra), dict):
            out[extra] = result[extra]
    if "per_namespace_accuracy" in result:
        out["per_namespace_accuracy"] = result["per_namespace_accuracy"]
        out["reportable_metric"] = "per_namespace_accuracy"
    if "unambiguous_accuracy" in result:
        out["unambiguous_accuracy"] = (result["unambiguous_accuracy"] or {}).get(
            "per_namespace_accuracy"
        )
        out["ambiguous_flagrate"] = (result["ambiguous_flagrate"] or {}).get("comparable_core")
    if "capability_gate" in result:
        out["capability_gate"] = result["capability_gate"]
    if "entries" in result:
        out["entries"] = [
            {"key": e.get("key"), "comparable_core": (e.get("result") or {}).get("comparable_core")}
            for e in result["entries"]
        ]
    return out


def _table_cell(text: str) -> str:
    """Make free text safe inside one Markdown table cell.

    Skip reasons are operator-supplied, so a ``|`` or a newline in one would split the row or
    fabricate an extra one, garbling exactly the account of what was skipped and why.
    """
    return " ".join(str(text).split()).replace("|", "\\|")


def _suite_readme(manifest: dict[str, Any]) -> str:
    """A short human-readable index beside the machine-readable manifest.

    Exists so the first thing a reader opens states the backend, the build, and which arms are
    coverage rather than accuracy — the three things most often lost between a run and a write-up.
    """
    pins = manifest["pins"]
    lines = [
        "# External benchmark suite run",
        "",
        f"- Run id: `{manifest['run_id']}`",
        f"- Created: {manifest['created']}",
        f"- API endpoint: {pins['api_endpoint']}",
        f"- Kestrel: {pins['kestrel_url']} (service {pins['kestrel_version']})",
        f"- KG build: {pins['kg_version']} / biolink {pins['biolink_version']}"
        f" / commit {pins['kg_git_commit']}",
        f"- Provenance pinned: {pins['provenance_pinned']}",
        f"- KG stable during run: {manifest.get('kg_stable_during_run', 'not probed')}",
        "",
        f"{manifest['n_ok']} ok, {manifest['n_partial']} partial, {manifest['n_failed']} failed, "
        f"{manifest['n_skipped']} skipped.",
        "",
        (
            "Scope: full suite."
            if manifest.get("full_suite", True)
            else f"Scope: SUBSET. The operator omitted {', '.join(manifest['operator_skipped'])}; "
            f"each is a skipped row below with the operator's reason. This run is not the full "
            f"benchmark."
        ),
        "",
        "## Arms",
        "",
        "| arm | status | label | why this label | declared role | note |",
        "|---|---|---|---|---|---|",
    ]
    for entry in manifest["datasets"]:
        circ = (manifest["circularity"].get(entry["dataset"], {}) or {}).get("label", "")
        if entry["status"] == "skipped":
            # A skipped arm measured nothing, so it has no claim to label. Printing the arm's
            # circularity verdict here read as if a skipped arm had an accuracy_candidate result.
            circ = ""
        declared = entry.get("role") or ""
        flag = " **(disagrees)**" if circ and declared and circ != declared else ""
        note = _table_cell(entry.get("reason") or entry.get("error") or "")
        lines.append(
            f"| {entry['dataset']} | {entry['status']} | "
            f"{weakest_claim(declared, circ) or 'n/a'} | "
            f"{label_basis(declared, circ) or 'n/a'} | {declared or 'n/a'}{flag} | {note} |"
        )
    lines += [
        "",
        "## Reading these numbers",
        "",
        "- An arm labelled `coverage` measures whether an identifier was produced, not whether it",
        "  was right, and must not be quoted as accuracy. There are TWO distinct reasons an arm",
        "  earns that label:",
        "    - its gold source is ingested into the graph being measured, so the gold and the",
        "      answer share a source (RefMet, LMSD, the gene arms); or",
        "    - the headline metric is a coverage measure by construction, such as a name-hit rate,",
        "      regardless of where the gold came from (MetaboliteAnnotator).",
        "  Do not assume the first reason: it is false for a coverage-by-construction arm. The",
        "  `why this label` column names which SOURCE decided, and the manifest's `circularity`",
        "  block carries that source's own per-arm reason verbatim. Read the reason there rather",
        "  than inferring it from the label.",
        "- Gene arms report accuracy PER TARGET NAMESPACE. The any-namespace roll-up is emitted",
        "  flagged non-quotable.",
        "- A `skipped` arm has a reason. It is not a zero and not a pass.",
        "- A `partial` arm completed some sub-arms and not others. Its numbers cover only what",
        "  completed, so they are not the full benchmark.",
        "- Where `label` and `declared role` disagree, `label` is the WEAKER of the two claims.",
        "  Neither source can simply win: `declared role` is a static config field that defaults",
        "  to `accuracy` and so overstates when unset, while the circularity verdict only asks",
        "  whether the gold source was ingested and cannot see an arm that is coverage by",
        "  construction. Overstating coverage as accuracy is the error that misleads a reader, so",
        "  the weaker claim is taken and its origin is recorded per row.",
        "",
        "## Which structure number is 'strict'",
        "",
        "Structure-oracle arms report three figures on the same scored rows. They are NOT",
        "interchangeable, and one of them is the published one:",
        "",
        "- `comparable_core_strict_kg_only` is **the published strict figure**. The chosen node's",
        "  own InChIKey matched. A row whose structure came from the external name lookup is a",
        "  miss, because the graph did not supply it.",
        "- `comparable_core` is the same measurement with a Metabolomics Workbench or PubChem",
        "  lookup on the node's NAME allowed to fill in a structure-less node. Report it as the",
        "  name-fallback variant in a methods note. It is **not** 'strict', though it was the",
        "  headline historically, which is how one word came to mean two numbers.",
        "- `comparable_core_kg_equivalence_set` counts a match against ANY connectivity the node",
        "  asserts, so it is partly a measure of the graph's curation.",
        "",
        "Each carries `definition` and `is_published_strict` so the distinction survives being",
        "read out of the JSON by someone who was not in the decision.",
        "",
        f"Generated {dt.datetime.now(dt.UTC).isoformat()}.",
    ]
    return "\n".join(lines) + "\n"
