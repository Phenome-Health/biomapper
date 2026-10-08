"""Unit 6: success criteria, offline, on the committed UK Biobank x Arivale clinical-lab replay.

The replay (``tests/fixtures/cohorts_labs_cm/``, built by ``scripts/build_cohorts_labs_replay.py``)
holds every mapping result ``harmonize_cohorts(..., entity="labs")`` needs on these panels at KRAKEN
kg 2.3.0 / 3dd08a5b. The 37 pairs / 21 one-to-one target holds only at that build.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import warnings
from pathlib import Path
from typing import Any

import pytest

from biomapper.cohorts import harmonize_cohorts
from biomapper.models import MappingResult
from tests.cohorts_helpers import FakeMapper, mr

FIXTURE = Path(__file__).parent / "fixtures" / "cohorts_labs_cm"
MANIFEST = json.loads((FIXTURE / "manifest.json").read_text())


def _replay_key(entity_type: str, name: str, identifiers: dict, mode: str) -> str:
    payload = json.dumps(
        {"t": entity_type, "n": name, "i": {k: sorted(v) for k, v in sorted(identifiers.items())},
         "m": mode},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class ReplayMapper:
    """Serves recorded results by (entity type, name, identifiers, annotation mode).

    A request missing from the replay fails loudly: it means the protocol sent something the
    recorded run never did (for example, a one-sided LOINC code as mapping input).
    """

    pins = MANIFEST["pin"]

    def __init__(self) -> None:
        with gzip.open(FIXTURE / "replay.json.gz", "rt") as fh:
            self.table: dict[str, dict[str, Any]] = json.load(fh)
        self.requests: list[tuple[str, dict, str]] = []

    def __call__(self, records, *, entity_type, annotation_mode):  # noqa: ANN001, ANN204
        out = []
        for r in records:
            ids = r.get("identifiers") or {}
            self.requests.append((r["name"], ids, annotation_mode))
            key = _replay_key(entity_type, r["name"], ids, annotation_mode)
            assert key in self.table, f"not in replay: {r['name']!r} {ids} {annotation_mode}"
            out.append(MappingResult.model_validate(self.table[key]))
        return out


def test_fixture_checksums_match_the_manifest() -> None:
    for name, digest in MANIFEST["sha256"].items():
        assert hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest() == digest, name
    assert MANIFEST["pin"]["required"] == ["2.3.0", "2.2.0", "3dd08a5b"]
    assert MANIFEST["pin"]["kestrel_before"]["git_commit"].startswith("3dd08a5b")


@pytest.fixture(scope="module")
def labs_run() -> tuple[Any, ReplayMapper]:
    mapper = ReplayMapper()
    with pytest.warns(UserWarning, match="LOINC is declared only for 'arivale'"):
        report = harmonize_cohorts(
            FIXTURE / "ukbb_labs.tsv",
            FIXTURE / "arivale_labs.tsv",
            entity="labs",
            a_key_column="key",
            b_key_column="key",
            b_vocabularies={"LOINC": ["Labcorp LOINC ID", "Quest LOINC ID"]},
            a_label="ukbb",
            b_label="arivale",
            mapper=mapper,
            probe_pins=False,
            save=False,
        )
    return report, mapper


def test_labs_reproduce_the_names_only_clinical_measurement_result(labs_run) -> None:
    report, _ = labs_run
    s = report.summary()
    assert s["category"] == "biolink:ClinicalMeasurement"
    assert s["arms"]["names_only"]["n_links"] == 37
    assert s["arms"]["names_only"]["n_one_to_one"] == 21
    assert s["arms"]["names_only"]["ukbb"]["total"] == 65
    assert s["arms"]["names_only"]["arivale"]["total"] == 128
    assert s["arms"]["names_only"]["arivale"]["unresolved"] == 1


def test_labs_loinc_is_one_sided_never_supplied_and_the_identifier_arm_is_skipped(labs_run) -> None:
    report, mapper = labs_run
    assert report.settings["vocabularies"] == {"shared": [], "one_sided": {"LOINC": "arivale"}}
    assert report.identifier is None and report.diff is None
    assert "no vocabulary is shared" in report.identifier_skipped_reason
    assert any("LOINC" in w for w in report.warnings)
    # LOINC only ever travels in the code-only review requests, never as mapping input.
    assert all(mode == "none" for _, ids, mode in mapper.requests if ids)
    assert sum(1 for _, ids, _ in mapper.requests if not ids) == 65 + 128


def test_labs_review_queue_lists_glucose_and_matches_the_recorded_counts(labs_run) -> None:
    report, _ = labs_run
    by_status = report.summary()["review"]["by_status"]
    assert by_status == MANIFEST["expected"]["review_by_status"]
    queue = report.review_queue
    assert queue
    glucose = [i for i in queue if i.key == "GLUCOSE|Glucose"]
    assert len(glucose) == 1  # Labcorp and Quest both give 2345-7: one line, not two
    item = glucose[0]
    assert (item.vocabulary, item.code, item.status) == ("LOINC", "2345-7", "disagree")
    assert item.code_entry == "LOINC:2345-7"
    assert item.name_entry != item.code_entry
    # Every coded Arivale row appears once per distinct code.
    assert len(report.review) == sum(by_status.values()) == 92


def test_metabolites_with_hmdb_on_both_sides_supply_it_and_report_both_arms() -> None:
    a = [
        {"BIOCHEMICAL_NAME": "glucose", "HMDB": "HMDB0000122"},
        {"BIOCHEMICAL_NAME": "lactate", "HMDB": "HMDB0000190"},
    ]
    b = [
        {"title": "Glucose", "hmdb_id": "HMDB0000122"},
        {"title": "Lactate", "hmdb_id": None},
    ]

    def resolve(name: str, ids: dict, mode: str, etype: str) -> MappingResult:
        assert etype == "biolink:SmallMolecule"
        if ids:
            return mr(name, "CHEBI:17234", {"HMDB": ["HMDB0000122"]}) if "HMDB0000122" in ids[
                "HMDB"] else mr(name, "CHEBI:24996", {"HMDB": ["HMDB0000190"]})
        return {
            "glucose": mr(name, "CHEBI:4167"),
            "Glucose": mr(name, "CHEBI:17234", {"HMDB": ["HMDB0000122"]}),
            "lactate": mr(name, "CHEBI:24996", {"CHEBI": ["24996"]}),
            "Lactate": mr(name, "CHEBI:24996", {"CHEBI": ["24996"]}),
        }[name]

    mapper = FakeMapper(resolve)
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # a shared vocabulary must not warn
        report = harmonize_cohorts(
            a, b, entity="metabolites",
            a_name_column="BIOCHEMICAL_NAME", b_name_column="title",
            a_vocabularies={"HMDB": "HMDB"}, b_vocabularies={"hmdb": ["hmdb_id"]},
            a_label="arivale", b_label="ukbb", mapper=mapper, probe_pins=False, save=False,
        )
    s = report.summary()
    assert s["vocabularies"] == {"shared": ["HMDB"], "one_sided": {}}
    assert s["arms"]["identifier"] is not None
    supplied = [ids for _, ids, mode in mapper.sent if ids]
    assert supplied == [{"HMDB": ["HMDB0000122"]}, {"HMDB": ["HMDB0000190"]},
                        {"HMDB": ["HMDB0000122"]}]
    assert s["arms"]["names_only"]["n_links"] == 2  # lactate by node; glucose by casefold name
    assert s["arms"]["identifier"]["n_links"] == 2
    assert s["arms"]["names_only"]["links_by_basis"]["name_casefold"] == 1
    assert s["arms"]["identifier"]["links_by_basis"]["node"] == 2
    assert s["diff"] == {"names_only_only": 0, "identifier_only": 0, "both": 2}
    assert report.review == ()
