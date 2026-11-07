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

Every link also records how many partners each side has (``a_partners`` / ``b_partners``). A link
whose either side has more than one partner is ``ambiguous``: several distinct entities landed on
the same entry, so the pair is a candidate for review rather than a confident match. Measured on
NECS x Xu et al. 2022 against Metabolon compound IDs (2026-10-27, engine cdd05e5): all links 439 of
575 true (76.3% precision, 98.4% recall); one-to-one links only 397 of 398 (99.7%, recall 89.0%);
the 177 ambiguous links held 42 true matches.

Every link also records the Metabolon identification-confidence marker on each side's name
(``a_id_confidence`` / ``b_id_confidence``, see :func:`id_confidence_marker`). A link whose two
names carry different markers (``glyco-beta-muricholate**`` vs ``glyco-beta-muricholate``) has
``id_confidence_differs``: the two cohorts identified the compound with different confidence, so a
person should look at it even when the link is one-to-one. ``needs_review`` is ``ambiguous`` OR
``id_confidence_differs`` OR ``mixture``; ``ambiguous`` itself is unchanged.

Every link also records whether each side's name is a MIXTURE label (``a_is_mixture`` /
``b_is_mixture``, see :func:`is_mixture_name`): a name that lists alternative compounds, such as
``butyrate/isobutyrate (4:0)`` or ``dihomo-linolenate (20:3n3 or n6)``. The measured compound is
one of several. A link's ``mixture`` flag is set when the two sides DIFFER as mixtures: exactly one
side is a mixture, or both are and list different alternatives (compared order-insensitively by
:func:`mixture_alternatives_key`). Such a link is a review candidate even when it is one-to-one;
two names listing the same alternatives are not flagged.

Finally, :attr:`HarmonizationResult.possible_missed` lists the entities that ended up in NO link
but whose mapping already carries a review signal (a ``chosen_kg_id_review`` value, a
``contradicted`` certificate, or a mixture name). Such an entity may be a missed match rather than
a cohort-specific compound, so it is shown to a reviewer next to the review links instead of
disappearing silently.
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

# Metabolon marks how confidently a compound was identified with trailing asterisks on its name:
# none = confirmed with an authentic chemical standard, "*" = identified with confidence but not
# confirmed with a standard, "**" = reasonably identified without an available standard. A marker
# is a run of exactly one or two asterisks that directly follows a non-space character and either
# ends the name (``glyco-beta-muricholate**``) or is followed by " (" (``1-palmitoyl-gpi* (16:0)``).
# Any other asterisk (a run of three or more, a leading or space-separated asterisk, one inside a
# word) is not a marker and is ignored.
_ID_CONFIDENCE_MARKER = re.compile(r"(?<=[^\s*])\*{1,2}(?=$| \()")

# Mixture labels: a name that lists alternative compounds (see is_mixture_name for the rule).
# " or " / " + " as whole words with a space on each side.
_MIXTURE_OR = re.compile(r"(?:^|\s)or\s")
_MIXTURE_PLUS = re.compile(r"\s\+\s")
# A "/" between two compound names: at least three letters right before it, and right after it a
# letter or a locant ("2-", "5alpha-"). Lipid chain notation ("18:2/20:4", "d18:1/17:0",
# "16:0(oh)/18:1") always has a digit or ")" right before the slash, so it never matches.
_MIXTURE_SLASH = re.compile(r"(?<=[a-z]{3})/(?=([a-z]+)|\d+[a-z]*-)")
# Unit words after a slash ("mmol/mol", "cells/ul") mean a unit, not an alternative name.
_SLASH_UNIT_WORDS: frozenset[str] = frozenset(
    {"l", "dl", "ml", "ul", "mol", "mmol", "umol", "min", "h", "hr", "day", "kg", "g", "mg"}
)
# One lipid chain specification, as an item of a comma-separated list: optional one-letter
# prefix ("d18:1", "t18:0") or ether prefix ("O-16:0", "P-18:0"), then carbons:double bonds.
_CHAIN_ITEM = re.compile(r"^(?:[a-z]|[op]-)?\d+:\d+")


def normalize_name(name: str) -> str:
    """Whitespace normalization only: strip, and collapse internal runs to one space."""
    return _WS.sub(" ", name).strip()


