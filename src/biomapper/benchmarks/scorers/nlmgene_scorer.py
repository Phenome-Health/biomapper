"""NLM-Gene ambiguous-partition scorer — EITL flag-rate + silent-over-commit + membership.

The UNAMBIGUOUS partition is scored for ACCURACY by ``curie_scorer.score_curie`` (identical to HGNC).
This scorer handles ONLY the AMBIGUOUS partition, where a bare context-stripped surface form denotes
>=2 genes and there is NO single correct answer: the correct behavior is to ABSTAIN / route to EITL,
not to silently emit one confident id. Metrics (never blended into the accuracy number):

  - ``flag_rate`` (comparable_core) = flagged / n_ambiguous, where FLAGGED = the mapper returned NO
    ``chosen_kg_id``. An empty commit is the only ambiguity signal the current mapper surface exposes;
    a first-class EITL flag field is owned by the arbitration workstream, so until it exists
    flag == abstain — stated honestly on the card and in the report.
  - ``silent_over_commit_rate`` = committed-but-WRONG / n_ambiguous — the dangerous case: a confident
    single id that is not even a legitimate referent of the ambiguous form.
  - ``member_when_committed`` = of committed forms, the fraction whose predicted CURIE set intersects
    the gold referent set (landed on a REAL referent — the hallucination guard).

Predicted/gold CURIE extraction is REUSED from ``curie_scorer`` so equality semantics match the
accuracy path exactly.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from biomapper.benchmarks.config import CurieDatasetConfig
from biomapper.benchmarks.scorers.curie_scorer import (
    CHOSEN_COL,
    gold_curies,
    normalize_curie,
    predicted_curies,
)


class UnscorableRunError(RuntimeError):
    """Raised when the ambiguous partition is empty — never report a hollow flag-rate."""


def score_nlmgene_ambiguity(
    mapped_df: pd.DataFrame, config: CurieDatasetConfig, *, vocab: str | None = None
) -> dict[str, Any]:
    """Score the ambiguous partition on flag-rate / silent-over-commit / membership."""
    n = len(mapped_df)
    if n == 0:
        raise UnscorableRunError("ambiguous partition is empty; nothing to score")

    flagged = committed = committed_member = committed_wrong = 0
    per_row: list[dict[str, Any]] = []
    for _, row in mapped_df.iterrows():
        golds = gold_curies(row, config)
        preds = predicted_curies(row)
        chosen = normalize_curie(row.get(CHOSEN_COL))
        if chosen is None:
            flagged += 1
            is_flagged, is_member = True, False
        else:
            committed += 1
            is_flagged = False
            is_member = bool(preds & golds)
            if is_member:
                committed_member += 1
            else:
                committed_wrong += 1
        per_row.append(
            {
                "query": row.get(config.name_column),
                "chosen": chosen,
                "predicted": sorted(preds),
                "gold": sorted(golds),
                "flagged": is_flagged,
                "member": is_member,
            }
        )

    return {
        "vocab": vocab,
        "arm": config.arm,
        "input_type": config.input_type,
        "partition": "ambiguous",
        "comparable_core": {
            "metric": "flag_rate",
            "flag_rate": flagged / n,
            "flagged": flagged,
            "n_ambiguous": n,
        },
        "silent_over_commit_rate": committed_wrong / n,
        "member_when_committed": (committed_member / committed) if committed else None,
        "committed": committed,
        "per_row": per_row,
    }
