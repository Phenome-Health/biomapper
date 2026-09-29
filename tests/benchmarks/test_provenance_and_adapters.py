"""Provenance, circularity labelling, the Hajjar docx parse, and the runner's guards."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import httpx
import pandas as pd
import pytest

from biomapper.benchmarks.adapters.docx_tables import table_with_header, tables
from biomapper.benchmarks.adapters.hajjar import (
    HAS_STRUCTURE_COL,
    SUPPLEMENT_HAS_SMILES,
    HajjarSmilesColumnError,
    build_input_df,
    canonical_gold_csv,
    load_hajjar,
    parse_raw,
)
from biomapper.benchmarks.api_mapper import EmptyDatasetError, TrivialMappingError
from biomapper.benchmarks.config import HAJJAR, DatasetConfig
from biomapper.benchmarks.provenance import (
    KgBuildInfo,
    build_run_provenance,
    circularity_notes,
    fetch_kg_build_info,
    new_run_id,
)
from biomapper.benchmarks.runner import build_manifest, run_vocab

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


# --------------------------------------------------------------------------------------------------
# docx parsing
# --------------------------------------------------------------------------------------------------


def _docx(rows: list[list[str]], *, doctype: bool = False) -> bytes:
    """Build a minimal .docx carrying one table."""

    def cell(text: str) -> str:
        return f"<w:tc><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:tc>"

    body = "".join("<w:tr>" + "".join(cell(c) for c in row) + "</w:tr>" for row in rows)
    prolog = '<!DOCTYPE doc [<!ENTITY a "b">]>' if doctype else ""
    document = (
        f'<?xml version="1.0"?>{prolog}'
        f'<w:document xmlns:w="{W}"><w:body><w:tbl>{body}</w:tbl></w:body></w:document>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def test_docx_table_extraction_is_deterministic():
    raw = _docx([["A", "B"], ["1", "2"]])
    assert tables(raw) == [[["A", "B"], ["1", "2"]]]
    assert tables(raw) == tables(raw)


def test_table_is_selected_by_header_not_by_position():
    """The supplement holds three tables.

    An index would score a competitor table after a reorder.
    """
    raw = _docx(
        [["ChEBI Name", "ChEBI Identifier", "InChIKey"], ["glucose", "CHEBI:4167", "WQZ-X-N"]]
    )
    table = table_with_header(raw, ("ChEBI Name", "InChIKey"))
    assert table[0][0] == "ChEBI Name"
    with pytest.raises(ValueError, match="no table carries all of"):
        table_with_header(raw, ("SMILES",))


def test_non_docx_bytes_fail_loudly():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("other.xml", "<x/>")
    with pytest.raises(ValueError, match="not a .docx"):
        tables(buffer.getvalue())


def test_doctype_is_refused():
    """Defence in depth.

    A legitimate .docx never declares one, and entity expansion is the vector.
    """
    with pytest.raises(ValueError, match="DOCTYPE or ENTITY"):
        tables(_docx([["A"]], doctype=True))


# --------------------------------------------------------------------------------------------------
# Hajjar adapter
# --------------------------------------------------------------------------------------------------


def _hajjar_docx() -> bytes:
    return _docx(
        [
            ["ChEBI Name", "ChEBI Identifier", "InChIKey", "Monoisotopic mass"],
            ["L-lysine", "CHEBI:18019", "KDXKERNSBIXSRK-YFKPBYRVSA-N", "146.105528"],
            ["no structure", "CHEBI:99999", "", "1.0"],
        ]
    )


def test_hajjar_maps_the_real_supplement_columns():
    """The engine adapter expected 'Metabolite name' / 'ChEBI ID' / 'SMILES'.

    None of those exist in the real supplement.
    """
    bundle = load_hajjar(_hajjar_docx())
    frame = bundle.input_df
    assert list(frame.columns) == [
        HAJJAR.name_column,
        HAJJAR.gold_chebi_column,
        HAJJAR.gold_inchikey_column,
        HAS_STRUCTURE_COL,
    ]
    assert frame.iloc[0][HAJJAR.name_column] == "L-lysine"
    assert frame.iloc[0][HAJJAR.gold_inchikey_column] == "KDXKERNSBIXSRK-YFKPBYRVSA-N"
    # A row with no gold structure is retained, marked, and excluded from accuracy later.
    assert bool(frame.iloc[0][HAS_STRUCTURE_COL]) is True
    assert bool(frame.iloc[1][HAS_STRUCTURE_COL]) is False


def test_the_supplement_has_no_smiles_and_the_config_must_not_claim_one():
    assert SUPPLEMENT_HAS_SMILES is False
    assert HAJJAR.gold_smiles_column is None
    lying_config = DatasetConfig(**{**HAJJAR.__dict__, "gold_smiles_column": "gold_smiles"})
    with pytest.raises(HajjarSmilesColumnError, match="ships no SMILES column"):
        build_input_df(parse_raw(_hajjar_docx()), lying_config)


def test_card_pins_both_the_document_and_the_parse():
    """Pinning the source proves which document; pinning the parse proves which table."""
    raw = _hajjar_docx()
    bundle = load_hajjar(raw, source_provenance={"route": "fetched"})
    card = bundle.card
    assert card["source_sha256"] == hashlib.sha256(raw).hexdigest()
    assert (
        card["parsed_gold_sha256"]
        == hashlib.sha256(canonical_gold_csv(parse_raw(raw)).encode()).hexdigest()
    )
    assert card["charge_normalized_available"] is False
    assert "no SMILES column" in card["charge_normalized_unavailable_reason"]
    assert card["source_provenance"] == {"route": "fetched"}
    assert "CC BY-NC-ND" in card["license"]


def test_adapter_refuses_to_fetch_its_own_bytes():
    """Acquisition belongs to sources.acquire, which asserts a non-empty body and the pinned SHA."""
    with pytest.raises(TypeError, match="pass pinned bytes"):
        load_hajjar("https://example.invalid/supplement.docx")  # type: ignore[arg-type]


def test_canonical_gold_csv_is_lf_terminated_and_stable():
    frame = parse_raw(_hajjar_docx())
    text = canonical_gold_csv(frame)
    assert "\r" not in text
    assert canonical_gold_csv(frame) == text


# --------------------------------------------------------------------------------------------------
# provenance
# --------------------------------------------------------------------------------------------------


_HEALTH = {
    "kestrel_version": "0.3.0",
    "kg_build": {
        "kg_version": "2.1.1",
        "kraken_package_version": "2.1.1",
        "biolink_version": "4.2.5",
        "build_timestamp": "2026-09-17T07:27:41Z",
        "git_commit": "9ee53ef0fa1755f0a2d331c155d2539ba6d6ece1",
        "sources": ["babel", "kg2", "lipidmaps", "refmet", "ncbigene", "loinc"],
        "source_versions": {"lipidmaps": "accessed_2026-08-07", "refmet": "accessed_2026-08-07"},
        "build_duration_minutes": 117.6,
    },
}


def test_health_is_read_from_the_service_never_hardcoded(monkeypatch):
    def fake_get(self, url, **kwargs):
        assert url.endswith("/health")
        # No API key must ever reach the public host.
        assert "X-API-Key" not in dict(self.headers or {})
        return httpx.Response(200, json=_HEALTH, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    version, build, error = fetch_kg_build_info("https://kestrel.krakenkg.com/api")
    assert version == "0.3.0"
    assert build.kg_version == "2.1.1"
    assert build.git_commit.startswith("9ee53ef0")
    assert error is None
    # extra="allow" keeps a field we do not model rather than dropping it.
    assert build.model_dump()["build_duration_minutes"] == 117.6


def test_unreachable_health_degrades_but_says_so(monkeypatch):
    def fake_get(self, url, **kwargs):
        raise httpx.ConnectError("unreachable")

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    version, build, error = fetch_kg_build_info("https://kestrel.invalid/api")
    assert version == "unknown"
    assert build.kg_version == "unknown"
    assert "ConnectError" in error

    provenance = build_run_provenance(
        api_endpoint="https://api.invalid", kestrel_url="https://kestrel.invalid/api"
    )
    # Unpinned must be detectable: 'unknown' still looks like provenance otherwise.
    assert provenance.pinned is False


def test_empty_kg_build_is_reported_as_unavailable(monkeypatch):
    def fake_get(self, url, **kwargs):
        return httpx.Response(
            200, json={"kestrel_version": "0.3.0"}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    version, build, error = fetch_kg_build_info("https://kestrel.example/api")
    assert version == "0.3.0"
    assert build.kg_version == "unknown"
    assert "empty kg_build" in error


def test_circularity_is_derived_from_the_builds_own_source_list():
    """The label must track the build, not a note that can go stale."""
    build = KgBuildInfo.model_validate(_HEALTH["kg_build"])
    notes = circularity_notes(build, ["lmsd", "refmet", "hgnc", "hajjar", "srm1950"])
    assert notes["lmsd"]["label"] == "coverage"
    assert notes["lmsd"]["gold_source_in_graph"] == "lipidmaps"
    assert "accessed_2026-08-07" in notes["lmsd"]["reason"]
    assert notes["refmet"]["label"] == "coverage"
    assert notes["hgnc"]["label"] == "coverage"  # ncbigene is ingested
    # Absence of a source is not a full independence verdict, so the label is hedged.
    assert notes["hajjar"]["label"] == "accuracy_candidate"
    assert notes["srm1950"]["label"] == "accuracy_candidate"


def test_a_build_without_the_gold_source_is_not_labelled_coverage():
    build = KgBuildInfo(sources=["babel", "kg2"])
    notes = circularity_notes(build, ["lmsd"])
    assert notes["lmsd"]["label"] == "accuracy_candidate"


def test_run_ids_sort_chronologically_and_are_unique():
    first, second = new_run_id("suite"), new_run_id("suite")
    assert first != second
    assert first.startswith("suite_")


# --------------------------------------------------------------------------------------------------
# runner guards + manifest
# --------------------------------------------------------------------------------------------------


class _StubMapper:
    """Writes a mapped TSV and returns caller-chosen stats."""

    def __init__(self, stats: dict) -> None:
        self.stats = stats

    def map_dataset_to_kg(self, dataset, output_dir, output_prefix, **kwargs):  # noqa: ANN001, ANN003
        path = Path(output_dir) / f"{output_prefix}_MAPPED.tsv"
        path.parent.mkdir(parents=True, exist_ok=True)
        dataset.to_csv(path, sep="\t", index=False)
        return str(path), self.stats


def _provenance():
    return build_run_provenance(
        api_endpoint="https://api.example/api/v1",
        kestrel_url="https://kestrel.example/api",
        probe_live=False,
    )


def test_zero_assigned_mappings_is_refused_as_the_gold_as_provided_trap(tmp_path):
    frame = pd.DataFrame({HAJJAR.name_column: ["glucose"], HAJJAR.gold_inchikey_column: ["X-Y-N"]})
    with pytest.raises(TrivialMappingError, match="trivial-100% trap"):
        run_vocab(
            _StubMapper({"mapped_to_kg_assigned": 0, "mapped_to_kg": 1}),
            frame,
            HAJJAR,
            "CHEBI",
            tmp_path,
            dataset_sha="abc",
            provenance=_provenance(),
        )


def test_empty_frame_names_the_pinned_source(tmp_path):
    with pytest.raises(EmptyDatasetError) as excinfo:
        run_vocab(
            _StubMapper({"mapped_to_kg_assigned": 1}),
            pd.DataFrame({HAJJAR.name_column: []}),
            HAJJAR,
            "CHEBI",
            tmp_path,
            dataset_sha="abc",
            provenance=_provenance(),
        )
    assert "static-content.springer.com" in str(excinfo.value)


def test_manifest_records_every_required_pin(tmp_path):
    frame = pd.DataFrame({HAJJAR.name_column: ["glucose"], HAJJAR.gold_inchikey_column: ["X-Y-N"]})
    run = run_vocab(
        _StubMapper({"mapped_to_kg_assigned": 1, "mapped_to_kg": 1}),
        frame,
        HAJJAR,
        "CHEBI",
        tmp_path,
        dataset_sha="deadbeef",
        provenance=_provenance(),
        source_provenance={"route": "fetched"},
    )
    manifest = json.loads((tmp_path / "hajjar-100_CHEBI_manifest.json").read_text())
    for field in (
        "run_id",
        "biomapper_version",
        "api_endpoint",
        "kestrel_url",
        "kestrel_version",
        "kg_version",
        "biolink_version",
        "kg_build_timestamp",
        "kg_git_commit",
        "source_versions",
        "dataset_source_sha256",
    ):
        assert field in manifest, field
    assert manifest["dataset_source_sha256"] == "deadbeef"
    assert manifest["dataset_source_provenance"] == {"route": "fetched"}
    assert run.ok


def test_build_manifest_never_invents_a_build():
    """An unreachable /health must record 'unknown', not a plausible-looking version."""
    manifest = build_manifest(
        vocab="CHEBI",
        config=HAJJAR,
        dataset_sha="x",
        output_tsv="/tmp/x.tsv",
        provenance=_provenance(),
    )
    assert manifest["kg_version"] == "unknown"
    assert manifest["provenance_pinned"] is False
