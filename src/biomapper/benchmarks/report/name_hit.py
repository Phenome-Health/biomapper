"""Name-hit head-to-head report assembler (MetaboliteAnnotator arm).

Distinct from ``campaign.py`` in one way: this arm DOES have published same-set competitor numbers
(MetaboliteAnnotator 93.2%/93.5%, plus MetaboAnalyst 6.0 and metaboliteIDmapping), so — like Hajjar —
a competitor column is rendered. But following the CompetitorResult discipline (Metabolon-96.5% scar),
every competitor ``value`` is ``None`` in source control and renders as ``n/a (transcribe)`` until
verified against the paper's table at run time; nothing is fabricated from the abstract/memory.

ONE number per dataset (one ``name_hit_rate`` per ion mode). Internal only — never invokes
/publish-wiki. Every BioMapper number is sourced from a scored results bundle passed in.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from biomapper.benchmarks.config import CompetitorResult

NAME_HIT_FRAMING = (
    "Same-set, NAME-input head-to-head against MetaboliteAnnotator (Lu et al. 2026, J. Proteome Res., "
    "DOI 10.1021/acs.jproteome.5c00477). Metric = per-input name-hit-rate (fraction of input names for "
    "which BioMapper produced a target-vocab identifier), computed with the SAME protocol so the number "
    "lands beside the published MetaboliteAnnotator / MetaboAnalyst 6.0 / metaboliteIDmapping baselines. "
    "ONE number per ion mode. ID-concordance and charge-normalized structure concordance are reported as "
    "correctness qualifiers, never merged with the coverage-shaped hit-rate."
)


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _competitor_cell(competitors: Iterable[CompetitorResult], tool: str, mode: str) -> str:
    for c in competitors:
        if c.tool == tool and c.input_type == mode:
            return _pct(c.value) if c.value is not None else "n/a (transcribe)"
    return "n/a"


def assemble_name_hit_report(
    *,
    entries: list[dict[str, Any]],
    competitors: Iterable[CompetitorResult],
    integrity: dict[str, Any] | None = None,
    out_path: str | Path,
) -> str:
    """Assemble + write the internal name-hit head-to-head report. Returns the markdown text.

    ``entries`` items: ``{"key", "mode", "result": <score_name_hit output>}``.
    """
    integrity = integrity or {}
    competitors = list(competitors)
    baseline_tools = ("MetaboliteAnnotator", "MetaboAnalyst 6.0", "metaboliteIDmapping")

    lines: list[str] = []
    lines.append("# BioMapper External Benchmarks — MetaboliteAnnotator name-hit head-to-head (internal)")
    lines.append("")
    lines.append("> INTERNAL — repo/vault only. Not for wiki publication this pass.")
    lines.append("")
    lines.append("## Framing")
    lines.append("")
    lines.append(NAME_HIT_FRAMING)
    lines.append("")
    lines.append(
        f"> Accessions status: **{integrity.get('accessions_status', 'unknown')}** — the six MetaboLights "
        f"MTBLS accessions were not obtainable from the paper (ACS full text/SI blocked). Fill the real "
        f"ids before trusting a live run."
    )
    lines.append("")

    lines.append("## Name-hit-rate — BioMapper vs published same-set baselines (one number per mode)")
    lines.append("")
    header = (
        "| Mode | BioMapper hit-rate | Matched/Total | "
        + " | ".join(baseline_tools)
        + " | ID-concord. | ID-eq(UCI) | ID-eq(bridge) | Struct(cn) |"
    )
    lines.append(header)
    lines.append("|" + "---|" * (7 + len(baseline_tools)))
    for entry in entries:
        core = entry["result"]["comparable_core"]
        idc = entry["result"].get("id_concordance", {})
        uci = entry["result"].get("id_concordance_uci_equivalence")
        bridge = entry["result"].get("id_concordance_inchikey_bridge")
        cn = entry["result"].get("structure_concordance_charge_normalized")
        mode = entry.get("mode", core.get("mode", ""))
        comp_cells = " | ".join(_competitor_cell(competitors, t, mode) for t in baseline_tools)
        cn_cell = _pct(cn["concordance_rate"]) if cn else "n/a"
        uci_cell = _pct(uci["concordance_rate"]) if uci else "n/a"
        bridge_cell = _pct(bridge["concordance_rate"]) if bridge else "n/a"
        lines.append(
            f"| {mode} | {_pct(core['name_hit_rate'])} | {core['matched']}/{core['total']} | "
            f"{comp_cells} | {_pct(idc.get('concordance_rate'))} | {uci_cell} | {bridge_cell} | {cn_cell} |"
        )
    lines.append("")

    for entry in entries:
        matrix = entry["result"].get("namespace_confusion")
        if not matrix:
            continue
        mode = entry.get("mode", "")
        lines.append(f"## Namespace divergence — gold vs BioMapper-chosen ({mode})")
        lines.append("")
        lines.append("Rows the InChIKey bridge credits (right molecule) but strict exact-ID scored a miss.")
        lines.append("")
        chosen_ns = sorted({c for row in matrix.values() for c in row})
        lines.append("| gold ns \\\\ chosen ns | " + " | ".join(chosen_ns) + " |")
        lines.append("|" + "---|" * (len(chosen_ns) + 1))
        for gold_ns in sorted(matrix):
            cells = " | ".join(str(matrix[gold_ns].get(c, 0)) for c in chosen_ns)
            lines.append(f"| {gold_ns} | {cells} |")
        lines.append("")

    lines.append("## Notes")
    lines.append("")
    lines.append(
        "- Competitor values are transcribed from the paper's table at run time (value=None in source "
        "control); an unverified cell renders 'n/a (transcribe)' — no number is baked from the abstract."
    )
    lines.append(
        "- Hit-rate is coverage-shaped; ID-concordance / charge-normalized structure are correctness qualifiers."
    )
    lines.append(f"- Accessions status: {integrity.get('accessions_status')}")
    lines.append("")

    text = "\n".join(lines)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text)
    return text
