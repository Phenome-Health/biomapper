"""MetaBench scorer — one uniform accuracy over the whole 1,000-pair Grounding set.

MetaBench mixes two run regimes (ID->ID provided-ID, name->ID name-input), but SCORING is
identical in both: the gold is the held-out TARGET database id and correctness is CURIE equality
between BioMapper's equivalence-set predictions (``predicted_curies`` = ``chosen_kg_id`` +
``kg_equivalent_ids``) and the gold. The per-row bare gold (a raw ``C00626`` / ``HMDB0010090`` /
ChEBI number) is prefixed to its per-row TARGET namespace via ``curie_scorer.split_gold_curies``
so it matches the prediction form — the same bare-vs-prefixed normalization the provided-ID scorer
uses, generalized to a per-row target namespace.

ONE number per dataset (no per-vocab axis; the Hajjar calibration). A per-target-namespace
breakdown is retained for traceability only, never the headline.

ANTI-TRIVIAL-100% GUARD (fail-loud, ``assert_metabench_held_out``): the gold TARGET column + its
namespace column must be present (held out, scorer-only) and, for any ID->ID row, the source
namespace must differ from the target namespace — a same-namespace round-trip would let the
provided source id self-match the gold. There is NO charge-normalized variant: the target is a
database identifier, not a structure.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from biomapper.benchmarks.config import MetaBenchDatasetConfig
from biomapper.benchmarks.scorers.curie_scorer import _f1, predicted_curies, split_gold_curies


class MetaBenchNotHeldOutError(ValueError):
    """Raised when the gold TARGET is not actually held out (trivial-100% trap)."""


def assert_metabench_held_out(mapped_df: pd.DataFrame, config: MetaBenchDatasetConfig) -> None:
    """Fail loud if the gold TARGET could self-match the source (anti-trivial-100%)."""
    for col in (config.gold_target_column, config.target_namespace_column):
        if col not in mapped_df.columns:
            raise MetaBenchNotHeldOutError(
                f"{config.key}: held-out scoring column {col!r} missing from the mapped frame "
                f"(columns: {list(mapped_df.columns)}) — cannot score."
            )
    src_ns_col = config.source_namespace_column
    if src_ns_col in mapped_df.columns:
        for _, row in mapped_df.iterrows():
            src = str(row.get(src_ns_col, "")).strip().upper()
            tgt = str(row.get(config.target_namespace_column, "")).strip().upper()
            if src and src == tgt:
                raise MetaBenchNotHeldOutError(
                    f"{config.key}: an ID->ID row has source namespace == target namespace ({src!r}); "
                    f"a same-namespace round-trip self-matches the gold. Refusing to score a trivial 100%."
                )


def gold_target_curies(row: pd.Series, config: MetaBenchDatasetConfig) -> set[str]:
    """The held-out TARGET id, prefixed to its per-row target namespace (bare-vs-prefixed normalize)."""
    namespace = str(row.get(config.target_namespace_column, "")).strip()
    return split_gold_curies(row.get(config.gold_target_column), namespace)


def score_metabench(mapped_df: pd.DataFrame, config: MetaBenchDatasetConfig) -> dict[str, Any]:
    """Score the concatenated MetaBench mapper output. ONE Top-1 accuracy + coverage/precision/recall/F1.

    - scored denominator = rows carrying a held-out gold TARGET (accuracy/recall base).
    - correct = the row's predicted equivalence-set CURIEs intersect its gold TARGET CURIE.
    - coverage = rows with >=1 predicted CURIE / total.
    - precision = correct / (rows with BOTH a prediction and a gold).
    - recall = correct / scored.

    ``top1_accuracy`` is ``None`` iff nothing was scorable (empty gold) — the caller fails loud on
    that (an unscorable run must never be filed as success).
    """
    assert_metabench_held_out(mapped_df, config)  # fail-loud anti-trivial-100% guard

    total = len(mapped_df)
    n_predicted = 0
    scored = 0
    both = 0
    correct = 0
    per_namespace: dict[str, dict[str, int]] = {}
    per_row: list[dict[str, Any]] = []

    for _, row in mapped_df.iterrows():
        preds = predicted_curies(row)
        golds = gold_target_curies(row, config)
        target_ns = str(row.get(config.target_namespace_column, "")).strip().upper() or "UNKNOWN"
        per_namespace.setdefault(target_ns, {"correct": 0, "scored": 0})
        has_pred = bool(preds)
        has_gold = bool(golds)
        if has_pred:
            n_predicted += 1
        if has_gold:
            scored += 1
            per_namespace[target_ns]["scored"] += 1
        row_correct = bool(preds & golds)
        if has_pred and has_gold:
            both += 1
            if row_correct:
                correct += 1
        if has_gold and row_correct:
            per_namespace[target_ns]["correct"] += 1
        per_row.append(
            {
                "query": row.get(config.name_column) or row.get(config.source_id_column),
                "pair_type": row.get(config.pair_type_column),
                "target_namespace": target_ns,
                "predicted": sorted(preds),
                "gold": sorted(golds),
                "scored": has_gold,
                "correct": has_gold and row_correct,
            }
        )

    top1 = (correct / scored) if scored else None
    precision = (correct / both) if both else None
    recall = (correct / scored) if scored else None
    return {
        "dataset": config.key,
        "arm": config.arm,
        "input_type": config.input_type,
        "mode": "metabench_grounding",
        "comparable_core": {
            "metric": "top1_accuracy",
            "top1_accuracy": top1,
            "correct": correct,
            "scored_denominator": scored,
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
