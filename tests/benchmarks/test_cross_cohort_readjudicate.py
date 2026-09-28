"""Re-adjudication of non-certified cross-cohort cases (offline; a stub outside source).

The assertions that matter are the ones that stop a first-block mismatch from being reported as a
wrong molecule, and the ones that keep "we could not check" apart from "we checked and it is fine".
"""

from __future__ import annotations

import pandas as pd

from biomapper.benchmarks.cross_cohort_readjudicate import (
    OutsideRecord,
    OutsideResolver,
    annotations_state_different_composition,
    chain_annotations,
    classify,
    composition_relation,
    formula_contains,
    formulas_differ_only_in_hydrogen,
    readjudicate,
    suspected_derivative,
)

GLUCOSE = "WQZGKKKJIJFFOK"
OTHER = "ZZZZZZZZZZZZZZ"
THIRD = "QQQQQQQQQQQQQQ"


def _hit(block: str, formula: str, mass: float) -> OutsideRecord:
    return OutsideRecord(block=block, formula=formula, mass=mass, status="success")


_MISS = OutsideRecord(None, None, None, "clean_miss")
_FAILED = OutsideRecord(None, None, None, "lookup_failed")


# ==================================================================================================
# Formula comparison
# ==================================================================================================


def test_hydrogen_only_difference_is_recognized():
    # A protonation or tautomer shift. The compounds are the same for harmonization.
    assert formulas_differ_only_in_hydrogen("C7H9N2O", "C7H8N2O") is True
    assert formulas_differ_only_in_hydrogen("C6H12O6", "C6H12O6") is True


def test_real_composition_difference_is_not_excused():
    assert formulas_differ_only_in_hydrogen("C6H12O6", "C6H12O5") is False
    assert formulas_differ_only_in_hydrogen("C6H12O6", "C5H12O6") is False


def test_charge_suffix_does_not_defeat_the_comparison():
    assert formulas_differ_only_in_hydrogen("C7H9N2O+", "C7H8N2O") is True


def test_unparseable_formula_never_buys_a_free_pass():
    assert formulas_differ_only_in_hydrogen(None, "C6H12O6") is False
    assert formulas_differ_only_in_hydrogen("", "C6H12O6") is False
    assert formulas_differ_only_in_hydrogen("not a formula", "C6H12O6") is False


# ==================================================================================================
# Classification
# ==================================================================================================


def test_a_protonation_difference_is_an_artifact_not_an_error():
    outcome, rationale = classify(
        GLUCOSE,
        OTHER,
        _hit(GLUCOSE, "C6H12O6", 180.0634),
        _hit(OTHER, "C6H13O6", 181.0712),
    )
    assert outcome == "charge_or_protonation_artifact"
    assert "not invariant" in rationale


def test_identical_formula_with_different_blocks_stays_ambiguous():
    # A tautomer and a constitutional isomer are indistinguishable by composition, so neither is
    # claimed. Counting this as an artifact would excuse real wrong-molecule links.
    outcome, rationale = classify(
        GLUCOSE,
        OTHER,
        _hit(GLUCOSE, "C6H12O6", 180.0634),
        _hit(OTHER, "C6H12O6", 180.0634),
    )
    assert outcome == "same_formula_different_connectivity"
    assert "must not be counted in either direction" in rationale


def test_composition_relation_classifies_the_three_cases():
    same = _hit(GLUCOSE, "C6H12O6", 180.0634)
    protonated = _hit(OTHER, "C6H13O6", 180.0634 + 1.007825)
    unrelated = _hit(THIRD, "C9H8O4", 180.0423)
    assert composition_relation(same, _hit(OTHER, "C6H12O6", 180.0634)) == "same_formula"
    assert composition_relation(same, protonated) == "protonation_or_charge"
    assert composition_relation(same, unrelated) == "different"


def test_outside_source_backing_the_cohort_implicates_the_gold():
    outcome, _ = classify(
        OTHER,  # what the curated gold says
        GLUCOSE,  # what the cohort's vendor id says
        _hit(GLUCOSE, "C6H12O6", 180.0634),  # outside source, NECS name -> glucose
        _hit(GLUCOSE, "C6H12O6", 180.0634),
    )
    assert outcome == "necs_gold_suspect"


