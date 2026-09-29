"""metLinkR head-to-head adapter (metabolite arm — cross-linking, DUAL curator + InChIKey oracle).

Turns the metLinkR SI ``ManualMappings.csv`` (the COMETS expert-curator manual cross-link grouping
for the five ``inputs_*`` datasets) into a mapper-ready ``input_df`` (the metabolite NAME query +
held-out gold columns) and a ``dataset_card`` recording N names, per-source-file breakdown, curator
cross-link statistics, provided-ID coverage, pinned source SHA, and license. Mirrors
``metaboliteannotator.py`` / ``necs_metabolon.py``.

Design:
  - The comparable regime is metabolite-ID CROSS-LINKING, so a row is one source metabolite. The
    scorer emits TWO labelled numbers (see ``scorers/metlinkr_scorer.py``): a CURATOR-AGREEMENT rate
    (metLinkR's own ~85.3% metric) over the curator's cross-dataset linked pairs, and an INCHIKEY
    STRUCTURAL CONCORDANCE (the oracle metLinkR LACKS) against the curator's provided reference IDs.
  - INPUT IS NAME-ONLY. BioMapper is handed the metabolite name via ``name_column`` with
    ``provided_id_columns=[]`` (the runner's assigned>0 guard enforces the name path). The curator's
    cross-link group label AND the curator's provided HMDB/PubChem reference IDs are HELD OUT —
    carried alongside the query but consumed ONLY by the scorer. This is consistent with the whole
    name-input harness and is REQUIRED to keep oracle (b) non-trivial: feeding the curator's own
    provided ID back as input would let BioMapper echo its structure (a trivial self-concordance).
    A "(+ provided IDs where present)" parity variant that matches metLinkR's exact inputs is a
    defensible follow-on, deferred here to keep the run mode singular and both oracles held out.
  - Rows with a blank metabolite name (not a queryable input) are DROPPED so they never dilute a
    denominator. A row present but lacking a curator provided ID is RETAINED (still a linkable name)
    with empty gold — an honest unmatched-in-source row, never fabricated.
  - Network is isolated behind ``fetch_manual_mappings`` (fail-loud on a needs-fetching placeholder,
    IPv4-forced) so the transform is fully unit-testable on an in-memory fixture.
"""

from __future__ import annotations

import hashlib
import io
import socket
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pandas as pd

from biomapper.benchmarks.config import (
    METLINKR,
    NEEDS_FETCHING_SENTINEL_METLINKR,
    MetLinkRDatasetConfig,
)

# Stable per-input-row identity (source-file-scoped), carried into the mapper output so the scorer
# can key curator pairs / structural rows back to the exact source metabolite even when two rows in
# different COMETS datasets share a metabolite name. Rides along untouched like the gold columns.
INPUT_ROW_ID_COL = "input_row_id"

# Canonical held-out column -> candidate raw ManualMappings headers (case-insensitive, first match
# wins). The NAME query is REQUIRED; each gold column is optional (a missing one yields an empty
# held-out column and 0% coverage — honest, not fabricated).
QUERY_CANDIDATES: tuple[str, ...] = ("IPT_METABOLITE_NAME", "metabolite_name", "IPT_METABOLITE")
GROUP_LABEL_CANDIDATES: tuple[str, ...] = ("Manual_Metabolite_Group_Label", "manual_metabolite_group_label")
HMDB_CANDIDATES: tuple[str, ...] = ("IPT_HMDB_ID", "IPT_HMDB", "HMDB_ID", "HMDB")
PUBCHEM_CANDIDATES: tuple[str, ...] = ("IPT_PUBCHEM", "IPT_PUBCHEM_CID", "PUBCHEM", "PubChem")
SOURCE_FILE_CANDIDATES: tuple[str, ...] = ("SOURCE_FILE", "source_file")
METABID_CANDIDATES: tuple[str, ...] = ("IPT_METABID", "metabid", "IPT_COMP_ID")

# Sentinel string a source cell uses for "no value" (R ``NA``). Normalized to "" so an absent
# provided id is honestly empty, never a literal "NA" reference.
_NULL_TOKENS = frozenset({"", "na", "nan", "null", "none"})


class MappingsNotResolvedError(RuntimeError):
    """Raised when a live fetch is attempted against an unresolved needs-fetching placeholder."""


