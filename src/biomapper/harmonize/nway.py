"""Offline N-way grouping of already-mapped cohorts, anchored on the chosen KRAKEN node.

:func:`group_cohorts` takes two or more cohorts' mapping results and returns:

* **Node groups.** Rows are grouped by their raw ``chosen_kg_id`` string, the same value
  :func:`~biomapper.harmonize.harmonize`'s ``node`` basis compares. A group is *clean* when each
  cohort in it contributes exactly one row and no member resolved to a class-level ("broad")
  entry. A cohort contributing two or more rows flags *multiplicity*; any member whose
  ``lipid_resolution.mapping_relation`` is ``"broad"`` flags the group *broad*.
* **A cross-node queue.** Identifier and name links between rows on different nodes are never
  merged into one group (no chaining). They are aggregated by unordered node pair, with the row
  pairs kept as drill-down that can be streamed to a sink instead of held in memory.
* **Buckets for the rest.** Every row lands in exactly one bucket: ``grouped``; ``no_node`` (it
  has identifiers but no usable chosen node, for example a structure-namespace ``chosen_kg_id``);
  ``unresolved`` (an empty identifier-only CURIE set, the test ``harmonize()`` uses); or
  ``errored``. A ``no_node`` row's links go to the cross-node queue under :data:`NO_NODE`.
* **Per-pair counts and reconciliation (R15).** Links come from one call of the unchanged
  :func:`~biomapper.harmonize.harmonize` per cohort pair, so for every pair the same-node row
  pairs plus the cross-node link pairs equal ``harmonize()``'s link count by construction; the
  check guards this module's own bookkeeping.

Row identity is ``(cohort label, key)`` everywhere: default keys such as ``0|glucose`` repeat
across cohorts and must only be unique within one. Like the rest of :mod:`biomapper.harmonize`
this module makes no network calls; label rules beyond distinctness (filename safety, reserved
summary fields) are enforced by the caller.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any

from biomapper.harmonize.curies import curie_set, is_structure_curie
from biomapper.harmonize.linking import harmonize
from biomapper.models import MappingResult

__all__ = [
    "BUCKETS",
    "NO_NODE",
    "CrossNodeEntry",
    "GroupMember",
    "NodeGroup",
    "NwayCohort",
    "NwayResult",
    "PairCounts",
    "RowPair",
    "RowRef",
    "Withheld",
    "group_cohorts",
    "is_broad",
]

NO_NODE = "<no node>"
BUCKETS: tuple[str, ...] = ("grouped", "no_node", "unresolved", "errored")


@dataclass(frozen=True)
class NwayCohort:
    """One cohort's keys and mapping results, aligned by position."""

    label: str
    keys: Sequence[str]
    results: Sequence[MappingResult]


@dataclass(frozen=True)
class RowRef:
    """One input row and the bucket it landed in."""

    cohort: str
    key: str
    name: str
    bucket: str
    node: str | None  # the group node for ``grouped`` rows, else None
    chosen_kg_id: str | None
    broad: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"cohort": self.cohort, "key": self.key, "name": self.name,
                "bucket": self.bucket, "node": self.node, "chosen_kg_id": self.chosen_kg_id,
                "broad": self.broad, "error": self.error}


@dataclass(frozen=True)
class GroupMember:
    cohort: str
    key: str
    name: str
    broad: bool

    def to_dict(self) -> dict[str, Any]:
        return {"cohort": self.cohort, "key": self.key, "name": self.name, "broad": self.broad}


@dataclass(frozen=True)
class NodeGroup:
    """Rows from any cohorts that resolved to one chosen node."""

    node: str
    members: tuple[GroupMember, ...]
    cohorts: tuple[str, ...]  # cohorts present, in input order

    @property
    def rows_per_cohort(self) -> dict[str, int]:
        counts = Counter(m.cohort for m in self.members)
        return {c: counts[c] for c in self.cohorts}

    @property
    def multiplicity(self) -> bool:
        """Some cohort contributes two or more rows."""
        return any(n > 1 for n in self.rows_per_cohort.values())

    @property
    def broad(self) -> bool:
        """Some member resolved to a class-level entry (``mapping_relation == "broad"``)."""
        return any(m.broad for m in self.members)

    @property
    def clean(self) -> bool:
        return not self.multiplicity and not self.broad

    def to_dict(self) -> dict[str, Any]:
        """The group as plain JSON-safe data, with its flags and per-cohort row counts."""
        return {
            "node": self.node,
            "cohorts": list(self.cohorts),
            "n_cohorts": len(self.cohorts),
            "rows_per_cohort": self.rows_per_cohort,
            "clean": self.clean,
            "multiplicity": self.multiplicity,
            "broad": self.broad,
            "members": [m.to_dict() for m in self.members],
        }