def test_outside_source_backing_the_gold_implicates_the_cohort_annotation():
    outcome, _ = classify(
        GLUCOSE,
        OTHER,
        _hit(GLUCOSE, "C6H12O6", 180.0634),
        _hit(GLUCOSE, "C6H12O6", 180.0634),
    )
    assert outcome == "cohort_id_suspect"


def test_neither_side_matching_is_escalated_not_guessed():
    outcome, _ = classify(
        GLUCOSE,
        OTHER,
        _hit(THIRD, "C9H8O4", 180.0423),
        _hit(THIRD, "C9H8O4", 180.0423),
    )
    assert outcome == "both_sides_disagree_with_outside"


def test_both_sides_corroborated_at_different_structures_is_a_real_wrong_link():
    outcome, rationale = classify(
        GLUCOSE,
        OTHER,
        _hit(GLUCOSE, "C6H12O6", 180.0634),
        _hit(OTHER, "C9H8O4", 180.0423),
    )
    assert outcome == "genuine_structural_disagreement"
    assert "genuinely measured different molecules" in rationale


def test_unresolvable_outside_source_concludes_nothing():
    assert classify(GLUCOSE, OTHER, _MISS, _MISS)[0] == "outside_source_unresolved"
    assert classify(GLUCOSE, OTHER, _FAILED, _FAILED)[0] == "outside_source_unresolved"
    # One side only is also not enough to place the defect.
    assert (
        classify(GLUCOSE, OTHER, _hit(GLUCOSE, "C6H12O6", 180.0634), _MISS)[0]
        == "outside_source_unresolved"
    )


def test_mass_disagreement_blocks_the_artifact_call():
    # Same formula string but masses far apart: not one compound, so no free pass.
    outcome, _ = classify(
        GLUCOSE,
        OTHER,
        _hit(GLUCOSE, "C6H12O6", 180.0634),
        _hit(OTHER, "C6H12O6", 200.0),
    )
    assert outcome != "charge_or_protonation_artifact"


# ==================================================================================================
# Driving it over a case table
# ==================================================================================================


class _StubResolver:
    def __init__(self, table: dict[str, OutsideRecord]) -> None:
        self.table = table
        self.asked: list[str] = []

    def by_name(self, name: str) -> OutsideRecord:
        self.asked.append(name)
        return self.table.get(name, _MISS)


def _cases() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "necs_name": "certified_one",
                "cohort_name": "certified_one",
                "verdict": "certified",
                "refusal_class": "",
                "necs_block": GLUCOSE,
                "cohort_block": GLUCOSE,
            },
            {
                "necs_name": "refuted_one",
                "cohort_name": "refuted_other",
                "verdict": "refuted",
                "refusal_class": "",
                "necs_block": GLUCOSE,
                "cohort_block": OTHER,
            },
            {
                "necs_name": "names_only",
                "cohort_name": "names_only",
                "verdict": "refused",
                "refusal_class": "cohort_panel_has_no_structure_resolvable_id",
                "necs_block": GLUCOSE,
                "cohort_block": "",
            },
        ]
    )


def test_readjudicate_skips_certified_and_never_calls_out_for_construction_refusals():
    resolver = _StubResolver(
        {
            "refuted_one": _hit(GLUCOSE, "C6H12O6", 180.0634),
            "refuted_other": _hit(GLUCOSE, "C6H12O6", 180.0634),
        }
    )
    result = readjudicate(_cases(), resolver)  # type: ignore[arg-type]
    assert len(result) == 2  # the certified case is not re-examined
    outcomes = dict(zip(result["necs_name"], result["readjudication"], strict=True))
    assert outcomes["refuted_one"] == "cohort_id_suspect"
    assert outcomes["names_only"] == "not_adjudicable_by_construction"
    # A panel with no structure-resolvable identifier is never sent to the outside source: there is
    # nothing to adjudicate, and asking would invent a verdict the certificate never made.
    assert "names_only" not in resolver.asked


