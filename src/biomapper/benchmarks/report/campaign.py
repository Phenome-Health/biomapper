"""Campaign report assembler — NECS + gene/protein backbones (the deferred follow-on).

Distinct from ``assemble.py`` (the Hajjar internal report) in three ways, each a Hajjar
calibration learning folded in:

  1. **One accuracy number per dataset, no per-vocab axis.** The Hajjar run proved
     ``chosen_kg_id`` is annotation-driven, not vocab-steered, so a per-vocab heatmap is
     uninformative. Each dataset contributes exactly one headline accuracy.
  2. **Two arms, two correctness rules.** Metabolite (NECS) uses the structure oracle and
     reports BOTH the strict InChIKey-first-block accuracy AND the charge/protonation-normalized
     accuracy. Gene/protein (backbones) uses CURIE equality (Top-1 + coverage/precision/recall/F1).
  3. **No competitor figure/column.** NECS and the backbones have NO published same-set
     competitor numbers, so — unlike Hajjar — only BioMapper-vs-reference/oracle is reported.
     Nothing is fabricated (no S2, no per-tool spread).

Internal only; never invokes /publish-wiki. Every number is sourced from a validated results
bundle passed in — nothing is typed by hand.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from biomapper.benchmarks.scorers.structure_oracle_scorer import (
    STRICT_KG_ONLY_DEFINITION,
)

CAMPAIGN_FRAMING = (
    "Deferred follow-on to the Hajjar vertical slice. Metabolite arm (NECS) is scored by the "
    "independent InChIKey structure oracle (strict + charge-normalized); the gene/protein arm "
    "(HGNC / UniProt idmapping / NCBI gene2ensembl) is scored by CURIE equality against each "
    "backbone's authoritative held-out cross-references, reported per target namespace (the "
    "any-namespace roll-up is not quotable). No competitor comparison exists for these sets, so none is drawn."
)


#: Printed in a "Top-1 (strict)" cell when the artifact cannot supply the published strict figure.
#: Deliberately not a number and not blank: the name-fallback value is the wrong answer here, and a
#: blank cell reads as zero to a skimming reader.
STRICT_UNAVAILABLE = "n/a (strict not in artifact)"


def _strict_core(result: dict[str, Any]) -> dict[str, Any] | None:
    """The figure that belongs under a column headed "strict", or ``None`` if there isn't one.

    ``comparable_core`` is the name-fallback variant, so substituting it here is the very error
    this function exists to prevent: on Hajjar-100 it is 95/100 where the published strict figure
    is 92/100, and a reader of the column has no way to tell which they were handed.

    Three cases, in order:

    1. ``comparable_core_strict_kg_only`` is present. Use it.
    2. It is absent, but ``per_row`` is present. RECOMPUTE it: a row is strict-correct when it was
       correct and did not need the name fallback, which is exactly how the figure was derived by
       hand from older artifacts. The denominator is unchanged, so the recomputed value is
       comparable with the stored variants.
    3. Neither is available. Return ``None``, and the caller prints an explicit gap. An empty cell
       is recoverable; a wrong number under a "strict" heading is not.
    """
    strict = result.get("comparable_core_strict_kg_only")
    if isinstance(strict, dict):
        return strict

    per_row = result.get("per_row")
    core = result.get("comparable_core")
    if not isinstance(per_row, list) or not isinstance(core, dict):
        return None

    scored = [row for row in per_row if row.get("scored")]
    if not scored:
        return None
    correct = sum(1 for row in scored if row.get("correct") and not row.get("needed_fallback"))
    return {
        "metric": "top1_accuracy_strict_kg_only",
        "top1_accuracy": correct / len(scored),
        "correct": correct,
        "scored_denominator": len(scored),
        "definition": STRICT_KG_ONLY_DEFINITION,
        "is_published_strict": True,
        "recomputed_from_per_row": True,
    }


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


# Human labels for the name-source regimes emitted by the structure-oracle scorer.
REGIME_LABELS: dict[str, str] = {
    "shorthand": "shorthand (ABBREVIATION)",
    "common_systematic": "common / systematic",
}

# Both the strict and charge-normalized Top-1 columns are reported per regime. The design
# expectation was that they would COINCIDE for lipids (misses being connectivity errors — chain
# length / double-bond position / headgroup — not protonation), but the ~20-name live smoke
# contradicted that: charge-normalization recovered extra matches (blended 57.1% vs strict 42.9%),
# i.e. some lipid misses ARE charge-state differences. Lipids with fixed-charge headgroups (e.g. the
# phosphocholine [O-]...[N+] of glycerophospholipids) can carry an explicit charge in the source
# SMILES that shifts the InChIKey first block relative to the KG's neutral form; neutralizing charge
# collapses those. So the two columns may DIFFER — read strict as the conservative floor and the gap
# (strict -> charge-normalized) as protonation/charge-state recoveries, not connectivity fixes.
LIPID_CHARGE_NORM_NOTE = (
    "Strict and charge-normalized Top-1 are BOTH reported per regime and may differ for lipids: "
    "charge-normalization neutralizes protonation/charge state before the InChIKey-connectivity "
    "comparison, so lipids with fixed-charge headgroups (e.g. phosphocholine) whose source SMILES "
    "carries an explicit charge can be recovered by the charge-normalized column that the strict "
    "first-block comparison misses. Read strict as the conservative floor; the gap to charge-"
    "normalized is charge-state recoveries, not connectivity fixes. (The ~20-name live smoke showed "
    "charge-norm above strict — the initial 'the two coincide' expectation did not hold.)"
)


def _name_source_regime_rows(entry: dict[str, Any]) -> list[str]:
    """One table row per name-source regime for a metabolite entry that carries the breakout.

    Shorthand (the hard class) is listed first; any unexpected regime keys follow in a stable order.
    Returns ``[]`` for entries without a ``by_name_source_regime`` block (all non-LMSD arms).
    """
    by = entry["result"].get("by_name_source_regime")
    if not by:
        return []
    preferred = ["shorthand", "common_systematic"]
    keys = [k for k in preferred if k in by] + sorted(k for k in by if k not in preferred)
    rows: list[str] = []
    for k in keys:
        r = by[k]
        core = _strict_core(r)
        cn = r.get("comparable_core_charge_normalized")
        cn_acc = _pct(cn["top1_accuracy"]) if cn else "n/a"
        cov = r.get("coverage", {})
        strict_acc = _pct(core["top1_accuracy"]) if core else STRICT_UNAVAILABLE
        scored_n = core["scored_denominator"] if core else "n/a"
        rows.append(
            f"| {entry['key']} | {REGIME_LABELS.get(k, k)} | {strict_acc} | {cn_acc} | "
            f"{scored_n} | {cov.get('n_predicted', '?')}/{cov.get('total', '?')} |"
        )
    return rows


def _metabolite_row(entry: dict[str, Any]) -> str:
    core = _strict_core(entry["result"])
    cn = entry["result"].get("comparable_core_charge_normalized")
    cn_acc = _pct(cn["top1_accuracy"]) if cn else "n/a"
    cov = entry["result"]["coverage"]
    fb = entry["result"].get("fallback_bucket", {})
    strict_acc = _pct(core["top1_accuracy"]) if core else STRICT_UNAVAILABLE
    scored_n = core["scored_denominator"] if core else "n/a"
    return (
        f"| {entry['key']} | metabolite | {strict_acc} | {cn_acc} | "
        f"{scored_n} | {cov['n_predicted']}/{cov['total']} | {fb.get('count', 0)} |"
    )


def _curie_row(entry: dict[str, Any]) -> str:
    """Table row(s) for a gene/protein entry: one per target namespace when the result has them.

    The any-namespace ``comparable_core`` is non-quotable, so it is only printed for a result that
    predates per-namespace scoring. Precision/recall/F1 are derived from that roll-up, so they are
    not repeated on the per-namespace rows.
    """
    per_ns = entry["result"].get("per_namespace_accuracy")
    if isinstance(per_ns, dict) and per_ns:
        cov = entry["result"]["coverage"]
        return "\n".join(
            f"| {entry['key']} ({ns}) | {entry.get('arm', 'gene/protein')} | "
            f"{_pct(e.get('top1_accuracy'))} | {e.get('scored_denominator')} | "
            f"{cov['n_predicted']}/{cov['total']} | n/a | n/a | n/a |"
            for ns, e in per_ns.items()
        )
    core = entry["result"]["comparable_core"]
    stats = entry["result"].get("curie_stats", {})
    cov = entry["result"]["coverage"]
    return (
        f"| {entry['key']} | {entry.get('arm', 'gene/protein')} | {_pct(core['top1_accuracy'])} | "
        f"{core['scored_denominator']} | {cov['n_predicted']}/{cov['total']} | "
        f"{_pct(stats.get('precision'))} | {_pct(stats.get('recall'))} | {_pct(stats.get('f1'))} |"
    )


def _flagrate_row(entry: dict[str, Any]) -> str:
    """One row for an ambiguous-partition flag-rate entry (score_nlmgene_ambiguity output)."""
    core = entry["result"]["comparable_core"]
    member = entry["result"].get("member_when_committed")
    return (
        f"| {entry['key']} | {entry.get('arm', 'gene')} | {_pct(core['flag_rate'])} | "
        f"{core['flagged']}/{core['n_ambiguous']} | {_pct(entry['result'].get('silent_over_commit_rate'))} | "
        f"{_pct(member)} | {entry['result'].get('committed', 0)} |"
    )


def assemble_campaign_report(
    *,
    metabolite_entries: list[dict[str, Any]],
    curie_entries: list[dict[str, Any]],
    flagrate_entries: list[dict[str, Any]] | None = None,
    integrity: dict[str, Any] | None = None,
    out_path: str | Path,
) -> str:
    """Assemble + write the internal campaign report. Returns the markdown text.

    ``metabolite_entries`` items: ``{"key", "result": <score_structure_oracle output>}``.
    ``curie_entries`` items: ``{"key", "arm", "result": <score_curie output>}``.
    ``flagrate_entries`` items: ``{"key", "arm", "result": <score_nlmgene_ambiguity output>}`` —
    the AMBIGUOUS partition, reported as EITL flag-rate in its OWN table (never blended with accuracy).
    """
    integrity = integrity or {}
    lines: list[str] = []
    lines.append("# BioMapper External Benchmarks — NECS + gene/protein backbones (internal)")
    lines.append("")
    lines.append("> INTERNAL — repo/vault only. Not for wiki publication this pass.")
    lines.append("")
    lines.append("## Framing")
    lines.append("")
    lines.append(CAMPAIGN_FRAMING)
    lines.append("")

    if metabolite_entries:
        lines.append("## Metabolite arm — structure-oracle Top-1 accuracy (one number per dataset)")
        lines.append("")
        lines.append(
            "| Dataset | Arm | Top-1 (strict) | Top-1 (charge-normalized) | Scored n | Coverage | Fallback bucket |"
        )
        lines.append("|---|---|---|---|---|---|---|")
        for entry in metabolite_entries:
            lines.append(_metabolite_row(entry))
        lines.append("")

        # Name-source regime breakout (LMSD lipid arm): the blended number above averages two very
        # different input populations, so break it out where the scorer supplied the split.
        regime_entries = [e for e in metabolite_entries if e["result"].get("by_name_source_regime")]
        if regime_entries:
            lines.append("### Name-source regime breakout (shorthand vs common/systematic)")
            lines.append("")
            lines.append(
                "The blended Top-1 above averages two very different input populations. The lipid "
                "shorthand `ABBREVIATION` (e.g. `TG 57:6`) is the hardest name->structure class and "
                "dominates the sample (~90%); common/systematic names are easier. Broken out here per "
                "regime; the blended overall is retained above for continuity."
            )
            lines.append("")
            lines.append(
                "| Dataset | Name-source regime | Top-1 (strict) | Top-1 (charge-normalized) | Scored n | Coverage |"
            )
            lines.append("|---|---|---|---|---|---|")
            for entry in regime_entries:
                lines.extend(_name_source_regime_rows(entry))
            lines.append("")
            lines.append(f"- {LIPID_CHARGE_NORM_NOTE}")
            lines.append("")

    if curie_entries:
        lines.append("## Gene/protein arm — CURIE-equality accuracy (per target namespace)")
        lines.append("")
        lines.append(
            "| Dataset | Arm | Top-1 accuracy | Scored n | Coverage | Precision | Recall | F1 |"
        )
        lines.append("|---|---|---|---|---|---|---|---|")
        for entry in curie_entries:
            lines.append(_curie_row(entry))
        lines.append("")

    flagrate_entries = flagrate_entries or []
    if flagrate_entries:
        lines.append("## Gene/protein arm — ambiguous partition (EITL flag-rate; NOT accuracy)")
        lines.append("")
        lines.append(
            "Ambiguous surface forms denote >=2 genes with NO single correct answer absent context; "
            "the correct behavior is to ABSTAIN / route to expert-in-the-loop, not to emit one "
            "confident id. This is scored SEPARATELY from accuracy and MUST NOT be blended into it. "
            "Flag == abstain (no chosen_kg_id) until a first-class EITL flag exists (arbitration "
            "workstream); 'silent over-commit' is a confident id that is not even a legitimate referent."
        )
        lines.append("")
        lines.append(
            "| Dataset | Arm | Flag-rate (abstain) | Flagged n | Silent over-commit | "
            "Member when committed | Committed |"
        )
        lines.append("|---|---|---|---|---|---|---|")
        for entry in flagrate_entries:
            lines.append(_flagrate_row(entry))
        lines.append("")

    lines.append("## Notes")
    lines.append("")
    lines.append(
        "- No published same-set competitor exists for NECS or the backbones — no competitor figure is drawn."
    )
    lines.append(
        "- Per-vocab breakdown is intentionally omitted (annotation-driven, not vocab-steered)."
    )
    lines.append(f"- Reconciliation passed: {integrity.get('reconciliation_passed')}")
    lines.append(f"- Validation passed: {integrity.get('validation_passed')}")
    lines.append("")

    text = "\n".join(lines)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text)
    return text