@dataclass(frozen=True)
class RowPair:
    """One cross-node link between two rows (drill-down for :class:`CrossNodeEntry`)."""

    a_cohort: str
    a_key: str
    a_name: str
    a_node: str
    b_cohort: str
    b_key: str
    b_name: str
    b_node: str
    basis: str
    bases: frozenset[str]

    def to_dict(self) -> dict[str, Any]:
        return {"a_cohort": self.a_cohort, "a_key": self.a_key, "a_name": self.a_name,
                "a_node": self.a_node, "b_cohort": self.b_cohort, "b_key": self.b_key,
                "b_name": self.b_name, "b_node": self.b_node, "basis": self.basis,
                "bases": sorted(self.bases)}


@dataclass(frozen=True)
class CrossNodeEntry:
    """Links between rows on two different nodes, aggregated over every cohort pair."""

    node_a: str
    node_b: str
    bases: frozenset[str]
    cohorts: tuple[str, ...]
    n_row_pairs: int

    def to_dict(self) -> dict[str, Any]:
        return {"node_a": self.node_a, "node_b": self.node_b, "bases": sorted(self.bases),
                "cohorts": list(self.cohorts), "n_row_pairs": self.n_row_pairs}


@dataclass(frozen=True)
class Withheld:
    """A short name pair equal only after casefold (Co vs CO): reported, never linked."""

    a_cohort: str
    a_key: str
    b_cohort: str
    b_key: str

    def to_dict(self) -> dict[str, Any]:
        return {"a_cohort": self.a_cohort, "a_key": self.a_key, "b_cohort": self.b_cohort,
                "b_key": self.b_key}


@dataclass(frozen=True)
class PairCounts:
    """One cohort pair's counts and its reconciliation against ``harmonize()``.

    ``same_node_row_pairs`` counts row pairs (rows from A x rows from B per group);
    ``overlap_groups`` counts groups containing both cohorts.
    """

    a: str
    b: str
    same_node_row_pairs: int
    cross_node_link_pairs: int
    overlap_groups: int
    harmonize_links: int
    same_node_links: int  # links whose rows share a group, counted from harmonize()'s links

    @property
    def reconciles(self) -> bool:
        return (
            self.same_node_row_pairs == self.same_node_links
            and self.same_node_row_pairs + self.cross_node_link_pairs == self.harmonize_links
        )

    def to_dict(self) -> dict[str, Any]:
        return {"a": self.a, "b": self.b, "overlap_groups": self.overlap_groups,
                "same_node_row_pairs": self.same_node_row_pairs,
                "cross_node_link_pairs": self.cross_node_link_pairs,
                "harmonize_links": self.harmonize_links, "reconciles": self.reconciles}