def test_readjudicate_records_the_outside_evidence_for_review():
    resolver = _StubResolver(
        {
            "refuted_one": _hit(THIRD, "C9H8O4", 180.0423),
            "refuted_other": _hit(OTHER, "C6H12O6", 180.0634),
        }
    )
    result = readjudicate(_cases(), resolver)  # type: ignore[arg-type]
    row = result[result["necs_name"] == "refuted_one"].iloc[0]
    assert row["outside_necs_block"] == THIRD
    assert row["outside_cohort_block"] == OTHER
    assert row["outside_necs_formula"] == "C9H8O4"
    assert row["rationale"]


def test_outside_resolver_treats_an_ambiguous_name_as_a_miss():
    body = (
        '{"PropertyTable": {"Properties": ['
        '{"InChIKey": "WQZGKKKJIJFFOK-GASJEMHNSA-N", "MolecularFormula": "C6H12O6",'
        ' "MonoisotopicMass": "180.06"},'
        '{"InChIKey": "ZZZZZZZZZZZZZZ-YYYYYYYYYY-N", "MolecularFormula": "C6H12O6",'
        ' "MonoisotopicMass": "180.06"}]}}'
    )
    # Two different structures behind one name: adjudicating off PubChem's rank 1 would be a guess.
    assert OutsideResolver._parse(body).status == "clean_miss"


def test_outside_resolver_parses_a_single_hit():
    body = (
        '{"PropertyTable": {"Properties": [{"InChIKey": "WQZGKKKJIJFFOK-GASJEMHNSA-N",'
        ' "MolecularFormula": "C6H12O6", "MonoisotopicMass": "180.063388"}]}}'
    )
    record = OutsideResolver._parse(body)
    assert record.resolved is True
    assert record.block == GLUCOSE and record.formula == "C6H12O6"
    assert record.mass is not None and abs(record.mass - 180.063388) < 1e-6


def test_outside_resolver_reports_a_malformed_body_as_a_failure_not_a_miss():
    assert OutsideResolver._parse("not json").status == "lookup_failed"
    assert OutsideResolver._parse('{"PropertyTable": {"Properties": []}}').status == "clean_miss"


# ==================================================================================================
# The outside source returning a derivative instead of the parent (observed live)
# ==================================================================================================

# PubChem's name index for "Prolylleucine" returns the Cbz-protected Z-Pro-Leu rather than the free
# dipeptide, which made the API certificate mark a CORRECT mapping as contradicted.
PRO_LEU = _hit("ZKQOUHVVXABNDG", "C11H20N2O3", 228.14739250)  # CID 3527720, free dipeptide
Z_PRO_LEU = _hit("YCYXUKRYYSXSLJ", "C19H26N2O5", 362.18417193)  # CID 3584406, Cbz-protected
LEU_PRO = _hit("VTJUNIYRYIAIHF", "C11H20N2O3", 228.14739250)  # the reverse dipeptide, a real isomer


def test_formula_containment_detects_a_protecting_group():
    assert formula_contains("C19H26N2O5", "C11H20N2O3") is True
    assert formula_contains("C11H20N2O3", "C19H26N2O5") is False
    # Identical skeletons are not containment, and neither is a rival skeleton.
    assert formula_contains("C11H20N2O3", "C11H20N2O3") is False
    assert formula_contains("C11H20N2O3", "C9H8O4") is False


def test_the_observed_prolylleucine_pair_is_called_a_derivative_not_a_disagreement():
    assert suspected_derivative(PRO_LEU, Z_PRO_LEU) is True
    outcome, rationale = classify("ZKQOUHVVXABNDG", "YCYXUKRYYSXSLJ", PRO_LEU, Z_PRO_LEU)
    assert outcome == "outside_source_hit_a_derivative"
    assert "not evidence that the link is wrong" in rationale


def test_a_true_isomer_is_never_excused_as_a_derivative():
    # Pro-Leu against Leu-Pro: same formula, same mass, different connectivity. A real disagreement.
    assert suspected_derivative(PRO_LEU, LEU_PRO) is False
    outcome, rationale = classify("ZKQOUHVVXABNDG", "VTJUNIYRYIAIHF", PRO_LEU, LEU_PRO)
    # Same formula, different connectivity. Not excused as an artifact, and not asserted to be a
    # wrong molecule either: composition cannot tell a tautomer from a constitutional isomer.
    assert outcome == "same_formula_different_connectivity"
    assert "Pro-Leu against Leu-Pro" in rationale


