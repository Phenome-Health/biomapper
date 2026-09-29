"""Cross-dataset equivalence linking over two already-resolved datasets.

``link_by_intersection``, :class:`Link` and the overlap counters are ported from the engine's
``studies/external_benchmarks/scorers/cross_cohort_overlap.py`` (read at ``origin/dev``). The
refusal accounting — naming the entities that did not resolve, and keeping an API error apart
from a non-resolution — is written for this package, because a client-side harmonization report
has to be able to say which inputs it could not judge.

Fully offline: it consumes CURIE sets that are already resolved (real ones from a live mapping
run, or literals in tests) and never calls the API or the knowledge graph.

:func:`harmonize` can also link by NAME (decided 2026-09-28, reversing the earlier
"identifier-set intersection, never string matching" rule): with ``link_by_name=True``, two
resolved entities whose keys match link even when resolution put them on different nodes. It is
opt-in and currently intended for metabolite panels only. Every link records its basis, so a report
can separate identifier evidence from a name match. See :func:`harmonize` for the exact rules.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from biomapper.harmonize.curies import curie_set
from biomapper.models import MappingResult

# Link bases, strongest first. ``node``: both sides resolved to the same chosen_kg_id.
# ``identifier``: the identifier-only CURIE sets intersect (different nodes allowed).
# ``name_exact``: the keys are equal after whitespace normalization.
# ``name_casefold``: the keys are equal only after casefold.
BASIS_ORDER: tuple[str, ...] = ("node", "identifier", "name_exact", "name_casefold")

# A match that holds only after casefold is withheld, not linked, when the normalized name is this
# many characters or shorter: element symbols and short abbreviations collide across case with a
# different meaning (Co cobalt vs CO carbon monoxide). The value is a judgement call, kept here so
# it can be changed in one place.
NAME_CASEFOLD_MIN_LENGTH_EXCLUSIVE = 4

_WS = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """Whitespace normalization only: strip, and collapse internal runs to one space."""
    return _WS.sub(" ", name).strip()


@dataclass(frozen=True)
class Link:
    """One harmonized pair.

    ``shared`` carries the CURIE(s) that formed an identifier link, for audit (empty for a link
    formed by name alone). ``basis`` is the strongest basis that applies (see ``BASIS_ORDER``) and
    ``bases`` is every basis that applies.
    """

    a_key: str
    b_key: str
    shared: frozenset[str]
    basis: str = "identifier"
    bases: frozenset[str] = frozenset({"identifier"})

    @property
    def name_only(self) -> bool:
        """True when no identifier evidence supports the link, only a name match."""
        return not (self.bases & {"node", "identifier"})


@dataclass(frozen=True)
class OverlapResult:
    """Outcome of intersecting two sides' CURIE sets."""

    links: tuple[Link, ...]
    n_links: int  # distinct (a, b) linked pairs
    n_a_linked: int  # distinct A-side keys appearing in a link
    n_b_linked: int  # distinct B-side keys appearing in a link
    n_a_comparable: int  # A-side keys with a non-empty CURIE set (the shared denominator)
    n_b_comparable: int  # B-side keys with a non-empty CURIE set
    a_unresolved: tuple[str, ...]  # A-side keys whose CURIE set was EMPTY — refusal candidates
    b_unresolved: tuple[str, ...]  # B-side keys whose CURIE set was EMPTY — refusal candidates


