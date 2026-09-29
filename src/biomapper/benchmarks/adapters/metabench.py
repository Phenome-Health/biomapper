"""MetaBench Grounding adapter (Lu et al. 2025, arXiv:2510.14944).

Turns the paper's 1,000-pair cross-database Grounding set — delivered as natural-language QA
(``question``, ``answer``) — into mapper-ready ``input_df`` frames plus a dataset card recording
N, per-subgroup counts, source SHA, and license.

The grounding set is bidirectional and mixed-regime:

  - **ID -> ID** (400): ``What is the KEGG ID of HMDB ID HMDB0010090?`` -> ``C00626``. The source
    id is handed to BioMapper as a *provided id* (provided-ID mode, ``annotation_mode='none'``).
  - **name -> ID** (600): ``What is the ChEBI ID of metabolite Tramadol?`` -> ``90911``. The source
    is a metabolite *name* handed to the annotate path (name-input mode, ``annotation_mode='all'``).

Both regimes hold out the TARGET database id as the gold; correctness is CURIE equality between
BioMapper's equivalence-set predictions and the gold. So one uniform scorer, ONE number.

FAIL-LOUD: every question must match one of the five known templates; an unrecognized template
raises ``MetaBenchParseError`` (never silently dropped). Network is isolated behind
``fetch_grounding`` so the transform is fully unit-testable on an in-memory fixture.
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from typing import Any, cast

import pandas as pd

from biomapper.benchmarks.config import METABENCH, MetaBenchDatasetConfig, ProvidedIdDatasetConfig

RAW_QUESTION_COL = "question"
RAW_ANSWER_COL = "answer"

# Canonical target namespaces (as they appear in the gold CURIE prefix).
_NS_CANON = {"KEGG": "KEGG", "HMDB": "HMDB", "CHEBI": "CHEBI", "CHEBI ID": "CHEBI"}
# How the paper's question text names each database.
_QUESTION_NS = {"KEGG": "KEGG", "HMDB": "HMDB", "CHEBI": "ChEBI"}

# ID -> ID:  "What is the <TGT> ID of <SRC> ID <source_id>?"
_ID2ID_RE = re.compile(r"^What is the (KEGG|HMDB|ChEBI) ID of (HMDB|KEGG|ChEBI) ID (\S.*?)\?\s*$")
# name -> ID: "What is the <TGT> ID of metabolite <name>?"
_NAME2ID_RE = re.compile(r"^What is the (KEGG|HMDB|ChEBI) ID of metabolite (.+?)\?\s*$")


class MetaBenchParseError(ValueError):
    """Raised when a grounding question does not match any known template (fail-loud, never drop)."""


class MetaBenchShaMismatchError(RuntimeError):
    """Raised when the fetched grounding CSV SHA does not match the pinned acquisition SHA.

    The MetaBench comparison is only valid against the SAME 1,000-pair file the 25-LLM baselines
    were scored on. If the upstream HuggingFace CSV drifts, silently scoring a *different* dataset
    invalidates the same-set comparison — so the pin GATES the run rather than merely decorating the
    card. Re-pin ``expected_source_sha256`` intentionally (after re-verifying the dataset) to accept
    a new file.
    """


def enforce_sha_pin(sha: str, config: MetaBenchDatasetConfig) -> None:
    """Fail loud if the fetched CSV SHA does not match the pinned acquisition SHA.

    A blank ``expected_source_sha256`` disables the gate (intentionally un-pinned — e.g. an
    in-memory fixture whose SHA is not known ahead of time). A non-blank pin MUST match.
    """
    expected = (config.expected_source_sha256 or "").strip()
    if expected and sha != expected:
        raise MetaBenchShaMismatchError(
            f"{config.key}: fetched grounding CSV SHA256 {sha} does not match the pinned acquisition "
            f"SHA256 {expected}. The upstream MetaBench file has changed — the same-set LLM "
            f"comparison is only valid against the pinned file, so this run is REFUSED before any "
            f"parsing/scoring. If the change is intentional, re-verify the dataset and update "
            f"config.py's expected_source_sha256 to re-pin it."
        )


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def fetch_grounding(url: str, *, timeout: float = 60.0) -> bytes:
    """Fetch the MetaBench Grounding CSV bytes (network). Isolated so tests never hit it."""
    import requests

    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def parse_raw(raw: bytes) -> pd.DataFrame:
    """Parse the grounding CSV bytes into the raw ``question``/``answer`` frame."""
    text = raw.decode("utf-8-sig")
    df = pd.read_csv(io.StringIO(text), dtype=str).fillna("")
    missing = {RAW_QUESTION_COL, RAW_ANSWER_COL} - set(df.columns)
    if missing:
        raise MetaBenchParseError(
            f"MetaBench grounding CSV missing required column(s) {sorted(missing)} "
            f"(columns present: {list(df.columns)})"
        )
    return df


def _canon_target_ns(question_ns: str) -> str:
    key = question_ns.strip().upper()
    if key not in _NS_CANON:
        raise MetaBenchParseError(f"unrecognized target namespace {question_ns!r} in grounding question")
    return _NS_CANON[key]


def parse_grounding(raw: bytes, config: MetaBenchDatasetConfig = METABENCH) -> pd.DataFrame:
    """Parse the raw QA frame into the normalized long form (the adapter's column contract).

    Columns: ``question``, ``metabolite_name``, ``source_id``, ``source_namespace``,
    ``gold_target`` (bare target id, HELD OUT), ``target_namespace`` (HELD OUT), ``pair_type``.
    Fail-loud on any unrecognized question template.
    """
    raw_df = parse_raw(raw)
    rows: list[dict[str, str]] = []
    for _, r in raw_df.iterrows():
        q = str(r[RAW_QUESTION_COL]).strip()
        answer = str(r[RAW_ANSWER_COL]).strip()
        m_id = _ID2ID_RE.match(q)
        m_nm = _NAME2ID_RE.match(q)
        if m_id:
            tgt = _canon_target_ns(m_id.group(1))
            src = _canon_target_ns(m_id.group(2))
            source_id = m_id.group(3).strip()
            rows.append(
                {
                    config.question_column: q,
                    config.name_column: "",
                    config.source_id_column: source_id,
                    config.source_namespace_column: src,
                    config.gold_target_column: answer,
                    config.target_namespace_column: tgt,
                    config.pair_type_column: "id2id",
                }
            )
        elif m_nm:
            tgt = _canon_target_ns(m_nm.group(1))
            name = m_nm.group(2).strip()
            rows.append(
                {
                    config.question_column: q,
                    config.name_column: name,
                    config.source_id_column: "",
                    config.source_namespace_column: "",
                    config.gold_target_column: answer,
                    config.target_namespace_column: tgt,
                    config.pair_type_column: "name2id",
                }
            )
        else:
            raise MetaBenchParseError(f"grounding question does not match any known template (id2id / name2id): {q!r}")
    return pd.DataFrame(rows, columns=list(rows[0].keys()) if rows else None)


# --------------------------------------------------------------------------------------------------
# Per-subgroup input frames. One subgroup per (pair_type, source_ns, target_ns). ID->ID subgroups
# run in provided-ID mode (source id in a normalizer-recognizable column); name->ID subgroups run
# in name-input mode. Every subgroup carries the two HELD-OUT scoring columns verbatim so the
# concatenated mapper output is scorable by the single uniform scorer.
# --------------------------------------------------------------------------------------------------

# Provided-ID source column name per source namespace (must be a biomapper2-normalizer-recognized
# vocab column name so provided-ID mode identifies the source vocab).
_PROVIDED_SOURCE_COLUMN = {"HMDB": "hmdb", "KEGG": "kegg", "CHEBI": "chebi"}


@dataclass(frozen=True)
class MetaBenchSubgroup:
    """One runnable slice of MetaBench: an input_df plus how to run + score it."""

    key: str  # e.g. "metabench-grounding-hmdb2kegg"
    pair_type: str  # "id2id" | "name2id"
    source_namespace: str  # "HMDB" / "KEGG" / "" (name)
    target_namespace: str  # "KEGG" / "HMDB" / "CHEBI"
    source_id_column: str | None  # provided source column (id2id) or None (name2id)
    vocab: str  # target-namespace vocab hint for the name-input run
    input_df: pd.DataFrame


def _subgroup_key(config: MetaBenchDatasetConfig, pair_type: str, src_ns: str, tgt_ns: str) -> str:
    if pair_type == "id2id":
        return f"{config.key}-{src_ns.lower()}2{tgt_ns.lower()}"
    return f"{config.key}-name2{tgt_ns.lower()}"


def build_subgroups(long_df: pd.DataFrame, config: MetaBenchDatasetConfig = METABENCH) -> list[MetaBenchSubgroup]:
    """Split the normalized long frame into per-subgroup runnable input frames.

    ID->ID: input_df = [name placeholder, provided source column, held-out gold + target_ns +
    source_ns]. name->ID: input_df = [name query, held-out gold + target_ns + source_ns].
    """
    subgroups: list[MetaBenchSubgroup] = []
    grp_cols = [config.pair_type_column, config.source_namespace_column, config.target_namespace_column]
    for key, grp in long_df.groupby(grp_cols, sort=True):
        pair_type, src_ns, tgt_ns = cast("tuple[str, str, str]", key)
        grp = grp.reset_index(drop=True)
        if pair_type == "id2id":
            source_col = _PROVIDED_SOURCE_COLUMN.get(src_ns)
            if source_col is None:
                raise MetaBenchParseError(f"no provided-ID source column mapping for source namespace {src_ns!r}")
            input_df = pd.DataFrame(
                {
                    config.name_column: ["" for _ in range(len(grp))],  # inert placeholder query
                    source_col: grp[config.source_id_column].map(lambda s: str(s).strip()),
                    config.gold_target_column: grp[config.gold_target_column],  # held out
                    config.target_namespace_column: grp[config.target_namespace_column],  # held out
                    config.source_namespace_column: grp[config.source_namespace_column],  # for the guard
                }
            )
        else:  # name2id
            source_col = None
            input_df = pd.DataFrame(
                {
                    config.name_column: grp[config.name_column],  # the query
                    config.gold_target_column: grp[config.gold_target_column],  # held out
                    config.target_namespace_column: grp[config.target_namespace_column],  # held out
                    config.source_namespace_column: grp[config.source_namespace_column],  # "" for name rows
                }
            )
        subgroups.append(
            MetaBenchSubgroup(
                key=_subgroup_key(config, pair_type, src_ns, tgt_ns),
                pair_type=pair_type,
                source_namespace=src_ns,
                target_namespace=tgt_ns,
                source_id_column=source_col,
                vocab=tgt_ns,
                input_df=input_df,
            )
        )
    return subgroups


def build_card(
    long_df: pd.DataFrame,
    *,
    source_sha: str,
    config: MetaBenchDatasetConfig = METABENCH,
) -> dict[str, Any]:
    """MetaBench dataset card: N, per-subgroup counts, source SHA/URL/license, held-out identity."""
    n = len(long_df)
    counts: dict[str, int] = {}
    for key, grp in long_df.groupby(
        [config.pair_type_column, config.source_namespace_column, config.target_namespace_column], sort=True
    ):
        pair_type, src_ns, tgt_ns = cast("tuple[str, str, str]", key)
        counts[_subgroup_key(config, pair_type, src_ns, tgt_ns)] = int(len(grp))
    return {
        "dataset": config.key,
        "arm": config.arm,
        "entity_type": config.entity_type,
        "input_type": config.input_type,
        "n_rows": n,
        "n_id2id": int((long_df[config.pair_type_column] == "id2id").sum()),
        "n_name2id": int((long_df[config.pair_type_column] == "name2id").sum()),
        "subgroup_counts": counts,
        # Load-bearing anti-trivial record: the gold TARGET + its namespace are HELD OUT (scorer-only).
        "held_out_columns": [config.gold_target_column, config.target_namespace_column],
        "source_doi": config.source_doi,
        "source_url": config.source_url,
        "source_sha256": source_sha,
        "expected_source_sha256": config.expected_source_sha256,
        "license": config.license,
        "n_baseline_competitors": len(config.baseline_competitors),
    }


@dataclass(frozen=True)
class MetaBenchBundle:
    long_df: pd.DataFrame
    subgroups: list[MetaBenchSubgroup]
    card: dict[str, Any]


def provided_config_for_subgroup(
    sub: MetaBenchSubgroup, config: MetaBenchDatasetConfig = METABENCH
) -> ProvidedIdDatasetConfig:
    """Build the provided-ID run config for an id2id subgroup.

    ``known_source_gap`` is set for KEGG-source directions (e.g. kegg2hmdb): a KEGG compound id is not
    a queryable KG node, a DOCUMENTED gap, so a zero provided-path mapping is a genuine 0/n result and
    must be scored (all-misses), not refused as a broken run. The ``ProvidedIdDatasetConfig``
    ``__post_init__`` still re-enforces source-namespace != target-namespace.
    """
    assert sub.source_id_column is not None  # build_subgroups always sets it for id2id
    return ProvidedIdDatasetConfig(
        key=sub.key,
        arm=config.arm,
        entity_type=config.entity_type,
        source_id_column=sub.source_id_column,
        source_namespace=sub.source_namespace,
        name_column=config.name_column,
        gold_target_columns=((sub.target_namespace, config.gold_target_column),),
        target_vocabs=(sub.target_namespace,),
        source_label=f"MetaBench Grounding ({sub.key})",
        source_url=config.source_url,
        license=config.license,
        known_source_gap=(sub.source_namespace.upper() == "KEGG"),
    )


def load_metabench(source: Any, config: MetaBenchDatasetConfig = METABENCH) -> MetaBenchBundle:
    """Load MetaBench end to end. ``source`` is raw CSV bytes, a URL string, or a raw QA DataFrame.

    The SHA is computed over the fetched CSV bytes (URL/bytes); a DataFrame source (tests) records
    the SHA of its CSV serialization. The SHA is then GATED against ``config.expected_source_sha256``
    (``enforce_sha_pin``): a mismatch raises ``MetaBenchShaMismatchError`` before any parse/score, so
    a drifted upstream file can never be scored/reported as a valid same-set run.
    """
    if isinstance(source, pd.DataFrame):
        raw = source.to_csv(index=False).encode("utf-8")
    elif isinstance(source, str):
        raw = fetch_grounding(source)
    elif isinstance(source, (bytes, bytearray)):
        raw = bytes(source)
    else:
        raise TypeError(f"unsupported MetaBench source type: {type(source)!r}")
    sha = sha256_bytes(raw)
    enforce_sha_pin(sha, config)  # fail loud BEFORE parse/score if the file drifted from the pin
    long_df = parse_grounding(raw, config)
    subgroups = build_subgroups(long_df, config)
    card = build_card(long_df, source_sha=sha, config=config)
    return MetaBenchBundle(long_df=long_df, subgroups=subgroups, card=card)
