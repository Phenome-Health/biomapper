"""MetaBench report assembler — BioMapper's number alongside the 25-LLM baseline distribution.

MetaBench is the ONE external set with a valid LLM head-to-head: the paper (Lu et al. 2025,
arXiv:2510.14944) scores 25 open/closed LLMs on the SAME 1,000 grounding pairs. So — unlike NECS
and the gene/protein backbones, which have no same-set competitor — this report DOES place
BioMapper's single accuracy against a published baseline distribution.

CITATION DISCIPLINE (Metabolon-96.5% scar): the baseline numbers are transcribed, not computed.
Every ``CompetitorResult`` arrives with ``value=None`` (needs-verification) plus a DOI + table_ref;
this report renders those as "needs verification (transcribe from table)" rather than asserting a
from-memory figure. ``validate.citation_spot_check`` refuses any baseline missing a DOI/table_ref.

Internal only; never invokes /publish-wiki.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from biomapper.benchmarks.config import CompetitorResult, MetaBenchDatasetConfig

METABENCH_FRAMING = (
    "MetaBench (Lu et al. 2025, arXiv:2510.14944) Grounding task: 1,000 bidirectional cross-database "
    "ID<->ID / name->ID pairs (HMDB / KEGG / ChEBI), scored by CURIE-equality exact match. This is "
    "the ONE external dataset with a valid same-set LLM head-to-head — the paper reports 25 LLM "
    "baselines on these exact pairs — so BioMapper's number is placed alongside that published "
    "distribution. ONE accuracy over all 1,000 pairs (ID->ID scored in provided-ID mode, name->ID in "
    "name-input mode; no per-vocab axis). Baseline figures are TRANSCRIBED with citation discipline "
    "(left as needs-verification here), not asserted from memory."
)


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def assemble_metabench_report(
    *,
    config: MetaBenchDatasetConfig,
    result: dict[str, Any],
    card: dict[str, Any],
    baselines: Iterable[CompetitorResult],
    integrity: dict[str, Any] | None = None,
    out_path: str | Path,
) -> str:
    """Build + write the internal MetaBench report. Returns the markdown text."""
    integrity = integrity or {}
    baselines = list(baselines)
    core = result["comparable_core"]
    cov = result["coverage"]
    stats = result.get("curie_stats", {})

    lines: list[str] = []
    lines.append(f"# BioMapper External Benchmark — {config.key} (internal)")
    lines.append("")
    lines.append("> INTERNAL — repo/vault only. Not for wiki publication this pass.")
    lines.append("")
    lines.append("## Framing")
    lines.append("")
    lines.append(METABENCH_FRAMING)
    lines.append("")
    lines.append(
        f"- Dataset: **{config.key}** ({config.arm} arm, input_type=`{config.input_type}`), "
        f"source DOI [{config.source_doi}](https://doi.org/{config.source_doi})."
    )
    lines.append(
        f"- License: {config.license}. N={card.get('n_rows')} "
        f"(ID->ID={card.get('n_id2id')}, name->ID={card.get('n_name2id')})."
    )
    lines.append("")

    lines.append("## BioMapper — one accuracy over all 1,000 grounding pairs")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Top-1 accuracy (CURIE exact match) | {_pct(core['top1_accuracy'])} |")
    lines.append(f"| Scored n | {core['scored_denominator']} |")
    lines.append(f"| Coverage | {cov['n_predicted']}/{cov['total']} |")
    lines.append(f"| Precision | {_pct(stats.get('precision'))} |")
    lines.append(f"| Recall | {_pct(stats.get('recall'))} |")
    lines.append(f"| F1 | {_pct(stats.get('f1'))} |")
    lines.append("")

    lines.append("## Published LLM baselines on the SAME 1,000-pair set (25-model distribution)")
    lines.append("")
    lines.append("| Baseline | Value | Metric | Source |")
    lines.append("|---|---|---|---|")
    for b in baselines:
        val = "needs verification (transcribe from table)" if b.value is None else _pct(b.value)
        lines.append(f"| {b.tool} | {val} | {b.metric} | [{b.doi}](https://doi.org/{b.doi}) — {b.table_ref} |")
    lines.append("")

    lines.append("## Integrity notes")
    lines.append("")
    lines.append(f"- Source SHA256: `{card.get('source_sha256')}`")
    lines.append(f"- Expected SHA256 (acquisition pin): `{card.get('expected_source_sha256')}`")
    lines.append(f"- Reconciliation passed: {integrity.get('reconciliation_passed')}")
    lines.append(f"- Validation passed: {integrity.get('validation_passed')}")
    lines.append(
        "- Baseline figures are left as needs-verification: transcribe each from the paper's "
        "Grounding-task table before asserting any number (Metabolon-96.5% scar)."
    )
    lines.append("")

    text = "\n".join(lines)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text)
    return text
