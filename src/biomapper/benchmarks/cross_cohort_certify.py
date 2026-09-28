"""Cross-cohort link certification and refusal adjudication, KG-independent.

Second stage of the Monti/NECS arm. :mod:`biomapper.benchmarks.cross_cohort` produces the links;
this module asks whether a link is structurally defensible, using structures resolved
**independently of the KRAKEN node that formed the link**:

* NECS side: the curated InChIKey from the Monti MOESM5 supplement (Supplementary Table 5).
* cohort side: an InChIKey first block resolved from the cohort's own vendor identifier through
  PubChem PUG-REST
  (:class:`biomapper.benchmarks.scorers.independent_inchikey.PubChemInChIKeyResolver`).

Three things this deliberately does not do:

1. It never reads the linking node's own InChIKey. The API's ``ResolutionCertificateModel`` does
   read KRAKEN's InChIKey first and only falls back to an external source, so that certificate is
   NOT fully KG-independent and is reported separately as a resolution property.
2. It does not certify a cohort that ships names only. Xu, LLFS and BLSA have no vendor identifier
   column, so ``CohortPanel.certifiable`` is ``False`` and every link there is ``refused`` by
   construction. That is a property of the source panels. A PubChem name lookup would produce a
   number, but it would be a different instrument with a different failure mode, and for BLSA it
   collides with the settled finding that sum-composition lipids are not verifiable by an
   independent certificate at all. Counts only, and said out loud.
3. It does not treat a verdict as final. The comparison is a first-block (connectivity) comparison,
   which is neither tautomer- nor charge-invariant: it over-flags tautomers as ``refuted`` and
   silently accepts a stereoisomer error whenever a side lacks block 2. The NECS gold itself carries
   roughly 5% InChIKey errors. So every ``refuted`` case, and every ``refused`` case that is not
   refused by construction, is emitted individually for re-adjudication against an outside source.

Refusal is a first-class, correct outcome here, not a failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd

from biomapper.benchmarks.adapters.cohort_panel import ARIVALE, load_cohort_panel
from biomapper.benchmarks.adapters.necs_metabolon import load_necs
from biomapper.benchmarks.cross_cohort import (
    COHORTS,
    DEFAULT_ARIVALE_XLSX,
    sha256_path,
)
from biomapper.benchmarks.pacing import PUBCHEM_MIN_INTERVAL_S, Pacer
from biomapper.benchmarks.scorers.cross_cohort_overlap import Link
from biomapper.benchmarks.scorers.gold_structure import has_gold_structure
from biomapper.benchmarks.scorers.independent_inchikey import ProvidedBlock
from biomapper.benchmarks.scorers.independent_link_certificate_overlap import (
    certify_links_tagged,
)
from biomapper.benchmarks.scorers.link_certificate import certify_link
from biomapper.benchmarks.scorers.metlinkr_scorer import _hmdb_accession_forms

DEFAULT_MOESM5 = (
    Path.home()
    / "external_benchmark_runs/monti_string_replication_20260820T182111Z/monti_MOESM5_TableS5.xlsx"
)

# The only cohort in the Monti arm whose panel carries a structure-resolvable vendor identifier.
CERTIFIABLE_COHORTS: tuple[str, ...] = ("arivale",)

NECS_GOLD_SOURCE = "gold-necs-moesm5"


def first_block(inchikey: str | None) -> str | None:
    if not inchikey:
        return None
    block = str(inchikey).strip().split("-", 1)[0].strip()
    return block or None


def necs_gold_blocks(moesm5: Path) -> tuple[dict[str, ProvidedBlock], dict[str, Any]]:
    """NECS-side independent blocks from the MOESM5 curated InChIKey.

    ``gold_inchikey_standard`` is preferred over ``gold_inchikey`` because the latter is a Metabolon
    delivery variant whose stereo/charge suffix is not a standard InChIKey layer. Their first blocks
    agree, and only the first block is compared, but preferring the standard column keeps the
    recorded key interpretable when a case is re-adjudicated by hand.

    A row with no curated key yields no entry, which makes any link through it ``refused`` rather
    than certified off nothing.

    Every candidate value is screened with
    :func:`biomapper.benchmarks.scorers.gold_structure.has_gold_structure`, which rejects blanks and
    the documented corrupt ``4000`` placeholder. Without the screen a sentinel is a 4-character
    "block" that can never equal a real 14-character one, so every link through that row comes back
    REFUTED, and a refuted verdict reads as a wrong molecule rather than as a broken gold cell. The
    MOESM5 supplement carries ``4000`` on 10 rows; 9 of them also carry a usable standard key, so
    exactly one row reaches the block set unscreened. One spurious refutation in a hand-adjudicated
    set is one wrong published claim.
    """
    raw = moesm5.read_bytes()
    bundle = load_necs(raw)
    frame = bundle.input_df
    blocks: dict[str, ProvidedBlock] = {}
    both_present = agree = 0
    rejected: dict[str, int] = {}
    rows_with_corrupt: set[str] = set()
    rows_excluded: list[str] = []
    for _, row in frame.iterrows():
        name = str(row.get("chemical_name", "")).strip()
        if not name:
            continue
        raw_standard = str(row.get("gold_inchikey_standard", "")).strip()
        raw_legacy = str(row.get("gold_inchikey", "")).strip()
        for candidate in (raw_standard, raw_legacy):
            if candidate and not has_gold_structure(candidate):
                rejected[candidate] = rejected.get(candidate, 0) + 1
                rows_with_corrupt.add(name)
        # Screened BEFORE first_block: a corrupt cell must not become a comparable block.
        standard = first_block(raw_standard) if has_gold_structure(raw_standard) else None
        legacy = first_block(raw_legacy) if has_gold_structure(raw_legacy) else None
        if standard and legacy:
            both_present += 1
            agree += int(standard == legacy)
        block = standard or legacy
        if block is None:
            # A row lost ONLY because the screen rejected its cells is the decision-relevant count:
            # it would have contributed a block before the screen, and that block would have been a
            # guaranteed refutation. A row that was simply blank was never going to contribute.
            if name in rows_with_corrupt:
                rows_excluded.append(name)
            # A TAGGED entry with no block, not an absent entry. Both refuse, but only the tagged
            # form lets ``certify_links_tagged``'s untagged-sides canary mean what it claims: the
            # canary is supposed to catch provenance we failed to record, and an absent entry makes
            # "the gold has no key for this metabolite", which we DID record, indistinguishable from
            # it. The cohort side already worked this way; this is the same fix on the NECS side.
            blocks[name] = ProvidedBlock(
                block=None,
                source=NECS_GOLD_SOURCE,
                status="clean_miss",
                record_id=f"moesm5:{name}",
            )
            continue
        blocks[name] = ProvidedBlock(
            block=block,
            source=NECS_GOLD_SOURCE,
            status="success",
            record_id=f"moesm5:{name}",
        )
    n_with_block = sum(1 for b in blocks.values() if b.block)
    card = {
        "path": str(moesm5),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "n_rows": int(len(frame)),
        # Counts entries that yielded a usable BLOCK, not entries present. Since a row with no key
        # now gets a tagged no-block entry, len(blocks) is the row count and would silently turn a
        # 63% coverage figure into 100%.
        "n_entries": len(blocks),
        "n_with_curated_inchikey": n_with_block,
        "coverage": round(n_with_block / len(frame), 4) if len(frame) else None,
        "two_vintage_first_block_agreement": {
            "both_present": both_present,
            "agree": agree,
            "disagree": both_present - agree,
        },
        "rejected_gold_values": rejected,
        # Three distinct counts, because a row can carry a corrupt cell in either vintage or both.
        # The first counts CELLS, so it exceeds the row count when both vintages are corrupt; the
        # last is the only one that says how many rows actually stopped contributing a block.
        "n_corrupt_gold_cells": sum(rejected.values()),
        "n_rows_with_any_corrupt_gold": len(rows_with_corrupt),
        "n_rows_excluded_by_screen": len(rows_excluded),
        "rows_excluded_by_screen": sorted(rows_excluded),
        "screen": (
            "candidate keys screened with gold_structure.has_gold_structure, which rejects blanks "
            "and the corrupt '4000' placeholder. An unscreened sentinel becomes a 4-character "
            "block that can never match a real one, so every link through that row returns "
            "REFUTED and reads as a wrong molecule rather than a broken gold cell."
        ),
        "known_defect": (
            "the NECS curated gold carries roughly 5% InChIKey errors, so a disagreement "
            "with it is "
            "not by itself evidence that BioMapper is wrong"
        ),
    }
    return blocks, card


# Re-exported from the shared pacer so both PubChem callers in this package use one implementation.
# The first live certification run fired ~592 lookups with no spacing and got 109 `lookup_failed`
# back, which is 109 refusals that were run artifacts rather than absent structures.


def arivale_independent_blocks(
    arivale_xlsx: Path,
    names_needed: set[str],
    *,
    min_interval_s: float = PUBCHEM_MIN_INTERVAL_S,
) -> tuple[dict[str, ProvidedBlock], dict[str, Any]]:
    """Cohort-side independent blocks for Arivale, resolved from its vendor ids via PubChem.

    PubChem CID is tried first and HMDB second. CAS and KEGG are not structure-resolvable through
    this oracle, so a row carrying only those yields no block and its links refuse. A transient
    ``lookup_failed`` is kept distinct from a ``clean_miss``: a network failure must never be
    reported as an absent structure.
    """
    from biomapper.benchmarks.scorers.independent_inchikey import PubChemInChIKeyResolver

    frame = pd.read_excel(arivale_xlsx, sheet_name="Arivale_Metabolomics", dtype=str).fillna("")
    panel = load_cohort_panel(frame, ARIVALE)
    # The pacer goes INTO the resolver so it fires on the request path only. Calling it here, at the
    # call site, paced cache hits as well: the panel de-duplicates on NAME, not on identifier, so
    # distinct names can share a PubChem CID or HMDB accession and each repeat cost up to a full
    # interval without issuing a request, and delayed the next real lookup on top.
    resolver = PubChemInChIKeyResolver(pacer=Pacer(min_interval_s))

    blocks: dict[str, ProvidedBlock] = {}
    statuses: Counter[str] = Counter()
    route: Counter[str] = Counter()
    for _, row in panel.frame.iterrows():
        name = str(row.get(panel.name_column, "")).strip()
        if not name or name not in names_needed:
            continue
        cid = str(row.get("pubchem", "")).strip()
        hmdb = str(row.get("hmdb", "")).strip()
        block: str | None = None
        source = "none"
        status = "clean_miss"
        # A transient failure on the first route is STICKY across the fallback. Letting a later
        # clean_miss overwrite an earlier lookup_failed would turn a retryable run artifact into a
        # reported coverage gap, and the refusal classifier would then call it a real absence.
        any_transient_failure = False
        if cid:
            block, status = resolver._cached_resolve(  # noqa: SLF001 - status-aware accessor
                f"pubchem:{cid}", f"compound/cid/{cid}/property/InChIKey/TXT"
            )
            source = "provided-pubchem"
            any_transient_failure = status == "lookup_failed"
        if block is None and hmdb:
            # The Arivale panel ships legacy 5-digit accessions (HMDB13127), which PubChem's
            # RegistryID index does not know: they come back 404, a clean miss, and a real structure
            # reads as absent. Try the zero-padded modern form first, the original second.
            # Paced by the resolver, on its request path.
            for form in _hmdb_accession_forms(hmdb):
                block, status = resolver._cached_resolve(  # noqa: SLF001
                    f"hmdb:{form}", f"compound/xref/RegistryID/{form}/property/InChIKey/TXT"
                )
                any_transient_failure = any_transient_failure or status == "lookup_failed"
                if block is not None:
                    break
            source = "provided-hmdb"
        if block is None and any_transient_failure:
            status = "lookup_failed"
        if not cid and not hmdb:
            source = "none"
            status = "no_structure_resolvable_id"
        statuses[status] += 1
        route[source] += 1
        # An entry is recorded even when no block came back, carrying the status. Omitting the
        # failures would collapse three different refusals into one indistinguishable "absent",
        # and a transient lookup_failed would then read as a genuinely missing structure.
        blocks[name] = ProvidedBlock(
            block=first_block(block),
            source=source,
            status=status,
            record_id=f"arivale:{name}",
        )
    card = {
        "path": str(arivale_xlsx),
        "sha256": sha256_path(arivale_xlsx),
        "panel_n": int(len(panel.frame)),
        "names_requested": len(names_needed),
        "entries_recorded": len(blocks),
        "blocks_resolved": sum(1 for b in blocks.values() if b.block),
        "lookup_status": dict(statuses),
        "lookup_route": dict(route),
        "oracle": "PubChem PUG-REST (first block only, connectivity granularity)",
        "min_interval_s": min_interval_s,
        "transient_failures": statuses.get("lookup_failed", 0),
        "transient_failure_warning": (
            "a lookup_failed is the service pushing back, not an absent structure. Any non-zero "
            "count here means that many refusals are run artifacts and the certified/refused split "
            "is not publishable until they are retried."
        ),
    }
    return blocks, card


class MissingLinkArtifactError(RuntimeError):
    """A cohort's link file is absent or disagrees with the manifest.

    ``manifest.json`` existing does not mean every per-pair artifact does. A partially copied or
    truncated run directory would otherwise yield a successful certificate report claiming zero
    links, which is indistinguishable from a genuine zero and reads as a finding.
    """


def read_links(path: Path, cohort: str) -> list[Link]:
    if not path.exists():
        raise MissingLinkArtifactError(
            f"{path} is missing. A cohort with no link file is an incomplete run directory, not a "
            "cohort with zero links; refusing to report a certificate over it."
        )
    frame = pd.read_csv(path, dtype=str).fillna("")
    column = f"{cohort}_name"
    if frame.empty:
        return []
    return [
        Link(
            a_name=str(row["necs_name"]).strip(),
            b_name=str(row[column]).strip(),
            shared=frozenset(str(row.get("shared_curies", "")).split("|")) - {""},
        )
        for _, row in frame.iterrows()
    ]


def _classify_refusal(a: ProvidedBlock | None, b: ProvidedBlock | None) -> str:
    """Why a link refused, in the terms a write-up needs.

    The distinction that matters is adjudicable versus not. A panel with no structure-resolvable
    identifier refuses as a property of its source and is not an individual case to chase; a failed
    lookup is a run artifact to retry; a clean miss and a missing curated key are genuine, citable
    coverage gaps. Collapsing them into one "refused" bucket is what makes a refusal count look like
    a failure rate.
    """
    # Tested on the BLOCK, not on entry presence. Both sides now record a tagged entry even when
    # they have no structure, so an `is None` test would find the NECS entry present, fall through
    # to the cohort branches, and attribute a NECS-side gap to the cohort. That is what happened the
    # first time this ran with tagged entries: necs_gold_has_no_curated_inchikey went to zero and
    # cohort_lookup_clean_miss absorbed its cases.
    a_has_block = a is not None and bool(a.block)
    b_has_block = b is not None and bool(b.block)
    if not a_has_block and not b_has_block:
        return "no_independent_structure_either_side"
    if not a_has_block:
        return "necs_gold_has_no_curated_inchikey"
    if b is None:
        return "cohort_name_absent_from_panel_lookup"
    if b.status == "lookup_failed":
        return "cohort_lookup_failed_transient"
    if b.status == "no_structure_resolvable_id":
        return "cohort_panel_has_no_structure_resolvable_id"
    return "cohort_lookup_clean_miss"


def adjudicate_cases(
    links: list[Link],
    necs_blocks: dict[str, ProvidedBlock],
    cohort_blocks: dict[str, ProvidedBlock],
) -> pd.DataFrame:
    """Per-link verdict table with both blocks, both sources, and the refusal or refutation reason.

    ``refusal_class`` is what makes the counts usable: a refusal because the panel has no
    structure-resolvable identifier is a source property and is not individually adjudicable, while
    a refusal from a failed lookup is a run artifact that must be retried rather than reported.
    """
    rows: list[dict[str, Any]] = []
    for link in links:
        a = necs_blocks.get(link.a_name)
        b = cohort_blocks.get(link.b_name)
        same_record = a is not None and b is not None and a.record_id == b.record_id
        if same_record:
            verdict, reason, stereo = "refused", "same curator record on both sides", False
        else:
            certificate = certify_link(
                a.block if a else None,
                b.block if b else None,
                necs_source=a.source if a else None,
                cohort_source=b.source if b else None,
                require_tags=True,
            )
            verdict, reason, stereo = (
                certificate.verdict,
                certificate.reason,
                certificate.stereo_checked,
            )
        refusal_class = "" if verdict != "refused" else _classify_refusal(a, b)
        rows.append(
            {
                "necs_name": link.a_name,
                "cohort_name": link.b_name,
                "verdict": verdict,
                "refusal_class": refusal_class,
                "reason": reason,
                "stereo_checked": stereo,
                "necs_block": a.block if a else "",
                "necs_source": a.source if a else "",
                "cohort_block": b.block if b else "",
                "cohort_source": b.source if b else "",
                "cohort_lookup_status": b.status if b else "",
                "shared_curies": "|".join(sorted(link.shared)),
            }
        )
    return pd.DataFrame(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m biomapper.benchmarks.cross_cohort_certify",
        description="KG-independent certification and refusal adjudication for the Monti arm.",
    )
    parser.add_argument("--run-dir", type=Path, required=True, help="A cross_cohort run directory")
    parser.add_argument("--moesm5", type=Path, default=DEFAULT_MOESM5)
    parser.add_argument("--arivale-xlsx", type=Path, default=DEFAULT_ARIVALE_XLSX)
    parser.add_argument(
        "--min-interval-s",
        type=float,
        default=PUBCHEM_MIN_INTERVAL_S,
        help="Seconds between PubChem requests for the cohort-side lookups. Raise it under a rate "
        "limit; a throttled request becomes a lookup_failed refusal that is a run artifact.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run_dir: Path = args.run_dir
    if not (run_dir / "manifest.json").exists():
        print(
            f"[fatal] {run_dir}/manifest.json is missing; run cross_cohort first", file=sys.stderr
        )
        return 2

    necs_blocks, necs_card = necs_gold_blocks(args.moesm5)
    print(
        f"[necs] curated InChIKey on {necs_card['n_with_curated_inchikey']}/{necs_card['n_rows']} "
        f"MOESM5 rows ({necs_card['coverage']})",
        flush=True,
    )

    report: dict[str, Any] = {
        "necs_gold": necs_card,
        "certifiable_cohorts": list(CERTIFIABLE_COHORTS),
        "cohorts": {},
    }

    manifest = json.loads((run_dir / "manifest.json").read_text())
    manifest_links = {
        cohort: int(result.get("arm_m_links", -1))
        for cohort, result in (manifest.get("results") or {}).items()
    }

    for cohort in COHORTS:
        links_path = run_dir / f"links_necs_{cohort}.csv"
        links = read_links(links_path, cohort)
        expected_links = manifest_links.get(cohort, -1)
        if expected_links >= 0 and len(links) != expected_links:
            print(
                f"[fatal] {links_path.name} holds {len(links)} links but the manifest recorded "
                f"{expected_links} for this pair. Refusing to certify over an artifact that does "
                "not match the run that produced it.",
                file=sys.stderr,
            )
            return 2
        if cohort not in CERTIFIABLE_COHORTS:
            report["cohorts"][cohort] = {
                "certifiable": False,
                "n_links": len(links),
                "certified": 0,
                "refuted": 0,
                "refused": len(links),
                "refused_by_construction": True,
                "note": (
                    "names-only panel with no vendor identifier column, so no structure can be "
                    "resolved independently of the KRAKEN node that formed the link. Links are "
                    "countable; they are never structurally certifiable. This is a property of the "
                    "source panel, not an uncertified failure and not a BioMapper error."
                ),
            }
            print(
                f"[cert] NECS<->{cohort}: {len(links)} links, refused by construction "
                "(names-only panel, certifiable=False)",
                flush=True,
            )
            continue

        needed = {link.b_name for link in links}
        cohort_blocks, cohort_card = arivale_independent_blocks(
            args.arivale_xlsx, needed, min_interval_s=args.min_interval_s
        )
        overlap, untagged = certify_links_tagged(links, necs_blocks, cohort_blocks)
        cases = adjudicate_cases(links, necs_blocks, cohort_blocks)
        cases.to_csv(run_dir / f"certificate_cases_{cohort}.csv", index=False)
        cases[cases["verdict"] != "certified"].to_csv(
            run_dir / f"certificate_open_cases_{cohort}.csv", index=False
        )
        report["cohorts"][cohort] = {
            "certifiable": True,
            "n_links": len(links),
            "certified": overlap.certified,
            "refuted": overlap.refuted,
            "refused": overlap.refused,
            "adjudicable": overlap.adjudicable,
            "certified_rate_over_adjudicable": overlap.certified_rate,
            "untagged_sides_canary": untagged,
            "refusal_classes": dict(
                Counter(cases.loc[cases["verdict"] == "refused", "refusal_class"].tolist())
            ),
            "stereo_checked_any": bool(cases["stereo_checked"].any()),
            "comparison_granularity": (
                "InChIKey first block (connectivity) only. Not tautomer- or charge-invariant: "
                "over-flags tautomers as refuted, silently accepts stereoisomer errors."
            ),
            "independent_source_card": cohort_card,
        }
        print(
            f"[cert] NECS<->{cohort}: certified={overlap.certified} refuted={overlap.refuted} "
            f"refused={overlap.refused} adjudicable={overlap.adjudicable} untagged={untagged}",
            flush=True,
        )

    out = run_dir / "certificate_report.json"
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"[done] {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
