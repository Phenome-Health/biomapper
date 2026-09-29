"""Unit 3 — Arm-B baseline reconstruction (Monti et al. 2026's published per-pair method).

Ported from the biomapper2 engine at ``origin/dev``
(``studies/external_benchmarks/scorers/arm_b_baseline.py``, commit
``1ffb571e54fe028ef0ae4e748fc2e7ec093ee603``). The reconstruction logic is unchanged.

**The published-overlap table is sourced from the SUPPLEMENT'S TABLE, not the paper's prose, and
that is the point of this docstring.** An earlier pass re-derived these from the Methods narrative
and moved two of the four (Xu to 385, BLSA to 188). Both moves were wrong: supplement MOESM6's
"Table 2. Datasets" carries an explicit ``# Overlap`` column, Table S03 corroborates it
independently, and both agree with the original values. The prose loses to the table. The
prose-derived values are retained in :data:`MONTI_PUBLISHED_SUPERSEDED` so the reversal stays
traceable, and :data:`MONTI_PUBLISHED_PROVENANCE` records where each number comes from and what
disagrees with it.

Arm B is the number BioMapper is measured against. It is reconstructed here on the IDENTICAL row
set the M/M+ID arms use, so the comparison is like-for-like (R21a). Two methods, per the paper's
own ``Datasets harmonization`` text:

  - NECS↔Arivale, NECS↔Xu : Metabolon ``CHEMICAL_NAME`` string match (Arivale case-insensitive,
    Xu case-sensitive — the settings that reproduced the paper in the 2026-08-20 replication).
  - NECS↔LLFS, NECS↔BLSA  : RefMet standardized-name join, with ``drop_na(refmet_name)`` applied
    BEFORE the join (a name that does not RefMet-standardize cannot match).

Because WE build this baseline, it is a controlled variable: it must be **frozen** (locked as a
characterization test at a recorded commit) before any BioMapper live run, and the recovery claim
must beat **Monti-published** — the number we did NOT compute — by more than the per-pair
``|re-derived − published|`` reconstruction gap. This module exposes that gap on every result.

Fully offline: pure set logic over name lists and a precomputed RefMet map (a dict, loaded by the
caller from the persisted cache — never a network call here).
"""

from __future__ import annotations

from dataclasses import dataclass

# Monti et al. 2026 published NECS<->cohort overlaps (the un-gamed comparator; we did not compute
# these). These are the TABULATED values, which is the point: an earlier pass derived them from the
# Methods prose and got two of the four wrong. The prose loses to the table.
#
# Primary source: supplement MOESM6, sheet "Table 2. Datasets", which carries an explicit
# ``# Overlap`` column with NECS as the reference row (NECS's own cell is blank because it is the
# reference). Read directly:
#
#     Dataset (Platform)          # Subjects  # Metabolites  # Overlap
#     NECS (Metabolon)                   213           1213          -
#     LLFS (MS)                         2764            408        163
#     Arivale (Metabolon)                634            626        615
#     BLSA (Biocrates)                  1135            468         99
#     Xu et al., '22 (Metabolon)         382            821        432
#
# Corroborated independently by supplement Table S03 (signatures), 1052 metabolite rows: counting
# rows with any non-null statistic in each cohort's columns derives xuetal = 432 EXACTLY, llfs = 162
# against a published 163, and blsa = 88. 88 is 11 short of 99 and 100 short of 188, so it places
# BLSA at 99 and rules 188 out.
#
# What the earlier pass got wrong, recorded because the reasoning was superficially good:
#
#   xuetal: reached 385 from the Methods sentence "we used the Metabolon-provided CHEMICAL_NAME's,
#           which yielded an overlap of 615 and 385 metabolites, respectively". Both the overlap
#           table and the S03 derivation say 432, and the Xu cohort description says 432. The prose
#           "385" is the lone outlier and is not the number to reconstruct against.
#
#   blsa:   reached 188 from "When comparing NECS with LLFS and BLSA, we mapped all metabolites to
#           RefMet identifiers, which yielded an overlap of 163 and 188 metabolites, respectively".
#           The table says 99. **188 is the paper's own count of LIPID metabolites in the LLFS
#           panel** ("408 metabolites (188 lipid and 220 polar)"), which now explains where the
#           prose number came from and why it was the wrong one to reach for: the sentence appears
#           to have carried the LLFS lipid count. Keeping this note visible is the point, because
#           the coincidence is the evidence.
#
# A note on the citation, since an earlier pass also overstated this: the MAIN TEXT's Table 2 is
# indeed "Age-only markers" and is not an overlap table. The overlaps are in the SUPPLEMENT's
# "Table 2. Datasets" (MOESM6). Both statements are true and the earlier one was only half right.
MONTI_PUBLISHED: dict[str, int] = {"arivale": 615, "xuetal": 432, "llfs": 163, "blsa": 99}

# Values a prose-derived pass briefly used, kept so the reversal stays traceable rather than looking
# like the table had always been consulted.
MONTI_PUBLISHED_SUPERSEDED: dict[str, int] = {
    "arivale": 615,
    "xuetal": 385,
    "llfs": 163,
    "blsa": 188,
}

# Per-cohort counts derived from supplement Table S03 over its 1052 metabolite rows, recorded
# alongside the published overlaps as the independent corroboration. Not a substitute for the
# published number: S03 counts metabolites carrying a statistic, which is a related but distinct
# quantity, hence llfs 162 against a published 163 and blsa 88 against 99.
MONTI_S03_DERIVED: dict[str, int] = {"xuetal": 432, "llfs": 162, "blsa": 88}