@dataclass(frozen=True)
class NwayResult:
    labels: tuple[str, ...]
    link_by_name: bool
    rows: tuple[RowRef, ...]
    groups: tuple[NodeGroup, ...]
    cross_node: tuple[CrossNodeEntry, ...]
    withheld: tuple[Withheld, ...]
    pair_counts: tuple[PairCounts, ...]
    drilldown: tuple[RowPair, ...] | None = None
    _by_row: dict[tuple[str, str], RowRef] = field(default_factory=dict, repr=False,
                                                   compare=False)

    def row(self, cohort: str, key: str) -> RowRef:
        if not self._by_row:
            self._by_row.update({(r.cohort, r.key): r for r in self.rows})
        return self._by_row[(cohort, key)]

    def bucket_keys(self, cohort: str, bucket: str) -> tuple[str, ...]:
        return tuple(r.key for r in self.rows if r.cohort == cohort and r.bucket == bucket)

    def counts_by_cohort(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for label in self.labels:
            counts = Counter(r.bucket for r in self.rows if r.cohort == label)
            out[label] = {"total": sum(counts.values()), **{b: counts.get(b, 0) for b in BUCKETS}}
        return out

    @property
    def clean_groups(self) -> tuple[NodeGroup, ...]:
        return tuple(g for g in self.groups if g.clean)

    @property
    def flagged_groups(self) -> tuple[NodeGroup, ...]:
        return tuple(g for g in self.groups if not g.clean)

    def to_dict(self) -> dict[str, Any]:
        """The grouping as plain JSON-safe data.

        ``rows`` holds every input row with its bucket; ``groups`` every node group (filter on
        ``clean`` for the flagged ones); ``cross_node`` the review queue of links between rows on
        different nodes; ``withheld`` the short casefold-only name pairs that were not linked.
        ``cross_node_pairs`` is the row-pair drill-down, or ``None`` when it was streamed to a
        file instead of held in memory. Keys are stable: new ones may be added, existing ones
        are not renamed or removed.
        """
        return {
            "labels": list(self.labels),
            "link_by_name": self.link_by_name,
            "counts_by_cohort": self.counts_by_cohort(),
            "rows": [r.to_dict() for r in self.rows],
            "groups": [g.to_dict() for g in self.groups],
            "cross_node": [e.to_dict() for e in self.cross_node],
            "cross_node_pairs": (
                [p.to_dict() for p in self.drilldown] if self.drilldown is not None else None
            ),
            "withheld": [w.to_dict() for w in self.withheld],
            "pair_counts": [p.to_dict() for p in self.pair_counts],
        }


def is_broad(result: MappingResult) -> bool:
    lipid = result.lipid_resolution
    return lipid is not None and lipid.mapping_relation == "broad"


def _validate(cohorts: Sequence[NwayCohort]) -> None:
    if len(cohorts) < 2:
        raise ValueError(f"group_cohorts needs at least two cohorts, got {len(cohorts)}")
    labels = [c.label for c in cohorts]
    dupes = sorted(label for label, n in Counter(labels).items() if n > 1)
    if dupes:
        raise ValueError(f"cohort labels must be distinct; repeated: {dupes}")
    for c in cohorts:
        if len(c.keys) != len(c.results):
            raise ValueError(
                f"cohort {c.label!r}: {len(c.keys)} keys for {len(c.results)} results; keys and "
                "results must align by position"
            )
        seen: set[str] = set()
        for k in c.keys:
            if k in seen:
                raise ValueError(
                    f"duplicate key {k!r} in cohort {c.label!r}: two rows would collapse onto one "
                    "and one would be dropped. Keys must be unique within each cohort."
                )
            seen.add(k)


def _bucket(result: MappingResult) -> str:
    if result.error:
        return "errored"
    if not curie_set(result.chosen_kg_id, result.kg_equivalent_ids):
        return "unresolved"
    if not result.chosen_kg_id or is_structure_curie(result.chosen_kg_id):
        return "no_node"
    return "grouped"


def group_cohorts(
    cohorts: Sequence[NwayCohort],
    *,
    link_by_name: bool,
    drilldown_sink: Callable[[RowPair], None] | None = None,
) -> NwayResult:
    """Group N cohorts' results by chosen node and queue every cross-node link for review.

    Args:
        cohorts: Two or more cohorts, each with keys unique within it and distinct labels.
        link_by_name: Passed to every pairwise :func:`~biomapper.harmonize.harmonize` call.
        drilldown_sink: Receives each cross-node row pair as it is found. When given, the pairs
            are not held in memory (``NwayResult.drilldown`` is ``None``).

    Raises:
        ValueError: Fewer than two cohorts, repeated labels, misaligned keys, or a duplicate key
            within one cohort. Raised before any linking.
    """
    _validate(cohorts)
    labels = tuple(c.label for c in cohorts)
    # Copies, so every result object is distinct across and within cohorts; harmonize()'s key
    # callable sees (result, index) and tells the two sides apart by identity at that index.
    copies = {c.label: [r.model_copy() for r in c.results] for c in cohorts}
    keys = {c.label: list(c.keys) for c in cohorts}

    rows: list[RowRef] = []
    node_of: dict[tuple[str, str], str | None] = {}
    members: dict[str, list[GroupMember]] = defaultdict(list)
    for c in cohorts:
        for k, r in zip(c.keys, copies[c.label], strict=True):
            bucket = _bucket(r)
            node = r.chosen_kg_id if bucket == "grouped" else None
            broad = is_broad(r)
            node_of[(c.label, k)] = node
            rows.append(RowRef(cohort=c.label, key=k, name=r.query_name, bucket=bucket, node=node,
                               chosen_kg_id=r.chosen_kg_id, broad=broad, error=r.error))
            if node is not None:
                members[node].append(GroupMember(cohort=c.label, key=k, name=r.query_name,
                                                 broad=broad))
    groups = tuple(
        NodeGroup(node=node, members=tuple(ms),
                  cohorts=tuple(label for label in labels if any(m.cohort == label for m in ms)))
        for node, ms in sorted(members.items())
    )
    per_group_counts = [g.rows_per_cohort for g in groups]

    held: list[RowPair] = []
    sink = drilldown_sink if drilldown_sink is not None else held.append
    names = {(row.cohort, row.key): row.name for row in rows}
    cross_bases: dict[tuple[str, str], set[str]] = defaultdict(set)
    cross_cohorts: dict[tuple[str, str], set[str]] = defaultdict(set)
    cross_n: Counter[tuple[str, str]] = Counter()
    withheld: list[Withheld] = []
    pair_counts: list[PairCounts] = []

    for a, b in combinations(labels, 2):
        ra, rb, ka, kb = copies[a], copies[b], keys[a], keys[b]

        def key_for(r: MappingResult, i: int, ra: list[MappingResult] = ra,
                    ka: list[str] = ka, kb: list[str] = kb) -> str:
            return ka[i] if i < len(ra) and ra[i] is r else kb[i]

        result = harmonize(ra, rb, a_label=a, b_label=b, key=key_for, link_by_name=link_by_name)
        same_links = 0
        cross = 0
        for lk in result.links:
            na, nb = node_of[(a, lk.a_key)], node_of[(b, lk.b_key)]
            if na is not None and na == nb:
                same_links += 1
                continue
            cross += 1
            a_node, b_node = na or NO_NODE, nb or NO_NODE
            pair_key = (a_node, b_node) if a_node <= b_node else (b_node, a_node)
            cross_bases[pair_key].update(lk.bases)
            cross_cohorts[pair_key].update((a, b))
            cross_n[pair_key] += 1
            sink(RowPair(a_cohort=a, a_key=lk.a_key, a_name=names[(a, lk.a_key)], a_node=a_node,
                         b_cohort=b, b_key=lk.b_key, b_name=names[(b, lk.b_key)], b_node=b_node,
                         basis=lk.basis, bases=lk.bases))
        withheld.extend(Withheld(a, ak, b, bk) for ak, bk in result.name_match_withheld)
        pair_counts.append(PairCounts(
            a=a,
            b=b,
            same_node_row_pairs=sum(n.get(a, 0) * n.get(b, 0) for n in per_group_counts),
            cross_node_link_pairs=cross,
            overlap_groups=sum(1 for n in per_group_counts if a in n and b in n),
            harmonize_links=result.n_links,
            same_node_links=same_links,
        ))

    cross_node = tuple(
        CrossNodeEntry(node_a=na, node_b=nb, bases=frozenset(cross_bases[(na, nb)]),
                       cohorts=tuple(x for x in labels if x in cross_cohorts[(na, nb)]),
                       n_row_pairs=cross_n[(na, nb)])
        for na, nb in sorted(cross_n)
    )
    return NwayResult(
        labels=labels,
        link_by_name=link_by_name,
        rows=tuple(rows),
        groups=groups,
        cross_node=cross_node,
        withheld=tuple(withheld),
        pair_counts=tuple(pair_counts),
        drilldown=None if drilldown_sink is not None else tuple(held),
    )