def link_by_intersection(
    a_curies: dict[str, frozenset[str]],
    b_curies: dict[str, frozenset[str]],
) -> OverlapResult:
    """Link A<->B entities whose normalized CURIE sets intersect.

    Uses an inverted CURIE->keys index so cost is O(total CURIEs) rather than O(|A|.|B|). A pair
    sharing several CURIEs yields ONE link carrying all the shared CURIEs. The comparable
    denominator is the count of keys with a non-empty CURIE set on each side, so an entity that
    never resolved is never scored as a failure to link — it is reported as a refusal candidate in
    ``a_unresolved`` / ``b_unresolved`` instead of being silently dropped.
    """
    idx_b: dict[str, set[str]] = defaultdict(set)
    for b_key, curies in b_curies.items():
        for c in curies:
            idx_b[c].add(b_key)

    pair_shared: dict[tuple[str, str], set[str]] = defaultdict(set)
    for a_key, curies in a_curies.items():
        for c in curies:
            for b_key in idx_b.get(c, ()):
                pair_shared[(a_key, b_key)].add(c)

    links = tuple(
        Link(a_key=a, b_key=b, shared=frozenset(shared))
        for (a, b), shared in sorted(pair_shared.items())
    )
    return OverlapResult(
        links=links,
        n_links=len(links),
        n_a_linked=len({lk.a_key for lk in links}),
        n_b_linked=len({lk.b_key for lk in links}),
        n_a_comparable=sum(1 for s in a_curies.values() if s),
        n_b_comparable=sum(1 for s in b_curies.values() if s),
        a_unresolved=tuple(k for k, s in a_curies.items() if not s),
        b_unresolved=tuple(k for k, s in b_curies.items() if not s),
    )


# Scalar fields `summary()` emits alongside the two per-cohort blocks. A cohort label equal to
# one of these would overwrite it, so the label is rejected rather than allowed to corrupt the
# summary's shape.
_RESERVED_SUMMARY_KEYS: frozenset[str] = frozenset(
    {"n_links", "links_by_basis", "n_name_only_links", "n_name_match_withheld"}
)


def _require_distinct_labels(a_label: str, b_label: str) -> None:
    """Reject cohort labels that would collide as keys in :meth:`HarmonizationResult.summary`."""
    if a_label == b_label:
        raise ValueError(
            f"a_label and b_label must be distinct; both are {a_label!r}. They key the summary, "
            "so equal labels would drop one cohort's counts."
        )
    for side, label in (("a_label", a_label), ("b_label", b_label)):
        if label in _RESERVED_SUMMARY_KEYS:
            raise ValueError(
                f"{side}={label!r} is reserved: summary() emits it as a scalar field, so a cohort "
                "under that label would overwrite it."
            )


def _keys_for(
    results: Sequence[MappingResult],
    key: Callable[[MappingResult, int], str] | None,
) -> list[str]:
    """Key every result ONCE, against its position in the ORIGINAL input.

    Keying a filtered subset would renumber it. A custom index-based key would then hand the same
    string to two different rows, and a link could be attributed to an entity that never produced
    one, so every key in a side is derived here and reused everywhere downstream.
    """
    return [key(r, i) if key is not None else r.query_name for i, r in enumerate(results)]


def _require_unique(keys: Sequence[str]) -> None:
    """Reject duplicate keys. Two entities on one key means one of them is silently dropped."""
    seen: set[str] = set()
    for k in keys:
        if k in seen:
            raise ValueError(
                f"duplicate key {k!r} in the input results. Two entities would collapse onto one "
                "entry and one of them would be dropped. Pass key=... to disambiguate."
            )
        seen.add(k)


def curie_sets_from_results(
    results: Iterable[MappingResult],
    key: Callable[[MappingResult, int], str] | None = None,
) -> dict[str, frozenset[str]]:
    """Build the linker's input from mapping results: ``{key: identifier-only CURIE set}``.

    ``key`` defaults to the result's ``query_name``. A duplicate key raises ``ValueError`` rather
    than collapsing two distinct entities onto one entry, which would silently drop one of them;
    pass a ``key`` callable (it receives the result and its index) when a cohort genuinely has
    repeated names.
    """
    materialized = list(results)
    keys = _keys_for(materialized, key)
    _require_unique(keys)
    return {
        k: curie_set(r.chosen_kg_id, r.kg_equivalent_ids)
        for k, r in zip(keys, materialized, strict=True)
    }


