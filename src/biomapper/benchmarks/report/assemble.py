"""Unit 6 — internal report assembler.

Assembles an **internal** markdown report (repo/vault), framed explicitly as
parity-establishment on solved cases — NOT differentiation evidence. Every number is
sourced from a validated results bundle or a DOI-cited competitor entry; nothing is typed
in by hand. This module never calls /publish-wiki (R7): wiki publication is deferred until
hard-case differentiation numbers can accompany it.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from biomapper.benchmarks.config import CompetitorResult, DatasetConfig

PARITY_FRAMING = (
    "This is a **parity-establishment and harness-validation** pass on solved cases. It is "
    "explicitly NOT the differentiation evidence for BioMapper — that lives in the hard-case "
    "gold set / EITL campaign. Do not anchor BioMapper's external story on these numbers."
)


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def assemble_report(
    *,
    config: DatasetConfig,
    per_vocab_results: dict[str, dict[str, Any]],
    paper_metrics: dict[str, dict[str, Any]],
    competitors: Iterable[CompetitorResult],
    figure_paths: dict[str, str],
    integrity: dict[str, Any],
    out_path: str | Path,
) -> str:
    """Build + write the internal markdown report. Returns the markdown text."""
    competitors = list(competitors)
    lines: list[str] = []
    lines.append(f"# BioMapper External Benchmark — {config.key} (internal)")
    lines.append("")
    lines.append("> INTERNAL — repo/vault only. Not for wiki publication this pass (R7).")
    lines.append("")
    lines.append("## Framing")
    lines.append("")
    lines.append(PARITY_FRAMING)
    lines.append("")
    lines.append(
        f"- Dataset: **{config.key}** ({config.arm} arm, input_type=`{config.input_type}`), "
        f"source DOI [{config.source_doi}](https://doi.org/{config.source_doi})."
    )
    lines.append("")

    # Comparable core (structure-oracle Top-1) per vocab
    lines.append("## Comparable core — structure-oracle Top-1 accuracy (input_type=name)")
    lines.append("")
    lines.append("| Target vocab | Top-1 accuracy | Scored n | Coverage | Fallback bucket |")
    lines.append("|---|---|---|---|---|")
    for vocab, res in per_vocab_results.items():
        core = res["comparable_core"]
        cov = res["coverage"]
        fb = res.get("fallback_bucket", {})
        lines.append(
            f"| {vocab} | {_pct(core['top1_accuracy'])} | {core['scored_denominator']} | "
            f"{cov['n_predicted']}/{cov['total']} | {fb.get('count', 0)} |"
        )
    lines.append("")

    # Hajjar native match-rate (reported separately, never merged)
    lines.append("## Hajjar native metric — per-input match-rate (reported separately)")
    lines.append("")
    lines.append("| Target vocab | Match-rate | Matched | Total |")
    lines.append("|---|---|---|---|")
    for vocab, pm in paper_metrics.items():
        lines.append(f"| {vocab} | {_pct(pm['match_rate'])} | {pm['matched']} | {pm['total']} |")
    lines.append("")

    # Published competitors on the same 100-set (transcribed, DOI-cited)
    lines.append("## Published tools on the same 100-set (same-dataset comparison)")
    lines.append("")
    lines.append("| Tool | Value | Metric | Source |")
    lines.append("|---|---|---|---|")
    for c in competitors:
        val = "not transcribed" if c.value is None else _pct(c.value)
        lines.append(f"| {c.tool} | {val} | {c.metric} | [{c.doi}](https://doi.org/{c.doi}) — {c.table_ref} |")
    lines.append("")

    lines.append("## Figures")
    lines.append("")
    for label, path in figure_paths.items():
        lines.append(f"- {label}: `{path}`")
    lines.append("")

    lines.append("## Integrity notes")
    lines.append("")
    lines.append(f"- Reconciliation passed: {integrity.get('reconciliation_passed')}")
    lines.append(f"- Validation passed: {integrity.get('validation_passed')}")
    if integrity.get("protocol_parity") is not None:
        lines.append(f"- Protocol-parity gate: {integrity.get('protocol_parity')}")
    lines.append("- Coverage caveats / fallback buckets are reported per-vocab above.")
    lines.append("")

    text = "\n".join(lines)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text)
    return text