def test_a_small_mass_gap_is_not_enough_for_the_derivative_call():
    near = _hit("AAAAAAAAAAAAAA", "C12H22N2O3", 242.16)  # containing formula, only 14 Da apart
    assert suspected_derivative(PRO_LEU, near) is False


# ==================================================================================================
# An artifact call must never rest on evidence that was not obtained
# ==================================================================================================


def test_a_hydrogen_difference_without_a_mass_is_not_called_an_artifact():
    no_mass = OutsideRecord(block=OTHER, formula="C6H13O6", mass=None, status="success")
    assert composition_relation(_hit(GLUCOSE, "C6H12O6", 180.0634), no_mass) == (
        "hydrogen_difference_unverified"
    )
    outcome, rationale = classify(GLUCOSE, OTHER, _hit(GLUCOSE, "C6H12O6", 180.0634), no_mass)
    assert outcome == "outside_source_unresolved"
    assert "could not be confirmed" in rationale
    # The old behaviour would have claimed a matching mass gap it never measured.
    assert "mass gap matches" not in rationale


def test_a_hydrogen_difference_with_the_wrong_mass_gap_is_a_real_difference():
    # Same heavy atoms, one hydrogen apart on paper, but the masses are 20 Da apart. Not a
    # protonation state.
    outcome, _ = classify(
        GLUCOSE,
        OTHER,
        _hit(GLUCOSE, "C6H12O6", 180.0634),
        _hit(OTHER, "C6H13O6", 200.0),
    )
    assert outcome == "genuine_structural_disagreement"


def test_a_confirmed_hydrogen_gap_is_still_called_an_artifact():
    outcome, rationale = classify(
        GLUCOSE,
        OTHER,
        _hit(GLUCOSE, "C6H12O6", 180.0634),
        _hit(OTHER, "C6H13O6", 180.0634 + 1.007825),
    )
    assert outcome == "charge_or_protonation_artifact"
    assert "mass gap matches" in rationale


def test_the_resolver_throttles_misses_and_not_cache_hits(monkeypatch):
    # PubChem asks for at most 5 requests per second. The certification path learned this the
    # expensive way; this module fires up to two lookups per case, so it needs the same rule.
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))

    class _Resp:
        status_code = 200
        text = (
            '{"PropertyTable": {"Properties": [{"InChIKey": "WQZGKKKJIJFFOK-GASJEMHNSA-N",'
            ' "MolecularFormula": "C6H12O6", "MonoisotopicMass": "180.06"}]}}'
        )

    class _Session:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, _url: str, timeout: float = 0) -> _Resp:
            self.calls += 1
            return _Resp()

    session = _Session()
    resolver = OutsideResolver(session=session, min_interval_s=0.25)
    resolver.by_name("glucose")
    resolver.by_name("glucose")  # cache hit: no request, no sleep
    resolver.by_name("urea")
    assert session.calls == 2
    # The first request has no predecessor to space from, so only the second sleeps.
    assert len(sleeps) == 1 and sleeps[0] <= 0.25


def test_throttling_can_be_disabled_for_tests():
    class _Session:
        def get(self, _url: str, timeout: float = 0):  # noqa: ANN202
            raise AssertionError("should not be called")

    resolver = OutsideResolver(session=_Session(), min_interval_s=0)
    assert resolver._pacer.min_interval_s == 0


# ==================================================================================================
# The two linked names differing is evidence, and it outranks a missing lookup
# ==================================================================================================


def test_differing_chain_annotations_are_adverse_without_any_lookup():
    # Observed live: "palmitoyl sphingomyelin (d18:1/16:0)" linked to "stearoyl sphingomyelin
    # (d18:1/18:0)". PubChem cannot parse Metabolon shorthand, but the names state different
    # compositions, so reporting "nothing concluded" would hide a wrong link behind a missing
    # lookup.
    outcome, rationale = classify(
        GLUCOSE,
        OTHER,
        _MISS,
        _MISS,
        same_name=False,
        necs_name="palmitoyl sphingomyelin (d18:1/16:0)",
        cohort_name="stearoyl sphingomyelin (d18:1/18:0)",
    )
    assert outcome == "differing_lipid_chain_annotation"
    assert "wrong link" in rationale


