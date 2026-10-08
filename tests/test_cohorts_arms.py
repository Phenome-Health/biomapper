"""Unit 3: names-only arm always; identifier arm only for vocabularies both cohorts declare."""

from __future__ import annotations

import warnings

import pytest

from biomapper.cohorts import BatchedApiMapper, classify_vocabularies, harmonize_cohorts
from biomapper.models import MappingResult
from tests.cohorts_helpers import FakeMapper, mr

UKBB = [{"field_id": "30740", "title": "Glucose"}, {"field_id": "30670", "title": "Urea"}]
ARIVALE = [
    {"Name": "GLUCOSE", "Display": "Glucose", "Labcorp LOINC ID": "2345-7", "Quest LOINC ID": None},
    {"Name": "UREA", "Display": "Urea", "Labcorp LOINC ID": None, "Quest LOINC ID": "3094-0"},
    {"Name": "NOTE", "Display": "Comment", "Labcorp LOINC ID": None, "Quest LOINC ID": None},
]


def _labs_resolver(name: str, ids: dict, mode: str, etype: str) -> MappingResult:
    if ids:  # code-only review requests: the LOINC code's own entry
        code = next(iter(ids.values()))[0]
        return mr(name, f"LOINC:{code}", {"LOINC": [code]})
    table = {"Glucose": "LOINC:33352-6", "Urea": "LOINC:3094-0"}
    chosen = table.get(name)
    return mr(name, chosen, {"LOINC": [chosen.split(":")[1]]} if chosen else None)


def _run(tmp_path, mapper, **kwargs):
    defaults = dict(
        entity="labs",
        a_name_column="title",
        b_name_column="Display",
        a_key_column="field_id",
        b_key_column="Name",
        a_label="ukbb",
        b_label="arivale",
        mapper=mapper,
        probe_pins=False,
        output_dir=tmp_path / "run",
    )
    defaults.update(kwargs)
    return harmonize_cohorts(UKBB, ARIVALE, **defaults)


def test_classification_shared_and_one_sided() -> None:
    shared, one_sided = classify_vocabularies(
        {"HMDB": ("h",), "LOINC": ("l",)}, {"HMDB": ("x",), "KEGG": ("k",)}, "a", "b"
    )
    assert shared == ("HMDB",)
    assert one_sided == {"LOINC": "a", "KEGG": "b"}


def test_no_vocabulary_declared_runs_names_only(tmp_path) -> None:
    mapper = FakeMapper(_labs_resolver)
    report = _run(tmp_path, mapper)
    assert report.identifier is None
    assert "no identifier columns" in report.identifier_skipped_reason
    # Two names-only calls (one per cohort), every request carrying no identifiers.
    assert [c.annotation_mode for c in mapper.calls] == ["missing", "missing"]
    assert all(ids == {} for _, ids, _ in mapper.sent)
    assert {c.entity_type for c in mapper.calls} == {"biolink:ClinicalMeasurement"}
    assert report.review == ()


def test_one_sided_loinc_warns_skips_the_identifier_arm_and_is_never_sent(tmp_path) -> None:
    mapper = FakeMapper(_labs_resolver)
    with pytest.warns(UserWarning, match="LOINC"):
        report = _run(
            tmp_path, mapper, b_vocabularies={"LOINC": ["Labcorp LOINC ID", "Quest LOINC ID"]}
        )
    assert report.identifier is None
    assert "no vocabulary is shared" in report.identifier_skipped_reason
    assert report.settings["vocabularies"]["one_sided"] == {"LOINC": "arivale"}
    assert any("LOINC" in w and "arivale" in w for w in report.warnings)
    # The only requests that carry LOINC are the code-only review requests (annotation_mode none).
    for name, ids, mode in mapper.sent:
        if ids:
            assert mode == "none", (name, ids, mode)
    names_only = [s for s in mapper.sent if s[2] == "missing"]
    assert all(ids == {} for _, ids, _ in names_only)


