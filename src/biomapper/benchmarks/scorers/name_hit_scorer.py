"""Name-hit-rate scorer (MetaboliteAnnotator regime) — the same-set NAME-input head-to-head.

The comparable core is a per-input NAME-HIT-RATE: the fraction of input names for which BioMapper
produced an identifier in ANY of the target vocabs. This is MetaboliteAnnotator's own metric, computed
identically so BioMapper's number lands directly beside the published 93.2% (pos) / 93.5% (neg) and
the MetaboAnalyst 6.0 / metaboliteIDmapping baselines. It is a coverage-shaped number (labeled
``input_type=name``) and is NEVER merged with the correctness qualifiers below.

HIT DEFINITION (union across target vocabs — the one number is NOT per-vocab): a name is a HIT when
its resolved node exposes an identifier in ANY of ``config.target_vocabs`` (CHEBI/HMDB/PubChem/KEGG),
read from BioMapper's ``chosen_kg_id`` PLUS its cross-namespace ``kg_equivalent_ids``. Requiring the
CHEBI-specific id alone would score a name that maps only to HMDB/PubChem/KEGG a miss and under-count
the hit rate. The live orchestrator additionally unions the per-vocab runs (``merge_vocab_runs``) so a
hit caught in any run counts — still exactly ONE name-hit-rate, no per-vocab axis.

Discipline (Hajjar/NECS learnings):
  - ONE number per dataset — one ``name_hit_rate`` per mode config (per-accession is traceability).
  - ANTI-TRIVIAL: the hit is adjudicated on BioMapper's OUTPUT (``chosen_kg_id`` + equivalents), never
    on the held-out gold column. A name with a gold id but no produced target-vocab id is a MISS. (The
    runner's ``assigned>0`` guard separately enforces the name path so the gold can't leak as an id.)
  - FAIL-LOUD on unscorable: zero input names raises rather than reporting a hollow ``None``.
  - ID-CONCORDANCE qualifier: of the names we hit that also carry a gold ``database_identifier``, how
    many hit the RIGHT id — reusing ``curie_scorer.namespace_bare_gold`` for the ``|``-multi gold cell,
    which namespaces bare chemical accessions correctly and drops non-chemical tokens (feature labels,
    placeholders) from the denominator rather than mis-prefixing them as ChEBI.
  - CHARGE-NORMALIZED STRUCTURE qualifier (optional, live): when an oracle exposing ``neutral_block``
    is supplied, a protonation-neutralized structure concordance over the hit-and-gold-SMILES subset —
    the dominant-miss variant, reusing ``structure_oracle_scorer.neutralize_first_block``.
"""

from __future__ import annotations

from typing import Any, Protocol

import pandas as pd

from biomapper.benchmarks.config import NameHitDatasetConfig
from biomapper.benchmarks.scorers.curie_scorer import (
    EQUIV_COL,
    namespace_bare_gold,
    predicted_curies,
)
from biomapper.benchmarks.scorers.id_equivalence import (
    IdEquivalenceJudge,  # noqa: TC001 (Protocol used in annotation)
)
from biomapper.benchmarks.scorers.structure_oracle_scorer import (
    CHOSEN_COL,
    _has_prediction,
    neutralize_first_block,
)

# Passthrough columns produced by the adapter (kept optional so a bare df still scores). Values must
# match the adapter's constants of the same name.
SOURCE_ACCESSION_COL = "source_accession"
INPUT_ROW_ID_COL = "input_row_id"


class UnscorableRunError(RuntimeError):
    """Raised when there is nothing to score (zero input names) — never report a hollow rate."""


def _norm(value: Any) -> str:
    return "" if value is None else str(value).strip()


def resolves_to_target_vocab(row: pd.Series, target_vocabs: tuple[str, ...]) -> bool:
    """True iff the row resolved to an identifier in ANY target vocab (the hit definition).

    Reads BioMapper's predicted CURIEs (``chosen_kg_id`` + cross-namespace ``kg_equivalent_ids``)
    and tests whether any of their namespaces is a target vocab — so a name mapping only to
    HMDB/PubChem/KEGG (not CHEBI) is a hit. Reads predictions only, never the held-out gold.
    """
    targets = {v.strip().upper() for v in target_vocabs}
    for curie in predicted_curies(row):
        prefix = curie.split(":", 1)[0].upper() if ":" in curie else curie.upper()
        if prefix in targets:
            return True
    return False


def _row_identity(row: pd.Series, config: NameHitDatasetConfig) -> tuple:
    """The stable identity of ONE input row, used to union its per-vocab passes.

    Prefers the adapter's ``input_row_id`` (accession-scoped, unique even for duplicate names). Falls
    back to ``(name, source_accession)`` for bare/legacy frames without the id — so two inputs sharing
    a metabolite name (across studies, or within one) are NEVER collapsed into a single scored row.
    """
    rid = _norm(row.get(INPUT_ROW_ID_COL))
    if rid:
        return ("id", rid)
    return ("name_acc", _norm(row.get(config.name_column)), _norm(row.get(SOURCE_ACCESSION_COL)))


