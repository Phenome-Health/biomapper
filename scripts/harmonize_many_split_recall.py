#!/usr/bin/env python3
"""Split-recall gate for harmonize_many(): where do TRUE cross-cohort matches land?

``harmonize_many()`` groups rows strictly by chosen KRAKEN node, so a true match whose two rows
resolved to different nodes (stereo, salt or charge variants) is split: it can only surface in the
cross-node review queue, or nowhere. Before the five-cohort demo builds on ``harmonize_many()``,
this script measures that split on an answer key that does not come from names:

* **Answer key.** A true pair is a NECS row (Monti et al. 2026, MOESM5) and an Arivale row whose
  curated HMDB identifiers agree (legacy 5-digit codes zero-padded). Monti publishes only overlap
  counts and its pairs come from a name match, which would make this gate pass by construction.
* **Screening.** NECS gold carries about 5% wrong InChIKeys, so the key is restricted to
  certifiable NECS rows first: a name, an HMDB code, a well-formed InChIKey, and, where the
  supplement carries a second InChIKey vintage, the same first block in both. Screened-out rows
  are counted by reason.
* **The run.** ``harmonize_many()`` on the two cohorts, names only (no identifiers are declared,
  so none reach the mapper), with ``remap_all=True`` so the run is publication-grade.
* **Classification.** Each true pair is ``same_group``, ``cross_node_queue`` or ``neither``, in two
  strata: ``name_equal`` (names equal after whitespace normalization and casefold) and
  ``different_name``. Only the different-name stratum is evidence about split risk; name-equal
  pairs link by name whatever their nodes. A pair whose row is not in the run is
  ``missing_from_panel`` and is not counted in the shares.

The output is numbers for a person to accept, not a pass/fail threshold. Results are saved by
default to ``biomapper_runs/split_recall_<UTC stamp>/`` with the pins, the NECS and Arivale
sha256s and the answer key's checksum; ``--out`` overrides the location.

Usage (live; the operator starts it, it maps about 2,500 names on the hosted API)::

    python scripts/harmonize_many_split_recall.py --necs path/to/MOESM5.xlsx \\
        --necs-sha256 <recorded sha256>

Needs the ``benchmarks`` extra (pandas, openpyxl) to read the NECS workbook.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from biomapper.cohorts import DEFAULT_RUNS_DIR, Mapper, _new_run_dir
from biomapper.harmonize.curies import normalize_curie, normalize_hmdb
from biomapper.harmonize.linking import normalize_name
from biomapper.many_cohorts import ManyCohortReport, harmonize_many

REPO = Path(__file__).resolve().parents[1]
DEFAULT_ARIVALE = (
    REPO / "notebooks" / "data" / "ukbb_arivale" / "panels"
    / "metabolites_arivale_metabolomics_metadata.tsv"
)
SCREENING_RULE = "name+hmdb+wellformed_inchikey+vintage_first_block_agrees/v1"
KEY_RULE = "necs_gold_hmdb_intersects_arivale_declared_hmdb/v1"
CLASSES = ("same_group", "cross_node_queue", "neither")
STRATA = ("name_equal", "different_name")
_INCHIKEY = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$")
_SPLIT = re.compile(r"[,;|\s]+")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hmdb_set(cell: Any) -> frozenset[str]:  # noqa: ANN401 — any table cell
    text = "" if cell is None else str(cell).strip()
    if not text or text.lower() == "nan":
        return frozenset()
    out = set()
    for part in _SPLIT.split(text):
        if not part:
            continue
        curie = normalize_curie(part if part.upper().startswith("HMDB:") else f"HMDB:{part}")
        if curie is not None:
            out.add(normalize_hmdb(curie))
    return frozenset(out)


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def load_necs(path: Path) -> list[dict[str, str]]:
    """NECS rows as ``{key, name, hmdb, inchikey, inchikey_standard}`` via the benchmark adapter."""
    import pandas as pd

    from biomapper.benchmarks.adapters.necs_metabolon import build_input_df, parse_xlsx
    from biomapper.benchmarks.config import NECS

    if path.suffix.lower() in (".xlsx", ".xls"):
        raw = parse_xlsx(path.read_bytes())
    else:
        # Blank cells as "" (as the workbook path does), never NaN, which would become "nan".
        raw = pd.read_csv(
            path, sep="," if path.suffix.lower() == ".csv" else "\t", dtype=str
        ).fillna("")
    frame = build_input_df(raw, NECS)
    rows = []
    for i, r in enumerate(frame.to_dict(orient="records")):
        name = str(r.get(NECS.name_column) or "").strip()
        rows.append({
            "key": f"{i}|{name}",
            "name": name,
            "hmdb": str(r.get("gold_hmdb") or ""),
            "inchikey": str(r.get("gold_inchikey") or "").strip().upper(),
            "inchikey_standard": str(r.get("gold_inchikey_standard") or "").strip().upper(),
        })
    return rows


def load_arivale(path: Path) -> list[dict[str, str]]:
    """Arivale metabolite rows (unnamed ``X - 12345`` vendor features dropped)."""
    with path.open(encoding="utf-8") as fh:
        lines = [line for line in fh if not line.startswith("#")]
    rows = []
    for r in csv.DictReader(lines, delimiter="\t"):
        name = (r.get("BIOCHEMICAL_NAME") or "").strip()
        if not name or (name.startswith("X - ") and name[4:].strip().isdigit()):
            continue
        rows.append({"key": f"{r['CHEMICAL_ID']}|{name}", "name": name,
                     "hmdb": r.get("HMDB") or ""})
    return rows


def screen_necs(rows: Sequence[dict[str, str]]) -> tuple[list[dict[str, str]], Counter[str]]:
    """Keep certifiable NECS rows; count the rest by reason."""
    kept: list[dict[str, str]] = []
    reasons: Counter[str] = Counter()
    for r in rows:
        key = r["inchikey"]
        if not r["name"]:
            reasons["blank name"] += 1
        elif not _hmdb_set(r["hmdb"]):
            reasons["no gold HMDB"] += 1
        elif not _INCHIKEY.match(key):
            reasons["missing or malformed gold InChIKey"] += 1
        elif r.get("inchikey_standard") and _INCHIKEY.match(r["inchikey_standard"]) and (
            r["inchikey_standard"].split("-")[0] != key.split("-")[0]
        ):
            reasons["InChIKey vintages disagree (first block)"] += 1
        else:
            kept.append(r)
    return kept, reasons


@dataclass(frozen=True)
class TruePair:
    necs_key: str
    necs_name: str
    arivale_key: str
    arivale_name: str
    shared_hmdb: tuple[str, ...]

    @property
    def stratum(self) -> str:
        same = normalize_name(self.necs_name).casefold() == normalize_name(
            self.arivale_name).casefold()
        return "name_equal" if same else "different_name"


def build_answer_key(
    necs: Sequence[dict[str, str]], arivale: Sequence[dict[str, str]]
) -> list[TruePair]:
    by_hmdb: dict[str, list[dict[str, str]]] = {}
    for a in arivale:
        for code in _hmdb_set(a["hmdb"]):
            by_hmdb.setdefault(code, []).append(a)
    pairs: dict[tuple[str, str], TruePair] = {}
    for n in necs:
        codes = _hmdb_set(n["hmdb"])
        for code in sorted(codes):
            for a in by_hmdb.get(code, ()):
                shared = tuple(sorted(codes & _hmdb_set(a["hmdb"])))
                pairs[(n["key"], a["key"])] = TruePair(n["key"], n["name"], a["key"], a["name"],
                                                       shared)
    return [pairs[k] for k in sorted(pairs)]


def key_checksum(pairs: Sequence[TruePair]) -> str:
    canonical = json.dumps([[p.necs_key, p.arivale_key, list(p.shared_hmdb)] for p in pairs],
                           ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _cross_pairs(report: ManyCohortReport) -> set[tuple[str, str]]:
    arm = report.names_only
    out: set[tuple[str, str]] = set()
    if arm.nway.drilldown is not None:
        rows = [(p.a_cohort, p.a_key, p.b_cohort, p.b_key) for p in arm.nway.drilldown]
    else:
        assert arm.drilldown_file is not None
        with arm.drilldown_file.open(encoding="utf-8") as fh:
            rows = [(r["a_cohort"], r["a_key"], r["b_cohort"], r["b_key"])
                    for r in csv.DictReader(fh, delimiter="\t")]
    for ac, ak, bc, bk in rows:
        out.add((ak, bk) if (ac, bc) == ("necs", "arivale") else (bk, ak))
    return out


def classify_pairs(report: ManyCohortReport, pairs: Sequence[TruePair]) -> list[dict[str, Any]]:
    nway = report.names_only.nway
    rows = {(r.cohort, r.key): r for r in nway.rows}
    cross = _cross_pairs(report)
    out = []
    for p in pairs:
        n, a = rows.get(("necs", p.necs_key)), rows.get(("arivale", p.arivale_key))
        if n is None or a is None:
            where = "missing_from_panel"
        elif n.node is not None and n.node == a.node:
            where = "same_group"
        elif (p.necs_key, p.arivale_key) in cross:
            where = "cross_node_queue"
        else:
            where = "neither"
        out.append({
            "necs_key": p.necs_key, "necs_name": p.necs_name,
            "arivale_key": p.arivale_key, "arivale_name": p.arivale_name,
            "shared_hmdb": "|".join(p.shared_hmdb), "stratum": p.stratum, "landed": where,
            "necs_node": n.chosen_kg_id if n else None,
            "arivale_node": a.chosen_kg_id if a else None,
        })
    return out


def tally(classified: Sequence[dict[str, Any]]) -> dict[str, Any]:
    strata: dict[str, Any] = {}
    for stratum in STRATA:
        rows = [r for r in classified if r["stratum"] == stratum]
        counted = [r for r in rows if r["landed"] in CLASSES]
        counts = Counter(r["landed"] for r in counted)
        n = len(counted)
        strata[stratum] = {
            "n_pairs": n,
            "counts": {c: counts.get(c, 0) for c in CLASSES},
            "shares": {c: (counts.get(c, 0) / n if n else None) for c in CLASSES},
            "missing_from_panel": sum(1 for r in rows if r["landed"] == "missing_from_panel"),
            "split_evidence": stratum == "different_name",
        }
    return strata


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run(
    *,
    necs_path: Path,
    necs_sha256: str,
    arivale_path: Path = DEFAULT_ARIVALE,
    out: Path | None = None,
    mapper: Mapper | None = None,
    pins: dict[str, Any] | None = None,
    store_dir: Path | None = None,
    remap_all: bool = True,
    answer_key: Sequence[TruePair] | None = None,
) -> Path:
    """Run the gate and save its results; return the results directory."""
    actual = _sha256(necs_path)
    if actual != necs_sha256.lower():
        raise SystemExit(
            f"NECS file {necs_path.name} has sha256 {actual}, not the recorded {necs_sha256}; "
            "refusing to score against an unpinned answer key."
        )
    necs_all = load_necs(necs_path)
    certifiable, screened_out = screen_necs(necs_all)
    arivale = load_arivale(arivale_path)
    pairs = list(answer_key) if answer_key is not None else build_answer_key(certifiable, arivale)

    directory = (out.resolve() if out is not None
                 else _new_run_dir(Path(DEFAULT_RUNS_DIR).resolve(), "split_recall"))
    directory.mkdir(parents=True, exist_ok=True)
    report = harmonize_many(
        {"necs": [{"key": r["key"], "name": r["name"]} for r in necs_all if r["name"]],
         "arivale": [{"key": r["key"], "name": r["name"]} for r in arivale]},
        entity="metabolites", key_columns={"necs": "key", "arivale": "key"},
        mapper=mapper, pins=pins, store_dir=store_dir, output_dir=directory / "harmonize_many",
        remap_all=remap_all,
    )
    classified = classify_pairs(report, pairs)
    s = report.summary()
    result = {
        "gate": "harmonize_many split recall",
        "answer_key": {
            "rule": KEY_RULE,
            "checksum_sha256": key_checksum(pairs),
            "n_pairs": len(pairs),
            "screening_rule": SCREENING_RULE,
            "necs_rows": len(necs_all),
            "necs_certifiable": len(certifiable),
            "necs_screened_out": dict(screened_out),
            "necs_file": necs_path.name,
            "necs_sha256": actual,
            "arivale_file": arivale_path.name,
            "arivale_sha256": _sha256(arivale_path),
        },
        "strata": tally(classified),
        "run": {
            "status": s["status"],
            "publication_grade": s["publication_grade"],
            "kg_build": s["kg_build"],
            "kg_verified": s["kg_verified"],
            "biomapper_version": (s["pins"] or {}).get("biomapper_version"),
            "session_id": s["session_id"],
            "remap_all": remap_all,
            "harmonize_many_dir": "harmonize_many",
        },
        "note": (
            "Numbers for a person to accept, not a pass/fail gate. Only the different_name "
            "stratum is evidence about split risk."
        ),
    }
    (directory / "split_recall.json").write_text(json.dumps(result, indent=1, sort_keys=True)
                                                 + "\n")
    with (directory / "pairs.tsv").open("w", newline="", encoding="utf-8") as fh:
        header = ["necs_key", "necs_name", "arivale_key", "arivale_name", "shared_hmdb",
                  "stratum", "landed", "necs_node", "arivale_node"]
        writer = csv.DictWriter(fh, fieldnames=header, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(classified)
    print(f"split recall: wrote results to {directory}")
    return directory


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--necs", type=Path, required=True,
                        help="NECS MOESM5 workbook (.xlsx) or a TSV/CSV export of it")
    parser.add_argument("--necs-sha256", required=True, help="the file's recorded sha256")
    parser.add_argument("--arivale", type=Path, default=DEFAULT_ARIVALE)
    parser.add_argument("--out", type=Path, default=None,
                        help="results directory (default biomapper_runs/split_recall_<stamp>/)")
    parser.add_argument("--store-dir", type=Path, default=None)
    parser.add_argument("--no-remap-all", action="store_true",
                        help="reuse saved mappings (iteration-grade; not for the gate)")
    args = parser.parse_args(argv)
    run(necs_path=args.necs, necs_sha256=args.necs_sha256, arivale_path=args.arivale,
        out=args.out, store_dir=args.store_dir, remap_all=not args.no_remap_all)
    return 0


if __name__ == "__main__":
    sys.exit(main())
