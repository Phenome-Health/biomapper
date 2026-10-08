"""Build the committed UK Biobank x Arivale clinical-lab replay for the harmonize_cohorts() tests.

Writes ``tests/fixtures/cohorts_labs_cm/``:

- ``ukbb_labs.tsv`` / ``arivale_labs.tsv``: the lab panels after the SOP notebook's inclusion rules
  (65 UK Biobank fields, 128 Arivale tests). Panel metadata only: keys, names, LOINC codes.
- ``replay.json.gz``: every mapping result ``harmonize_cohorts(..., entity="labs")`` needs on these
  panels, keyed by sha256 of (entity type, name, identifiers sent, annotation mode).
- ``manifest.json``: pins, sources, checksums and the cross-check against the earlier S16 run.

Two sources, both at KRAKEN kg 2.3.0 / package 2.2.0 / build 3dd08a5b:

1. **Names-only arm** (both cohorts, ``biolink:ClinicalMeasurement``, no identifiers): the stored
   S9 run (``labs_category_measurement_20261006T184300Z/scenario_results.json``), which carries the
   ``MappingResult`` fields linking reads. Reused, not re-mapped, because S9 *is* the success
   criterion (37 pairs, 21 one-to-one).
2. **Review pass** (Arivale LOINC codes, ``annotation_mode="none"``, one request per row and code):
   no stored run sent these requests (S16 sent each row's codes together, so rows with two
   different codes differ), so they are mapped ONCE live through ``harmonize_cohorts``'s default
   mapper, after checking Kestrel ``/health`` reports the pinned build. The run stops if it does
   not, and re-checks the build afterwards.

Usage (from the repository root, network required for step 2)::

    RUNS=~/harmonization_runs
    python scripts/build_cohorts_labs_replay.py \\
        --s9 $RUNS/labs_category_measurement_20261006T184300Z/scenario_results.json \\
        --s16 $RUNS/labs_edge_rollup_20261008T171112Z/scenario_results.json
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import re
import sys
import tempfile
import warnings
from pathlib import Path
from typing import Any

from biomapper._provenance import fetch_kg_build_info
from biomapper.cohorts import BatchedApiMapper, harmonize_cohorts, one_to_one
from biomapper.models import MappingResult

REPO = Path(__file__).resolve().parent.parent
PANELS = REPO / "notebooks" / "data" / "ukbb_arivale" / "panels"
OUT = REPO / "tests" / "fixtures" / "cohorts_labs_cm"
CATEGORY = "biolink:ClinicalMeasurement"
PIN = ("2.3.0", "2.2.0", "3dd08a5b")

# The SOP notebook's lab inclusion rules (notebooks/ukbb_arivale_harmonization_sop.ipynb, Step 2).
QC = re.compile(
    r"(freeze-thaw cycles|acquisition time|acquisition route|device ID|missing reason"
    r"|reportability|result flag|correction level|correction reason|aliquot|assay date"
    r"|plate barcode|well position|sign-off timestamp|\(interim dataset\)|date sent)",
    re.I,
)
PLACEHOLDER = re.compile(r"^Field\s+\d+$", re.I)
NOT_A_LAB_ANALYTE = {
    "Rate of ramp phase load increase (exercise test)",
    "V02max per kg bodyweight estimated from the exercise test",
}


def blank(v: Any) -> bool:  # noqa: ANN401
    return v is None or str(v).strip() in ("", "nan", "NaN", "NA")


def clean_id(v: Any) -> str | None:  # noqa: ANN401
    if blank(v):
        return None
    s = str(v).strip()
    return s[:-2] if s.endswith(".0") and s[:-2].isdigit() else s


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as fh:
        lines = [line for line in fh if not line.startswith("#")]
    return list(csv.DictReader(lines, delimiter="\t"))


def ukbb_rows() -> list[dict[str, str]]:
    out = []
    for r in read_tsv(PANELS / "labs_ukbb_ukbb_chemistry_full_20250924.tsv"):
        if all(blank(v) for v in r.values()):
            continue
        name = str(r.get("field_name") or "").strip()
        dtype = str(r.get("data_type") or "").strip()
        if PLACEHOLDER.match(name) or QC.search(name):
            continue
        if dtype not in ("Continuous", "Unknown"):
            continue
        if name in NOT_A_LAB_ANALYTE:
            continue
        out.append({"key": f"{r['field_id']}|{name}", "name": name})
    return out


def arivale_rows() -> list[dict[str, str]]:
    out = []
    for r in read_tsv(PANELS / "labs_arivale_chemistries_metadata.tsv"):
        disp, raw = r.get("Display Name"), r.get("Name")
        name = disp if not blank(disp) else raw
        if blank(name):
            continue
        out.append({
            "key": f"{str(raw).strip()}|{str(name).strip()}",
            "name": str(name).strip(),
            "Labcorp LOINC ID": clean_id(r.get("Labcorp LOINC ID")) or "",
            "Quest LOINC ID": clean_id(r.get("Quest LOINC ID")) or "",
        })
    return out


def write_tsv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]), delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def replay_key(entity_type: str, name: str, identifiers: dict[str, list[str]], mode: str) -> str:
    payload = json.dumps(
        {"t": entity_type, "n": name, "i": {k: sorted(v) for k, v in sorted(identifiers.items())},
         "m": mode},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def slim(r: MappingResult) -> dict[str, Any]:
    return {
        "query_name": r.query_name,
        "resolved": r.resolved,
        "chosen_kg_id": r.chosen_kg_id,
        "kg_equivalent_ids": r.kg_equivalent_ids,
        "error": r.error,
    }


def from_stored(x: dict[str, Any]) -> MappingResult:
    return MappingResult(
        query_name=x["name"],
        resolved=bool(x.get("chosen_kg_id")),
        chosen_kg_id=x.get("chosen_kg_id") or None,
        kg_equivalent_ids=x.get("kg_equivalent_ids") or {},
        error=x.get("error") or None,
    )


class Recorder:
    """Names-only requests from the stored S9 run; review requests live; everything recorded."""

    def __init__(self, s9: dict[str, MappingResult]) -> None:
        self.s9 = s9
        self.live = BatchedApiMapper(batch_size=10, timeout=300.0, progress=True)
        self.entries: dict[str, dict[str, Any]] = {}
        self.sources: dict[str, int] = {"stored_s9": 0, "live": 0}

    def __call__(self, records, *, entity_type, annotation_mode):  # noqa: ANN001, ANN204
        if annotation_mode == "none":
            results = self.live(records, entity_type=entity_type, annotation_mode=annotation_mode)
            source = "live"
        else:
            assert all(not r.get("identifiers") for r in records), "LOINC must never be supplied"
            results = [self.s9[r["name"]] for r in records]
            source = "stored_s9"
        for r, res in zip(records, results, strict=True):
            k = replay_key(entity_type, r["name"], r.get("identifiers") or {}, annotation_mode)
            self.entries[k] = {"source": source, "result": slim(res)}
            self.sources[source] += 1
        return results


def check_build(stage: str) -> dict[str, Any]:
    version, kg, error = fetch_kg_build_info()
    got = (kg.kg_version, kg.kraken_package_version, kg.git_commit[:8])
    if error or got != PIN:
        sys.exit(f"{stage}: Kestrel reports {got} (error={error}); expected {PIN}. Stopping.")
    return {"kestrel_version": version, **kg.model_dump(include={
        "kg_version", "kraken_package_version", "git_commit", "biolink_version",
        "build_timestamp"})}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--s9", type=Path, required=True)
    ap.add_argument("--s16", type=Path, required=True)
    args = ap.parse_args()

    ukbb, ariv = ukbb_rows(), arivale_rows()
    s9 = json.loads(args.s9.read_text())
    s9_u, s9_a = s9["ukbb"][CATEGORY], s9["arivale"]["S9"]
    assert s9["pin"]["git_commit"] == PIN[2] and s9["pin"]["kg_version"] == PIN[0], s9["pin"]
    # The fixture panels must be exactly the records S9 mapped, in the same order.
    assert [r["key"] for r in ukbb] == [x["key"] for x in s9_u], "UK Biobank records differ from S9"
    assert [r["key"] for r in ariv] == [x["key"] for x in s9_a], "Arivale records differ from S9"
    by_name: dict[str, MappingResult] = {}
    for x in s9_u + s9_a:
        assert not x.get("submitted_identifiers"), "S9 is names-only"
        prev = by_name.setdefault(x["name"], from_stored(x))
        assert prev.chosen_kg_id == (x.get("chosen_kg_id") or None), f"S9 disagrees on {x['name']}"

    OUT.mkdir(parents=True, exist_ok=True)
    write_tsv(OUT / "ukbb_labs.tsv", ukbb)
    write_tsv(OUT / "arivale_labs.tsv", ariv)

    before = check_build("before mapping")
    recorder = Recorder(by_name)
    with tempfile.TemporaryDirectory() as tmp, warnings.catch_warnings():
        warnings.simplefilter("ignore")
        report = harmonize_cohorts(
            OUT / "ukbb_labs.tsv",
            OUT / "arivale_labs.tsv",
            entity="labs",
            a_key_column="key",
            b_key_column="key",
            b_vocabularies={"LOINC": ["Labcorp LOINC ID", "Quest LOINC ID"]},
            a_label="ukbb",
            b_label="arivale",
            mapper=recorder,
            output_dir=Path(tmp) / "run",
        )
    after = check_build("after mapping")
    assert before == after

    # Cross-check the live review pass against S16 where the request was the same (one code).
    s16 = {x["key"]: x for x in json.loads(args.s16.read_text())["arivale"]["S16"]}
    same = differ = 0
    for item in report.review:
        prior = s16.get(item.key)
        if prior and prior["submitted_identifiers"].get("LOINC") == [item.code]:
            if (prior.get("chosen_kg_id") or None) == item.code_entry:
                same += 1
            else:
                differ += 1

    with gzip.open(OUT / "replay.json.gz", "wt", compresslevel=9) as fh:
        json.dump({k: v["result"] for k, v in sorted(recorder.entries.items())}, fh,
                  sort_keys=True)
    s = report.summary()
    manifest = {
        "description": (
            "UK Biobank x Arivale clinical labs replay for harmonize_cohorts(entity='labs'): "
            "panel metadata only (keys, names, LOINC codes), no participant data."
        ),
        "category": CATEGORY,
        "replay_key": "sha256 of json {t: entity type, n: name, i: identifiers sent (sorted), "
                      "m: annotation_mode}, sort_keys",
        "sources": {
            "names_only": f"stored S9 run {args.s9.parent.name} (map_entity, mode missing, "
                          "no identifiers), reused",
            "review": "live re-map of Arivale LOINC codes, one per request, annotation_mode none, "
                      "via BatchedApiMapper (batch 10, timeout 300 s)",
            "requests_by_source": recorder.sources,
        },
        "pin": {"required": list(PIN), "kestrel_before": before, "kestrel_after": after,
                "s9_pin": s9["pin"], "api": report.pins["api"],
                "biomapper_version": report.pins["biomapper_version"],
                "built_utc": report.pins["timestamp_utc"]},
        "cross_check_vs_S16": {
            "single_code_rows_compared": same + differ, "same_entry": same, "different": differ,
            "source": args.s16.parent.name,
        },
        "expected": {
            "names_only_pairs": s["arms"]["names_only"]["n_links"],
            "names_only_one_to_one": s["arms"]["names_only"]["n_one_to_one"],
            "review_by_status": s["review"]["by_status"],
        },
        "sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(OUT.iterdir()) if p.name != "manifest.json"
        },
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    n = report.names_only
    print(f"names-only: {n.n_links} pairs, {len(one_to_one(n.links))} one-to-one")
    print("review:", s["review"]["by_status"], f"| vs S16 same {same}, different {differ}")
    print("wrote", OUT.relative_to(REPO))


if __name__ == "__main__":
    main()
