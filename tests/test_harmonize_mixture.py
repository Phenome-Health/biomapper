"""Mixture-name review and the possible-missed-match list (offline; literal results).

A mixture label lists alternative compounds ("butyrate/isobutyrate (4:0)"), so a link from it is a
review candidate even when one-to-one. An entity in no link whose mapping already carries a review
signal is listed in ``possible_missed`` instead of disappearing. The named cases are NECS x Xu
et al. 2022 names (cross-cohort run 1ee319e, 2026-11-05).
"""

from __future__ import annotations

import pytest

from biomapper.cohorts import harmonize_cohorts
from biomapper.harmonize import (
    PossibleMiss,
    harmonize,
    is_mixture_name,
    mapping_review_signals,
)
from biomapper.harmonize.linking import Link
from biomapper.models import MappingResult, RawApiResult, ResolutionCertificate
from tests.cohorts_helpers import FakeMapper, by_name, mr


def _r(
    name: str,
    chosen: str | None,
    *,
    review: str | None = None,
    state: str | None = None,
    engine_mixture: bool | None = None,
) -> MappingResult:
    return MappingResult(
        query_name=name,
        chosen_kg_id=chosen,
        chosen_kg_id_review=review,
        certificate=ResolutionCertificate(state=state) if state is not None else None,
        name_is_mixture=engine_mixture,
    )


@pytest.mark.parametrize(
    "name",
    [
        # 1. " or " between alternatives
        "linolenate [alpha or gamma; (18:3n3 or 6)]",
        "dihomo-linolenate (20:3n3 or n6)",
        "dihomolinolenate (20:3n3 or 3n6)",
        "branched-chain, straight-chain, or cyclopropyl 10:1 fatty acid (1)*",
        "(14 or 15)-methylpalmitate (a17:0 or i17:0)",
        # 2. " + " between compounds
        "dimethylarginine (sdma + adma)",
        "(n(1) + n(8))-acetylspermidine",
        # 3. "/" between alternative compound names
        "butyrate/isobutyrate (4:0)",
        "2-hydroxybutyrate/2-hydroxyisobutyrate",
        "Butyrate/Isobutyrate (4:0)",
        "isobutyrylglycine/butyrylglycine*",
        # 4. Metabolon multi-isomer parentheticals: comma list of lipid chains
        "sphingomyelin (d18:1/17:0, d17:1/18:0, d19:1/16:0)",
        "ceramide (d18:1/14:0, d16:1/16:0)*",
        "diacylglycerol (16:1/18:2 [2], 16:0/18:3 [1])*",
        "phosphatidylcholine (o-16:0/18:1, p-16:0/18:0)",
    ],
)
def test_mixture_names(name: str) -> None:
    assert is_mixture_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "glucose",
        "PC 16:0/18:1",
        "1-palmitoyl-gpi (16:0)",
        "1-palmitoyl-2-linoleoyl-gpc (16:0/18:2)",
        "sphingomyelin (d18:1/17:0)",
        "hydroxypalmitoyl sphingomyelin (d18:1/16:0(oh))**",
        "TG 16:0/18:1/18:2",
        "Cer 18:1;OH/16:0",
        "androstenediol (3alpha, 17alpha) monosulfate (3)",
        "bilirubin (z,z)",
        "NAD+",
        "calcium (Ca2+)",
        "HbA1c (mmol/mol)",
        "cholesterol (mg/dL)",
        "Albumin/Creatinine ratio",
        "ornithine",
        "chloride",
        "",
    ],
)
def test_non_mixture_names(name: str) -> None:
    assert not is_mixture_name(name)


def _one_to_one(a_names: list[str], b_names: list[str]) -> tuple[list[MappingResult], ...]:
    return (
        [_r(n, f"RM:{i}") for i, n in enumerate(a_names)],
        [_r(n, f"RM:{i}") for i, n in enumerate(b_names)],
    )


def test_mixture_link_goes_to_review_even_when_one_to_one() -> None:
    report = harmonize(
        *_one_to_one(
            ["dimethylarginine (sdma + adma)", "glucose", "sphingomyelin (d18:1/17:0)"],
            ["dimethylarginine (adma + sdma)", "glucose", "linolenate (18:3n3 or 3n6)"],
        )
    )
    by_a = {lk.a_key: lk for lk in report.links}
    dma = by_a["dimethylarginine (sdma + adma)"]
    assert (dma.a_is_mixture, dma.b_is_mixture) == (True, True)
    assert not dma.ambiguous and dma.mixture and dma.needs_review
    assert dma.review_reasons == ("mixture",)
    one_side = by_a["sphingomyelin (d18:1/17:0)"]
    assert (one_side.a_is_mixture, one_side.b_is_mixture) == (False, True)
    assert one_side.needs_review
    assert not by_a["glucose"].needs_review and by_a["glucose"].review_reasons == ()
    s = report.summary()
    assert s["n_mixture_links"] == 2 and s["n_needs_review"] == 2
    assert s["n_one_to_one_links"] == 3


