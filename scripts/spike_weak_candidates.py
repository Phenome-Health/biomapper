"""Unit 0 spike: can top-N Kestrel candidates rescue weak rows, and do re-maps reproduce them?

Plan: docs/plans/2026-11-08-001-feat-mcp-agent-candidate-review-plan.md (Phase A, Unit 0).

For each gold row (Hajjar-100 plus a seeded NECS subsample), the name is mapped once the way a
file job maps it: ``map_entities`` in batches of 5. Some rows are ones the MCP layer would call
**weak**: status unresolved or refused, or certificate uncorroborated or contradicted. Each weak
row is re-mapped as a batch of one with ``kestrel_top_n``, which matches the original endpoint
family. For every weak row the script records:

- **hit@5/10/25:** whether a candidate's ``equivalent_ids`` contain the gold ChEBI or the gold
  InChIKey.
  - Strict means the full key.
  - Lenient means the InChIKey connectivity block. That block is not tautomer- or
    charge-invariant, so lenient is reported only as an upper bound, never as the headline.
- **engine_correct:** whether the engine's own choice was already right, judged by the same rule
  against its equivalents.
- **run_mismatch:** whether the re-map changed ``chosen_kg_id`` or the certificate state.
- **Latency, endpoints used, and candidate status.**

**Limitation recorded in the output:** the client cannot force a cold engine cache, so the
mismatch rate covers the warm path only. Results persist by default under a timestamped directory,
together with the run pins, dataset SHAs and the git commit.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import random
import subprocess
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from biomapper._pins import collect_pins
from biomapper.benchmarks import sources
from biomapper.benchmarks.config import HAJJAR, NECS
from biomapper.client import DEFAULT_BASE_URL, BioMapperClient
from biomapper.mcp.tools import entity_status
from biomapper.models import MappingResult

ENTITY_TYPE = "biolink:SmallMolecule"
WEAK_CERTS = {"uncorroborated", "contradicted"}
# Measured as its own trigger (not weak under R1): the first run found 4 of 10 engine-wrong Hajjar
# rows had certificate ``unavailable``, so whether it should count as weak is tested, not assumed.
PROBE_CERTS = {"unavailable"}
NS = (5, 10, 25)
PACE_S = 1.1  # stay under the prod per-IP limit of 1 r/s


def _weak_trigger(r: MappingResult) -> str | None:
    status = entity_status(r)
    if status in ("unresolved", "refused"):
        return status
    cert = r.certificate.state if r.certificate is not None else None
    return cert if cert in WEAK_CERTS | PROBE_CERTS else None


def _norm_chebi(v: object) -> str | None:
    if v is None or str(v).strip() in ("", "nan", "None"):
        return None
    s = str(v).strip().upper()
    s = s.split(":", 1)[1] if ":" in s else s
    return f"CHEBI:{s}" if s.isdigit() else None


def _norm_ik(v: object) -> str | None:
    if v is None:
        return None
    s = str(v).strip().upper()
    s = s.split(":", 1)[1] if s.startswith("INCHIKEY:") else s
    return s if len(s) == 27 else None


def _matches(ids: list[str], gold_chebi: str | None, gold_ik: str | None) -> tuple[bool, bool]:
    """(strict, lenient) gold match against a flat list of CURIEs."""
    up = {i.upper() for i in ids}
    iks = {i.split(":", 1)[1] for i in up if i.startswith("INCHIKEY:")}
    strict = bool(gold_chebi and gold_chebi in up) or bool(gold_ik and gold_ik in iks)
    lenient = strict or bool(gold_ik and any(k[:14] == gold_ik[:14] for k in iks))
    return strict, lenient


def _engine_ids(r: MappingResult) -> list[str]:
    ids = [i for i in (r.chosen_kg_id, r.primary_curie) if i]
    for prefix, values in (r.kg_equivalent_ids or {}).items():
        ids += [v if ":" in v else f"{prefix}:{v}" for v in values]
    return ids


def _candidate_rows(r: MappingResult) -> tuple[str, list[dict[str, Any]], list[str]]:
    """(status, rows ranked by score, endpoints) from the passthrough block."""
    blocks = [b if isinstance(b, dict) else b.model_dump() for b in (r.kestrel_results or [])]
    if not blocks:
        return "none_searched", [], []
    rows: dict[str, dict[str, Any]] = {}
    errors = []
    for b in blocks:
        if b.get("error"):
            errors.append(b["error"])
        for row in b.get("rows") or []:
            cur = rows.get(row["id"])
            if cur is None or (row.get("score") or 0) > (cur.get("score") or 0):
                rows[row["id"]] = row
    ranked = sorted(rows.values(), key=lambda x: -(x.get("score") or 0))
    endpoints = [b.get("endpoint") for b in blocks]
    if not ranked:
        return (f"fetch_failed:{errors[0]}" if errors else "none_searched"), [], endpoints
    return "present", ranked, endpoints


def _load(dataset: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if dataset == "hajjar":
        from biomapper.benchmarks.adapters.hajjar import load_hajjar

        raw, prov = sources.acquire(
            "hajjar", HAJJAR.source_url, expected_sha256=HAJJAR.expected_source_sha256
        )
        bundle, cfg = load_hajjar(raw, HAJJAR, source_provenance=prov), HAJJAR
    else:
        from biomapper.benchmarks.adapters.necs_metabolon import load_necs

        raw, prov = sources.acquire("necs", NECS.source_url)
        bundle, cfg = load_necs(raw, NECS), NECS
    df = bundle.input_df
    # NECS ships a non-standard 25-char key in gold_inchikey; the standard key is a separate column.
    ik_col = "gold_inchikey_standard" if "gold_inchikey_standard" in df.columns else (
        cfg.gold_inchikey_column
    )
    rows = []
    for _, rec in df.iterrows():
        name = str(rec.get(cfg.name_column) or "").strip()
        if not name or name == "nan":
            continue
        rows.append(
            {
                "dataset": dataset,
                "name": name,
                "gold_chebi": _norm_chebi(rec.get(cfg.gold_chebi_column))
                if cfg.gold_chebi_column
                else None,
                "gold_inchikey": _norm_ik(rec.get(ik_col)),
            }
        )
    rows = [r for r in rows if r["gold_chebi"] or r["gold_inchikey"]]
    return rows, {"card_source_sha256": bundle.card.get("source_sha256"), "route": prov}


async def _run(args: argparse.Namespace, out: Path) -> dict[str, Any]:
    rng = random.Random(args.seed)
    rows: list[dict[str, Any]] = []
    datasets: dict[str, Any] = {}
    for ds, cap in (("hajjar", args.hajjar), ("necs", args.necs)):
        if cap <= 0:
            continue
        loaded, meta = _load(ds)
        datasets[ds] = {**meta, "rows_with_gold": len(loaded)}
        rows += loaded if len(loaded) <= cap else rng.sample(loaded, cap)
    if args.limit:
        rows = rows[: args.limit]

    per_row = out / "rows.jsonl"
    t_start = time.time()
    async with BioMapperClient(anonymous=True, timeout=300) as client:
        # Pass 1: original mapping, file-job style.
        originals: list[MappingResult] = []
        for i in range(0, len(rows), 5):
            chunk = rows[i : i + 5]
            originals += await client.map_entities(
                [{"name": r["name"]} for r in chunk], entity_type=ENTITY_TYPE
            )
            await asyncio.sleep(PACE_S)
        t_pass1 = time.time() - t_start

        # Pass 2: weak rows re-mapped as a batch of one with candidates.
        with per_row.open("w") as fh:
            for row, orig in zip(rows, originals, strict=True):
                trig = _weak_trigger(orig)
                g_c, g_ik = row["gold_chebi"], row["gold_inchikey"]
                e_strict, e_len = _matches(_engine_ids(orig), g_c, g_ik)
                rec: dict[str, Any] = {
                    **row,
                    "weak_trigger": trig,
                    "engine_chosen": orig.chosen_kg_id,
                    "engine_cert": orig.certificate.state if orig.certificate else None,
                    "engine_correct_strict": e_strict,
                    "engine_correct_lenient": e_len,
                }
                if trig is not None:
                    t0 = time.time()
                    try:
                        (re,) = await client.map_entities(
                            [{"name": row["name"]}],
                            entity_type=ENTITY_TYPE,
                            kestrel_top_n=max(NS),
                        )
                        status, cands, eps = _candidate_rows(re)
                        rec["remap_error"] = re.error
                    except Exception as exc:  # recorded, never fatal
                        re, status, cands, eps = None, f"fetch_failed:{type(exc).__name__}", [], []
                        rec["remap_error"] = str(exc)[:200]
                    rec["remap_latency_s"] = round(time.time() - t0, 2)
                    rec["candidate_status"] = status
                    rec["endpoints"] = eps
                    rec["n_candidates"] = len(cands)
                    if re is not None:
                        rec["mismatch_choice"] = re.chosen_kg_id != orig.chosen_kg_id
                        rec["mismatch_cert"] = (
                            re.certificate.state if re.certificate else None
                        ) != rec["engine_cert"]
                    for n in NS:
                        hits = [
                            _matches(c.get("equivalent_ids") or [c["id"]], g_c, g_ik)
                            for c in cands[:n]
                        ]
                        rec[f"hit{n}_strict"] = any(h[0] for h in hits)
                        rec[f"hit{n}_lenient"] = any(h[1] for h in hits)
                    await asyncio.sleep(PACE_S)
                fh.write(json.dumps(rec) + "\n")
    return {
        "rows": len(rows),
        "pass1_s": round(t_pass1, 1),
        "total_s": round(time.time() - t_start, 1),
        "datasets": datasets,
    }


def _summarise(out: Path) -> dict[str, Any]:
    recs = [json.loads(line) for line in (out / "rows.jsonl").read_text().splitlines()]
    weak = [r for r in recs if r["weak_trigger"]]
    wrong = [r for r in recs if not r["engine_correct_strict"]]
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in weak:
        by[r["weak_trigger"]].append(r)
    by["ALL_WEAK"] = weak

    def rate(xs: list[dict[str, Any]], key: str) -> float | None:
        vals = [bool(x.get(key)) for x in xs if key in x]
        return round(sum(vals) / len(vals), 3) if vals else None

    triggers = {}
    for trig, xs in by.items():
        engine_wrong = [x for x in xs if not x["engine_correct_strict"]]
        triggers[trig] = {
            "n": len(xs),
            **{f"hit{n}_strict": rate(xs, f"hit{n}_strict") for n in NS},
            **{f"hit{n}_lenient_upper_bound": rate(xs, f"hit{n}_lenient") for n in NS},
            "engine_correct_strict": rate(xs, "engine_correct_strict"),
            "rescue_hit10_strict_among_engine_wrong": rate(engine_wrong, "hit10_strict"),
            "n_engine_wrong": len(engine_wrong),
            "mismatch_choice_rate_warm": rate(xs, "mismatch_choice"),
            "mismatch_cert_rate_warm": rate(xs, "mismatch_cert"),
            "candidate_status": dict(
                sorted(
                    {
                        s: sum(1 for x in xs if x.get("candidate_status") == s)
                        for s in {x.get("candidate_status") for x in xs}
                    }.items(),
                    key=lambda kv: str(kv[0]),
                )
            ),
            "median_remap_latency_s": (
                sorted(x["remap_latency_s"] for x in xs)[len(xs) // 2] if xs else None
            ),
        }
    go = [
        t
        for t, v in triggers.items()
        if t not in ("ALL_WEAK", *PROBE_CERTS)
        and v["n"] >= 5
        and (v["hit10_strict"] or 0) >= 0.30
        and (v["mismatch_choice_rate_warm"] or 0) <= 0.20
    ]
    return {
        "n_rows": len(recs),
        "n_weak": len(weak),
        "weak_share": round(len(weak) / len(recs), 3) if recs else None,
        "triggers": triggers,
        "go_criteria": (
            "any trigger with n>=5, hit@10 strict >= 0.30 and warm choice-mismatch <= 0.20"
        ),
        "go_triggers": go,
        "engine_wrong_strict": len(wrong),
        "engine_wrong_by_trigger": {
            str(k): sum(1 for r in wrong if r["weak_trigger"] == k)
            for k in {r["weak_trigger"] for r in wrong}
        },
        "verdict": "GO" if go else "NO-GO",
        "limitations": [
            "mismatch measured on the warm path only: the client cannot force a cold engine cache",
            "lenient (InChIKey connectivity block) hits are an upper bound; the block is not "
            "tautomer- or charge-invariant",
            "NECS gold carries ~5% InChIKey errors (see run_necs docstring)",
        ],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hajjar", type=int, default=100, help="max Hajjar rows (default all 100)")
    ap.add_argument("--necs", type=int, default=50, help="seeded NECS subsample size")
    ap.add_argument("--limit", type=int, default=0, help="total row cap (timing runs)")
    ap.add_argument("--seed", type=int, default=20261108)
    ap.add_argument("--out", type=Path, default=None, help="override output dir")
    args = ap.parse_args()

    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    out = args.out or Path("spike_results") / f"weak_candidates_{stamp}"
    out.mkdir(parents=True, exist_ok=False)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    ).stdout.strip()
    pins = collect_pins(probe=True, base_url=DEFAULT_BASE_URL, api_key=None)
    run = asyncio.run(_run(args, out))
    summary = _summarise(out)
    (out / "summary.json").write_text(
        json.dumps(
            {"args": vars(args) | {"out": str(out)}, "git_commit": commit, "pins": pins,
             "run": run, **summary},
            indent=1,
            default=str,
        )
    )
    print(json.dumps({k: summary[k] for k in ("n_rows", "n_weak", "verdict", "go_triggers")}))
    print(f"saved: {out}")


if __name__ == "__main__":
    main()