class NoManualMappingsError(RuntimeError):
    """Raised when the fetched supplementary bundle contains no ManualMappings.csv to score."""


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


@contextmanager
def force_ipv4():
    """Force IPv4 for the duration of the block (desktop IPv6->Cloudflare route is broken).

    Monkeypatches ``socket.getaddrinfo`` to only return AF_INET results, so ``requests`` (and its
    urllib3 pool) never attempts a hanging IPv6 connection to the mirror host. Restored on exit.
    """
    real_getaddrinfo = socket.getaddrinfo

    def ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
        return real_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = ipv4_only  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.getaddrinfo = real_getaddrinfo  # type: ignore[assignment]


def _extract_manual_mappings_bytes(bundle: bytes, config: MetLinkRDatasetConfig) -> bytes:
    """Pull ManualMappings.csv out of the EuropePMC supplementary bundle (a zip of the SI zips).

    The bundle is a zip that CONTAINS ``pr4c01051_si_003.zip``; that inner zip holds
    ``ManualMappings.csv``. Fails loud if either member is absent rather than silently scoring the
    wrong file. macOS ``__MACOSX/._*`` resource-fork entries are ignored.
    """
    with zipfile.ZipFile(io.BytesIO(bundle)) as outer:
        si_names = [n for n in outer.namelist() if n.rsplit("/", 1)[-1] == config.si_zip_member]
        if not si_names:
            # Some mirrors serve the SI zip directly (not double-wrapped); try treating the bundle
            # itself as the SI zip before failing loud.
            inner_bytes = bundle
        else:
            inner_bytes = outer.read(si_names[0])
    with zipfile.ZipFile(io.BytesIO(inner_bytes)) as inner:
        mm = [
            n
            for n in inner.namelist()
            if n.rsplit("/", 1)[-1] == config.manual_mappings_member and not n.startswith("__MACOSX")
        ]
        if not mm:
            raise NoManualMappingsError(
                f"no {config.manual_mappings_member!r} found in the metLinkR SI bundle "
                f"(members: {sorted(inner.namelist())!r}); refusing to score the wrong file."
            )
        return inner.read(mm[0])


def fetch_manual_mappings(config: MetLinkRDatasetConfig = METLINKR, *, timeout: float = 90.0) -> bytes:
    """Fetch + extract ManualMappings.csv bytes from the live mirror (network, IPv4-forced).

    FAILS LOUD on a needs-fetching placeholder before any network call. The ACS SI is Cloudflare-
    bot-blocked on direct fetch, so bytes come from the EuropePMC ``supplementaryFiles`` bundle for
    PMC12053952 (``config.fetch_url``), which mirrors the identical SI zips. Network is isolated here
    so the transform is unit-testable without it.
    """
    if config.fetch_url.startswith(NEEDS_FETCHING_SENTINEL_METLINKR):
        raise MappingsNotResolvedError(
            f"metLinkR fetch_url {config.fetch_url!r} is a needs-fetching placeholder — resolve the "
            f"real SI mirror (DOI {config.source_doi}, {config.source_pmcid}) before any live run."
        )
    import requests

    with force_ipv4():
        resp = requests.get(config.fetch_url, timeout=timeout)
        resp.raise_for_status()
        bundle = resp.content
    mm_bytes = _extract_manual_mappings_bytes(bundle, config)
    # Verify the fetched curator oracle against the pinned SHA BEFORE any scoring. If EuropePMC serves
    # different/corrupt bytes (mirror drift, truncated download, a swapped SI revision), fail LOUD —
    # silently scoring a different ManualMappings.csv would benchmark against the wrong curator oracle.
    actual_sha = sha256_bytes(mm_bytes)
    if actual_sha != config.expected_manual_mappings_sha256:
        raise RuntimeError(
            f"metLinkR {config.manual_mappings_member!r} SHA256 mismatch: fetched {actual_sha} but the "
            f"config pins {config.expected_manual_mappings_sha256} (DOI {config.source_doi}, "
            f"{config.source_pmcid}). The mirror served different/corrupt bytes; refusing to score a "
            f"curator oracle that is not the pinned one."
        )
    return mm_bytes


def parse_manual_mappings(raw: bytes) -> pd.DataFrame:
    """Parse ManualMappings.csv bytes into a raw DataFrame (all cells as strings)."""
    text = raw.decode("utf-8-sig")
    return pd.read_csv(io.StringIO(text), dtype=str).fillna("")