def test_bare_name_inequality_is_not_treated_as_adverse():
    # Two cohorts can legitimately use synonyms or different formatting for one metabolite. Calling
    # that suspicious would inflate the triage pile with valid pairs.
    outcome, rationale = classify(
        GLUCOSE,
        OTHER,
        _MISS,
        _MISS,
        same_name=False,
        necs_name="vitamin E",
        cohort_name="alpha-tocopherol",
    )
    assert outcome == "distinct_vendor_names_structure_unverified"
    assert "not evidence either way" in rationale


def test_an_annotation_on_only_one_side_proves_nothing():
    # Comparing an annotated name against an unannotated one would turn "cannot tell" into "differ".
    assert annotations_state_different_composition(
        "palmitoyl sphingomyelin (d18:1/16:0)", "SM"
    ) is (False)
    assert annotations_state_different_composition("glucose", "dextrose") is False


def test_matching_annotations_are_not_adverse():
    # Same chains, different head group (GPC vs GPE) still lands in the triage bucket rather than
    # being asserted either way: the annotation does not discriminate here.
    assert (
        annotations_state_different_composition(
            "1-palmitoyl-2-linoleoyl-gpc (16:0/18:2)", "1-palmitoyl-2-linoleoyl-GPE (16:0/18:2)"
        )
        is False
    )


def test_chain_annotations_are_extracted_case_and_space_insensitively():
    assert chain_annotations("palmitoyl sphingomyelin (d18:1/16:0)") == frozenset({"d18:1/16:0"})
    assert chain_annotations("hexanoylcarnitine (C6)") == frozenset()  # not an acyl x:y token
    assert chain_annotations("no annotation here") == frozenset()


def test_same_name_with_no_outside_structure_stays_unadjudicated():
    outcome, _ = classify(GLUCOSE, OTHER, _MISS, _MISS, same_name=True)
    assert outcome == "outside_source_unresolved"


def test_a_derivative_across_different_names_is_a_wrong_link_not_a_lookup_artifact():
    # n-oleoyltaurine linked to taurine. Same evidence as the Prolylleucine case, opposite meaning,
    # and the earlier version excused it as an outside-source artifact.
    outcome, rationale = classify(
        "ZKQOUHVVXABNDG", "YCYXUKRYYSXSLJ", PRO_LEU, Z_PRO_LEU, same_name=False
    )
    assert outcome == "conjugate_linked_to_parent"
    assert "WRONG LINK" in rationale


def test_a_derivative_under_the_same_name_is_still_a_lookup_artifact():
    outcome, _ = classify("ZKQOUHVVXABNDG", "YCYXUKRYYSXSLJ", PRO_LEU, Z_PRO_LEU, same_name=True)
    assert outcome == "outside_source_hit_a_derivative"


def test_readjudicate_derives_name_equality_and_records_it():
    resolver = _StubResolver({})
    cases = pd.DataFrame(
        [
            {
                "necs_name": "n-oleoyltaurine",
                "cohort_name": "taurine",
                "verdict": "refuted",
                "refusal_class": "",
                "necs_block": "KOGRJTUIKPMZEJ",
                "cohort_block": "XOAAWQZATWQOTB",
            }
        ]
    )
    result = readjudicate(cases, resolver)  # type: ignore[arg-type]
    row = result.iloc[0]
    assert bool(row["same_vendor_name"]) is False
    # Neither name carries an acyl annotation, so this is the triage bucket, not adverse evidence.
    assert row["readjudication"] == "distinct_vendor_names_structure_unverified"


def test_both_pubchem_callers_default_to_the_stdlib_tls_session():
    # PubChem's edge answers urllib3's own default TLS context with 503 PUGREST.ServerBusy on every
    # request, which reads as load. Both callers must default to the stdlib-context session.
    import ssl

    from biomapper.benchmarks.cross_cohort_readjudicate import OutsideResolver
    from biomapper.benchmarks.scorers.independent_inchikey import PubChemInChIKeyResolver

    for resolver in (OutsideResolver(), PubChemInChIKeyResolver()):
        adapter = resolver._session.get_adapter("https://pubchem.ncbi.nlm.nih.gov/")  # noqa: SLF001
        context = adapter.poolmanager.connection_pool_kw.get("ssl_context")
        assert isinstance(context, ssl.SSLContext)