def id_confidence_marker(name: str) -> int:
    """Return the Metabolon identification-confidence marker on ``name``: 0, 1 or 2 asterisks.

    0 means the name carries no marker (for a Metabolon name: confirmed with an authentic
    standard; for any other name: nothing is claimed). 1 is ``*`` and 2 is ``**`` (see
    ``_ID_CONFIDENCE_MARKER`` for exactly what counts). When a name carries more than one marker,
    the longest wins: Xu et al. write ``1-arachidonoyl-GPC* (20:4)*`` for the compound other
    cohorts name ``1-arachidonoyl-GPC (20:4n6)*``, so the two single markers are one ``*``, not
    ``**``. The name is whitespace-normalized first.
    """
    return max((len(m) for m in _ID_CONFIDENCE_MARKER.findall(normalize_name(name))), default=0)


def is_mixture_name(name: str) -> bool:
    """True when ``name`` is a mixture label: it lists alternative compounds, not one compound.

    The name is whitespace-normalized and casefolded, then any of these makes it a mixture:

    1. ``or`` as a whole word between spaces: ``linolenate [alpha or gamma; (18:3n3 or 6)]``,
       ``dihomo-linolenate (20:3n3 or n6)``.
    2. ``+`` with a space on each side: ``dimethylarginine (sdma + adma)``,
       ``(n(1) + n(8))-acetylspermidine``. ``NAD+`` or ``Ca2+`` (no space after) are not.
    3. ``/`` between two compound names: at least three letters right before the slash, and a
       letter or a numeric locant (``2-``, ``5alpha-``) right after it, as in
       ``butyrate/isobutyrate (4:0)`` and ``2-hydroxybutyrate/2-hydroxyisobutyrate``. Lipid chain
       notation (``PC 16:0/18:1``, ``d18:1/17:0``, ``16:0(oh)/18:1``) has a digit or ``)`` before
       the slash and never counts. A slash followed by a unit word (``mmol/mol``) does not count,
       and no slash counts in a name containing the word ``ratio`` (``albumin/creatinine ratio``).
    4. A parenthetical or bracketed group that is a comma-separated list of two or more lipid
       chain specifications (Metabolon's multi-isomer names):
       ``sphingomyelin (d18:1/17:0, d17:1/18:0, d19:1/16:0)``. A comma list of anything else
       (``androstenediol (3alpha, 17alpha) monosulfate``, ``bilirubin (z,z)``) does not count.

    The rule is written against Metabolon-style metabolite names. It reads the name only; an
    engine-provided mixture flag (``lipid_resolution.name_is_mixture``) is combined with it in
    :func:`harmonize`.
    """
    text = normalize_name(name).casefold()
    if _MIXTURE_OR.search(text) or _MIXTURE_PLUS.search(text):
        return True
    if not re.search(r"\bratio\b", text):
        for m in _MIXTURE_SLASH.finditer(text):
            if m.group(1) is None or m.group(1) not in _SLASH_UNIT_WORDS:
                return True
    for items in _group_items(text):
        if len(items) >= 2 and all(_CHAIN_ITEM.match(item) for item in items):
            return True
    return False


def _group_items(text: str) -> list[list[str]]:
    """Every parenthetical or bracketed group in ``text``, split on its own top-level commas.

    Nested groups are their own entries, and a comma inside a nested group does not split the
    outer one: ``(16:1/18:2 [2], 16:0/18:3 [1])`` gives the items ``16:1/18:2 [2]`` and
    ``16:0/18:3 [1]``. An unbalanced bracket ends nothing and is ignored.
    """
    groups: list[list[str]] = []
    # Per open group: its items so far, and the start of the item being read.
    stack: list[tuple[list[str], int]] = []
    for i, ch in enumerate(text):
        if ch in "([":
            stack.append(([], i + 1))
        elif ch in ")]" and stack:
            items, start = stack.pop()
            groups.append([*items, text[start:i]])
        elif ch == "," and stack:
            items, start = stack[-1]
            items.append(text[start:i])
            stack[-1] = (items, i + 1)
    return [[item.strip() for item in g] for g in groups]


# One separator between alternatives inside one bracket level (see mixture_alternatives_key):
# "or" as a word (with any comma before it), " + ", or a "/" between compound names.
_ALT_SEPARATOR = re.compile(
    r"(?P<or>,?\s*(?:(?<=\s)|^)or\s+)"
    r"|(?P<plus>\s\+\s)"
    r"|(?P<slash>(?<=[a-z]{3})/(?=(?P<word>[a-z]+)|\d+[a-z]*-))"
    r"|(?P<comma>,\s*)"
)


