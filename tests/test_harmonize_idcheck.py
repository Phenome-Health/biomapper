"""Unit 3: the offline identifier check (declared IDs vs the chosen node's identifiers)."""

from __future__ import annotations

import pytest

from biomapper.harmonize.curies import normalize_hmdb
from biomapper.harmonize.idcheck import (
    COMPARISON_RULE,
    ID_CHECK_STATUSES,
    check_identifiers,
)
from tests.cohorts_helpers import mr

GLUCOSE = mr(
    "glucose", "CHEBI:17234",
    {"HMDB": ["HMDB0000122"], "KEGG.COMPOUND": ["C00031"], "CHEBI": ["17234", "4167"],
     "INCHIKEY": ["WQZGKKKJIJFFOK-GASJEMHNSA-N"]},
)


def status(declared: dict, result=GLUCOSE) -> str:  # noqa: ANN001
    return check_identifiers(declared, result).status


def test_a_declared_id_among_the_node_equivalents_is_confirmed() -> None:
    check = check_identifiers({"HMDB": ("HMDB0000122",)}, GLUCOSE)
    assert check.status == "confirmed"
    assert check.matched == ("HMDB:HMDB0000122",)
    assert check.rule == COMPARISON_RULE


@pytest.mark.parametrize("code", ["HMDB00122", "00122", "HMDB0000122", "hmdb00122"])
def test_legacy_five_digit_hmdb_codes_are_zero_padded(code: str) -> None:
    assert status({"HMDB": (code,)}) == "confirmed"


def test_kegg_compound_and_kegg_are_one_vocabulary() -> None:
    assert status({"KEGG": ("C00031",)}) == "confirmed"
    assert status({"KEGG.COMPOUND": ("C00031",)}) == "confirmed"


def test_a_declared_inchikey_is_compared_in_full_against_the_node_inchikey() -> None:
    assert status({"INCHIKEY": ("WQZGKKKJIJFFOK-GASJEMHNSA-N",)}) == "confirmed"
    assert status({"INCHIKEY": ("wqzgkkkjijffok-gasjemhnsa-n",)}) == "confirmed"
    # same first block, different stereo block: not the same structure for this check
    assert status({"INCHIKEY": ("WQZGKKKJIJFFOK-VFUOTHLCSA-N",)}) == "not_confirmed"


def test_no_declared_ids_and_errored_rows() -> None:
    assert status({}) == "no_declared_id"
    assert status({"HMDB": ("HMDB0000122",)}, mr("glucose", error="timeout")) == "errored"


def test_a_cell_that_already_carries_its_prefix_is_used_as_is() -> None:
    assert status({"CHEBI": ("CHEBI:15422",)}, mr("ATP", "CHEBI:15422")) == "confirmed"
    assert status({"CHEBI": ("chebi:15422",)}, mr("ATP", "CHEBI:15422")) == "confirmed"


def test_an_unmappable_vocabulary_label_is_no_declared_id_with_a_reason() -> None:
    check = check_identifiers({"PUBCHEM CID": ("5793",)}, GLUCOSE)
    assert check.status == "no_declared_id"
    assert "unmappable vocabulary" in (check.reason or "")


def test_a_present_but_absent_id_is_not_confirmed() -> None:
    check = check_identifiers({"HMDB": ("HMDB0000190",)}, GLUCOSE)
    assert check.status == "not_confirmed"
    assert check.declared == ("HMDB:HMDB0000190",)


def test_one_matching_code_among_several_confirms() -> None:
    assert status({"HMDB": ("HMDB0000190", "HMDB0000122")}) == "confirmed"


def test_the_engine_assigned_identifiers_are_never_read() -> None:
    # Only MappingResult.identifiers (engine-assigned) carries the declared code; the node's own
    # identifiers do not. That is not a confirmation.
    result = mr("glucose", "CHEBI:4167", {"CHEBI": ["4167"]}).model_copy(
        update={"identifiers": {"HMDB": ["HMDB0000122"]}})
    assert status({"HMDB": ("HMDB0000122",)}, result) == "not_confirmed"


def test_statuses_are_a_closed_set() -> None:
    assert ID_CHECK_STATUSES == ("confirmed", "not_confirmed", "no_declared_id", "errored")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("HMDB:HMDB00122", "HMDB:HMDB0000122"), ("HMDB:HMDB0000122", "HMDB:HMDB0000122"),
     ("HMDB:122", "HMDB:HMDB0000122"), ("CHEBI:15422", "CHEBI:15422"),
     ("HMDB:HMDB_X", "HMDB:HMDB_X")],
)
def test_normalize_hmdb(raw: str, expected: str) -> None:
    assert normalize_hmdb(raw) == expected



@pytest.mark.parametrize(("vocab", "code", "equivalents"), [
    ("KEGG", "KEGG.COMPOUND:C00031", {"KEGG": ["KEGG:C00031"]}),
    ("PUBCHEM", "PUBCHEM.COMPOUND:5793", {"PUBCHEM.COMPOUND": ["PUBCHEM.COMPOUND:5793"]}),
])
def test_a_cell_carrying_an_aliased_prefix_is_not_prefixed_again(
    vocab: str, code: str, equivalents: dict
) -> None:
    check = check_identifiers({vocab: [code]}, mr("glucose", "CHEBI:17234", equivalents))
    assert check.status == "confirmed"
