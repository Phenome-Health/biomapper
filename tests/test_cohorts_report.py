"""Unit 5: the report (diff, link bases, one-to-one, pins) and its timestamped output directory."""

from __future__ import annotations

import json
import re
import warnings
from pathlib import Path

import httpx
import pytest

from biomapper.cohorts import compute_diff, harmonize_cohorts, one_to_one
from biomapper.harmonize.linking import Link
from biomapper.models import MappingResult
from tests.cohorts_helpers import FakeMapper, mr


def _link(a: str, b: str) -> Link:
    return Link(a_key=a, b_key=b, shared=frozenset())


def test_one_to_one_on_a_three_by_two_grid_is_empty() -> None:
    grid = [_link(a, b) for a in ("a1", "a2", "a3") for b in ("b1", "b2")]
    assert len(grid) == 6
    assert one_to_one(grid) == []


def test_one_to_one_keeps_only_exclusive_pairs() -> None:
    links = [_link("a1", "b1"), _link("a2", "b2"), _link("a2", "b3")]
    assert [(lk.a_key, lk.b_key) for lk in one_to_one(links)] == [("a1", "b1")]


def test_diff_by_key_pair() -> None:
    names = [_link("a1", "b1"), _link("a2", "b2")]
    ids = [_link("a2", "b2"), _link("a3", "b3")]
    diff = compute_diff(names, ids)
    assert diff.names_only_only == (("a1", "b1"),)
    assert diff.identifier_only == (("a3", "b3"),)
    assert diff.both == (("a2", "b2"),)


@pytest.mark.parametrize("label", ["n_links", "links_by_basis", "n_one_to_one"])
def test_reserved_labels_raise_before_any_mapping(label: str) -> None:
    mapper = FakeMapper(lambda n, i, m, t: mr(n))
    with pytest.raises(ValueError, match="reserved"):
        harmonize_cohorts(
            [{"name": "x"}], [{"name": "x"}], entity="labs", a_label=label,
            mapper=mapper, probe_pins=False, save=False,
        )
    assert mapper.calls == []


def test_labels_with_colliding_filenames_raise_before_any_mapping() -> None:
    mapper = FakeMapper(lambda n, i, m, t: mr(n))
    with pytest.raises(ValueError, match="overwrite"):
        harmonize_cohorts(
            [{"name": "x"}], [{"name": "x"}], entity="labs", a_label="UK/B", b_label="UK B",
            mapper=mapper, probe_pins=False, save=False,
        )
    assert mapper.calls == []


def test_new_run_dir_claims_a_fresh_directory_each_call(tmp_path) -> None:
    from biomapper.cohorts import _new_run_dir

    first = _new_run_dir(tmp_path)
    second = _new_run_dir(tmp_path)
    assert first.is_dir() and second.is_dir()
    assert first != second


def test_equal_labels_raise_before_any_mapping() -> None:
    mapper = FakeMapper(lambda n, i, m, t: mr(n))
    with pytest.raises(ValueError, match="distinct"):
        harmonize_cohorts(
            [{"name": "x"}], [{"name": "x"}], entity="labs", a_label="c", b_label="c",
            mapper=mapper, probe_pins=False, save=False,
        )
    assert mapper.calls == []


# -- a full small run ------------------------------------------------------------------


A = [{"name": "Glucose", "hmdb": "HMDB0000122"}, {"name": "Lactate", "hmdb": None}]
B = [
    {"name": "D-Glucose", "hmdb": "HMDB0000122", "kegg": "C00031"},
    {"name": "Lactate", "hmdb": None, "kegg": None},
]


def _resolve(name: str, ids: dict, mode: str, etype: str) -> MappingResult:
    if mode == "none":
        return mr(name, "KEGG:C00031", {"KEGG": ["C00031"]})
    if ids.get("HMDB"):
        return mr(name, "CHEBI:17234", {"HMDB": ["HMDB0000122"]})
    return {
        "Glucose": mr(name, "CHEBI:4167"),
        "D-Glucose": mr(name, "CHEBI:17634"),
        "Lactate": mr(name, "CHEBI:24996", {"CHEBI": ["24996"]}),
    }[name]


def _run(**kwargs):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return harmonize_cohorts(
            A,
            B,
            entity="metabolites",
            a_vocabularies={"HMDB": "hmdb"},
            b_vocabularies={"HMDB": "hmdb", "KEGG": "kegg"},
            a_label="ukbb",
            b_label="arivale",
            mapper=FakeMapper(_resolve),
            **kwargs,
        )


def test_summary_reports_both_arms_the_diff_and_round_trips_through_json(tmp_path) -> None:
    report = _run(probe_pins=False, save=False)
    s = report.summary()
    assert json.loads(json.dumps(s)) == s
    assert s["category"] == "biolink:SmallMolecule"
    assert s["vocabularies"] == {"shared": ["HMDB"], "one_sided": {"KEGG": "arivale"}}
    names, ident = s["arms"]["names_only"], s["arms"]["identifier"]
    assert names["n_links"] == 1 and names["n_one_to_one"] == 1
    assert ident["n_links"] == 2 and ident["n_one_to_one"] == 2
    assert set(names["links_by_basis"]) == {"node", "identifier", "name_exact", "name_casefold"}
    assert ident["links_by_basis"]["node"] == 2
    assert names["ukbb"]["unresolved"] == 0 and names["arivale"]["errors"] == 0
    assert s["diff"] == {"names_only_only": 0, "identifier_only": 1, "both": 1}
    assert s["identifier_arm_skipped"] is None


