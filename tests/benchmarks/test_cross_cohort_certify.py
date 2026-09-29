"""Cross-cohort certification and refusal adjudication (offline; no PubChem, no KG).

The load-bearing assertions here are about refusal accounting, because refusal is the outcome the
write-up has to be able to explain: a names-only panel refuses by construction, a failed lookup is a
transient run artifact that must not be reported as an absent structure, and a first-block
comparison never claims to have checked stereochemistry.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

import biomapper.benchmarks.cross_cohort_certify as certify_module
from biomapper.benchmarks.cross_cohort_certify import (
    MissingLinkArtifactError,
    adjudicate_cases,
    first_block,
    necs_gold_blocks,
    read_links,
)
from biomapper.benchmarks.scorers.cross_cohort_overlap import Link
from biomapper.benchmarks.scorers.independent_inchikey import ProvidedBlock

GLUCOSE_BLOCK = "WQZGKKKJIJFFOK"
OTHER_BLOCK = "ZZZZZZZZZZZZZZ"


def test_first_block_extracts_connectivity_layer_only():
    assert first_block("WQZGKKKJIJFFOK-GASJEMHNSA-N") == GLUCOSE_BLOCK
    assert first_block("WQZGKKKJIJFFOK") == GLUCOSE_BLOCK
    assert first_block("") is None and first_block(None) is None


def _necs(block: str | None, name: str = "glucose") -> ProvidedBlock:
    return ProvidedBlock(block, "gold-necs-moesm5", "success", record_id=f"moesm5:{name}")


def _cohort(
    block: str | None,
    status: str = "success",
    source: str = "provided-pubchem",
    name: str = "glucose",
) -> ProvidedBlock:
    return ProvidedBlock(block, source, status, record_id=f"arivale:{name}")


def _link(a: str = "glucose", b: str = "glucose") -> Link:
    return Link(a_name=a, b_name=b, shared=frozenset({"CHEBI:17234"}))


def test_agreeing_blocks_certify_but_never_claim_stereo():
    cases = adjudicate_cases(
        [_link()], {"glucose": _necs(GLUCOSE_BLOCK)}, {"glucose": _cohort(GLUCOSE_BLOCK)}
    )
    row = cases.iloc[0]
    assert row["verdict"] == "certified"
    # Both sides are first blocks, so the stereo layer was not compared. Saying otherwise would
    # pass a stereoisomer error silently.
    assert bool(row["stereo_checked"]) is False


def test_connectivity_disagreement_is_refuted_with_both_blocks_recorded():
    cases = adjudicate_cases(
        [_link()], {"glucose": _necs(GLUCOSE_BLOCK)}, {"glucose": _cohort(OTHER_BLOCK)}
    )
    row = cases.iloc[0]
    assert row["verdict"] == "refuted"
    assert row["necs_block"] == GLUCOSE_BLOCK and row["cohort_block"] == OTHER_BLOCK
    assert row["refusal_class"] == ""


def test_missing_necs_gold_is_refused_and_classified():
    cases = adjudicate_cases([_link()], {}, {"glucose": _cohort(GLUCOSE_BLOCK)})
    row = cases.iloc[0]
    assert row["verdict"] == "refused"
    assert row["refusal_class"] == "necs_gold_has_no_curated_inchikey"


def test_transient_lookup_failure_is_not_reported_as_absent_structure():
    cases = adjudicate_cases(
        [_link()],
        {"glucose": _necs(GLUCOSE_BLOCK)},
        {"glucose": _cohort(None, status="lookup_failed")},
    )
    row = cases.iloc[0]
    assert row["verdict"] == "refused"
    assert row["refusal_class"] == "cohort_lookup_failed_transient"


def test_clean_miss_is_distinguished_from_a_failure():
    cases = adjudicate_cases(
        [_link()],
        {"glucose": _necs(GLUCOSE_BLOCK)},
        {"glucose": _cohort(None, status="clean_miss")},
    )
    assert cases.iloc[0]["refusal_class"] == "cohort_lookup_clean_miss"


def test_panel_without_structure_resolvable_id_is_classified_separately():
    cases = adjudicate_cases(
        [_link()],
        {"glucose": _necs(GLUCOSE_BLOCK)},
        {"glucose": _cohort(None, status="no_structure_resolvable_id", source="none")},
    )
    assert cases.iloc[0]["refusal_class"] == "cohort_panel_has_no_structure_resolvable_id"


def test_both_sides_missing_is_its_own_class():
    cases = adjudicate_cases([_link()], {}, {})
    assert cases.iloc[0]["refusal_class"] == "no_independent_structure_either_side"


def test_same_curator_record_refuses_rather_than_self_certifying():
    shared = ProvidedBlock(GLUCOSE_BLOCK, "provided-hmdb", "success", record_id="same:glucose")
    cases = adjudicate_cases([_link()], {"glucose": shared}, {"glucose": shared})
    row = cases.iloc[0]
    assert row["verdict"] == "refused" and "same curator record" in row["reason"]


def test_read_links_round_trips_the_driver_output(tmp_path):
    path = tmp_path / "links_necs_arivale.csv"
    pd.DataFrame(
        [
            {
                "necs_name": "glucose",
                "arivale_name": "glucose",
                "shared_curies": "CHEBI:17234|KEGG:C00031",
            }
        ]
    ).to_csv(path, index=False)
    links = read_links(path, "arivale")
    assert len(links) == 1
    assert links[0].a_name == "glucose" and links[0].b_name == "glucose"
    assert links[0].shared == frozenset({"CHEBI:17234", "KEGG:C00031"})


def test_read_links_handles_an_empty_link_file(tmp_path):
    path = tmp_path / "links_necs_llfs.csv"
    pd.DataFrame(columns=["necs_name", "llfs_name", "shared_curies"]).to_csv(path, index=False)
    assert read_links(path, "llfs") == []


# ==================================================================================================
# The NECS gold, and the end-to-end report
# ==================================================================================================


def _moesm5_xlsx(path: Path) -> None:
    """A MOESM5-shaped supplement: two InChIKey vintages, one row with no curated key at all."""
    pd.DataFrame(
        {
            "CHEMICAL_NAME": ["glucose", "cortisone", "unknown_thing"],
            "INCHIKEY": [
                "WQZGKKKJIJFFOK-UHFFFAOYAK",
                "MFYSYFVPBJMHGN-ZPOLXVRWSA-N",
                "",
            ],
            "inchi_key": [
                "WQZGKKKJIJFFOK-GASJEMHNSA-N",
                "ZZZZZZZZZZZZZZ-ZPOLXVRWSA-N",  # vintages disagree at the first block
                "",
            ],
            "HMDB": ["HMDB0000122", "HMDB0000016", ""],
        }
    ).to_excel(path, index=False)


def test_necs_gold_blocks_prefers_the_standard_vintage_and_counts_the_disagreement(tmp_path):
    path = tmp_path / "moesm5.xlsx"
    _moesm5_xlsx(path)
    blocks, card = necs_gold_blocks(path)
    # The standard column wins, so a hand re-adjudication reads an interpretable key.
    assert blocks["glucose"].block == "WQZGKKKJIJFFOK"
    assert blocks["cortisone"].block == "ZZZZZZZZZZZZZZ"
    assert blocks["glucose"].source == "gold-necs-moesm5"
    # A row with no curated key yields a TAGGED entry carrying no block. It still refuses, but the
    # tag is what lets the untagged-sides canary mean "provenance we failed to record" rather than
    # "the gold genuinely has no key here", which we did record.
    assert blocks["unknown_thing"].block is None
    assert blocks["unknown_thing"].source == "gold-necs-moesm5"
    assert card["n_rows"] == 3 and card["n_with_curated_inchikey"] == 2
    # The supplement disagreeing with itself is measured, not assumed.
    assert card["two_vintage_first_block_agreement"] == {
        "both_present": 2,
        "agree": 1,
        "disagree": 1,
    }
    assert "5% InChIKey errors" in card["known_defect"]


def _run_dir(tmp_path: Path, *, arivale_links: int = 1, write_manifest: bool = True) -> Path:
    run = tmp_path / "run"
    run.mkdir()
    if write_manifest:
        (run / "manifest.json").write_text(
            json.dumps(
                {
                    "results": {
                        "arivale": {"arm_m_links": arivale_links},
                        "xuetal": {"arm_m_links": 1},
                        "llfs": {"arm_m_links": 1},
                        "blsa": {"arm_m_links": 1},
                    }
                }
            )
        )
    for cohort in ("arivale", "xuetal", "llfs", "blsa"):
        pd.DataFrame(
            [
                {
                    "necs_name": "glucose",
                    f"{cohort}_name": "glucose",
                    "shared_curies": "CHEBI:17234",
                }
            ]
        ).to_csv(run / f"links_necs_{cohort}.csv", index=False)
    return run


def test_certify_reports_names_only_cohorts_as_refused_by_construction(tmp_path, monkeypatch):
    run = _run_dir(tmp_path)
    moesm5 = tmp_path / "moesm5.xlsx"
    _moesm5_xlsx(moesm5)
    monkeypatch.setattr(
        certify_module,
        "arivale_independent_blocks",
        lambda _path, _names, **_kw: (
            {
                "glucose": ProvidedBlock(
                    "WQZGKKKJIJFFOK", "provided-pubchem", "success", "arivale:g"
                )
            },
            {"oracle": "stub"},
        ),
    )
    exit_code = certify_module.main(
        ["--run-dir", str(run), "--moesm5", str(moesm5), "--arivale-xlsx", str(moesm5)]
    )
    assert exit_code == 0
    report = json.loads((run / "certificate_report.json").read_text())
    for cohort in ("xuetal", "llfs", "blsa"):
        entry = report["cohorts"][cohort]
        assert entry["certifiable"] is False
        assert entry["refused_by_construction"] is True
        assert entry["refused"] == entry["n_links"] == 1
        assert entry["certified"] == 0
        # The wording matters: a blank certification column must not read as a failure.
        assert "not an uncertified failure" in entry["note"]
    arivale = report["cohorts"]["arivale"]
    assert arivale["certifiable"] is True and arivale["certified"] == 1
    assert arivale["stereo_checked_any"] is False  # first-block comparison only


def test_certify_refuses_a_run_directory_with_a_missing_link_file(tmp_path, monkeypatch):
    run = _run_dir(tmp_path)
    (run / "links_necs_llfs.csv").unlink()
    moesm5 = tmp_path / "moesm5.xlsx"
    _moesm5_xlsx(moesm5)
    monkeypatch.setattr(
        certify_module,
        "arivale_independent_blocks",
        lambda _path, _names, **_kw: ({}, {"oracle": "stub"}),
    )
    # A missing artifact must not be reported as a cohort with zero links.
    with pytest.raises(MissingLinkArtifactError):
        certify_module.main(
            ["--run-dir", str(run), "--moesm5", str(moesm5), "--arivale-xlsx", str(moesm5)]
        )


def test_certify_refuses_a_link_file_that_disagrees_with_its_manifest(tmp_path, monkeypatch):
    run = _run_dir(tmp_path, arivale_links=99)
    moesm5 = tmp_path / "moesm5.xlsx"
    _moesm5_xlsx(moesm5)
    monkeypatch.setattr(
        certify_module,
        "arivale_independent_blocks",
        lambda _path, _names, **_kw: ({}, {"oracle": "stub"}),
    )
    assert (
        certify_module.main(
            ["--run-dir", str(run), "--moesm5", str(moesm5), "--arivale-xlsx", str(moesm5)]
        )
        == 2
    )


def test_certify_requires_a_manifest(tmp_path):
    run = _run_dir(tmp_path, write_manifest=False)
    assert certify_module.main(["--run-dir", str(run)]) == 2


# ==================================================================================================
# A corrupt gold cell must not become a comparable block
# ==================================================================================================


def _moesm5_with_corrupt_sentinel(path: Path) -> None:
    """The documented corrupt '4000' placeholder, in both vintages and in each alone."""
    pd.DataFrame(
        {
            "CHEMICAL_NAME": ["clean", "both_corrupt", "legacy_corrupt_standard_ok", "blank"],
            "INCHIKEY": [
                "WQZGKKKJIJFFOK-UHFFFAOYAK",
                "4000",
                "4000",
                "",
            ],
            "inchi_key": [
                "WQZGKKKJIJFFOK-GASJEMHNSA-N",
                "4000",
                "MFYSYFVPBJMHGN-ZPOLXVRWSA-N",
                "",
            ],
        }
    ).to_excel(path, index=False)


def test_the_corrupt_sentinel_never_becomes_a_block(tmp_path):
    path = tmp_path / "moesm5.xlsx"
    _moesm5_with_corrupt_sentinel(path)
    blocks, card = necs_gold_blocks(path)

    # Unscreened, "4000" would be a 4-character block that can never equal a real 14-character one,
    # so every link through this row would come back REFUTED and read as a wrong molecule. The row
    # is still present, tagged, with NO block, which refuses instead.
    assert blocks["both_corrupt"].block is None
    assert blocks["clean"].block == "WQZGKKKJIJFFOK"
    # A row whose legacy cell is corrupt but whose standard cell is usable still contributes.
    assert blocks["legacy_corrupt_standard_ok"].block == "MFYSYFVPBJMHGN"
    assert blocks["blank"].block is None
    # n_with_curated_inchikey counts rows that yielded a usable BLOCK, not rows with an entry.
    assert card["n_with_curated_inchikey"] == 2


def test_corrupt_cells_rows_and_exclusions_are_counted_separately(tmp_path):
    path = tmp_path / "moesm5.xlsx"
    _moesm5_with_corrupt_sentinel(path)
    _blocks, card = necs_gold_blocks(path)
    # Three corrupt CELLS across two ROWS: both vintages on one, the legacy vintage on another. A
    # single count cannot describe both, and naming a cell count after rows overstates the damage.
    assert card["rejected_gold_values"] == {"4000": 3}
    assert card["n_corrupt_gold_cells"] == 3
    assert card["n_rows_with_any_corrupt_gold"] == 2
    # Only one of those rows actually lost its block: the other still has a usable standard key.
    assert card["n_rows_excluded_by_screen"] == 1
    assert card["rows_excluded_by_screen"] == ["both_corrupt"]
    assert "REFUTED" in card["screen"]


def test_a_blank_row_is_not_counted_as_excluded_by_the_screen(tmp_path):
    # A row with no key at all was never going to contribute; only a row the SCREEN removed counts.
    path = tmp_path / "moesm5.xlsx"
    _moesm5_with_corrupt_sentinel(path)
    _blocks, card = necs_gold_blocks(path)
    assert "blank" not in card["rows_excluded_by_screen"]


def test_a_corrupt_row_does_not_inflate_the_two_vintage_agreement(tmp_path):
    path = tmp_path / "moesm5.xlsx"
    _moesm5_with_corrupt_sentinel(path)
    _blocks, card = necs_gold_blocks(path)
    # Only "clean" has two usable vintages; the corrupt row must not count as an agreement.
    assert card["two_vintage_first_block_agreement"] == {
        "both_present": 1,
        "agree": 1,
        "disagree": 0,
    }


def test_coverage_counts_usable_blocks_not_entries(tmp_path):
    # A row with no key now gets a tagged no-block entry, so len(blocks) is the ROW count. Reporting
    # that as curated-key coverage would silently turn a 63% figure into 100%.
    path = tmp_path / "moesm5.xlsx"
    _moesm5_with_corrupt_sentinel(path)
    blocks, card = necs_gold_blocks(path)
    assert card["n_entries"] == len(blocks) == 4
    assert card["n_with_curated_inchikey"] == 2
    assert card["coverage"] == 0.5


def test_a_necs_side_gap_is_not_attributed_to_the_cohort():
    # Both sides now carry a tagged entry even with no structure, so a presence test would find the
    # NECS entry, fall through to the cohort branches, and blame the cohort for a NECS-side gap.
    cases = adjudicate_cases(
        [_link()],
        {"glucose": ProvidedBlock(None, "gold-necs-moesm5", "clean_miss", "moesm5:glucose")},
        {"glucose": _cohort(GLUCOSE_BLOCK)},  # cohort side is perfectly fine
    )
    row = cases.iloc[0]
    assert row["verdict"] == "refused"
    assert row["refusal_class"] == "necs_gold_has_no_curated_inchikey"


def test_both_sides_tagged_without_blocks_is_its_own_class():
    cases = adjudicate_cases(
        [_link()],
        {"glucose": ProvidedBlock(None, "gold-necs-moesm5", "clean_miss", "moesm5:glucose")},
        {"glucose": _cohort(None, status="clean_miss")},
    )
    assert cases.iloc[0]["refusal_class"] == "no_independent_structure_either_side"


class _RecordingResolver:
    """Stands in for PubChemInChIKeyResolver: answers only the ids in ``known``, 404s the rest."""

    def __init__(self, known: dict[str, str], **_kw: object) -> None:
        self.known = known
        self.asked: list[str] = []

    def _cached_resolve(self, key: str, _path: str) -> tuple[str | None, str]:
        self.asked.append(key)
        block = self.known.get(key)
        return block, ("success" if block else "clean_miss")


def _arivale_xlsx(path: Path, rows: list[dict[str, str]]) -> None:
    columns = ["BiochemicalName", "CAS_ID", "KEGG_ID", "HMDB_ID", "PubChem_ID"]
    frame = pd.DataFrame(rows, columns=columns)
    with pd.ExcelWriter(path) as writer:
        frame.fillna("").to_excel(writer, sheet_name="Arivale_Metabolomics", index=False)


def test_a_legacy_hmdb_accession_is_looked_up_in_its_modern_form(tmp_path, monkeypatch):
    # The Arivale panel ships 5-digit accessions that PubChem's RegistryID index 404s. Sent as-is,
    # a real structure came back as a clean miss and its links refused as absent.
    import biomapper.benchmarks.scorers.independent_inchikey as inchikey_module

    resolver = _RecordingResolver({"hmdb:HMDB0013127": "UEFRDQSMQXDWTO"})
    monkeypatch.setattr(inchikey_module, "PubChemInChIKeyResolver", lambda **_kw: resolver)
    xlsx = tmp_path / "arivale.xlsx"
    _arivale_xlsx(xlsx, [{"BiochemicalName": "3-hydroxybutyrylcarnitine", "HMDB_ID": "HMDB13127"}])

    blocks, card = certify_module.arivale_independent_blocks(xlsx, {"3-hydroxybutyrylcarnitine"})

    entry = blocks["3-hydroxybutyrylcarnitine"]
    assert entry.block == "UEFRDQSMQXDWTO" and entry.status == "success"
    assert entry.source == "provided-hmdb"
    assert resolver.asked == ["hmdb:HMDB0013127"]  # modern form first, no wasted legacy request
    assert card["blocks_resolved"] == 1


def test_a_legacy_hmdb_accession_falls_back_to_its_original_form(tmp_path, monkeypatch):
    import biomapper.benchmarks.scorers.independent_inchikey as inchikey_module

    resolver = _RecordingResolver({})
    monkeypatch.setattr(inchikey_module, "PubChemInChIKeyResolver", lambda **_kw: resolver)
    xlsx = tmp_path / "arivale.xlsx"
    _arivale_xlsx(xlsx, [{"BiochemicalName": "x", "HMDB_ID": "HMDB13127"}])

    blocks, _card = certify_module.arivale_independent_blocks(xlsx, {"x"})

    assert resolver.asked == ["hmdb:HMDB0013127", "hmdb:HMDB13127"]
    assert blocks["x"].status == "clean_miss"


def test_min_interval_is_settable_from_the_command_line(tmp_path, monkeypatch):
    run = _run_dir(tmp_path)
    moesm5 = tmp_path / "moesm5.xlsx"
    _moesm5_xlsx(moesm5)
    seen: dict[str, float] = {}

    def _stub(_path, _names, *, min_interval_s):
        seen["min_interval_s"] = min_interval_s
        return {}, {"oracle": "stub", "min_interval_s": min_interval_s}

    monkeypatch.setattr(certify_module, "arivale_independent_blocks", _stub)
    exit_code = certify_module.main(
        [
            "--run-dir",
            str(run),
            "--moesm5",
            str(moesm5),
            "--arivale-xlsx",
            str(moesm5),
            "--min-interval-s",
            "1.5",
        ]
    )
    assert exit_code == 0
    assert seen["min_interval_s"] == 1.5
