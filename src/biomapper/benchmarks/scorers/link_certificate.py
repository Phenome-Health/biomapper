"""Unit 5 — KG-independent structural certificate for a cross-cohort link.

Ported verbatim from the biomapper2 engine at ``origin/dev``
(``studies/external_benchmarks/scorers/link_certificate.py``, commit
``1ffb571e54fe028ef0ae4e748fc2e7ec093ee603``). No engine imports, so nothing changed but the path.

Read this alongside the API's ``ResolutionCertificateModel``, which is a DIFFERENT instrument and
must not be confused with it. The API certificate reads the chosen KRAKEN node's own InChIKey first
and only falls back to an external source, so it is NOT fully KG-independent. This module is the
one that refuses rather than certifying off the KG, and ``certify_link`` will refuse a side tagged
``source="kg"`` outright.

The comparison is also not a clean oracle in either instrument: an InChIKey first block is neither
tautomer- nor charge-invariant, so a first-block comparison over-flags tautomers as refuted while
silently passing stereoisomer errors whenever a side lacks block 2. That is why
``LinkCertificate.stereo_checked`` exists and why a ``refuted`` verdict is a candidate for
re-adjudication against an outside source, not a finding on its own.

A link is asserted by CURIE-set intersection (Unit 2), i.e. because both cohorts' metabolites
resolved — via the SAME Kestrel KG — toward an intersecting CURIE. Certifying that link by reading
that same KG node's InChIKey would be **circular**: a shared CURIE mechanically implies a shared
KG-InChIKey, so the certificate would carry no information (the exact Deliverable-1 failure:
"reads KRAKEN's own InChIKey first ... NOT KG-independent").

So this module certifies a link ONLY by comparing two structures that are each resolved
**independently of the KG node that formed the link** (R6a):

  - NECS side   : the repaired gold InChIKey / SMILES (Deliverable 1).
  - cohort side : ``independent_inchikey`` (PubChem PUG-REST) from the cohort's vendor id.

The API deliberately never accepts the linking CURIE's KG-node InChIKey. A link whose cohort side
has no independent structure (BLSA sum-composition, LLFS formula/mass, or a PubChem lookup failure)
is **REFUSED** — counts-only — never certified off the KG (R6b). Refusal is a first-class verdict.

Certificate key = ``block1 + "-" + block2[:8]`` (block-1 alone merges 11 groups this domain must
keep apart). When a side carries only a first-block InChIKey (the PubChem resolver's current
granularity), the comparison degrades to connectivity-only and says so (``stereo_checked=False``) —
never a silent stereo pass.

Pure/offline: it operates on InChIKey strings passed in. Resolving the cohort-side InChIKey from
vendor ids (network) is the caller's gated step; tests pass keys directly / monkeypatch the fetch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Verdict = Literal["certified", "refuted", "refused"]


@dataclass(frozen=True)
class CertificateKey:
    connectivity: str | None  # InChIKey block 1
    stereo8: str | None  # first 8 chars of block 2, or None if the key is first-block-only


@dataclass(frozen=True)
class LinkCertificate:
    verdict: Verdict
    stereo_checked: bool  # False when either side lacked block 2 (connectivity-only comparison)
    reason: str
    necs_key: CertificateKey | None
    cohort_key: CertificateKey | None

    @property
    def certified(self) -> bool:
        return self.verdict == "certified"

    @property
    def refused(self) -> bool:
        return self.verdict == "refused"


def certificate_key(inchikey: str | None) -> CertificateKey | None:
    """block1 + block2[:8] from a full InChIKey; connectivity-only if first-block is all we have.

    Returns None for absent/empty input (→ the link has no independent structure on that side).
    """
    if not inchikey or not inchikey.strip():
        return None
    blocks = inchikey.strip().split("-")
    conn = blocks[0] if blocks[0] else None
    if conn is None:
        return None
    stereo8 = blocks[1][:8] if len(blocks) > 1 and blocks[1] else None
    return CertificateKey(connectivity=conn, stereo8=stereo8)


def certify_link(
    necs_inchikey: str | None,
    cohort_independent_inchikey: str | None,
    *,
    necs_source: str | None = None,
    cohort_source: str | None = None,
    require_tags: bool = False,
) -> LinkCertificate:
    """Certify a link from two KG-INDEPENDENT structures. NEVER pass the linking CURIE's KG node.

    Fail-closed provenance guard (R4): ``necs_source``/``cohort_source`` tag each structure's origin.
    A side tagged ``"kg"`` is KG-derived and NOT independent → REFUSED (never certified off it). In
    strict mode (``require_tags=True``, used by the reported metric) an untagged side (source ``None``)
    also REFUSES — independence is enforced here, not assumed by the caller. Legacy callers omit the
    tags and keep the prior behavior.

    - either side has no independent structure → REFUSED (counts-only; reported separately, R6b).
    - block-1 (connectivity) differs → REFUTED (wrong molecule — the co-derivation catch).
    - block-1 same, block2[:8] differs (both present) → REFUTED (stereoisomer).
    - block-1 same and (block2[:8] same OR not checkable on a side) → CERTIFIED
      (with ``stereo_checked`` recording whether the stereo layer was actually compared).
    """
    for side, src in (("NECS", necs_source), ("cohort", cohort_source)):
        if src == "kg":
            return LinkCertificate(
                verdict="refused",
                stereo_checked=False,
                reason=f"{side}-side structure is KG-derived (source='kg') — not independent; refused",
                necs_key=None,
                cohort_key=None,
            )
    if require_tags and (necs_source is None or cohort_source is None):
        missing = "NECS" if necs_source is None else "cohort"
        return LinkCertificate(
            verdict="refused",
            stereo_checked=False,
            reason=f"{missing}-side block is untagged and provenance is required — refused (fail-closed)",
            necs_key=None,
            cohort_key=None,
        )

    nk = certificate_key(necs_inchikey)
    ck = certificate_key(cohort_independent_inchikey)

    if nk is None or ck is None:
        missing = "NECS" if nk is None else "cohort"
        return LinkCertificate(
            verdict="refused",
            stereo_checked=False,
            reason=f"no independent structure on the {missing} side — counts-only, not certified off the KG",
            necs_key=nk,
            cohort_key=ck,
        )

    if nk.connectivity != ck.connectivity:
        return LinkCertificate(
            verdict="refuted",
            stereo_checked=False,
            reason="independent structures disagree at connectivity (block 1) — wrong-molecule link",
            necs_key=nk,
            cohort_key=ck,
        )

    stereo_checked = nk.stereo8 is not None and ck.stereo8 is not None
    if stereo_checked and nk.stereo8 != ck.stereo8:
        return LinkCertificate(
            verdict="refuted",
            stereo_checked=True,
            reason="connectivity agrees but stereo layer (block2[:8]) disagrees — stereoisomer",
            necs_key=nk,
            cohort_key=ck,
        )

    return LinkCertificate(
        verdict="certified",
        stereo_checked=stereo_checked,
        reason="independent structures agree"
        + ("" if stereo_checked else " at connectivity (stereo not resolvable)"),
        necs_key=nk,
        cohort_key=ck,
    )