@dataclass(frozen=True)
class HarmonizationResult:
    """Cross-dataset equivalence between two cohorts, plus what could not be judged.

    Every input entity lands in exactly one bucket per side:

    - **comparable** — resolved to at least one identifier, so it could link (whether it did or not)
    - **unresolved** — resolved to nothing. A refusal candidate, never a link, never a miss.
    - **errored** — the mapping call itself failed. "We do not know", which is a different claim
      from "it did not resolve", so the two are counted apart.
    """

    overlap: OverlapResult
    a_label: str
    b_label: str
    a_errors: tuple[str, ...]
    b_errors: tuple[str, ...]
    n_a_total: int
    n_b_total: int
    # (a_key, b_key) pairs whose keys match only after casefold and are too short to link by name
    # (see NAME_CASEFOLD_MIN_LENGTH_EXCLUSIVE). Reported for review, never linked.
    name_match_withheld: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        # The labels key summary(); enforce that here so the invariant holds however the result
        # was built, not only via harmonize().
        _require_distinct_labels(self.a_label, self.b_label)

    # -- linking ---------------------------------------------------------

    @property
    def links(self) -> tuple[Link, ...]:
        return self.overlap.links

    @property
    def n_links(self) -> int:
        return self.overlap.n_links

    @property
    def n_a_linked(self) -> int:
        return self.overlap.n_a_linked

    @property
    def n_b_linked(self) -> int:
        return self.overlap.n_b_linked

    @property
    def links_by_basis(self) -> dict[str, int]:
        """Links counted by their strongest basis, in ``BASIS_ORDER``."""
        counts = Counter(lk.basis for lk in self.links)
        return {b: counts.get(b, 0) for b in BASIS_ORDER}

    @property
    def n_name_only_links(self) -> int:
        """Links with no identifier evidence behind them, only a name match."""
        return sum(1 for lk in self.links if lk.name_only)

    # -- what could link -------------------------------------------------

    @property
    def n_a_comparable(self) -> int:
        return self.overlap.n_a_comparable

    @property
    def n_b_comparable(self) -> int:
        return self.overlap.n_b_comparable

    # -- what could not ---------------------------------------------------

    @property
    def a_unresolved(self) -> tuple[str, ...]:
        """A-side entities that resolved to no identifier. Refusal candidates."""
        return self.overlap.a_unresolved

    @property
    def b_unresolved(self) -> tuple[str, ...]:
        """B-side entities that resolved to no identifier. Refusal candidates."""
        return self.overlap.b_unresolved

    @property
    def n_a_unresolved(self) -> int:
        return len(self.a_unresolved)

    @property
    def n_b_unresolved(self) -> int:
        return len(self.b_unresolved)

    @property
    def n_a_errors(self) -> int:
        return len(self.a_errors)

    @property
    def n_b_errors(self) -> int:
        return len(self.b_errors)

    # -- rates -------------------------------------------------------------

    @property
    def a_link_rate(self) -> float | None:
        """Linked A-side entities over COMPARABLE A-side entities; ``None`` if none comparable.

        The denominator excludes unresolved entities deliberately: an entity that resolved to
        nothing had no opportunity to link, so counting it as a miss would understate the rate and
        conflate non-resolution with non-equivalence.
        """
        return (self.n_a_linked / self.n_a_comparable) if self.n_a_comparable else None

    @property
    def b_link_rate(self) -> float | None:
        """Linked B-side entities over COMPARABLE B-side entities; ``None`` if none comparable."""
        return (self.n_b_linked / self.n_b_comparable) if self.n_b_comparable else None

    def summary(self) -> dict[str, Any]:
        """Counts-only summary, keyed by the two cohort labels. Safe to log or serialize.

        The labels are dictionary keys here, which is why :func:`harmonize` rejects labels that
        are equal to each other or to a reserved field name: either would silently overwrite a
        sibling entry and hand back a summary that is quietly missing a cohort.
        """
        return {
            "n_links": self.n_links,
            "links_by_basis": self.links_by_basis,
            "n_name_only_links": self.n_name_only_links,
            "n_name_match_withheld": len(self.name_match_withheld),
            self.a_label: {
                "total": self.n_a_total,
                "comparable": self.n_a_comparable,
                "unresolved": self.n_a_unresolved,
                "errors": self.n_a_errors,
                "linked": self.n_a_linked,
                "link_rate": self.a_link_rate,
            },
            self.b_label: {
                "total": self.n_b_total,
                "comparable": self.n_b_comparable,
                "unresolved": self.n_b_unresolved,
                "errors": self.n_b_errors,
                "linked": self.n_b_linked,
                "link_rate": self.b_link_rate,
            },
        }


