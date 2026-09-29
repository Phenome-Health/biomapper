"""Identifier-only CURIE sets for cross-dataset harmonization.

Ported from the biomapper2 engine's offline benchmark scorers
(``studies/external_benchmarks/scorers/curie_scorer.py`` and
``scorers/cross_cohort_overlap.py``, read at ``origin/dev``). The engine versions take a
``pandas.Series`` per row; this port takes plain values so ``biomapper.harmonize`` has no
pandas dependency — pandas is an optional extra of this package, and harmonization must work
without it. The normalization rules, the alias table, and the structural exclusion are
unchanged from the engine so a wrapper-side link and an engine-side link agree exactly.

Nothing in this module touches the network or the knowledge graph. It consumes CURIE sets that
have already been resolved (by a live run, or mocked in tests).
"""

from __future__ import annotations

import ast
from typing import Any

# Namespace-prefix synonyms that denote the SAME identifier space, canonicalized to one form so
# equal entities compare equal regardless of which prefix a source emitted. The metabolite KG /
# equivalence expansion writes the Biolink-style database-section prefixes (``KEGG.COMPOUND``,
# ``PUBCHEM.COMPOUND``) while cohort exports and benchmark golds ship the bare database prefix
# (``KEGG``, ``PUBCHEM``); without this, ``KEGG:C00626`` never matches ``KEGG.COMPOUND:C00626``
# and every KEGG-bearing pair fails to link. Keys/values are the UPPERCASED prefix (matched after
# the prefix is upper-cased). Generic across namespaces — no per-row special-casing; only the
# compound identifier space is aliased (KEGG.GLYCAN / KEGG.DRUG are DELIBERATELY not folded in,
# they are different id spaces).
_NAMESPACE_ALIASES: dict[str, str] = {
    "KEGG.COMPOUND": "KEGG",
    "PUBCHEM.COMPOUND": "PUBCHEM",
}

# Structure-encoding namespaces are EXCLUDED from the linker: linking two entities via a shared
# InChIKey (a structure hash) would make the linker structural — and would make any later
# independent-structure certificate circular, because the certificate would be checking the very
# thing that formed the link. Precision would then be 100% by construction and would mean nothing.
# The linker is IDENTIFIER-only (CHEBI/KEGG/HMDB/PUBCHEM/CAS/UNIPROT/...); structure is a separate,
# downstream judgement made from a source independent of these identifiers.
_STRUCTURAL_NAMESPACES: frozenset[str] = frozenset({"INCHIKEY", "INCHI", "SMILES"})


def _is_nan(value: Any) -> bool:  # noqa: ANN401 — accepts any cell value
    """True for a float NaN. Replaces ``pandas.isna`` so this module stays pandas-free."""
    return isinstance(value, float) and value != value


def canonical_prefix(prefix: str) -> str:
    """Map an (already stripped and upper-cased) namespace prefix to its canonical synonym."""
    return _NAMESPACE_ALIASES.get(prefix, prefix)


def normalize_curie(curie: Any) -> str | None:  # noqa: ANN401 — accepts any cell value
    """Canonicalize a CURIE for equality: strip, canonicalize/uppercase the prefix, keep the local.

    Gene/protein identifiers (Ensembl/UniProt/Entrez/RefSeq) are conventionally case-stable in the
    local part but the *prefix* casing varies across sources (``Ensembl`` vs ``ENSEMBL``), so only
    the prefix is uppercased. Prefix SYNONYMS for one identifier space are folded to a canonical
    form via :data:`_NAMESPACE_ALIASES` (``KEGG.COMPOUND`` -> ``KEGG``) so a bare-vs-database
    section prefix mismatch cannot silently fail to link two equal entities.

    Returns ``None`` for blank / NaN input.
    """
    if curie is None or _is_nan(curie):
        return None
    s = str(curie).strip()
    if not s or s.lower() == "nan":
        return None
    if ":" in s:
        prefix, local = s.split(":", 1)
        return f"{canonical_prefix(prefix.strip().upper())}:{local.strip()}"
    return canonical_prefix(s.upper())


def _parse_equivalents(value: Any) -> dict[str, Any]:  # noqa: ANN401 — accepts any cell value
    """Parse a ``kg_equivalent_ids`` cell (a dict, a dict-repr string from a TSV, or NaN)."""
    if isinstance(value, dict):
        return value
    if value is None or _is_nan(value):
        return {}
    s = str(value).strip()
    if not s or s.lower() == "nan":
        return {}
    try:
        parsed = ast.literal_eval(s)
    except (ValueError, SyntaxError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def predicted_curies(
    chosen: Any = None,  # noqa: ANN401 — accepts any cell value
    equivalents: Any = None,  # noqa: ANN401 — accepts any cell value
) -> set[str]:
    """Every CURIE BioMapper assigned for one entity: ``chosen_kg_id`` + all ``kg_equivalent_ids``.

    ``kg_equivalent_ids`` is ``{prefix: [local_id, ...]}`` with the prefix STRIPPED from each value
    (biomapper2 ``Linker.get_equivalent_ids``), so each cross-reference CURIE is reconstructed as
    ``prefix:local_id``. A value that already carries a prefix (defensive) is taken as-is.

    Structural namespaces are NOT filtered here — see :func:`curie_set`, which is the linker's entry
    point and applies the identifier-only rule.
    """
    out: set[str] = set()
    normalized_chosen = normalize_curie(chosen)
    if normalized_chosen is not None:
        out.add(normalized_chosen)
    for namespace, ids in _parse_equivalents(equivalents).items():
        values = ids if isinstance(ids, list | tuple | set) else [ids]
        for v in values:
            raw = str(v).strip()
            if not raw:
                continue
            curie = raw if ":" in raw else f"{namespace}:{raw}"
            normalized = normalize_curie(curie)
            if normalized is not None:
                out.add(normalized)
    return out


def curie_set(
    chosen: Any = None,  # noqa: ANN401 — accepts any cell value
    equivalents: Any = None,  # noqa: ANN401 — accepts any cell value
) -> frozenset[str]:
    """Identifier-only CURIE set for one entity: ``chosen_kg_id`` u ``kg_equivalent_ids``, MINUS
    any structure-encoding namespace (InChIKey / InChI / SMILES).

    This is what the linker compares. An EMPTY result means the entity did not resolve: it is a
    refusal candidate, never a link, and callers must surface it rather than drop it (see
    :func:`biomapper.harmonize.harmonize`).
    """
    return frozenset(
        c
        for c in predicted_curies(chosen, equivalents)
        if c.split(":", 1)[0] not in _STRUCTURAL_NAMESPACES
    )
