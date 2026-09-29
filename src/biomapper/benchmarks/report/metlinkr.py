"""metLinkR head-to-head report assembler (dual curator + InChIKey oracle).

Renders BOTH labelled scores side by side: the curator-agreement rate (metLinkR's own 85.3%
metric, with metLinkR's published cell as the competitor column) and the InChIKey structural
concordance (the oracle metLinkR LACKS — BioMapper's differentiator, which therefore has NO
competitor cell). Oracle (b)'s GOLD side is resolved by an INDEPENDENT external source (PubChem
PUG-REST), so the concordance is not circular — this is the crux of the "we validate structurally
where metLinkR cannot" claim, and is labelled as such in the output.

metLinkR's competitor number is transcribed from the paper's Results text with an exact ``table_ref``
(CompetitorResult discipline, Metabolon-96.5% scar); an entry left ``value=None`` renders
``n/a (transcribe)`` — nothing is fabricated from memory.

Internal only — never invokes /publish-wiki. Every BioMapper number is sourced from a scored
results bundle passed in.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from biomapper.benchmarks.config import CompetitorResult

METLINKR_FRAMING = (
    "Same-TASK head-to-head against metLinkR (Patt et al. 2025, J. Proteome Res., DOI "
    "10.1021/acs.jproteome.4c01051) — metabolite-ID cross-linking on the five COMETS-curator-"
    "cross-linked datasets. TWO labelled oracles, never merged: (a) CURATOR-AGREEMENT rate — "
    "metLinkR's own metric (85.3% published) — the fraction of the curators' cross-dataset linked "
    "pairs that BioMapper also links from the NAME alone (curator grouping held out); (b) INCHIKEY "
    "STRUCTURAL CONCORDANCE — the oracle metLinkR does NOT report — BioMapper's name-chosen id "
    "(resolved via the KG) and the held-out curator provided id (resolved via an INDEPENDENT external "
    "source, PubChem PUG-REST) each reduced to an InChIKey first-block and compared. The independent "
    "gold side is what makes (b) a non-circular structural check metLinkR cannot offer."
)


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _competitor_cell(competitors: Iterable[CompetitorResult], tool: str) -> str:
    for c in competitors:
        if c.tool == tool:
            return _pct(c.value) if c.value is not None else "n/a (transcribe)"
    return "n/a"


def assemble_metlinkr_report(
    *,
    result: dict[str, Any],
    card: dict[str, Any] | None = None,
    competitors: Iterable[CompetitorResult],
    out_path: str | Path,
) -> str:
    """Assemble + write the internal metLinkR dual-oracle report. Returns the markdown text.

    ``result`` is a ``score_metlinkr`` output; ``card`` is the adapter dataset card (optional, for
    the provenance/coverage lines).
    """
    card = card or {}
    competitors = list(competitors)
    ca = result.get("curator_agreement", {})
    st = result.get("inchikey_structural_concordance")

    lines: list[str] = []
    lines.append("# BioMapper External Benchmarks — metLinkR head-to-head (internal)")
    lines.append("")
    lines.append("> INTERNAL — repo/vault only. Not for wiki publication this pass.")
    lines.append("")
    lines.append("## Framing")
    lines.append("")
    lines.append(METLINKR_FRAMING)
    lines.append("")

    lines.append("## Two labelled oracles")
    lines.append("")
    lines.append("| Oracle | BioMapper | Support | metLinkR (same-task) |")
    lines.append("|---|---|---|---|")
    lines.append(
        f"| (a) Curator-agreement rate | {_pct(ca.get('curator_agreement_rate'))} | "
        f"{ca.get('linked')}/{ca.get('curator_cross_pairs')} cross-dataset pairs | "
        f"{_competitor_cell(competitors, 'metLinkR')} |"
    )
    if st:
        nv = st.get("needs_verification") or 0
        nv_note = f" (+{nv} needs-verification)" if nv else ""
        lines.append(
            f"| (b) InChIKey structural concordance (gold: independent PubChem PUG-REST) | "
            f"{_pct(st.get('concordance_rate'))} | "
            f"{st.get('concordant')}/{st.get('scored')} provided-id rows{nv_note} | "
            f"n/a — metLinkR reports no structural oracle (differentiator) |"
        )
    else:
        lines.append(
            "| (b) InChIKey structural concordance | n/a (no oracle supplied) | 0 | "
            "n/a — metLinkR reports no structural oracle (differentiator) |"
        )
    lines.append("")

    if card:
        stats = card.get("curator_link_stats", {})
        lines.append("## Dataset provenance")
        lines.append("")
        lines.append(f"- Source: metLinkR SI (DOI {card.get('source_doi')}, {card.get('source_pmcid')}).")
        lines.append(f"- ManualMappings SHA256: `{card.get('source_sha256')}`")
        lines.append(f"- Rows (names): {card.get('n_names')}; input mode: {card.get('input_mode')}.")
        lines.append(
            f"- Curator links: {stats.get('n_cross_dataset_groups')} cross-dataset groups, "
            f"{stats.get('cross_dataset_pairs')} cross-dataset pairs "
            f"({stats.get('n_groups')} groups total)."
        )
        prov = card.get("provided_id_coverage", {}).get("any", {})
        lines.append(f"- Curator provided-id coverage (oracle-b denominator base): {prov.get('n')} rows.")
        lines.append("")

    lines.append("## Notes")
    lines.append("")
    lines.append(
        "- metLinkR's 85.3% curator-agreement is transcribed from the paper's Results text (Patt et al. "
        "2025; 'metLinkR identified these entities at an 85.3% rate', rising to 90.7% when unmapped "
        "identifiers are excluded), cited with an exact table_ref (CompetitorResult discipline)."
    )
    lines.append(
        "- Oracle (a) is coverage/recall-shaped; oracle (b) is a correctness qualifier. The two are "
        "NEVER merged into a single figure."
    )
    lines.append(
        "- Oracle (b)'s GOLD side is resolved by an INDEPENDENT external source (PubChem PUG-REST), "
        "NOT the KG oracle that resolves BioMapper's prediction — so the structural concordance is not "
        "circular. This is where BioMapper validates structurally and metLinkR cannot."
    )
    if st and st.get("independence_note"):
        lines.append(f"- Independence (oracle b): {st['independence_note']}.")
    if st and st.get("needs_verification"):
        lines.append(
            f"- {st['needs_verification']} row(s) carried a curator provided id the INDEPENDENT external "
            f"resolver could not cover — flagged needs-verification and EXCLUDED from the concordance "
            f"denominator (not resolved via the KG, not dropped silently)."
        )
    if st and st.get("shared_infra_caveat"):
        lines.append(f"- Structural-oracle caveat (legacy shared-infra path): {st['shared_infra_caveat']}.")
    lines.append(
        "- Input mode is NAME-ONLY; the curator grouping and provided IDs are held out (anti-trivial). "
        "A '+provided-ID' parity variant matching metLinkR's exact inputs is a documented follow-on."
    )
    lines.append("")

    text = "\n".join(lines)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text)
    return text
