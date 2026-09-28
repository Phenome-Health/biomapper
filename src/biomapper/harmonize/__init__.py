"""Local, client-side harmonization of two already-resolved datasets.

Harmonization here means **cross-dataset equivalence**: two entities, one from each cohort, are
equivalent when they resolve to the same canonical KRAKEN node. It is an identifier-set
intersection, never string matching, and it runs entirely on the client over outputs that have
already come back from the API. Nothing in this package calls the network.

Two invariants the linking logic is built around, ported verbatim from the engine:

1. **The linker is identifier-only.** ``INCHIKEY`` / ``INCHI`` / ``SMILES`` are excluded, because
   linking on a structure hash makes any downstream structural certificate circular and makes
   precision 100% by construction. See :mod:`biomapper.harmonize.curies`.
2. **CURIE prefix synonyms normalize.** ``KEGG.COMPOUND:C00031`` equals ``KEGG:C00031``, while
   genuinely different identifier spaces such as ``KEGG.GLYCAN`` stay distinct.

Quick start::

    from biomapper import map_entities
    from biomapper.harmonize import harmonize

    ukbb = map_entities([{"name": "Glucose"}, {"name": "Urea"}])
    arivale = map_entities([{"name": "glucose"}, {"name": "creatinine"}])

    report = harmonize(ukbb, arivale, a_label="ukbb", b_label="arivale")
    print(report.n_links)            # linked pairs
    print(report.a_unresolved)       # refusal candidates, never silently dropped
    print(report.summary())
"""

from biomapper.harmonize.curies import (
    canonical_prefix,
    curie_set,
    normalize_curie,
    predicted_curies,
)
from biomapper.harmonize.linking import (
    BASIS_ORDER,
    NAME_CASEFOLD_MIN_LENGTH_EXCLUSIVE,
    HarmonizationResult,
    Link,
    OverlapResult,
    curie_sets_from_results,
    harmonize,
    link_by_intersection,
    name_matches,
    normalize_name,
)

__all__ = [
    # CURIE normalization / identifier-only sets
    "normalize_curie",
    "canonical_prefix",
    "predicted_curies",
    "curie_set",
    # Linking
    "link_by_intersection",
    "curie_sets_from_results",
    "harmonize",
    "normalize_name",
    "name_matches",
    "BASIS_ORDER",
    "NAME_CASEFOLD_MIN_LENGTH_EXCLUSIVE",
    "Link",
    "OverlapResult",
    "HarmonizationResult",
]