def mixture_alternatives_key(name: str) -> str:
    """An order-insensitive form of ``name``'s alternatives, for comparing two mixture names.

    The name is whitespace-normalized and casefolded, and its Metabolon identification-confidence
    markers are removed (``id_confidence_differs`` reports those). Then, in every bracket level
    separately (``(...)`` and ``[...]`` are treated alike), the text is split into alternatives at
    the separators :func:`is_mixture_name` recognizes: ``or`` as a word (with a comma before it),
    `` + ``, a ``/`` between compound names (same letter, locant and unit conditions, and no slash
    in a ``ratio`` name), and commas when that level contains ``or`` or is a list of two or more
    lipid chains. The alternatives are sorted and rejoined, nested levels first.

    So ``dimethylarginine (sdma + adma)`` and ``dimethylarginine (adma + sdma)`` give the same key,
    as do ``aconitate [cis or trans]`` and ``aconitate (trans or cis)``. Anything else that differs
    (a spelling, an extra alternative, ``(20:3n3 or n6)`` vs ``(20:3n3 or 3n6)``) gives different
    keys. Alternatives are only reordered within their own bracket level, so moving a shared
    qualifier between levels (``butyrate/isobutyrate (4:0)`` vs ``isobutyrate (4:0)/butyrate``)
    also gives different keys; that errs toward review.
    """
    text = _ID_CONFIDENCE_MARKER.sub("", normalize_name(name).casefold())
    return _canonical_level(text, allow_slash=not re.search(r"\bratio\b", text))


def _canonical_level(text: str, *, allow_slash: bool) -> str:
    """Canonicalize one bracket level of ``text`` (see :func:`mixture_alternatives_key`)."""
    # Replace each top-level bracket group with a placeholder holding its canonical form.
    parts: list[str] = []
    groups: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(text):
        if ch in "([":
            if depth == 0:
                start = i
            depth += 1
        elif ch in ")]" and depth:
            depth -= 1
            if depth == 0:
                groups.append(
                    "(" + _canonical_level(text[start + 1 : i], allow_slash=allow_slash) + ")"
                )
                parts.append(f"\x00{len(groups) - 1}\x00")
        elif depth == 0:
            parts.append(ch)
    if depth:  # unbalanced: keep the unclosed remainder literally
        parts.append(text[start:])
    level = "".join(parts)

    split_commas = bool(re.search(r"(?:^|\s)or\s", level))
    if not split_commas:
        comma_items = [item.strip() for item in level.split(",")]
        split_commas = len(comma_items) >= 2 and all(_CHAIN_ITEM.match(i) for i in comma_items)
    items: list[str] = []
    pos = 0
    for m in _ALT_SEPARATOR.finditer(level):
        if m.group("comma") is not None and not split_commas:
            continue
        if m.group("slash") is not None and (
            not allow_slash or (m.group("word") or "") in _SLASH_UNIT_WORDS
        ):
            continue
        items.append(level[pos : m.start()])
        pos = m.end()
    items.append(level[pos:])

    def restore(item: str) -> str:
        return re.sub(r"\x00(\d+)\x00", lambda g: groups[int(g.group(1))], item).strip()

    restored = [restore(item) for item in items]
    return " | ".join(sorted(restored)) if len(restored) > 1 else restored[0]


