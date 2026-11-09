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
