"""Gene/protein arm scorer — CURIE-equality Top-1 accuracy + coverage/precision/recall/F1.

There is no structure oracle for genes/proteins; correctness is CURIE equality between
BioMapper's *assigned* cross-reference CURIEs and the backbone's authoritative held-out
cross-refs. This mirrors the mapper's own ``analysis.py`` "assigned-vs-provided" semantics
(``_calculate_precision/_recall/_f1``), applied to the held-out gold instead of a provided id.

``chosen_kg_id`` IS vocab-steered: the same query can resolve to a different node in each target
vocab's run. A multi-run arm must therefore score each namespace from its own run
(``score_curie_per_target_run``); ``score_curie`` scores a single run. BioMapper's predicted
CURIEs are drawn from ``chosen_kg_id`` plus its ``kg_equivalent_ids`` (any namespace); the gold restricts the comparison to the target
namespaces, so the source-namespace query id can never trivially self-match.
"""

from __future__ import annotations

import ast
import re
from typing import Any

import pandas as pd

from biomapper.benchmarks.config import CurieDatasetConfig

CHOSEN_COL = "chosen_kg_id"
EQUIV_COL = "kg_equivalent_ids"
CURIE_DELIM = "|"

# Namespace-prefix synonyms that denote the SAME identifier space, canonicalized to one form so
# equal entities compare equal regardless of which prefix a source emitted. The metabolite KG /
# equivalence expansion writes the Biolink-style database-section prefixes (``KEGG.COMPOUND``,
# ``PUBCHEM.COMPOUND``) while the benchmark golds ship the bare database prefix (``KEGG``,
# ``PUBCHEM``); without this, gold ``KEGG:C00626`` never matches predicted ``KEGG.COMPOUND:C00626``
# and every KEGG-target row scores 0 (the live-run 24.3% vs 54.5% gap). Keys/values are the
# UPPERCASED prefix (matched after the prefix is upper-cased). Generic across namespaces — no
# per-row special-casing; only the compound identifier space is aliased (KEGG.GLYCAN / KEGG.DRUG
# are DELIBERATELY not folded in, they are different id spaces).
_NAMESPACE_ALIASES: dict[str, str] = {
    "KEGG.COMPOUND": "KEGG",
    "PUBCHEM.COMPOUND": "PUBCHEM",
}


def canonical_prefix(prefix: str) -> str:
    """Map a (already stripped/upper-cased) namespace prefix to its canonical synonym."""
    return _NAMESPACE_ALIASES.get(prefix, prefix)


def normalize_curie(curie: Any) -> str | None:
    """Canonicalize a CURIE for equality: strip, canonicalize+uppercase the prefix, keep the local part.

    Gene/protein identifiers (Ensembl/UniProt/Entrez/RefSeq) are conventionally case-stable in
    the local part but the *prefix* casing varies across sources (``Ensembl`` vs ``ENSEMBL``),
    so only the prefix is uppercased. Prefix SYNONYMS for one identifier space are folded to a
    canonical form via ``_NAMESPACE_ALIASES`` (e.g. ``KEGG.COMPOUND`` -> ``KEGG``) so a bare-vs-
    database-section prefix mismatch cannot under-count equal entities. Returns None for blank/NaN.
    """
    if curie is None or (isinstance(curie, float) and pd.isna(curie)):
        return None
    s = str(curie).strip()
    if not s or s.lower() == "nan":
        return None
    if ":" in s:
        prefix, local = s.split(":", 1)
        return f"{canonical_prefix(prefix.strip().upper())}:{local.strip()}"
    return canonical_prefix(s.upper())


def _split_curies(value: Any) -> set[str]:
    """Split a ``|``-delimited gold CURIE cell into a normalized set."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return set()
    out: set[str] = set()
    for part in str(value).split(CURIE_DELIM):
        n = normalize_curie(part)
        if n is not None:
            out.add(n)
    return out


def split_gold_curies(value: Any, namespace: str) -> set[str]:
    """Split a ``|``-delimited gold cell, prefixing BARE values with their DECLARED namespace.

    Golds are stored two ways depending on the source: CURIE-prefixed (e.g. ``NCBIGene:1234``,
    the gene/protein backbones) or BARE (e.g. an InChIKey ``KDXKERNSBIXSRK-YFKPBYRVSA-N`` with no
    ``INCHIKEY:`` prefix, the Hajjar structure anchor). ``predicted_curies`` always emits the
    prefixed form (``prefix:local``), so a bare gold would never intersect and the dataset would
    under-report as 0%. This prefixes any bare value with its declared target ``namespace`` before
    normalization so it matches the prediction form; an already-prefixed value keeps its own prefix
    (untouched). Generic across namespaces — no per-vocab special-casing.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return set()
    out: set[str] = set()
    for part in str(value).split(CURIE_DELIM):
        raw = part.strip()
        if not raw or raw.lower() == "nan":
            continue
        curie = raw if ":" in raw else f"{namespace}:{raw}"
        n = normalize_curie(curie)
        if n is not None:
            out.add(n)
    return out