# Per-pair citation for every published value, so a manifest can carry the source rather than a bare
# integer. ``conflict`` records where the paper's PROSE disagrees with its own table.
MONTI_PUBLISHED_PROVENANCE: dict[str, dict[str, object]] = {
    "arivale": {
        "published": 615,
        "source": "supplement MOESM6, sheet 'Table 2. Datasets', '# Overlap' column",
        "s03_derived": None,
        "corroborated_by": (
            "Methods prose and the Arivale cohort description both also say 615: 'After name "
            "curation, 615 metabolites were matched to metabolites in the NECS dataset'"
        ),
        "conflict": None,
    },
    "xuetal": {
        "published": 432,
        "source": "supplement MOESM6, sheet 'Table 2. Datasets', '# Overlap' column",
        "s03_derived": 432,
        "corroborated_by": (
            "Table S03 derives exactly 432, and the Xu cohort description says '432 metabolites "
            "were matched to metabolites in the NECS dataset'"
        ),
        "conflict": {
            "prose_value": 385,
            "quote": "which yielded an overlap of 615 and 385 metabolites, respectively",
            "section": "Methods, 'Datasets harmonization'",
            "resolution": (
                "the table, the S03 derivation and the cohort description all say 432; the prose "
                "385 is the lone outlier and is not used"
            ),
        },
    },
    "llfs": {
        "published": 163,
        "source": "supplement MOESM6, sheet 'Table 2. Datasets', '# Overlap' column",
        "s03_derived": 162,
        "corroborated_by": (
            "Methods prose and the LLFS cohort description both also say 163; Table S03 derives "
            "162, one short, consistent with S03 counting metabolites that carry a statistic"
        ),
        "conflict": None,
    },
    "blsa": {
        "published": 99,
        "source": "supplement MOESM6, sheet 'Table 2. Datasets', '# Overlap' column",
        "s03_derived": 88,
        "corroborated_by": (
            "Table S03 derives 88, which is 11 from 99 and 100 from 188, placing BLSA at 99"
        ),
        "conflict": {
            "prose_value": 188,
            "quote": "which yielded an overlap of 163 and 188 metabolites, respectively",
            "section": "Methods, 'Datasets harmonization'",
            "resolution": (
                "the table says 99 and S03 derives 88. 188 is the paper's own count of LIPID "
                "metabolites in the LLFS panel ('408 metabolites (188 lipid and 220 polar)'), so "
                "the prose sentence appears to have carried the LLFS lipid count. That coincidence "
                "is the evidence and must stay visible."
            ),
        },
    },
}

# Per-pair method: ("name", case_sensitive) or ("refmet",). Unknown cohort → fail loud.
# The union type is spelled out rather than left as ``tuple[str, ...]`` (the engine's annotation) so
# the ``case_sensitive`` element keeps its bool type through to ``name_match_overlap``.
PAIR_METHOD: dict[str, tuple[str, bool] | tuple[str]] = {
    "arivale": ("name", False),
    "xuetal": ("name", True),
    "llfs": ("refmet",),
    "blsa": ("refmet",),
}


@dataclass(frozen=True)
class ArmBResult:
    cohort: str
    method: str
    count: int  # re-derived overlap on the identical row set
    published: int  # Monti Table 2
    gap: int  # count - published; the error bar recovery must exceed


def _name_key(name: str, case_sensitive: bool) -> str:
    s = name.strip()
    return s if case_sensitive else s.lower()


def name_match_overlap(names_a: list[str], names_b: list[str], *, case_sensitive: bool) -> set[str]:
    """Overlap by exact CHEMICAL_NAME string match (Arm B for same-vendor pairs)."""
    a = {_name_key(n, case_sensitive) for n in names_a if n.strip()}
    b = {_name_key(n, case_sensitive) for n in names_b if n.strip()}
    return a & b


def refmet_join_overlap(
    names_a: list[str],
    names_b: list[str],
    refmet_map: dict[str, str],
) -> set[str]:
    """Overlap by RefMet standardized-name join, drop_na(refmet_name) BEFORE the join.

    ``refmet_map`` maps a raw name to its RefMet name; a name absent from the map or mapping to
    an empty string does not standardize and is dropped before the intersection (exactly Monti's
    ``drop_na`` step — a non-standardizing name can never match).
    """

    def refmet_set(names: list[str]) -> set[str]:
        out: set[str] = set()
        for n in names:
            r = refmet_map.get(n.strip(), "").strip()
            if r:
                out.add(r.lower())
        return out

    return refmet_set(names_a) & refmet_set(names_b)


def arm_b_overlap(
    cohort: str,
    necs_names: list[str],
    cohort_names: list[str],
    *,
    refmet_map: dict[str, str] | None = None,
) -> ArmBResult:
    """Compute Arm B for one NECS↔cohort pair using the cohort's published method. Fails loud on
    an unknown cohort (never defaults to name-match)."""
    if cohort not in PAIR_METHOD:
        raise ValueError(
            f"no Arm-B method registered for cohort {cohort!r}; known: {sorted(PAIR_METHOD)}"
        )
    method_name, *rest = PAIR_METHOD[cohort]
    if method_name == "name":
        case_sensitive = bool(rest[0]) if rest else False
        matched = name_match_overlap(necs_names, cohort_names, case_sensitive=case_sensitive)
        method = f"CHEMICAL_NAME ({'case-sensitive' if case_sensitive else 'case-insensitive'})"
    else:
        if refmet_map is None:
            raise ValueError(
                f"cohort {cohort!r} uses the RefMet method but no refmet_map was provided"
            )
        matched = refmet_join_overlap(necs_names, cohort_names, refmet_map)
        method = "RefMet join (drop_na before join)"
    published = MONTI_PUBLISHED[cohort]
    return ArmBResult(
        cohort=cohort,
        method=method,
        count=len(matched),
        published=published,
        gap=len(matched) - published,
    )