def test_write_creates_the_timestamped_directory_by_default(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    report = _run(probe_pins=False)  # save defaults to True
    out = report.output_dir
    assert out is not None and out.is_dir()
    assert out.parent == Path("biomapper_runs").resolve()
    assert re.fullmatch(r"harmonize_cohorts_\d{8}T\d{6}Z(_\d+)?", out.name)
    assert str(out) in capsys.readouterr().out
    names = {p.name for p in out.iterdir()}
    assert {
        "summary.json",
        "settings.json",
        "links_names_only.tsv",
        "links_identifier.tsv",
        "diff.tsv",
        "review_queue.tsv",
        "mapping_names_only_ukbb.tsv",
        "mapping_names_only_arivale.tsv",
        "mapping_identifier_ukbb.tsv",
        "mapping_identifier_arivale.tsv",
        "mapping_review_arivale.tsv",
    } <= names


def test_write_to_an_override_path_and_no_absolute_paths_in_json(tmp_path) -> None:
    out = tmp_path / "my_run"
    report = _run(probe_pins=False, output_dir=out)
    assert report.output_dir == out
    for name in ("summary.json", "settings.json"):
        text = (out / name).read_text()
        assert str(tmp_path) not in text
        assert not re.search(r'"/(home|tmp|Users)/', text)
    links = (out / "links_identifier.tsv").read_text().splitlines()
    assert links[0].split("\t")[:4] == ["ukbb_key", "arivale_key", "basis", "bases"]
    assert len(links) == 3
    queue = (out / "review_queue.tsv").read_text().splitlines()
    assert queue[0].split("\t") == [
        "cohort", "key", "name", "vocabulary", "code", "name_entry", "code_entry", "status",
    ]


def test_write_can_be_called_again_to_another_path(tmp_path) -> None:
    report = _run(probe_pins=False, save=False)
    assert report.output_dir is None
    path = report.write(tmp_path / "later")
    assert (path / "summary.json").is_file()


def test_arms_are_persisted_before_a_later_failure(tmp_path) -> None:
    def resolve(name: str, ids: dict, mode: str, etype: str) -> MappingResult:
        if mode == "none":
            raise RuntimeError("review pass died")
        return _resolve(name, ids, mode, etype)

    out = tmp_path / "partial"
    with warnings.catch_warnings(), pytest.raises(RuntimeError, match="review pass died"):
        warnings.simplefilter("ignore")
        harmonize_cohorts(
            A, B, entity="metabolites", a_vocabularies={"HMDB": "hmdb"},
            b_vocabularies={"HMDB": "hmdb", "KEGG": "kegg"}, a_label="ukbb", b_label="arivale",
            mapper=FakeMapper(resolve), probe_pins=False, output_dir=out,
        )
    assert (out / "mapping_names_only_ukbb.tsv").is_file()
    assert (out / "mapping_identifier_arivale.tsv").is_file()
    assert not (out / "summary.json").exists()


def test_pins_label_the_api_version_and_record_the_kestrel_url(tmp_path, monkeypatch) -> None:
    def fake_get(self, url, **kwargs):
        if url.endswith("/kestrel/health"):
            body = {
                "kestrel_version": "0.3.0",
                "kg_build": {"kg_version": "2.3.0", "git_commit": "3dd08a5b"},
            }
        else:
            body = {"status": "healthy", "version": "0.1.0", "mapper_initialized": True}
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    report = _run(
        kestrel_url="https://k.example/kestrel",
        base_url="https://api.example/v1",
        output_dir=tmp_path / "pinned",
    )
    pins = report.pins
    assert pins["api"]["engine_release"] == "unavailable"
    assert pins["api"]["self_reported_version"] == "0.1.0"
    assert pins["api"]["self_reported_version_label"] == "self-reported, known stale"
    assert pins["api"]["status"] == "healthy"
    assert pins["kestrel"]["url"] == "https://k.example/kestrel"
    assert pins["kestrel"]["kg_build"]["kg_version"] == "2.3.0"
    assert pins["kestrel"]["kestrel_version"] == "0.3.0"
    assert pins["biomapper_version"]
    assert pins["timestamp_utc"].endswith("+00:00")
    assert pins["mapper"] == "FakeMapper"
    written = json.loads((tmp_path / "pinned" / "summary.json").read_text())
    assert written["pins"]["api"]["self_reported_version_label"] == "self-reported, known stale"


def test_unreachable_endpoints_are_recorded_not_raised(tmp_path, monkeypatch) -> None:
    def fake_get(self, url, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    report = _run(save=False)
    assert "ConnectError" in report.pins["api"]["error"]
    assert "ConnectError" in report.pins["kestrel"]["error"]
    assert report.pins["kestrel"]["kg_build"]["kg_version"] == "unknown"


def test_unprobed_pins_say_so() -> None:
    report = _run(probe_pins=False, save=False)
    assert report.pins["api"]["status"] == "not probed"
    assert report.pins["kestrel"]["kg_build"] is None


def test_mapper_pins_are_carried_into_the_report() -> None:
    mapper = FakeMapper(_resolve)
    mapper.pins = {"kg_version": "2.3.0"}  # type: ignore[attr-defined]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        report = harmonize_cohorts(
            A, B, entity="metabolites", mapper=mapper, probe_pins=False, save=False
        )
    assert report.pins["mapper_pins"] == {"kg_version": "2.3.0"}


def test_excluded_rows_are_reported(tmp_path) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        report = harmonize_cohorts(
            [{"name": "Glucose"}, {"name": None}], [{"name": "Glucose"}], entity="metabolites",
            mapper=FakeMapper(lambda n, i, m, t: mr(n, "CHEBI:4167")),
            probe_pins=False, save=False,
        )
    assert report.excluded == {"a": ({"row": 1, "reason": "blank name"},), "b": ()}
    assert report.summary()["excluded"] == {"a": 1, "b": 0}