def test_review_reasons_list_every_flag_in_order() -> None:
    lk = Link(
        a_key="x*", b_key="x", shared=frozenset(), a_partners=2, a_id_confidence=1,
        b_is_mixture=True,
    )
    assert lk.review_reasons == ("ambiguous", "id_confidence_differs", "mixture")
    assert Link(a_key="a", b_key="b", shared=frozenset()).review_reasons == ()


def test_engine_mixture_flag_is_honoured_when_present() -> None:
    a = [_r("compound one", "RM:1", engine_mixture=True), _r("compound two", "RM:2")]
    b = [_r("compound one", "RM:1"), _r("compound two", "RM:2", engine_mixture=False)]
    by_a = {lk.a_key: lk for lk in harmonize(a, b).links}
    assert by_a["compound one"].a_is_mixture and by_a["compound one"].needs_review
    assert not by_a["compound two"].mixture


def test_engine_false_does_not_clear_the_name_rule() -> None:
    a = [_r("butyrate/isobutyrate (4:0)", "RM:1", engine_mixture=False)]
    b = [_r("butyrate (4:0)", "RM:1")]
    (lk,) = harmonize(a, b).links
    assert lk.a_is_mixture


def test_engine_flag_parses_from_api_and_is_absent_on_older_servers() -> None:
    new = MappingResult.from_batch_entry(
        RawApiResult(chosen_kg_id="RM:1", name_is_mixture=True), query_name="x"
    )
    old = MappingResult.from_batch_entry(RawApiResult(chosen_kg_id="RM:1"), query_name="x")
    assert new.name_is_mixture is True
    assert old.name_is_mixture is None


@pytest.mark.parametrize(
    "result, reasons",
    [
        (_r("glucose", "RM:1"), ()),
        (_r("glucose", "RM:1", review="divergent_refmet"), ("divergent_refmet",)),
        (_r("glucose", "RM:1", review="conflict_no_structure"), ("conflict_no_structure",)),
        (_r("glucose", "RM:1", state="contradicted"), ("certificate_contradicted",)),
        (_r("glucose", "RM:1", state="uncorroborated"), ()),
        (_r("butyrate/isobutyrate (4:0)", "RM:1"), ("mixture",)),
        (_r("x", "RM:1", engine_mixture=True), ("mixture",)),
        (
            _r("a or b", "RM:1", review="divergent_refmet", state="contradicted"),
            ("divergent_refmet", "certificate_contradicted", "mixture"),
        ),
    ],
)
def test_mapping_review_signals(result: MappingResult, reasons: tuple[str, ...]) -> None:
    assert mapping_review_signals(result) == reasons


def test_possible_missed_lists_unlinked_entities_with_a_review_signal() -> None:
    # Monti et al.'s two pairs BioMapper missed on NECS x Xu: each side resolved to a different
    # entry, so neither links; the mixture names put them on the list.
    a = [
        _r("butyrate/isobutyrate (4:0)", "RM:butyric"),
        _r("dihomo-linolenate (20:3n3 or n6)", "RM:dgla"),
        _r("glucose", "RM:glc"),
        _r("cotinine", "RM:cot", review="divergent_refmet"),  # linked: never a possible miss
        _r("unlinked plain", "RM:p"),  # no signal: not listed
        _r("refuted", "RM:r", state="contradicted"),
        _r("errored or failed", None).model_copy(update={"error": "boom"}),  # errors: never
        _r("unresolved or other", None),  # unresolved, still listed (resolved=False)
    ]
    b = [
        _r("butyrate (4:0)", "RM:butyrate", review="conflict_no_structure"),
        _r("dihomolinolenate (20:3n3 or 3n6)", "RM:eicosatrienoic"),
        _r("glucose", "RM:glc"),
        _r("cotinine", "RM:cot"),
    ]
    report = harmonize(a, b, a_label="necs", b_label="xu")
    assert [(lk.a_key, lk.b_key) for lk in report.links] == [
        ("cotinine", "cotinine"),
        ("glucose", "glucose"),
    ]
    assert [(m.cohort, m.key, m.reasons, m.resolved) for m in report.possible_missed] == [
        ("necs", "butyrate/isobutyrate (4:0)", ("mixture",), True),
        ("necs", "dihomo-linolenate (20:3n3 or n6)", ("mixture",), True),
        ("necs", "refuted", ("certificate_contradicted",), True),
        ("necs", "unresolved or other", ("mixture",), False),
        ("xu", "butyrate (4:0)", ("conflict_no_structure",), True),
        ("xu", "dihomolinolenate (20:3n3 or 3n6)", ("mixture",), True),
    ]
    assert report.possible_missed[0] == PossibleMiss(
        cohort="necs", key="butyrate/isobutyrate (4:0)", name="butyrate/isobutyrate (4:0)",
        chosen_kg_id="RM:butyric", resolved=True, reasons=("mixture",),
    )
    assert len(report.a_possible_missed) == 4 and len(report.b_possible_missed) == 2
    s = report.summary()
    assert s["n_possible_missed"] == 6
    assert s["necs"]["possible_missed"] == 4 and s["xu"]["possible_missed"] == 2