@dataclass(frozen=True)
class Link:
    """One harmonized pair.

    ``shared`` carries the CURIE(s) that formed an identifier link, for audit (empty for a link
    formed by name alone). ``basis`` is the strongest basis that applies (see ``BASIS_ORDER``) and
    ``bases`` is every basis that applies. ``a_partners`` is how many B-side entities ``a_key``
    links to in the same result, and ``b_partners`` how many A-side entities ``b_key`` links to.
    ``a_id_confidence`` / ``b_id_confidence`` are the Metabolon identification-confidence markers
    on each side's name (0, 1 or 2 asterisks; see :func:`id_confidence_marker`). :func:`harmonize`
    sets them from each result's ``query_name``; a link built without names keeps 0 on both sides.
    ``a_is_mixture`` / ``b_is_mixture`` say whether each side's name is a mixture label (see
    :func:`is_mixture_name`, or the engine's ``name_is_mixture`` flag when it reports one), and
    ``a_mixture_key`` / ``b_mixture_key`` hold each mixture side's
    :func:`mixture_alternatives_key` (empty for a side that is not a mixture). A link built without
    names keeps ``False`` and ``""`` on both sides.
    """

    a_key: str
    b_key: str
    shared: frozenset[str]
    basis: str = "identifier"
    bases: frozenset[str] = frozenset({"identifier"})
    a_partners: int = 1
    b_partners: int = 1
    a_id_confidence: int = 0
    b_id_confidence: int = 0
    a_is_mixture: bool = False
    b_is_mixture: bool = False
    a_mixture_key: str = ""
    b_mixture_key: str = ""

    @property
    def name_only(self) -> bool:
        """True when no identifier evidence supports the link, only a name match."""
        return not (self.bases & {"node", "identifier"})

    @property
    def ambiguous(self) -> bool:
        """True when either side links to more than one entity: a review candidate, not a match.

        Typical cause: distinct entities in one cohort resolved to the same entry (a lipid class,
        cis/trans or mono/disulfate forms on one node), so each links to all of them.
        """
        return self.a_partners > 1 or self.b_partners > 1

    @property
    def id_confidence_differs(self) -> bool:
        """True when the two names carry different Metabolon identification-confidence markers.

        ``glyco-beta-muricholate**`` vs ``glyco-beta-muricholate``: one cohort confirmed the
        compound with a standard and the other did not. Two names without markers never differ,
        so panels that do not use Metabolon markers are unaffected.
        """
        return self.a_id_confidence != self.b_id_confidence

    @property
    def mixture(self) -> bool:
        """True when the two sides differ as mixtures: the ``mixture`` review reason.

        Exactly one side is a mixture label (``butyrate/isobutyrate (4:0)`` vs ``butyrate (4:0)``:
        the measured peak may be isobutyrate), OR both are and their alternative sets differ
        (``a_mixture_key != b_mixture_key``, as in ``dihomo-linolenate (20:3n3 or n6)`` vs
        ``dihomolinolenate (20:3n3 or 3n6)``). Two mixture names that list the same alternatives,
        in any order (``dimethylarginine (sdma + adma)`` vs ``dimethylarginine (adma + sdma)``),
        describe the same measurement and are not flagged.
        """
        if self.a_is_mixture != self.b_is_mixture:
            return True
        return self.a_is_mixture and self.a_mixture_key != self.b_mixture_key

    @property
    def review_reasons(self) -> tuple[str, ...]:
        """Why this link needs review, in a fixed order; empty for a confident match.

        Any of ``"ambiguous"``, ``"id_confidence_differs"`` and ``"mixture"``.
        """
        flags = (
            ("ambiguous", self.ambiguous),
            ("id_confidence_differs", self.id_confidence_differs),
            ("mixture", self.mixture),
        )
        return tuple(reason for reason, on in flags if on)

    @property
    def needs_review(self) -> bool:
        """True when a person should look at it: ``ambiguous``, ``id_confidence_differs`` or
        ``mixture``."""
        return bool(self.review_reasons)