def merge_vocab_runs(mapped_dfs: list[pd.DataFrame], config: NameHitDatasetConfig) -> pd.DataFrame:
    """Union the per-vocab mapper passes of the SAME input row into one row (no per-vocab axis).

    ``run_all`` runs one mapper pass per target vocab; a given input row may resolve in the HMDB pass
    but not the CHEBI pass. Keyed by per-input-row IDENTITY (``_row_identity``), this folds every
    pass's predictions (``chosen_kg_id`` + ``kg_equivalent_ids``) for that row into a single row so
    ``score_name_hit`` sees the UNION of namespaces — a hit caught in any pass counts, exactly once.
    Distinct input rows (same name, different accession, or duplicate names) stay separate, preserving
    the per-input denominator and per-accession totals. Gold columns are carried through.
    """
    if not mapped_dfs:
        raise ValueError("merge_vocab_runs: no vocab runs to merge")
    name_col = config.name_column
    agg: dict[tuple, dict[str, Any]] = {}
    order: list[tuple] = []
    for df in mapped_dfs:
        for _, row in df.iterrows():
            key = _row_identity(row, config)
            rec = agg.get(key)
            if rec is None:
                rec = {
                    name_col: _norm(row.get(name_col)),
                    CHOSEN_COL: "",
                    "_ns": {},  # namespace -> set of local ids seen across passes
                    config.gold_id_column: _norm(row.get(config.gold_id_column)),
                    SOURCE_ACCESSION_COL: _norm(row.get(SOURCE_ACCESSION_COL)),
                    INPUT_ROW_ID_COL: _norm(row.get(INPUT_ROW_ID_COL)),
                }
                if config.gold_smiles_column:
                    rec[config.gold_smiles_column] = _norm(row.get(config.gold_smiles_column))
                agg[key] = rec
                order.append(key)
            chosen = row.get(CHOSEN_COL)
            if _has_prediction(chosen) and not rec[CHOSEN_COL]:
                rec[CHOSEN_COL] = str(chosen).strip()  # representative node (first non-empty pass)
            for curie in predicted_curies(row):
                ns, _, local = curie.partition(":")
                rec["_ns"].setdefault(ns, set()).add(local or ns)
            if not rec[config.gold_id_column]:
                rec[config.gold_id_column] = _norm(row.get(config.gold_id_column))
            if config.gold_smiles_column and not rec.get(config.gold_smiles_column):
                rec[config.gold_smiles_column] = _norm(row.get(config.gold_smiles_column))
    rows: list[dict[str, Any]] = []
    for key in order:
        rec = agg[key]
        rec[EQUIV_COL] = {ns: sorted(locals_) for ns, locals_ in rec.pop("_ns").items()}
        rows.append(rec)
    return pd.DataFrame(rows)


class NeutralBlockOracle(Protocol):
    """Minimal live-oracle surface for the charge-normalized structure qualifier."""

    def neutral_block(self, node_id: str) -> str | None: ...


