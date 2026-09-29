"""InChIKey primitives and the name->structure fallback, reimplemented to match the engine.

This module is the one place where a subtle difference would move a headline number for
reasons that have nothing to do with the engine, so each function states exactly which
engine function it mirrors.

The fallback chain mirrors ``biomapper2.core.structure_resolver.StructureResolver``:
Metabolomics Workbench by name, then PubChem by name. The engine additionally consults a
pinned RefMet freeze *first* when one is loaded; that freeze is engine-internal and has no
API surface, so a client-side run cannot consult it. Consequence, stated rather than hidden:
where the deployment's freeze would have served a structure deterministically, this client
does a live lookup instead, which is fail-soft. :func:`NameStructureResolver.stats` reports
how many rows took the live path so a reader can size that gap instead of assuming it away.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import httpx

MW_INCHIKEY_URL = "https://www.metabolomicsworkbench.org/rest/refmet/name"
PUBCHEM_INCHIKEY_URL = "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name"
STRUCTURE_LOOKUP_TIMEOUT_S = 30.0

# Upstream missing-value sentinel. Must never be accepted as a real InChIKey.
_MISSING_SENTINEL = "-"

logger = logging.getLogger(__name__)


def first_block(inchikey: Any) -> str | None:  # noqa: ANN401 - a pandas cell is genuinely untyped (str | float NaN)
    """First InChIKey block (the 2-D connectivity skeleton), or ``None`` if absent/blank.

    Mirrors ``scorers.structure_oracle_scorer.first_block``. Accepts the float NaN pandas
    puts in an empty cell, and the literal string ``"nan"`` a round-trip through TSV
    produces — treating either as a real value would compare a missing structure as if it
    were present.
    """
    if inchikey is None:
        return None
    if isinstance(inchikey, float):  # NaN
        return None
    text = str(inchikey).strip()
    if not text or text.lower() == "nan":
        return None
    return text.split("-")[0]


def neutralize_first_block(smiles: Any) -> str | None:  # noqa: ANN401 - a pandas cell is genuinely untyped (str | float NaN)
    """Charge/protonation-normalized InChIKey first block from a SMILES, or ``None``.

    Mirrors ``scorers.structure_oracle_scorer.neutralize_first_block``. Neutralizes with
    RDKit's ``Uncharger`` before hashing so a carboxylate and its acid, or a zwitterion and
    the neutral species, collapse to one connectivity skeleton. Standard InChI already routes
    most protonation into the *second* block, so this only moves the residual cases where the
    recorded first block itself differs by charge state.

    RDKit is imported lazily: it is the heaviest dependency in the extra and most arms never
    reach this path.
    """
    if smiles is None:
        return None
    if isinstance(smiles, float):  # NaN
        return None
    text = str(smiles).strip()
    if not text or text.lower() == "nan":
        return None
    try:
        from rdkit import Chem, RDLogger
        from rdkit.Chem.MolStandardize import rdMolStandardize
    except ImportError:  # pragma: no cover - exercised only without the extra installed
        logger.warning("rdkit is not installed; charge-normalized scoring is unavailable")
        return None

    RDLogger.DisableLog("rdApp.*")  # type: ignore[attr-defined]  # rdkit ships no stubs
    mol = Chem.MolFromSmiles(text)
    if mol is None:
        return None
    mol = rdMolStandardize.Uncharger().uncharge(mol)
    return first_block(Chem.MolToInchiKey(mol))  # type: ignore[no-untyped-call]


def inchikey_from_smiles(smiles: Any) -> str | None:  # noqa: ANN401 - a pandas cell is genuinely untyped (str | float NaN)
    """Full standard InChIKey derived from a SMILES, or ``None``.

    Used by the NIST SRM 1950 adapter, whose delivery ships SMILES but an EMPTY InChIKey
    column, so the independent oracle structure is derived deterministically from the
    certified SMILES. RDKit shares no infrastructure with BioMapper's resolver, so deriving
    the gold this way does not compromise oracle independence.
    """
    if smiles is None or (isinstance(smiles, float)):
        return None
    text = str(smiles).strip()
    if not text or text.lower() == "nan":
        return None
    try:
        from rdkit import Chem, RDLogger
    except ImportError:  # pragma: no cover
        logger.warning("rdkit is not installed; cannot derive InChIKey from SMILES")
        return None
    RDLogger.DisableLog("rdApp.*")  # type: ignore[attr-defined]  # rdkit ships no stubs
    mol = Chem.MolFromSmiles(text)
    if mol is None:
        return None
    try:
        return str(Chem.MolToInchiKey(mol))  # type: ignore[no-untyped-call]
    except Exception:  # noqa: BLE001 - unparseable structure is a no-gold row, not a crash
        return None


class NameStructureResolver:
    """Full InChIKey for a NAME via Metabolomics Workbench, then PubChem. Fail-soft.

    Mirrors ``StructureResolver._resolve_name_key`` minus the engine-internal RefMet freeze
    (see the module docstring). Results are memoized per instance, matching the engine's
    per-process ``_name_cache``: a name resolves to at most one structure, and the arms
    re-query the same node names repeatedly.

    Every lookup degrades to ``None`` rather than raising, so a throttled service reads as
    "unresolvable" and the caller flags the row — never as a wrong structure. But
    unresolvable-because-throttled and unresolvable-because-no-such-compound are counted
    separately in :meth:`stats`, because collapsing them is how a degraded service gets
    reported as name difficulty.
    """

    def __init__(self, *, timeout: float = STRUCTURE_LOOKUP_TIMEOUT_S) -> None:
        self._timeout = timeout
        self._cache: dict[str, str | None] = {}
        self._hits: dict[str, int] = {"metabolomics_workbench": 0, "pubchem": 0}
        self._no_match = 0
        self._lookup_failed = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def inchikey(self, name: str | None) -> str | None:
        """Full InChIKey for ``name``, or ``None`` when no source resolves it."""
        if not name:
            return None
        if name in self._cache:
            return self._cache[name]
        key = self._resolve(name)
        self._cache[name] = key
        return key

    def block(self, name: str | None) -> str | None:
        """First InChIKey block for ``name``, or ``None``."""
        return first_block(self.inchikey(name))

    def stats(self) -> dict[str, Any]:
        """How the fallback path behaved, for the run manifest.

        ``lookup_failed`` is kept apart from ``no_match`` on purpose: the first is a
        service problem and invalidates nothing about the name, the second is a real
        negative. A single "unresolved" count would let a throttled MW read as chemistry.
        """
        return {
            "names_queried": len(self._cache),
            "resolved": sum(1 for v in self._cache.values() if v),
            "hits_by_source": dict(self._hits),
            "no_match": self._no_match,
            "lookup_failed": self._lookup_failed,
            "refmet_freeze_consulted": False,
            "refmet_freeze_note": (
                "the deployment's pinned RefMet freeze has no API surface, so this client "
                "resolved names live; where the freeze would have served deterministically, "
                "this path is fail-soft instead"
            ),
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolve(self, name: str) -> str | None:
        try:
            key = self._fetch_mw(name)
            if key:
                self._hits["metabolomics_workbench"] += 1
                return key
            key = self._fetch_pubchem(name)
            if key:
                self._hits["pubchem"] += 1
                return key
        except Exception:  # noqa: BLE001 — mirrors the engine's fail-soft guard
            self._lookup_failed += 1
            logger.warning(
                "Structure lookup failed for %r; treating as unresolvable", name, exc_info=True
            )
            return None
        self._no_match += 1
        return None

    def _fetch_mw(self, name: str) -> str | None:
        """Metabolomics Workbench: ``GET /rest/refmet/name/{name}/inchi_key``.

        ``safe=""`` encodes the whole name into one path segment. A slash-bearing name (an
        sn-position lipid shorthand such as ``PC 16:0/18:1``) has no addressable entry here:
        MW's web server rejects the encoded slash outright. A per-name 400/404 is a
        definitive "no such structure", returned as ``None`` rather than raised, so it is a
        clean no-match instead of a logged lookup failure. Only 5xx and transport errors
        reach the caller's fail-soft guard.
        """
        url = f"{MW_INCHIKEY_URL}/{quote(name, safe='')}/inchi_key"
        with httpx.Client(timeout=self._timeout, follow_redirects=True) as client:
            response = client.get(url)
            if response.status_code in (400, 404):
                return None
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError:
                return None
        if isinstance(data, dict):
            key = data.get("inchi_key")
            return key if key and key != _MISSING_SENTINEL else None
        return None

    def _fetch_pubchem(self, name: str) -> str | None:
        """PubChem: ``GET /rest/pug/compound/name/{name}/property/InChIKey/JSON``.

        ``safe=""`` for the same reason as :meth:`_fetch_mw` — an unescaped slash injects URL
        path segments and turns a resolvable name into a silent 404. A 404 here is PubChem's
        normal "no such name" and is a clean no-match.
        """
        url = f"{PUBCHEM_INCHIKEY_URL}/{quote(name, safe='')}/property/InChIKey/JSON"
        with httpx.Client(timeout=self._timeout, follow_redirects=True) as client:
            response = client.get(url)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError:
                return None
        props = data.get("PropertyTable", {}).get("Properties", [])
        if props and props[0].get("InChIKey"):
            return str(props[0]["InChIKey"])
        return None