def with_partner_counts(links: Iterable[Link]) -> tuple[Link, ...]:
    """Return the links with ``a_partners`` / ``b_partners`` set from this set of links.

    The counts depend on the whole set, so they are computed once, after every link is known;
    a link's flag is never a property of the pair alone.
    """
    materialized = tuple(links)
    a_count = Counter(lk.a_key for lk in materialized)
    b_count = Counter(lk.b_key for lk in materialized)
    return tuple(
        replace(lk, a_partners=a_count[lk.a_key], b_partners=b_count[lk.b_key])
        for lk in materialized
    )


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

    links = with_partner_counts(
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
    {
        "n_links",
        "links_by_basis",
        "n_name_only_links",
        "n_name_match_withheld",
        "n_one_to_one_links",
        "n_ambiguous_links",
        "n_id_confidence_differs",
        "n_mixture_links",
        "n_needs_review",
        "n_possible_missed",
    }
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


# Certificate states that count as a review signal on an unlinked entity.
_REVIEW_CERTIFICATE_STATES: frozenset[str] = frozenset({"contradicted"})


@dataclass(frozen=True)
class PossibleMiss:
    """An entity that ended up in NO link although its mapping carries a review signal.

    ``cohort`` is the side's label, ``key`` its key in the result and ``name`` its ``query_name``.
    ``resolved`` is whether it resolved to any identifier (an unresolved entity is also listed in
    ``a_unresolved`` / ``b_unresolved``). ``reasons`` is one or more of, in this order:

    - the mapping's ``chosen_kg_id_review`` value itself (``"divergent_refmet"``,
      ``"conflict_no_structure"``, or whatever value the engine sends);
    - ``"certificate_contradicted"``: the resolution certificate's state is ``contradicted``;
    - ``"mixture"``: the name is a mixture label (see :func:`is_mixture_name`) or the engine set
      ``name_is_mixture``.
    """

    cohort: str
    key: str
    name: str
    chosen_kg_id: str | None
    resolved: bool
    reasons: tuple[str, ...]


def mapping_review_signals(result: MappingResult) -> tuple[str, ...]:
    """The review signals one mapping result carries, as :attr:`PossibleMiss.reasons` names them."""
    reasons: list[str] = []
    if result.chosen_kg_id_review:
        reasons.append(result.chosen_kg_id_review)
    if result.certificate is not None and result.certificate.state in _REVIEW_CERTIFICATE_STATES:
        reasons.append(f"certificate_{result.certificate.state}")
    if _result_is_mixture(result):
        reasons.append("mixture")
    return tuple(reasons)


def _result_is_mixture(result: MappingResult) -> bool:
    """The name rule, OR the engine's mixture flag when the engine sent one.

    The engine reports it as ``lipid_resolution.name_is_mixture`` (engine v1.3.1+, lipid names
    only); a top-level ``name_is_mixture`` is read too. An engine ``True`` adds a mixture the name
    rule missed. An engine ``False`` does not clear the name rule: a false mixture flag costs one
    review, a missed one hides a possible wrong match.
    """
    lipid = result.lipid_resolution
    return (
        is_mixture_name(result.query_name)
        or result.name_is_mixture is True
        or (lipid is not None and lipid.name_is_mixture is True)
    )


def _mixture_key(result: MappingResult) -> str:
    return mixture_alternatives_key(result.query_name) if _result_is_mixture(result) else ""


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
    # Entities in no link whose mapping carries a review signal (see PossibleMiss), A side first,
    # each side in input order.
    possible_missed: tuple[PossibleMiss, ...] = ()

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

    @property
    def one_to_one_links(self) -> tuple[Link, ...]:
        """Links where each side has exactly one partner (not ``ambiguous``).

        A one-to-one link can still need review when its names carry different identification
        confidence markers; :attr:`review_links` is the full set a person should look at.
        """
        return tuple(lk for lk in self.links if not lk.ambiguous)

    @property
    def ambiguous_links(self) -> tuple[Link, ...]:
        """Links where either side has several partners: candidates for review, not matches."""
        return tuple(lk for lk in self.links if lk.ambiguous)

    @property
    def review_links(self) -> tuple[Link, ...]:
        """Links that need review: ``ambiguous``, ``id_confidence_differs`` or ``mixture``.

        The links NOT in this set are the confident matches. Look at :attr:`possible_missed`
        alongside them: those are the entities that might belong in a link but are in none.
        """
        return tuple(lk for lk in self.links if lk.needs_review)

    @property
    def mixture_links(self) -> tuple[Link, ...]:
        """Links whose sides differ as mixtures (see :attr:`Link.mixture`)."""
        return tuple(lk for lk in self.links if lk.mixture)

    @property
    def a_possible_missed(self) -> tuple[PossibleMiss, ...]:
        return tuple(m for m in self.possible_missed if m.cohort == self.a_label)

    @property
    def b_possible_missed(self) -> tuple[PossibleMiss, ...]:
        return tuple(m for m in self.possible_missed if m.cohort == self.b_label)

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
            "n_one_to_one_links": len(self.one_to_one_links),
            "n_ambiguous_links": len(self.ambiguous_links),
            "n_id_confidence_differs": sum(1 for lk in self.links if lk.id_confidence_differs),
            "n_mixture_links": len(self.mixture_links),
            "n_needs_review": len(self.review_links),
            "n_possible_missed": len(self.possible_missed),
            self.a_label: {
                "total": self.n_a_total,
                "comparable": self.n_a_comparable,
                "unresolved": self.n_a_unresolved,
                "errors": self.n_a_errors,
                "linked": self.n_a_linked,
                "link_rate": self.a_link_rate,
                "possible_missed": len(self.a_possible_missed),
            },
            self.b_label: {
                "total": self.n_b_total,
                "comparable": self.n_b_comparable,
                "unresolved": self.n_b_unresolved,
                "errors": self.n_b_errors,
                "linked": self.n_b_linked,
                "link_rate": self.b_link_rate,
                "possible_missed": len(self.b_possible_missed),
            },
        }