def score_name_hit(
    mapped_df: pd.DataFrame,
    config: NameHitDatasetConfig,
    vocab: str | None = None,
    *,
    oracle: NeutralBlockOracle | None = None,
    id_equivalence_judge: IdEquivalenceJudge | None = None,
) -> dict[str, Any]:
    """Name-hit-rate + ID-concordance + optional charge-normalized structure concordance."""
    total = len(mapped_df)
    if total == 0:
        raise UnscorableRunError(
            f"{config.key}: zero input names — nothing to score. Refusing to report a hollow "
            f"name-hit-rate for a run that measured nothing."
        )

    cn_available = oracle is not None and hasattr(oracle, "neutral_block")

    matched = 0
    id_scored = 0  # names that both hit and carry a gold id (concordance denominator)
    id_concordant = 0
    id_excluded_nonchemical = 0  # hit rows whose gold cell held only non-chemical tokens (feature ids / placeholders)
    cn_scored = 0
    cn_concordant = 0
    # Independent ID-equivalence-set tallies (only meaningful when a judge is injected). Same
    # scored population as strict id_concordance (has_hit and gold_ids) — reported BESIDE it.
    eq_available = id_equivalence_judge is not None
    uci_concordant = 0
    uci_needs = 0
    bridge_concordant = 0
    bridge_needs = 0
    ns_confusion: dict[str, dict[str, int]] = {}
    per_accession: dict[str, dict[str, int]] = {}
    per_row: list[dict[str, Any]] = []

    for _, row in mapped_df.iterrows():
        chosen = row.get(CHOSEN_COL)
        # HIT = resolved to an id in ANY target vocab (CHEBI/HMDB/PubChem/KEGG), read from the
        # prediction (chosen + equivalents), never the gold — so an HMDB/PubChem/KEGG-only name counts.
        has_hit = resolves_to_target_vocab(row, config.target_vocabs)
        if has_hit:
            matched += 1

        # The MAF ``database_identifier`` gold is a mix of already-prefixed CURIEs, bare chemical
        # accessions (HMDB/KEGG/PubChem/InChIKey — each namespaced by pattern), and non-chemical
        # tokens (spectral feature labels, placeholders). ``namespace_bare_gold`` assigns each bare
        # value its correct namespace and drops non-chemical tokens rather than mis-prefixing them.
        raw_gold = row.get(config.gold_id_column)
        gold_ids = namespace_bare_gold(raw_gold)
        row_concordant: bool | None = None
        if has_hit and gold_ids:
            id_scored += 1
            row_concordant = bool(predicted_curies(row) & gold_ids)
            if row_concordant:
                id_concordant += 1
        elif has_hit and str(raw_gold).strip() not in ("", "nan") and not gold_ids:
            id_excluded_nonchemical += 1  # had a gold token, but it was non-chemical (feature id / placeholder)

        if eq_available and has_hit and gold_ids:
            preds = predicted_curies(row)
            uci_v = id_equivalence_judge.uci_equivalent(gold_ids, preds)  # type: ignore[union-attr]
            bridge_v = id_equivalence_judge.block_equivalent(gold_ids, preds)  # type: ignore[union-attr]
            if uci_v is True:
                uci_concordant += 1
            elif uci_v is None:
                uci_needs += 1
            if bridge_v is True:
                bridge_concordant += 1
            elif bridge_v is None:
                bridge_needs += 1
            # Confusion matrix over rows the bridge newly credits but strict scored discordant.
            if bridge_v is True and not row_concordant:
                chosen_ns = str(chosen).split(":", 1)[0].upper() if chosen and ":" in str(chosen) else "?"
                for g in gold_ids:
                    g_ns = g.split(":", 1)[0].upper()
                    ns_confusion.setdefault(g_ns, {}).setdefault(chosen_ns, 0)
                    ns_confusion[g_ns][chosen_ns] += 1

        cn_row: bool | None = None
        if cn_available and has_hit and config.gold_smiles_column and _has_prediction(chosen):
            gold_smiles = row.get(config.gold_smiles_column)
            gold_cn = neutralize_first_block(gold_smiles)
            if gold_cn is not None:
                pred_cn = oracle.neutral_block(str(chosen).strip())  # type: ignore[union-attr]
                cn_scored += 1
                cn_row = bool(pred_cn is not None and pred_cn == gold_cn)
                if cn_row:
                    cn_concordant += 1

        acc = str(row.get(SOURCE_ACCESSION_COL, "") or "")
        if acc:
            bucket = per_accession.setdefault(acc, {"matched": 0, "total": 0})
            bucket["total"] += 1
            if has_hit:
                bucket["matched"] += 1

        per_row.append(
            {
                "name": row.get(config.name_column),
                "chosen_kg_id": str(chosen).strip() if _has_prediction(chosen) else None,
                "hit": has_hit,
                "id_concordant": row_concordant,
            }
        )

    id_eq_uci: dict[str, Any] | None = None
    id_eq_bridge: dict[str, Any] | None = None
    if eq_available:
        # Rate over the EVALUABLE subset (scored minus rows the independent judge could not
        # adjudicate), NOT the full strict population. Folding needs-verification rows into the
        # denominator would treat "UniChem couldn't check" as "checked, not equivalent" — so the
        # published rate would drift with UniChem availability, not chemistry. Unresolved rows are
        # surfaced via ``needs_verification``/``evaluable`` (coverage) instead of deflating the rate.
        uci_evaluable = id_scored - uci_needs
        bridge_evaluable = id_scored - bridge_needs
        id_eq_uci = {
            "metric": "id_concordance_uci_equivalence_rate",
            "scored": id_scored,
            "evaluable": uci_evaluable,
            "concordant": uci_concordant,
            "concordance_rate": (uci_concordant / uci_evaluable) if uci_evaluable else None,
            "needs_verification": uci_needs,
        }
        id_eq_bridge = {
            "metric": "id_concordance_inchikey_bridge_rate",
            "scored": id_scored,
            "evaluable": bridge_evaluable,
            "concordant": bridge_concordant,
            "concordance_rate": (bridge_concordant / bridge_evaluable) if bridge_evaluable else None,
            "needs_verification": bridge_needs,
        }

    structure_cn: dict[str, Any] | None = None
    if cn_available:
        structure_cn = {
            "metric": "structure_concordance_charge_normalized",
            "scored": cn_scored,
            "concordant": cn_concordant,
            "concordance_rate": (cn_concordant / cn_scored) if cn_scored else None,
        }

    return {
        "vocab": vocab,
        "mode": config.mode,
        "input_type": config.input_type,
        "comparable_core": {
            "metric": "name_hit_rate",
            "name_hit_rate": matched / total,
            "matched": matched,
            "total": total,
        },
        "id_concordance": {
            "metric": "id_concordance_rate",
            "scored": id_scored,
            "concordant": id_concordant,
            "concordance_rate": (id_concordant / id_scored) if id_scored else None,
            "excluded_nonchemical": id_excluded_nonchemical,
        },
        "id_concordance_uci_equivalence": id_eq_uci,
        "id_concordance_inchikey_bridge": id_eq_bridge,
        "namespace_confusion": ns_confusion if eq_available else None,
        "structure_concordance_charge_normalized": structure_cn,
        "per_accession": per_accession,
        "per_row": per_row,
    }
