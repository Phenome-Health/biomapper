"""Unit 3 — comparable-core scorer: structure-oracle Top-1 accuracy.

Correctness is adjudicated by InChIKey *connectivity* (first block), never by identifier
identity. The gold block comes from the dataset's own curated InChIKey column (no resolver,
zero shared infra with the SUT — the circularity guard). Only BioMapper's *prediction*
(``chosen_kg_id``) is resolved through the KG structure oracle.

A prediction whose ChEBI id differs from gold but shares connectivity is CORRECT; a
prediction with different connectivity is INCORRECT. Rows whose gold has no InChIKey are
excluded from the accuracy denominator but still counted in coverage. Rows whose predicted
structure had to come from the name fallback (not the KG record) are flagged into a
segregation bucket so a reviewer can see how many corrects leaned on the fallback path.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

import pandas as pd

from biomapper.benchmarks.config import DatasetConfig

CHOSEN_COL = "chosen_kg_id"

# The wording each reported variant carries, defined once. The blended figures and the per-regime
# breakout both emit these; duplicating the strings is how two copies of "what this measures" end
# up disagreeing, which is the exact failure this metadata exists to prevent.
STRICT_KG_ONLY_DEFINITION = (
    "the chosen KG node's own InChIKey matched the held-out gold. A row whose structure came from "
    "the Metabolomics Workbench or PubChem name fallback is a MISS here, because the graph did not "
    "supply the structure."
)
NAME_FALLBACK_DEFINITION = (
    "structure taken from the chosen node's InChIKey when present, otherwise from a Metabolomics "
    "Workbench or PubChem lookup on the node's NAME. Report this as the name-fallback variant in a "
    "methods note, never as 'strict'."
)
CHARGE_NORMALIZED_DEFINITION = (
    "both sides neutralized for charge and protonation before comparing connectivity. Reported "
    "beside the strict figure, never in place of it."
)
KG_EQUIVALENCE_SET_DEFINITION = (
    "gold matches ANY InChIKey first-block the chosen node asserts, not only the first. Partly a "
    "measure of the graph's curation rather than of resolution alone, because a node asserting "
    "several connectivities satisfies this more easily."
)

# ------------------------------------------------------------------------------------------------
# Name-source regimes (LMSD lipid arm — two-regime split).
#
# The LMSD sample is ~90% lipid SHORTHAND (the ``ABBREVIATION`` field, e.g. ``TG 57:6``) — by far
# the hardest name->structure input class — vs the easier common/systematic names. A single blended
# accuracy averages two very different populations, so when the caller supplies a name-source column
# (the adapter's ``query_source``, recording which SDF field supplied each query) the scorer breaks
# the strict + charge-normalized Top-1 out PER REGIME, alongside the blended overall (continuity).
# The split is purely additive: absent the column, ``by_name_source_regime`` is None and nothing
# else changes (NECS/RefMet/SRM1950/Hajjar are unaffected).
# ------------------------------------------------------------------------------------------------
SHORTHAND_REGIME = "shorthand"
COMMON_SYSTEMATIC_REGIME = "common_systematic"


def name_source_regime(source: Any) -> str:
    """Map a per-row name-source tag to its regime label (shorthand vs common/systematic).

    The LMSD adapter records which SDF field supplied each query (``abbreviation`` / ``common_name``
    / ``systematic_name``). The lipid ``ABBREVIATION`` is the shorthand regime — the hard input
    class; the common and systematic names fold into one "common/systematic" regime. Any unexpected
    or blank tag lands in common/systematic (never silently dropped from the breakout).
    """
    return (
        SHORTHAND_REGIME
        if str(source).strip().lower() == "abbreviation"
        else COMMON_SYSTEMATIC_REGIME
    )


class StructureOracle(Protocol):
    """KG-first structure resolution for a predicted node id.

    ``kg_block`` returns the InChIKey first-block from the KG record alone (None if the KG
    carries no structure). ``resolved_block`` additionally consults the name fallback
    (MW -> PubChem). The gap between the two is the fallback-segregation signal.

    ``neutral_block`` (optional; probed via ``hasattr``) returns the *charge/protonation-
    normalized* InChIKey first-block of the prediction — the connectivity after neutralizing
    the structure. It powers the charge-normalized accuracy variant (Hajjar's dominant miss was
    protonation state).
    """

    def kg_block(self, node_id: str) -> str | None: ...
    def resolved_block(self, node_id: str) -> str | None: ...


def neutralize_first_block(smiles: Any) -> str | None:
    """Charge/protonation-normalized InChIKey first-block from a SMILES, or None.

    Neutralizes the molecule (RDKit ``Uncharger``) before hashing so charged/protonated forms
    (e.g. a carboxylate vs its acid, a zwitterion vs the neutral species, a source table using a
    fixed-H convention) collapse to one connectivity skeleton. Standard InChI already routes
    most protonation into the second block, so this only moves the residual cases where the
    recorded first block itself differs by charge state — but those were the dominant Hajjar
    miss, so the variant is reported alongside the strict number.
    """
    if smiles is None or (isinstance(smiles, float) and pd.isna(smiles)):
        return None
    s = str(smiles).strip()
    if not s or s.lower() == "nan":
        return None
    from rdkit import Chem, RDLogger
    from rdkit.Chem.MolStandardize import rdMolStandardize

    RDLogger.DisableLog("rdApp.*")  # type: ignore[attr-defined]
    mol = Chem.MolFromSmiles(s)
    if mol is None:
        return None
    mol = rdMolStandardize.Uncharger().uncharge(mol)
    key = Chem.MolToInchiKey(mol)
    return first_block(key)


def first_block(inchikey: Any) -> str | None:
    """First InChIKey block (the 2-D connectivity skeleton), or None if absent/blank."""
    if inchikey is None:
        return None
    if isinstance(inchikey, float):  # NaN
        return None
    s = str(inchikey).strip()
    if not s or s.lower() == "nan":
        return None
    return s.split("-")[0]


def _has_prediction(chosen: Any) -> bool:
    if chosen is None or (isinstance(chosen, float) and pd.isna(chosen)):
        return False
    s = str(chosen).strip()
    return bool(s) and s.lower() != "nan"


def score_structure_oracle(
    mapped_df: pd.DataFrame,
    config: DatasetConfig,
    oracle: StructureOracle,
    vocab: str | None = None,
    *,
    gold_smiles_normalizer: Callable[[Any], str | None] | None = None,
    name_source_column: str | None = None,
) -> dict[str, Any]:
    """Compute Top-1 accuracy (structure-oracle) + coverage + the fallback bucket.

    When a charge-normalization capability is available — a ``gold_smiles_normalizer`` for the
    gold side AND an ``oracle.neutral_block`` for the prediction side — a SECOND accuracy is
    reported under ``comparable_core_charge_normalized``: the same Top-1 accuracy but with both
    blocks neutralized for charge/protonation state. Both numbers are reported; neither replaces
    the other (protonation was the dominant Hajjar miss). When the capability is absent the
    charge-normalized core is ``None`` with a recorded reason.

    When ``name_source_column`` is given AND present in ``mapped_df`` (the LMSD adapter's
    ``query_source``), the strict + charge-normalized Top-1 are ALSO broken out per name-source
    regime — shorthand (lipid ``ABBREVIATION``, the hard class) vs common/systematic — under
    ``by_name_source_regime``, so the blended headline is not read as one homogeneous population.
    Absent the column the breakout is ``None`` and nothing else changes (purely additive).
    """
    smiles_col = config.gold_smiles_column
    cn_available = gold_smiles_normalizer is not None and hasattr(oracle, "neutral_block")
    # KG-equivalence-set variant: gold matches ANY first-block in the chosen node's multi-valued
    # INCHIKEY list, not just keys[0] (the Hajjar keys[0] artifact). Optional oracle capability.
    eq_set_available = hasattr(oracle, "resolved_blocks")
    regime_available = name_source_column is not None and name_source_column in mapped_df.columns

    total = len(mapped_df)
    n_predicted = 0
    scored = 0  # accuracy denominator: rows with gold structure
    correct = 0
    # The KG-record-only variant: a row counts only when the chosen node's OWN InChIKey matched.
    # A row whose structure had to come from the external name fallback is a miss here, because
    # under this definition the graph did not supply the structure. This is the published "strict"
    # figure (decided 2026-09-23); ``comparable_core`` is the same measurement WITH the fallback
    # allowed, and the two must never be conflated (Hajjar: 92 against 95).
    strict_kg_only_correct = 0
    fallback_rows: list[str] = []
    # Charge-normalized tallies (only meaningful when cn_available).
    cn_scored = 0
    cn_correct = 0
    # KG-equivalence-set tallies (only meaningful when eq_set_available); same denominator as strict.
    eq_scored = 0
    eq_correct = 0
    # Per-regime tallies (only populated when regime_available). regime -> counter dict.
    regime_tally: dict[str, dict[str, int]] = {}
    per_row: list[dict[str, Any]] = []

    for _, row in mapped_df.iterrows():
        gold_block = first_block(row.get(config.gold_inchikey_column))
        chosen = row.get(CHOSEN_COL)
        has_pred = _has_prediction(chosen)
        if has_pred:
            n_predicted += 1

        predicted_block: str | None = None
        needed_fallback = False
        chosen_id: str | None = None
        if has_pred:
            chosen_id = str(chosen).strip()
            kg_b = oracle.kg_block(chosen_id)
            predicted_block = oracle.resolved_block(chosen_id)
            needed_fallback = kg_b is None and predicted_block is not None
            if needed_fallback:
                fallback_rows.append(chosen_id)

        is_scored = gold_block is not None
        is_correct = bool(
            is_scored and predicted_block is not None and predicted_block == gold_block
        )
        if is_scored:
            scored += 1
            if is_correct:
                correct += 1
                if not needed_fallback:
                    strict_kg_only_correct += 1

        # KG-equivalence-set membership (same scored population as strict). A hit means gold's
        # first-block is one of the chosen node's KG-asserted InChIKey blocks — recovers the
        # neutral-parent-vs-anion/salt keys[0] artifact WITHOUT crossing node boundaries (a wrong
        # entity only matches if its own node asserts equivalence to gold, so no free inflation).
        eq_correct_row: bool | None = None
        if eq_set_available and is_scored:
            eq_scored += 1
            eq_blocks: set[str] = set()
            if chosen_id is not None:  # chosen_id is set iff has_pred
                eq_blocks = oracle.resolved_blocks(chosen_id)  # type: ignore[attr-defined]
            eq_correct_row = gold_block in eq_blocks
            if eq_correct_row:
                eq_correct += 1

        cn_correct_row: bool | None = None
        # Gate the charge-normalized tally on the SAME population as strict (``gold_block is not
        # None``), so both accuracies share one denominator and stay comparable. A row with a
        # parseable gold SMILES but no gold InChIKey must NOT inflate the normalized denominator
        # (coverage-only rule): it is not in the strict scored set, so it is not in the cn set.
        if cn_available and gold_block is not None:
            assert gold_smiles_normalizer is not None
            gold_smiles = row.get(smiles_col) if smiles_col else None
            gold_cn = gold_smiles_normalizer(gold_smiles)
            # Neutralize the gold connectivity; fall back to the strict gold block when the
            # source ships no SMILES to neutralize (can't neutralize a hash). Because gold_block
            # is not None here, gold_cn_block is always defined.
            gold_cn_block = gold_cn if gold_cn is not None else gold_block
            # ``neutral_block`` is an optional oracle capability, probed by ``hasattr`` above and
            # gated by ``cn_available``, so it is absent from the Protocol on purpose.
            pred_cn_block = (
                oracle.neutral_block(chosen_id)  # type: ignore[attr-defined]
                if (has_pred and chosen_id is not None)
                else None
            )
            cn_scored += 1
            cn_correct_row = bool(pred_cn_block is not None and pred_cn_block == gold_cn_block)
            if cn_correct_row:
                cn_correct += 1

        # Per-regime accumulation (shorthand vs common/systematic). Same gating as the blended
        # tallies so each regime's strict + charge-normalized denominators are internally consistent
        # and sum to the overall (a row is in exactly one regime).
        row_source: str | None = None
        if regime_available:
            row_source = str(row.get(name_source_column)).strip()
            t = regime_tally.setdefault(
                name_source_regime(row_source),
                {
                    "n_rows": 0,
                    "n_predicted": 0,
                    "scored": 0,
                    "correct": 0,
                    "strict": 0,
                    "cn_scored": 0,
                    "cn_correct": 0,
                },
            )
            t["n_rows"] += 1
            if has_pred:
                t["n_predicted"] += 1
            if is_scored:
                t["scored"] += 1
                if is_correct:
                    t["correct"] += 1
                    if not needed_fallback:
                        t["strict"] += 1
            if cn_correct_row is not None:  # row is in the charge-normalized scored set
                t["cn_scored"] += 1
                if cn_correct_row:
                    t["cn_correct"] += 1

        per_row.append(
            {
                "name": row.get(config.name_column),
                "chosen_kg_id": None if not has_pred else chosen_id,
                "gold_block": gold_block,
                "predicted_block": predicted_block,
                "scored": is_scored,
                "correct": is_correct,
                "needed_fallback": needed_fallback,
                "charge_normalized_correct": cn_correct_row,
                "kg_equivalence_set_correct": eq_correct_row,
                "name_source": row_source,
            }
        )

    accuracy = (correct / scored) if scored else None
    if cn_available:
        cn_core: dict[str, Any] | None = {
            "metric": "top1_accuracy_charge_normalized",
            "top1_accuracy": (cn_correct / cn_scored) if cn_scored else None,
            "correct": cn_correct,
            "scored_denominator": cn_scored,
            "definition": CHARGE_NORMALIZED_DEFINITION,
            "is_published_strict": False,
        }
    else:
        cn_core = None

    if eq_set_available:
        eq_core: dict[str, Any] | None = {
            "metric": "top1_accuracy_kg_equivalence_set",
            "top1_accuracy": (eq_correct / eq_scored) if eq_scored else None,
            "correct": eq_correct,
            "scored_denominator": eq_scored,
            "definition": KG_EQUIVALENCE_SET_DEFINITION,
            "is_published_strict": False,
        }
    else:
        eq_core = None

    # Assemble the per-regime breakout (None when no name-source column was supplied). Each regime
    # carries its own strict + charge-normalized core + coverage, mirroring the blended shape so a
    # report renders them uniformly.
    by_regime: dict[str, Any] | None = None
    if regime_available:
        by_regime = {}
        for regime, t in regime_tally.items():
            regime_cn: dict[str, Any] | None = None
            if cn_available:
                regime_cn = {
                    "metric": "top1_accuracy_charge_normalized",
                    "top1_accuracy": (t["cn_correct"] / t["cn_scored"]) if t["cn_scored"] else None,
                    "correct": t["cn_correct"],
                    "scored_denominator": t["cn_scored"],
                    "definition": CHARGE_NORMALIZED_DEFINITION,
                    "is_published_strict": False,
                }
            by_regime[regime] = {
                "comparable_core_strict_kg_only": {
                    "metric": "top1_accuracy_strict_kg_only",
                    "top1_accuracy": (t["strict"] / t["scored"]) if t["scored"] else None,
                    "correct": t["strict"],
                    "scored_denominator": t["scored"],
                    "definition": STRICT_KG_ONLY_DEFINITION,
                    "is_published_strict": True,
                },
                "comparable_core": {
                    "metric": "top1_accuracy_with_name_fallback",
                    "top1_accuracy": (t["correct"] / t["scored"]) if t["scored"] else None,
                    "correct": t["correct"],
                    "scored_denominator": t["scored"],
                    "definition": NAME_FALLBACK_DEFINITION,
                    "is_published_strict": False,
                },
                "comparable_core_charge_normalized": regime_cn,
                "n_rows": t["n_rows"],
                "coverage": {
                    "n_predicted": t["n_predicted"],
                    "total": t["n_rows"],
                    "fraction": (t["n_predicted"] / t["n_rows"]) if t["n_rows"] else 0.0,
                },
            }

    return {
        "vocab": vocab,
        "input_type": config.input_type,
        # THE PUBLISHED STRICT FIGURE. KG-record InChIKey only, no external name lookup permitted
        # to supply a structure. Reported first because a reader reaching for "the strict number"
        # must land on this one and not on ``comparable_core``.
        "comparable_core_strict_kg_only": {
            "metric": "top1_accuracy_strict_kg_only",
            "top1_accuracy": (strict_kg_only_correct / scored) if scored else None,
            "correct": strict_kg_only_correct,
            "scored_denominator": scored,
            "definition": STRICT_KG_ONLY_DEFINITION,
            "is_published_strict": True,
        },
        # The SAME measurement with the external name fallback allowed. Historically the headline,
        # which is how "strict" came to mean two different numbers between documents.
        "comparable_core": {
            "metric": "top1_accuracy_with_name_fallback",
            "top1_accuracy": accuracy,
            "correct": correct,
            "scored_denominator": scored,
            "definition": NAME_FALLBACK_DEFINITION,
            "is_published_strict": False,
        },
        "comparable_core_charge_normalized": cn_core,
        "comparable_core_kg_equivalence_set": eq_core,
        "by_name_source_regime": by_regime,
        "coverage": {
            "n_predicted": n_predicted,
            "total": total,
            "fraction": (n_predicted / total) if total else 0.0,
        },
        "fallback_bucket": {"count": len(fallback_rows), "rows": fallback_rows},
        "per_row": per_row,
    }