def test_shared_hmdb_runs_the_identifier_arm_on_coded_rows_only(tmp_path) -> None:
    a = [{"name": "Glucose", "hmdb": "HMDB0000122"}, {"name": "Urea", "hmdb": None}]
    b = [{"name": "D-Glucose", "HMDB": "HMDB0000122", "kegg": "C00031"}, {"name": "Urea"}]

    def resolve(name: str, ids: dict, mode: str, etype: str) -> MappingResult:
        if "HMDB" in ids:
            return mr(name, "CHEBI:17234", {"HMDB": ["HMDB0000122"]})
        if "KEGG" in ids:
            return mr(name, "KEGG:C00031", {"KEGG": ["C00031"]})
        return {
            "Glucose": mr(name, "CHEBI:4167", {"CHEBI": ["4167"]}),
            "D-Glucose": mr(name, "CHEBI:17634", {"CHEBI": ["17634"]}),
            "Urea": mr(name, "CHEBI:16199", {"CHEBI": ["16199"]}),
        }[name]

    mapper = FakeMapper(resolve)
    with pytest.warns(UserWarning, match="KEGG"):
        report = harmonize_cohorts(
            a,
            b,
            entity="metabolites",
            a_vocabularies={"HMDB": "hmdb"},
            b_vocabularies={"hmdb": "HMDB", "KEGG": "kegg"},
            mapper=mapper,
            probe_pins=False,
            save=False,
        )
    assert report.settings["vocabularies"]["shared"] == ["HMDB"]
    assert report.identifier is not None
    id_calls = [c for c in mapper.calls if c.annotation_mode == "missing" and any(
        r.get("identifiers") for r in c.records)]
    # Only the rows that carry an HMDB value were re-mapped, and only HMDB was supplied.
    assert [r["name"] for c in id_calls for r in c.records] == ["Glucose", "D-Glucose"]
    assert all(set(r["identifiers"]) == {"HMDB"} for c in id_calls for r in c.records)
    # The normalized vocabulary key is what is sent, never the declared column-form string.
    assert id_calls[1].records[0]["identifiers"] == {"HMDB": ["HMDB0000122"]}
    # Names-only: glucose does not link (different nodes, different names); identifier arm: it does.
    names_pairs = {(lk.a_key, lk.b_key) for lk in report.names_only.links}
    id_pairs = {(lk.a_key, lk.b_key) for lk in report.identifier.links}
    assert ("0|Glucose", "0|D-Glucose") not in names_pairs
    assert ("0|Glucose", "0|D-Glucose") in id_pairs
    # Urea was not re-mapped, so its names-only result is reused and still links in both arms.
    assert ("1|Urea", "1|Urea") in names_pairs & id_pairs
    # Metabolites link by name as well, as harmonize() does today.
    assert report.settings["link_by_name"] is True


def test_identifier_arm_with_shared_vocab_but_no_values_reuses_names_only(tmp_path) -> None:
    a = [{"name": "Urea", "hmdb": None}]
    b = [{"name": "Urea", "hmdb": ""}]
    mapper = FakeMapper(lambda n, i, m, t: mr(n, "CHEBI:16199"))
    report = harmonize_cohorts(
        a, b, entity="metabolites", a_vocabularies={"HMDB": "hmdb"},
        b_vocabularies={"HMDB": "hmdb"}, mapper=mapper, probe_pins=False, save=False,
    )
    assert len(mapper.calls) == 2  # names-only only; nothing carried an HMDB value
    assert report.identifier is not None
    assert report.identifier.n_links == report.names_only.n_links == 1


def test_chunk_errors_become_counted_error_rows_not_exceptions(tmp_path) -> None:
    def resolve(name: str, ids: dict, mode: str, etype: str) -> MappingResult:
        if name == "Urea":
            return mr(name, error="ReadTimeout: chunk failed")
        return _labs_resolver(name, ids, mode, etype)

    report = _run(tmp_path, FakeMapper(resolve))
    s = report.summary()["arms"]["names_only"]
    assert s["ukbb"]["errors"] == 1
    assert s["arivale"]["errors"] == 1


def test_mapper_returning_the_wrong_length_raises(tmp_path) -> None:
    def short(records, *, entity_type, annotation_mode):
        return []

    with pytest.raises(RuntimeError, match="returned 0 results for 2"):
        _run(tmp_path, short)


def test_batched_api_mapper_batches_and_forwards_settings(monkeypatch) -> None:
    seen: list[dict] = []

    def fake_map_entities(records, **kwargs):
        seen.append({"n": len(records), **kwargs})
        return [mr(r["name"]) for r in records]

    monkeypatch.setattr("biomapper.cohorts.map_entities", fake_map_entities)
    mapper = BatchedApiMapper(batch_size=2, timeout=123.0, base_url="https://x.invalid/api")
    out = mapper(
        [{"name": n} for n in "abcde"], entity_type="biolink:Gene", annotation_mode="none"
    )
    assert [r.query_name for r in out] == list("abcde")
    assert [s["n"] for s in seen] == [2, 2, 1]
    assert all(s["timeout"] == 123.0 for s in seen)
    assert all(s["entity_type"] == "biolink:Gene" and s["annotation_mode"] == "none" for s in seen)
    assert all(s["base_url"] == "https://x.invalid/api" for s in seen)


def test_batched_api_mapper_progress_prints(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "biomapper.cohorts.map_entities", lambda records, **kw: [mr(r["name"]) for r in records]
    )
    BatchedApiMapper(batch_size=1, progress=True)(
        [{"name": "a"}, {"name": "b"}], entity_type="biolink:Gene", annotation_mode="missing"
    )
    assert "2/2" in capsys.readouterr().err


def test_batch_size_must_be_positive() -> None:
    with pytest.raises(ValueError):
        BatchedApiMapper(batch_size=0)


def test_default_mapper_uses_the_batching_and_timeout_settings(tmp_path, monkeypatch) -> None:
    calls: list[dict] = []

    def fake_map_entities(records, **kwargs):
        calls.append({"n": len(records), **kwargs})
        return [mr(r["name"]) for r in records]

    monkeypatch.setattr("biomapper.cohorts.map_entities", fake_map_entities)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        report = harmonize_cohorts(
            [{"name": n} for n in "abc"],
            [{"name": "a"}],
            entity="genes",
            batch_size=2,
            timeout=99.0,
            probe_pins=False,
            save=False,
        )
    assert [c["n"] for c in calls] == [2, 1, 1]
    assert {c["timeout"] for c in calls} == {99.0}
    assert report.settings["batch_size"] == 2
    assert report.settings["timeout_s"] == 99.0
    assert report.settings["mapper"] == "BatchedApiMapper"