def _partition_side(
    results: Sequence[MappingResult],
    key: Callable[[MappingResult, int], str] | None,
) -> tuple[
    dict[str, frozenset[str]], tuple[str, ...], dict[str, str], dict[str, MappingResult]
]:
    """Split one cohort into (linkable CURIE sets, errored keys, chosen nodes, results by key),
    keying each row exactly once.

    Uniqueness is enforced across errored AND resolved rows together. Checking only the resolved
    subset would let one key appear in both ``a_errors`` and a link while describing two different
    input entities. An error is not a non-resolution, so the two are partitioned, not merged.
    """
    keys = _keys_for(results, key)
    _require_unique(keys)
    curies: dict[str, frozenset[str]] = {}
    errored: list[str] = []
    nodes: dict[str, str] = {}
    by_key: dict[str, MappingResult] = {}
    for k, r in zip(keys, results, strict=True):
        if r.error:
            errored.append(k)
        else:
            curies[k] = curie_set(r.chosen_kg_id, r.kg_equivalent_ids)
            by_key[k] = r
            if r.chosen_kg_id:
                nodes[k] = r.chosen_kg_id
    return curies, tuple(errored), nodes, by_key


def _possible_missed(
    label: str,
    by_key: dict[str, MappingResult],
    curies: dict[str, frozenset[str]],
    linked: set[str],
) -> list[PossibleMiss]:
    """Non-errored entities of one side that are in no link and carry a review signal."""
    out: list[PossibleMiss] = []
    for k, r in by_key.items():
        if k in linked:
            continue
        reasons = mapping_review_signals(r)
        if reasons:
            out.append(
                PossibleMiss(
                    cohort=label,
                    key=k,
                    name=r.query_name,
                    chosen_kg_id=r.chosen_kg_id,
                    resolved=bool(curies[k]),
                    reasons=reasons,
                )
            )
    return out


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
        errored in ``a_errors`` / ``b_errors``. Neither is silently dropped. Each link carries its
        partner counts, each side's identification-confidence marker and whether each side's
        name is a mixture label. ``ambiguous_links`` (either side has several partners), links
        whose names carry different markers (``id_confidence_differs``) and links whose sides
        differ as mixtures (``mixture``: one side only, or different alternative sets) together
        form ``review_links``; every other link is a
        confident match. All links are still returned. ``possible_missed`` lists the entities in
        no link whose mapping carries a review signal (see :class:`PossibleMiss`).

    Raises:
        ValueError: If either side has duplicate keys (see ``key``), or if ``a_label`` and
            ``b_label`` are equal or collide with a reserved ``summary()`` field.
    """
    _require_distinct_labels(a_label, b_label)
    a_curies, a_errors, a_nodes, a_by_key = _partition_side(a_results, key)
    b_curies, b_errors, b_nodes, b_by_key = _partition_side(b_results, key)
    a_names = {k: r.query_name for k, r in a_by_key.items()}
    b_names = {k: r.query_name for k, r in b_by_key.items()}
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

    links = with_partner_counts(
        Link(
            a_key=a,
            b_key=b,
            shared=shared.get((a, b), frozenset()),
            basis=next(x for x in BASIS_ORDER if x in bases[(a, b)]),
            bases=frozenset(bases[(a, b)]),
            a_id_confidence=id_confidence_marker(a_names[a]),
            b_id_confidence=id_confidence_marker(b_names[b]),
            a_is_mixture=_result_is_mixture(a_by_key[a]),
            b_is_mixture=_result_is_mixture(b_by_key[b]),
            a_mixture_key=_mixture_key(a_by_key[a]),
            b_mixture_key=_mixture_key(b_by_key[b]),
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
    possible_missed = (
        *_possible_missed(a_label, a_by_key, a_curies, {lk.a_key for lk in links}),
        *_possible_missed(b_label, b_by_key, b_curies, {lk.b_key for lk in links}),
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
        possible_missed=possible_missed,
    )