def test_possible_missed_uses_custom_keys_and_query_names() -> None:
    a = [_r("butyrate/isobutyrate (4:0)", "RM:1")]
    b = [_r("butyrate (4:0)", "RM:2")]
    (miss,) = harmonize(a, b, key=lambda r, i: f"row{i}").possible_missed
    assert (miss.key, miss.name) == ("row0", "butyrate/isobutyrate (4:0)")


def test_name_link_removes_an_entity_from_possible_missed() -> None:
    a = [_r("butyrate/isobutyrate (4:0)", "RM:1")]
    b = [_r("butyrate/isobutyrate (4:0)", "RM:2")]
    assert len(harmonize(a, b).possible_missed) == 2
    report = harmonize(a, b, link_by_name=True)
    assert report.possible_missed == ()
    (lk,) = report.links
    assert lk.mixture and lk.needs_review


@pytest.mark.parametrize("label", ["n_mixture_links", "n_possible_missed"])
def test_new_summary_keys_are_reserved_labels(label: str) -> None:
    with pytest.raises(ValueError, match="reserved"):
        harmonize([_r("x", "RM:1")], [_r("x", "RM:1")], a_label=label)


def test_harmonize_cohorts_writes_mixture_columns_and_possible_missed_table(tmp_path) -> None:
    table = {
        "butyrate/isobutyrate (4:0)": mr("butyrate/isobutyrate (4:0)", "RM:butyric"),
        "butyrate (4:0)": mr("butyrate (4:0)", "RM:butyrate"),
        "dimethylarginine (sdma + adma)": mr("dimethylarginine (sdma + adma)", "RM:dma"),
        "dimethylarginine (adma + sdma)": mr("dimethylarginine (adma + sdma)", "RM:dma"),
    }
    report = harmonize_cohorts(
        [{"name": "butyrate/isobutyrate (4:0)"}, {"name": "dimethylarginine (sdma + adma)"}],
        [{"name": "butyrate (4:0)"}, {"name": "dimethylarginine (adma + sdma)"}],
        entity="metabolites",
        a_label="necs",
        b_label="xu",
        mapper=FakeMapper(by_name(table)),
        probe_pins=False,
        output_dir=tmp_path / "run",
    )
    arm = report.summary()["arms"]["names_only"]
    assert arm["n_links"] == 1 and arm["n_mixture_links"] == 1 and arm["n_needs_review"] == 1
    assert arm["n_possible_missed"] == 1 and arm["necs"]["possible_missed"] == 1
    links = (tmp_path / "run" / "links_names_only.tsv").read_text().splitlines()
    header = links[0].split("\t")
    row = dict(zip(header, links[1].split("\t"), strict=True))
    assert (row["a_is_mixture"], row["b_is_mixture"], row["mixture"]) == ("True", "True", "True")
    assert row["review_reasons"] == "mixture" and row["needs_review"] == "True"
    missed = (tmp_path / "run" / "possible_missed_names_only.tsv").read_text().splitlines()
    assert missed[0].split("\t") == ["cohort", "key", "name", "chosen_kg_id", "resolved", "reasons"]
    assert len(missed) == 2
    fields = missed[1].split("\t")
    assert fields[0] == "necs" and fields[2] == "butyrate/isobutyrate (4:0)"
    assert fields[3:] == ["RM:butyric", "True", "mixture"]
