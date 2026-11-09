"""Replay loaders shared by the harmonize_many() tests.

They read the UK Biobank x Arivale milestone replays shipped in ``notebooks/data/ukbb_arivale``
(real recorded API payloads at KRAKEN kg 2.3.0 / 3dd08a5b, keyed by sha256 of entity type, name and
submitted identifiers) so the N-way bookkeeping is exercised on production payload shapes.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
from functools import cache
from pathlib import Path
from typing import Any

from biomapper.models import MappingResult

DATA = Path(__file__).resolve().parents[1] / "notebooks" / "data" / "ukbb_arivale"
PANELS = DATA / "panels"
REPLAY = DATA / "replay"
CATEGORY = {"metabolites": "biolink:SmallMolecule", "proteins": "biolink:Protein"}


def replay_key(entity_type: str, name: str, identifiers: dict[str, list[str]]) -> str:
    payload = json.dumps(
        {"t": entity_type, "n": name, "i": {k: sorted(v) for k, v in sorted(identifiers.items())}},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


@cache
def replay_table(entity: str) -> dict[str, dict[str, Any]]:
    with gzip.open(REPLAY / f"{entity}_replay.json.gz", "rt") as fh:
        return json.load(fh)  # type: ignore[no-any-return]


def replay_result(entity: str, name: str) -> MappingResult:
    """The recorded names-only result for ``name``."""
    raw = replay_table(entity)[replay_key(CATEGORY[entity], name, {})]
    return MappingResult.model_validate(raw)


def metabolite_panels() -> dict[str, list[dict[str, str]]]:
    """The two metabolite panels as records (Arivale's unnamed ``X - 12345`` features dropped)."""
    with (PANELS / "metabolites_ukbb_ukbb_nmr_meta.tsv").open(encoding="utf-8") as fh:
        ukbb = [
            {"key": f"{r['field_id']}|{r['title'].strip()}", "name": r["title"].strip()}
            for r in csv.DictReader(fh, delimiter="\t") if (r.get("title") or "").strip()
        ]
    with (PANELS / "metabolites_arivale_metabolomics_metadata.tsv").open(encoding="utf-8") as fh:
        lines = [line for line in fh if not line.startswith("#")]
    arivale = []
    for r in csv.DictReader(lines, delimiter="\t"):
        name = (r.get("BIOCHEMICAL_NAME") or "").strip()
        if not name or (name.startswith("X - ") and name[4:].strip().isdigit()):
            continue
        arivale.append({"key": f"{r['CHEMICAL_ID']}|{name}", "name": name,
                        "HMDB": r.get("HMDB") or "", "KEGG": r.get("KEGG") or ""})
    return {"ukbb": ukbb, "arivale": arivale}


def protein_names() -> list[str]:
    """Every distinct name the protein replay holds a names-only result for, sorted."""
    names = {v["query_name"] for v in replay_table("proteins").values()}
    return sorted(n for n in names if replay_key(CATEGORY["proteins"], n, {})
                  in replay_table("proteins"))


class NameReplayMapper:
    """Serves recorded names-only results; any request with identifiers fails loudly."""

    def __init__(self, entity: str) -> None:
        self.entity = entity
        self.sent: list[str] = []

    def __call__(self, records, *, entity_type, annotation_mode):  # noqa: ANN001, ANN204
        assert entity_type == CATEGORY[self.entity]
        out = []
        for r in records:
            assert not r.get("identifiers"), f"identifiers sent: {r}"
            self.sent.append(r["name"])
            out.append(replay_result(self.entity, r["name"]))
        return out


def write_three_metabolite_cohorts(directory: Path) -> dict[str, Path]:
    """A three-cohort small-molecule fixture whose every name has a recorded real payload.

    ``ukbb_nmr`` is part of UK Biobank's NMR panel (its first 50 rows and every row that links to
    Arivale); ``arivale_a`` and ``arivale_b`` are two overlapping
    slices of the Arivale panel (rows 0-59 and 30-89, plus every Arivale row that links to a UK
    Biobank row), so all three pairs share rows. Arivale's HMDB and KEGG columns ride along as
    declared identifiers. Names are verbatim, so the shipped milestone replay serves every one.
    """
    panels = metabolite_panels()
    ukbb, arivale = panels["ukbb"], panels["arivale"]
    nodes_ukbb = {replay_result("metabolites", r["name"]).chosen_kg_id for r in ukbb}
    partners = [r for r in arivale
                if replay_result("metabolites", r["name"]).chosen_kg_id in nodes_ukbb]
    nodes_arivale = {replay_result("metabolites", r["name"]).chosen_kg_id for r in arivale}
    ukbb_rows = list({r["key"]: r for r in [
        *ukbb[:50],
        *(r for r in ukbb if replay_result("metabolites", r["name"]).chosen_kg_id in nodes_arivale),
    ]}.values())
    slices = {
        "arivale_a": [*arivale[:60], *partners[::2]],
        "arivale_b": [*arivale[30:90], *partners[1::2]],
    }
    directory.mkdir(parents=True, exist_ok=True)
    out = {"ukbb_nmr": directory / "ukbb_nmr.tsv"}
    with out["ukbb_nmr"].open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n")
        w.writerow(["key", "title"])
        w.writerows([r["key"], r["name"]] for r in ukbb_rows)
    for label, rows in slices.items():
        seen: set[str] = set()
        out[label] = directory / f"{label}.tsv"
        with out[label].open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh, delimiter="\t", lineterminator="\n")
            w.writerow(["key", "BIOCHEMICAL_NAME", "HMDB", "KEGG"])
            for r in rows:
                if r["key"] not in seen:
                    seen.add(r["key"])
                    w.writerow([r["key"], r["name"], r["HMDB"], r["KEGG"]])
    return out