def _partition_side(
    results: Sequence[MappingResult],
    key: Callable[[MappingResult, int], str] | None,
) -> tuple[dict[str, frozenset[str]], tuple[str, ...], dict[str, str], dict[str, str]]:
    """Split one cohort into (linkable CURIE sets, errored keys), keying each row exactly once.

    Uniqueness is enforced across errored AND resolved rows together. Checking only the resolved
    subset would let one key appear in both ``a_errors`` and a link while describing two different
    input entities. An error is not a non-resolution, so the two are partitioned, not merged.
    """
    keys = _keys_for(results, key)
    _require_unique(keys)
    curies: dict[str, frozenset[str]] = {}
    errored: list[str] = []
    nodes: dict[str, str] = {}
    names: dict[str, str] = {}
    for k, r in zip(keys, results, strict=True):
        if r.error:
            errored.append(k)
        else:
            curies[k] = curie_set(r.chosen_kg_id, r.kg_equivalent_ids)
            names[k] = r.query_name
            if r.chosen_kg_id:
                nodes[k] = r.chosen_kg_id
    return curies, tuple(errored), nodes, names


def name_matches(
    a_names: dict[str, str], b_names: dict[str, str]
) -> tuple[dict[tuple[str, str], str], list[tuple[str, str]]]:
    """Pair entities by name. Inputs map each key to its name (``query_name``).

    Returns ``{(a_key, b_key): "name_exact" | "name_casefold"}`` plus the withheld short pairs.

    Exact means equal after whitespace normalization. A pair equal only after casefold links
    unless either normalized name is NAME_CASEFOLD_MIN_LENGTH_EXCLUSIVE characters or shorter, in
    which case it is withheld. No fuzzy matching and no punctuation stripping.
    """
    by_fold: dict[str, list[str]] = defaultdict(list)
    for b, b_name in b_names.items():
        by_fold[normalize_name(b_name).casefold()].append(b)
    matched: dict[tuple[str, str], str] = {}
    withheld: list[tuple[str, str]] = []
    for a, a_name in a_names.items():
        a_norm = normalize_name(a_name)
        for b in by_fold.get(a_norm.casefold(), ()):
            b_norm = normalize_name(b_names[b])
            if a_norm == b_norm:
                matched[(a, b)] = "name_exact"
            elif min(len(a_norm), len(b_norm)) <= NAME_CASEFOLD_MIN_LENGTH_EXCLUSIVE:
                # Either side short withholds, so swapping the cohorts cannot change the outcome
                # (casefold can change length: Maße and Masse casefold equal).
                withheld.append((a, b))
            else:
                matched[(a, b)] = "name_casefold"
    return matched, withheld