# MetaboLights MAF ``database_identifier`` golds are NOT uniformly CURIE-prefixed. They are a mix of:
# already-prefixed CURIEs (``CHEBI:17234``), bare chemical accessions (HMDB / KEGG C-number / PubChem
# CID / InChIKey), and NON-chemical tokens (spectral feature labels ``M###T###``, ``--``/empty
# placeholders). ``split_gold_curies`` prefixes every bare value with ONE declared namespace, which
# both mislabels bare HMDB as ``CHEBI:HMDB…`` and pads the id-concordance denominator with rows that
# can never concord. ``namespace_bare_gold`` assigns each bare value its correct namespace by pattern
# and DROPS non-chemical tokens so they never enter the scored set.
_BARE_GOLD_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^HMDB\d+$", re.IGNORECASE), "HMDB"),
    (re.compile(r"^C\d{5}$"), "KEGG.COMPOUND"),
    (re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$"), "INCHIKEY"),
    (re.compile(r"^\d+$"), "PUBCHEM.COMPOUND"),
]
# Non-chemical tokens to drop outright (MetaboLights feature labels + placeholders).
_NON_CHEMICAL_GOLD = re.compile(r"^(M\d+T\d+.*|--)$", re.IGNORECASE)


def namespace_bare_gold(value: Any) -> set[str]:
    """Normalize a MAF ``database_identifier`` gold cell for id-concordance.

    Per ``|``-delimited part: an already-prefixed CURIE keeps its own namespace; a bare chemical
    accession is matched to its namespace (HMDB / KEGG.COMPOUND / PUBCHEM.COMPOUND / INCHIKEY); a
    non-chemical token (feature label, placeholder, unrecognized) is dropped. Returns the set of
    normalized CURIEs (empty if the cell holds no scorable chemical id).
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return set()
    out: set[str] = set()
    for part in str(value).split(CURIE_DELIM):
        raw = part.strip()
        if not raw or raw.lower() == "nan" or _NON_CHEMICAL_GOLD.match(raw):
            continue
        if ":" in raw:
            curie = raw
        else:
            ns = next((n for pat, n in _BARE_GOLD_PATTERNS if pat.match(raw)), None)
            if ns is None:
                continue
            curie = f"{ns}:{raw}"
        normalized = normalize_curie(curie)
        if normalized is not None:
            out.add(normalized)
    return out


def _parse_equiv(value: Any) -> dict[str, Any]:
    """Parse the ``kg_equivalent_ids`` cell (a dict, a dict-repr string from a TSV, or NaN)."""
    if isinstance(value, dict):
        return value
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return {}
    s = str(value).strip()
    if not s or s.lower() == "nan":
        return {}
    try:
        parsed = ast.literal_eval(s)
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, SyntaxError):
        return {}


def predicted_curies(row: pd.Series) -> set[str]:
    """All CURIEs BioMapper assigned for a row: ``chosen_kg_id`` + every ``kg_equivalent_ids``.

    ``kg_equivalent_ids`` is ``{prefix: [local_id, ...]}`` with the prefix STRIPPED from each
    value (biomapper2 ``Linker.get_equivalent_ids``), so each cross-ref CURIE is reconstructed as
    ``prefix:local_id``. A value that already carries a prefix (defensive) is taken as-is. The gold
    set — restricted to the target namespaces — does the filtering at intersection time.
    """
    out: set[str] = set()
    chosen = normalize_curie(row.get(CHOSEN_COL))
    if chosen is not None:
        out.add(chosen)
    for namespace, ids in _parse_equiv(row.get(EQUIV_COL)).items():
        values = ids if isinstance(ids, (list, tuple, set)) else [ids]
        for v in values:
            raw = str(v).strip()
            if not raw:
                continue
            curie = raw if ":" in raw else f"{namespace}:{raw}"
            n = normalize_curie(curie)
            if n is not None:
                out.add(n)
    return out


def gold_curies(row: pd.Series, config: CurieDatasetConfig) -> set[str]:
    """Union of the held-out authoritative cross-ref CURIEs across the target namespaces."""
    out: set[str] = set()
    for _namespace, column in config.gold_curie_columns:
        out |= _split_curies(row.get(column))
    return out


def _f1(precision: float | None, recall: float | None) -> float | None:
    if precision is None or recall is None or (precision + recall) == 0:
        return 0.0 if (precision is not None and recall is not None) else None
    return 2 * precision * recall / (precision + recall)


def score_curie(mapped_df: pd.DataFrame, config: CurieDatasetConfig, vocab: str | None = None) -> dict[str, Any]:
    """CURIE-equality scoring. One headline accuracy per dataset + coverage/precision/recall/F1.

    - scored denominator = rows carrying ≥1 gold cross-ref (the accuracy/recall base).
    - correct = the row's predicted CURIE set intersects its gold CURIE set.
    - coverage = rows with ≥1 predicted CURIE / total.
    - precision = correct / (rows with BOTH a prediction and a gold) — assigned-vs-provided.
    - recall = correct / scored.
    """
    total = len(mapped_df)
    n_predicted = 0
    scored = 0
    both = 0  # rows with a prediction AND a gold (precision denominator)
    correct = 0
    per_namespace: dict[str, dict[str, int]] = {ns: {"correct": 0, "scored": 0} for ns, _ in config.gold_curie_columns}
    per_row: list[dict[str, Any]] = []

    for _, row in mapped_df.iterrows():
        preds = predicted_curies(row)
        golds = gold_curies(row, config)
        has_pred = bool(preds)
        has_gold = bool(golds)
        if has_pred:
            n_predicted += 1
        if has_gold:
            scored += 1
        row_correct = bool(preds & golds)
        if has_pred and has_gold:
            both += 1
            if row_correct:
                correct += 1
        # Per-namespace breakdown (traceability only; never the headline).
        for namespace, column in config.gold_curie_columns:
            ns_gold = _split_curies(row.get(column))
            if ns_gold:
                per_namespace[namespace]["scored"] += 1
                if preds & ns_gold:
                    per_namespace[namespace]["correct"] += 1
        per_row.append(
            {
                "query": row.get(config.name_column),
                "predicted": sorted(preds),
                "gold": sorted(golds),
                "scored": has_gold,
                "correct": has_gold and row_correct,
            }
        )

    top1 = (correct / scored) if scored else None
    precision = (correct / both) if both else None
    recall = (correct / scored) if scored else None

    # Per-namespace accuracy is the REPORTABLE number for a gene/protein arm, not the
    # any-namespace roll-up. The namespaces perform very differently (the 2026-08-05 run read
    # Ensembl 79.1% / UniProtKB 92.4% / NCBI Gene 97.7% against a 96.4% roll-up), so a single
    # blended figure both flatters the weak namespace and has been quoted as if it described all
    # three. The roll-up is retained for continuity and explicitly flagged non-quotable rather
    # than dropped, because removing it would silently change the meaning of an existing field.
    per_namespace_accuracy = {
        namespace: {
            "metric": "top1_accuracy",
            "top1_accuracy": (t["correct"] / t["scored"]) if t["scored"] else None,
            "correct": t["correct"],
            "scored_denominator": t["scored"],
        }
        for namespace, t in per_namespace.items()
    }

    return {
        "vocab": vocab,
        "arm": config.arm,
        "input_type": config.input_type,
        "reportable_metric": "per_namespace_accuracy",
        "per_namespace_accuracy": per_namespace_accuracy,
        "comparable_core": {
            "metric": "top1_accuracy_any_namespace",
            "top1_accuracy": top1,
            "correct": correct,
            "scored_denominator": scored,
            "quotable": False,
            "quotable_note": (
                "any-namespace roll-up: a row counts as correct if ANY target namespace matched. "
                "Report per_namespace_accuracy instead. Do not quote this as the arm's accuracy."
            ),
        },
        "coverage": {"n_predicted": n_predicted, "total": total, "fraction": (n_predicted / total) if total else 0.0},
        "curie_stats": {
            "precision": precision,
            "recall": recall,
            "f1": _f1(precision, recall),
            "predicted_and_gold": both,
        },
        "per_namespace": per_namespace,
        "per_row": per_row,
    }


class MissingTargetRunError(ValueError):
    """A target namespace has no mapped frame of its own, so it cannot be scored honestly."""


def _namespace_prefix(namespace: str) -> str:
    """The canonical upper-cased prefix a predicted CURIE in ``namespace`` carries."""
    return canonical_prefix(namespace.strip().upper())


def score_curie_per_target_run(
    mapped_by_vocab: dict[str, pd.DataFrame],
    config: CurieDatasetConfig,
    sources: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Per-namespace accuracy where each namespace is scored from ITS OWN target-vocab run.

    The mapper is run once per target vocab, and node selection is conditioned on that vocab: the
    same symbol can resolve to a different node in the Ensembl pass than in the NCBIGene pass.
    Reading every namespace's cross-refs off one pass therefore measures that pass, not the
    requested mapping (the 2026-09 HGNC "NCBIGene fell" inversion). Here namespace ``ns`` is
    scored only against ``mapped_by_vocab[ns]``, and each figure records which run it came from.

    Also emits a FORCED-HIT diagnostic per namespace (diagnostic only, never a headline): rows
    whose gold carries no id in the target namespace but where the run returned one anyway, and,
    where another gold namespace makes it checkable, how many of those resolved to the wrong gene.
    The accuracy denominator excludes such rows by construction, so without this count a wrong
    answer on them is invisible.

    ``sources`` optionally maps each namespace to provenance for its run (e.g. output TSV path
    and SHA-256), copied verbatim into that namespace's entry.
    """
    sources = sources or {}
    missing = [ns for ns, _ in config.gold_curie_columns if ns not in mapped_by_vocab]
    if missing:
        raise MissingTargetRunError(
            f"{config.key}: no mapped run for target namespace(s) {missing}. Each namespace must be "
            f"scored from its own run; refusing to borrow another namespace's resolutions."
        )

    per_namespace_accuracy: dict[str, dict[str, Any]] = {}
    forced_hits: dict[str, dict[str, Any]] = {}
    for namespace, column in config.gold_curie_columns:
        frame = mapped_by_vocab[namespace]
        prefix = _namespace_prefix(namespace)
        other_columns = [c for ns, c in config.gold_curie_columns if ns != namespace]
        scored = correct = n_predicted = 0
        no_gold = forced = checkable = wrong_gene = 0
        for _, row in frame.iterrows():
            preds = predicted_curies(row)
            n_predicted += bool(preds)
            ns_gold = _split_curies(row.get(column))
            if ns_gold:
                scored += 1
                correct += bool(preds & ns_gold)
                continue
            no_gold += 1
            if not any(p.split(":", 1)[0] == prefix for p in preds):
                continue
            forced += 1
            other_gold: set[str] = set()
            for c in other_columns:
                other_gold |= _split_curies(row.get(c))
            if other_gold:
                checkable += 1
                wrong_gene += not (preds & other_gold)
        per_namespace_accuracy[namespace] = {
            "metric": "top1_accuracy",
            "top1_accuracy": (correct / scored) if scored else None,
            "correct": correct,
            "scored_denominator": scored,
            "n_rows": len(frame),
            # This run's own coverage, so it is never read off another namespace's run.
            "coverage": {
                "n_predicted": n_predicted,
                "total": len(frame),
                "fraction": (n_predicted / len(frame)) if len(frame) else 0.0,
            },
            "source_vocab_run": namespace,
            **sources.get(namespace, {}),
        }
        forced_hits[namespace] = {
            "rows_without_target_gold": no_gold,
            "returned_target_id_anyway": forced,
            "checkable_against_other_gold": checkable,
            "wrong_gene_where_checkable": wrong_gene,
            "source_vocab_run": namespace,
        }

    return {
        "reportable_metric": "per_namespace_accuracy",
        "per_namespace_accuracy": per_namespace_accuracy,
        "per_namespace": {
            ns: {"correct": v["correct"], "scored": v["scored_denominator"]}
            for ns, v in per_namespace_accuracy.items()
        },
        "per_namespace_scoring": "own_target_run",
        "forced_hit_diagnostic": {
            "diagnostic_only": True,
            "note": (
                "rows whose gold has no id in the target namespace but the run returned one; "
                "'wrong_gene_where_checkable' uses the row's gold in the other namespaces. Not an "
                "accuracy figure and not in any denominator."
            ),
            "per_namespace": forced_hits,
        },
    }