def _norm(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _norm_id(value: Any) -> str:
    """Normalize a provided-id cell: strip; map R ``NA``/null tokens to empty."""
    s = _norm(value)
    return "" if s.lower() in _NULL_TOKENS else s


def _resolve_column(raw_df: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    lookup = {str(c).strip().lower(): c for c in raw_df.columns}
    for cand in candidates:
        hit = lookup.get(cand.strip().lower())
        if hit is not None:
            return hit
    return None


def build_input_df(raw_df: pd.DataFrame, config: MetLinkRDatasetConfig = METLINKR) -> pd.DataFrame:
    """Build the mapper-ready input_df: name query + held-out curator grouping + provided IDs.

    The mapper is later called with ``name_column=config.name_column`` and ``provided_id_columns=[]``
    — the held-out columns ride along untouched and are consumed only by the scorer, never by
    BioMapper. Blank-name rows are dropped (not queryable). A stable, source-file-scoped
    ``input_row_id`` is emitted per surviving row so the scorer can key curator pairs / structural
    rows even across datasets that share a metabolite name.
    """
    query_raw = _resolve_column(raw_df, QUERY_CANDIDATES)
    if query_raw is None:
        raise KeyError(
            f"ManualMappings is missing a recognizable metabolite-name column; tried {QUERY_CANDIDATES!r} "
            f"against {list(raw_df.columns)!r}"
        )
    group_raw = _resolve_column(raw_df, GROUP_LABEL_CANDIDATES)
    if group_raw is None:
        raise KeyError(
            f"ManualMappings is missing the curator group-label column (the held-out oracle-(a) gold); "
            f"tried {GROUP_LABEL_CANDIDATES!r} against {list(raw_df.columns)!r}"
        )
    hmdb_raw = _resolve_column(raw_df, HMDB_CANDIDATES)
    pubchem_raw = _resolve_column(raw_df, PUBCHEM_CANDIDATES)
    source_raw = _resolve_column(raw_df, SOURCE_FILE_CANDIDATES)
    metabid_raw = _resolve_column(raw_df, METABID_CANDIDATES)

    out = pd.DataFrame()
    out[config.name_column] = raw_df[query_raw].map(_norm)
    out[config.group_label_column] = raw_df[group_raw].map(_norm)
    out[config.gold_hmdb_column] = raw_df[hmdb_raw].map(_norm_id) if hmdb_raw is not None else ""
    out[config.gold_pubchem_column] = raw_df[pubchem_raw].map(_norm_id) if pubchem_raw is not None else ""
    out[config.source_file_column] = raw_df[source_raw].map(_norm) if source_raw is not None else ""
    metabid = raw_df[metabid_raw].map(_norm) if metabid_raw is not None else pd.Series([""] * len(raw_df))

    # A blank name is not a queryable input -> drop so it never dilutes a denominator.
    keep = out[config.name_column] != ""
    out = out[keep].reset_index(drop=True)
    metabid = metabid[keep.values].reset_index(drop=True)

    # Stable, source-file-scoped input_row_id: prefer ``{source_file}:{metabid}`` (metabid is unique
    # within a COMETS file); fall back to a per-row index. Guarantees distinct ids even when the same
    # metabolite name appears in two datasets.
    def _row_id(i: int) -> str:
        sf = _norm(out[config.source_file_column].iat[i]) or "src"
        mid = _norm(metabid.iat[i])
        return f"{sf}:{mid}" if mid else f"{sf}:row{i}"

    ids = [_row_id(i) for i in range(len(out))]
    # De-collide any accidental duplicate id (same source_file+metabid) by suffixing.
    seen: dict[str, int] = {}
    uniq: list[str] = []
    for rid in ids:
        n = seen.get(rid, 0)
        seen[rid] = n + 1
        uniq.append(rid if n == 0 else f"{rid}#{n}")
    out[INPUT_ROW_ID_COL] = uniq
    return out


def _curator_link_stats(input_df: pd.DataFrame, config: MetLinkRDatasetConfig) -> dict[str, Any]:
    """Curator cross-link statistics (traceability): groups, cross-dataset groups, and pair counts."""
    groups: dict[str, list[str]] = {}
    for _, row in input_df.iterrows():
        label = _norm(row.get(config.group_label_column))
        if not label:
            continue
        groups.setdefault(label, []).append(_norm(row.get(config.source_file_column)))
    n_groups = len(groups)
    cross_groups = {g: sf for g, sf in groups.items() if len({s for s in sf if s}) > 1}
    within_pairs = sum(len(m) * (len(m) - 1) // 2 for m in groups.values())
    cross_pairs = 0
    for members in cross_groups.values():
        # count only pairs whose two members come from DIFFERENT source files (the cross-linking test)
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                if members[i] and members[j] and members[i] != members[j]:
                    cross_pairs += 1
    return {
        "n_groups": n_groups,
        "n_cross_dataset_groups": len(cross_groups),
        "within_group_pairs": within_pairs,
        "cross_dataset_pairs": cross_pairs,
    }


def build_card(
    raw_df: pd.DataFrame,
    source_sha: str,
    config: MetLinkRDatasetConfig = METLINKR,
) -> dict[str, Any]:
    """Build the dataset_card: N names, per-source-file breakdown, curator link stats, coverage, SHA."""
    input_df = build_input_df(raw_df, config)
    n = len(input_df)
    hmdb_n = int((input_df[config.gold_hmdb_column].map(_norm) != "").sum())
    pubchem_n = int((input_df[config.gold_pubchem_column].map(_norm) != "").sum())
    provided_any = int(
        (
            (input_df[config.gold_hmdb_column].map(_norm) != "")
            | (input_df[config.gold_pubchem_column].map(_norm) != "")
        ).sum()
    )

    per_source_file: dict[str, dict[str, Any]] = {}
    if config.source_file_column in input_df.columns:
        for sf, grp in input_df.groupby(config.source_file_column):
            per_source_file[str(sf)] = {"n_names": int(len(grp))}

    return {
        "dataset": config.key,
        "arm": config.arm,
        "entity_type": config.entity_type,
        "input_type": config.input_type,
        "input_mode": "name_only",  # provided IDs held out (see module docstring)
        "target_vocabs": list(config.target_vocabs),
        "n_names": n,
        "per_source_file": per_source_file,
        "curator_link_stats": _curator_link_stats(input_df, config),
        "provided_id_coverage": {
            "hmdb": {"n": hmdb_n, "fraction": (hmdb_n / n) if n else 0.0},
            "pubchem": {"n": pubchem_n, "fraction": (pubchem_n / n) if n else 0.0},
            "any": {"n": provided_any, "fraction": (provided_any / n) if n else 0.0},
        },
        "held_out_columns": {
            "curator_grouping": config.group_label_column,
            "curator_hmdb": config.gold_hmdb_column,
            "curator_pubchem": config.gold_pubchem_column,
        },
        "source_doi": config.source_doi,
        "source_pmcid": config.source_pmcid,
        "source_sha256": source_sha,
        "license": config.license,
    }


@dataclass(frozen=True)
class MetLinkRBundle:
    input_df: pd.DataFrame
    card: dict[str, Any]


def load_metlinkr(
    source: bytes | str | pd.DataFrame,
    config: MetLinkRDatasetConfig = METLINKR,
) -> MetLinkRBundle:
    """Load from a raw DataFrame (tests), ManualMappings.csv bytes, or the sentinel string "fetch".

    A DataFrame is used as-is (SHA pinned over its canonical CSV bytes for deterministic tests). The
    string ``"fetch"`` (or any non-empty string) triggers the live ``fetch_manual_mappings`` path
    (network isolated + fail-loud on placeholder). Raw bytes are parsed directly and SHA-pinned.
    """
    if isinstance(source, pd.DataFrame):
        raw_df = source
        raw_bytes = raw_df.to_csv(index=False).encode("utf-8")
    elif isinstance(source, bytes):
        raw_bytes = source
        raw_df = parse_manual_mappings(raw_bytes)
    elif isinstance(source, str):
        raw_bytes = fetch_manual_mappings(config)
        raw_df = parse_manual_mappings(raw_bytes)
    else:
        raise TypeError(f"unsupported source type {type(source)!r}")

    sha = sha256_bytes(raw_bytes)
    return MetLinkRBundle(input_df=build_input_df(raw_df, config), card=build_card(raw_df, sha, config))