def harmonize(
    a_results: Sequence[MappingResult],
    b_results: Sequence[MappingResult],
    *,
    a_label: str = "a",
    b_label: str = "b",
    key: Callable[[MappingResult, int], str] | None = None,
    link_by_name: bool = False,
) -> HarmonizationResult:
    """Harmonize two already-resolved datasets by cross-dataset equivalence.

    Two entities link when any of these holds, strongest first (the link's ``basis``):

    - ``node``: their identifier-only CURIE sets intersect AND both resolved to the same chosen
      node (a node label on an identifier link, never a link on its own, so a node in a structure
      namespace cannot link anything);
    - ``identifier``: their identifier-only CURIE sets intersect (structure namespaces excluded);
    - ``name_exact``: their keys are equal after whitespace normalization;
    - ``name_casefold``: their names are equal only after casefold. When either normalized name
      is ``NAME_CASEFOLD_MIN_LENGTH_EXCLUSIVE`` characters or shorter the pair is NOT linked and is
      listed in ``name_match_withheld`` instead (Co vs CO).

    Name matching applies to entities that resolved on both sides (an unresolved entity stays a
    refusal candidate), compares each result's ``query_name`` (a custom ``key`` such as
    ``"row_id|name"`` would otherwise never match), reports links under the usual keys, and does
    no fuzzy or punctuation matching. Pass results of ONE entity type per call: the results carry
    no type for this function to check, so keeping types apart is the caller's responsibility,
    and a cross-type name collision such as cAMP (metabolite) vs CAMP (the gene) is out of
    scope. This is a pure set operation over results you already have;
    it issues no requests.

    Args:
        a_results: Mapping results for cohort A (e.g. from :func:`biomapper.map_entities`).
        b_results: Mapping results for cohort B.
        a_label:   Name for cohort A in :meth:`HarmonizationResult.summary`.
        b_label:   Name for cohort B in :meth:`HarmonizationResult.summary`.
        key:       Optional ``(result, index) -> str`` key. Defaults to ``query_name``; supply one
                   when a cohort has repeated names (a duplicate key otherwise raises). Name
                   matching compares ``query_name``, not the key.
        link_by_name: Add the name-match links. Off by default, which links by identifier only
                   (the behaviour before 1.5.5). Currently intended for small-molecule /
                   metabolite panels; other entity types are pending review of case-sensitive
                   naming conventions (gene and protein symbols, for example).

    Returns:
        A :class:`HarmonizationResult`. Entities that resolved to nothing are reported in
        ``a_unresolved`` / ``b_unresolved`` as refusal candidates, and entities whose mapping call
        errored in ``a_errors`` / ``b_errors``. Neither is silently dropped.

    Raises:
        ValueError: If either side has duplicate keys (see ``key``), or if ``a_label`` and
            ``b_label`` are equal or collide with a reserved ``summary()`` field.
    """
    _require_distinct_labels(a_label, b_label)
    a_curies, a_errors, a_nodes, a_names = _partition_side(a_results, key)
    b_curies, b_errors, b_nodes, b_names = _partition_side(b_results, key)
    overlap = link_by_intersection(a_curies, b_curies)

    bases: dict[tuple[str, str], set[str]] = defaultdict(set)
    shared: dict[tuple[str, str], frozenset[str]] = {}
    for lk in overlap.links:
        pair = (lk.a_key, lk.b_key)
        shared[pair] = lk.shared
        bases[pair].add("identifier")
        if a_nodes.get(lk.a_key) is not None and a_nodes.get(lk.a_key) == b_nodes.get(lk.b_key):
            bases[pair].add("node")
    withheld: list[tuple[str, str]] = []
    if link_by_name:
        resolved_a = {k: a_names[k] for k, c in a_curies.items() if c}
        resolved_b = {k: b_names[k] for k, c in b_curies.items() if c}
        matched, short = name_matches(resolved_a, resolved_b)
        for pair, basis in matched.items():
            bases[pair].add(basis)
        # A short casefold-only pair already linked by identifier needs no review.
        withheld = sorted(p for p in short if p not in bases)

    links = tuple(
        Link(
            a_key=a,
            b_key=b,
            shared=shared.get((a, b), frozenset()),
            basis=next(x for x in BASIS_ORDER if x in bases[(a, b)]),
            bases=frozenset(bases[(a, b)]),
        )
        for (a, b) in sorted(bases)
    )
    overlap = replace(
        overlap,
        links=links,
        n_links=len(links),
        n_a_linked=len({lk.a_key for lk in links}),
        n_b_linked=len({lk.b_key for lk in links}),
    )
    return HarmonizationResult(
        overlap=overlap,
        a_label=a_label,
        b_label=b_label,
        a_errors=a_errors,
        b_errors=b_errors,
        n_a_total=len(a_results),
        n_b_total=len(b_results),
        name_match_withheld=tuple(withheld),
    )
