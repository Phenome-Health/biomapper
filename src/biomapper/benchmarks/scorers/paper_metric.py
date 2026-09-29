"""Unit 3 — Hajjar native match-rate.

Reported *alongside* the comparable core, never merged with it. Hajjar's native metric is
a per-input match-rate: the fraction of input names for which a target-vocab identifier was
produced. This is a coverage-shaped number and is labeled with input_type=name so it is
never mistaken for the structure-oracle accuracy.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from biomapper.benchmarks.config import DatasetConfig
from biomapper.benchmarks.scorers.structure_oracle_scorer import CHOSEN_COL, _has_prediction


def score_paper_metric(mapped_df: pd.DataFrame, config: DatasetConfig, vocab: str | None = None) -> dict[str, Any]:
    total = len(mapped_df)
    matched = int(sum(_has_prediction(c) for c in mapped_df.get(CHOSEN_COL, pd.Series([], dtype=object))))
    return {
        "vocab": vocab,
        "input_type": config.input_type,
        "metric": "match_rate",
        "match_rate": (matched / total) if total else None,
        "matched": matched,
        "total": total,
    }
