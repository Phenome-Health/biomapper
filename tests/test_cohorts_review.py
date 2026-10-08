"""Unit 4: one-sided codes become a review queue comparing name entry and code entry."""

from __future__ import annotations

import warnings

from biomapper.cohorts import harmonize_cohorts, review_status
from biomapper.models import MappingResult
from tests.cohorts_helpers import FakeMapper, mr


def test_status_rules() -> None:
    name = mr("Glucose", "UMLS:C5781949", {"UMLS": ["C5781949"]})
    code = mr("Glucose", "LOINC:2345-7", {"LOINC": ["2345-7"]})
    assert review_status(name, code) == "disagree"
    # Different chosen ids, overlapping equivalents: the same entity, so they agree.
    overlapping = mr("Glucose", "LOINC:2345-7", {"LOINC": ["2345-7"], "UMLS": ["C5781949"]})
    assert review_status(name, overlapping) == "agree"
    assert review_status(name, mr("Glucose")) == "code_unresolved"
    assert review_status(mr("Glucose"), mr("Glucose")) == "code_unresolved"
    assert review_status(mr("Glucose"), code) == "name_unresolved_code_resolved"
    assert review_status(mr("Glucose", error="boom"), code) == "errored"
    assert review_status(name, mr("Glucose", error="boom")) == "errored"


UKBB = [{"title": "Glucose"}]
ARIVALE = [
    {"Display": "Glucose", "Labcorp": "2345-7", "Quest": "2345-7"},  # same code twice: one line
    {"Display": "Sodium", "Labcorp": "2951-2", "Quest": None},
    {"Display": "Vitamin X", "Labcorp": "SOLOINC", "Quest": None},
    {"Display": "Lipid panel", "Labcorp": "57698-3", "Quest": "24331-1"},  # two codes: two lines
    {"Display": "Uncoded", "Labcorp": None, "Quest": None},
]


def _resolve(name: str, ids: dict, mode: str, etype: str) -> MappingResult:
    if mode == "none":
        code = ids["LOINC"][0]
        if code == "SOLOINC":
            return mr(name)
        if code == "24331-1":
            return mr(name, error="HTTP 503")
        return mr(name, f"LOINC:{code}", {"LOINC": [code]})
    return {
        "Glucose": mr(name, "UMLS:C5781949", {"UMLS": ["C5781949"]}),
        # Sodium's name entry carries the code as an equivalent: agrees.
        "Sodium": mr(name, "UMLS:C0337443", {"UMLS": ["C0337443"], "LOINC": ["2951-2"]}),
        "Vitamin X": mr(name, "UMLS:C9999999"),
        "Lipid panel": mr(name, "LOINC:57698-3", {"LOINC": ["57698-3"]}),
        "Uncoded": mr(name, "UMLS:C1111111"),
    }[name]


def _report(**kwargs):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return harmonize_cohorts(
            UKBB,
            ARIVALE,
            entity="labs",
            a_name_column="title",
            b_name_column="Display",
            b_vocabularies={"LOINC": ["Labcorp", "Quest"]},
            a_label="ukbb",
            b_label="arivale",
            mapper=kwargs.pop("mapper", FakeMapper(_resolve)),
            probe_pins=False,
            save=False,
            **kwargs,
        )


def test_review_covers_every_coded_row_once_per_code() -> None:
    report = _report()
    lines = [(i.key, i.code, i.status) for i in report.review]
    assert lines == [
        ("0|Glucose", "2345-7", "disagree"),
        ("1|Sodium", "2951-2", "agree"),
        ("2|Vitamin X", "SOLOINC", "code_unresolved"),
        ("3|Lipid panel", "57698-3", "agree"),
        ("3|Lipid panel", "24331-1", "errored"),
    ]
    assert all(i.cohort == "arivale" and i.vocabulary == "LOINC" for i in report.review)


def test_queue_lists_everything_but_agreements_with_both_entries() -> None:
    report = _report()
    queue = report.review_queue
    assert [i.status for i in queue] == ["disagree", "code_unresolved", "errored"]
    glucose = queue[0]
    assert glucose.name == "Glucose"
    assert glucose.name_entry == "UMLS:C5781949"
    assert glucose.code_entry == "LOINC:2345-7"


def test_review_requests_are_code_only_one_per_code() -> None:
    mapper = FakeMapper(_resolve)
    _report(mapper=mapper)
    review = [(n, ids) for n, ids, mode in mapper.sent if mode == "none"]
    assert review == [
        ("Glucose", {"LOINC": ["2345-7"]}),
        ("Sodium", {"LOINC": ["2951-2"]}),
        ("Vitamin X", {"LOINC": ["SOLOINC"]}),
        ("Lipid panel", {"LOINC": ["57698-3"]}),
        ("Lipid panel", {"LOINC": ["24331-1"]}),
    ]


def test_review_status_counts_are_in_the_summary() -> None:
    by_status = _report().summary()["review"]["by_status"]
    assert by_status == {
        "agree": 2,
        "disagree": 1,
        "code_unresolved": 1,
        "name_unresolved_code_resolved": 0,
        "errored": 1,
    }


def test_name_unresolved_code_resolved() -> None:
    def resolve(name: str, ids: dict, mode: str, etype: str) -> MappingResult:
        if mode == "none":
            return mr(name, "LOINC:2345-7", {"LOINC": ["2345-7"]})
        return mr(name)

    report = _report(mapper=FakeMapper(resolve))
    assert {i.status for i in report.review} == {"name_unresolved_code_resolved"}
