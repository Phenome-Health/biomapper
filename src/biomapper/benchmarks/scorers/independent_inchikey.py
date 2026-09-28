"""Independent external resolver for the curator's HELD-OUT provided ids -> InChIKey first-block.

Oracle (b)'s GOLD side must NOT share infrastructure with the KG structure oracle that resolves
BioMapper's PREDICTION -- otherwise the "structural concordance" is circular (both sides resolved by
the same Kestrel KG). This resolves the curator's HMDB / PubChem provided id to an InChIKey first-
block via **PubChem PUG-REST**, an external source independent of the KG. It is:

  - CACHED (per (namespace, id); repeated ids across rows cost one call),
  - IPv4-FORCED (reuses the adapter's ``force_ipv4`` -- the desktop's IPv6->CDN route is flaky), and
  - FAIL-SOFT per id: any non-200 / network error / unparseable body yields ``None`` (cached) so the
    scorer marks that row ``needs-verification`` rather than silently dropping it or fabricating a
    block.

PubChem endpoints used (property/InChIKey, TXT for a single bare value):
  - PubChem CID:  ``/compound/cid/{cid}/property/InChIKey/TXT``
  - HMDB accession (xref RegistryID): ``/compound/xref/RegistryID/{hmdb}/property/InChIKey/TXT``

Lipid oracle (provided-id path). Metabolon shorthand lipid NAMES do not resolve via PubChem-by-name
(and are neither a pygoslin dialect nor a LIPID MAPS abbreviation -- both probed at 0% coverage on the
real refused names). But the cohort source carries curator cross-reference ids (HMDB / PubChem CID /
InChIKey). ``block_for_provided`` resolves those ids -- KG-independent, since BioMapper commits via
name / RM / CHEBI, not via these HMDB/PubChem ids -- and tags each result with its provenance so the
certificate can enforce disjointness (Unit 2). It also distinguishes a transient ``lookup_failed`` from
a genuine ``clean_miss`` so a network blip is never silently counted as ``refused``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from biomapper.benchmarks.adapters.metlinkr import force_ipv4

_PUG_REST = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"


def pubchem_session() -> Any:  # noqa: ANN401 - a requests.Session
    """A ``requests`` session whose HTTPS pool uses the stdlib default SSL context.

    PubChem's edge rejects urllib3's own default TLS context with ``503 PUGREST.ServerBusy`` on
    every request, while curl, httpx, and urllib3 handed ``ssl.create_default_context()`` all get
    200 at the same moment (observed 2026-09-27: 0/10 vs 15/15). The 503 reads as load, so a whole
    certification pass came back ``lookup_failed`` three times running and looked like an outage.
    Both PubChem callers in this package take their default session from here.
    """
    import ssl

    import requests
    from requests.adapters import HTTPAdapter

    class _StdlibTLSAdapter(HTTPAdapter):
        def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
            kwargs["ssl_context"] = ssl.create_default_context()
            super().init_poolmanager(*args, **kwargs)

    session = requests.Session()
    session.mount("https://", _StdlibTLSAdapter())
    return session


def _first_block(inchikey: str | None) -> str | None:
    return inchikey.split("-")[0] if inchikey else None


@dataclass(frozen=True)
class ProvidedBlock:
    """An independent InChIKey first-block with its resolution provenance and status.

    ``source`` tags the KG-independent origin (never ``kg``); the certificate uses it to enforce
    disjointness. ``status`` distinguishes a clean miss (no such id / absent) from a transient
    ``lookup_failed`` (network / 5xx), so a lookup failure is never silently counted as ``refused``.
    """

    block: str | None
    source: str  # provided-inchikey | provided-hmdb | provided-pubchem | pubchem-name | none
    status: str  # success | clean_miss | lookup_failed
    record_id: str | None = None  # "{source_file}:{key}" — two sides sharing it are the SAME curator
    # record (not two independent derivations); the certificate refuses those. Set by the caller that
    # knows which source file/dict produced the block (the operator driver); None for the id lookups.


class PubChemInChIKeyResolver:
    """Resolve a curator PubChem CID / HMDB accession -> InChIKey first-block via PUG-REST (external).

    Independent of the Kestrel KG oracle used for BioMapper's prediction (the whole point of oracle
    (b)'s independence). Cached + IPv4-forced + fail-soft (unresolved -> ``None``).
    """

    def __init__(
        self,
        *,
        timeout: float = 20.0,
        session: Any | None = None,  # noqa: ANN401
        pacer: Any | None = None,  # noqa: ANN401 - a biomapper.benchmarks.pacing.Pacer
    ) -> None:
        self._timeout = timeout
        self._session = session or pubchem_session()
        # Optional request pacer, consulted in ``_resolve_txt`` — i.e. on the REQUEST path, which
        # only runs after ``_cached_resolve`` has missed. Pacing outside the resolver cannot know
        # whether a request is about to be sent, so it sleeps on cache hits too. Default None keeps
        # the suite's existing behaviour unchanged.
        self._pacer = pacer
        self._cache: dict[str, str | None] = {}
        self._status_cache: dict[str, tuple[str | None, str]] = {}

    def _resolve_txt(self, path: str) -> tuple[str | None, str]:
        """GET a PUG-REST .../InChIKey/TXT path -> ``(first_block_or_None, status)``.

        status: ``success`` (block found), ``clean_miss`` (404 / empty body = no such structure),
        ``lookup_failed`` (5xx / network error = transient; must NOT be read as absence).
        """
        if self._pacer is not None:
            self._pacer.wait()
        url = f"{_PUG_REST}/{path}"
        try:
            with force_ipv4():
                resp = self._session.get(url, timeout=self._timeout)
        except Exception:
            return None, "lookup_failed"
        if resp.status_code == 404:
            return None, "clean_miss"
        if resp.status_code != 200:
            return None, "lookup_failed"
        first_line = resp.text.strip().splitlines()[0].strip() if resp.text.strip() else ""
        block = _first_block(first_line) if first_line else None
        return block, ("success" if block else "clean_miss")

    def _get_txt_inchikey(self, path: str) -> str | None:
        """Backward-compatible fail-soft accessor: first InChIKey block, or ``None`` on any failure."""
        return self._resolve_txt(path)[0]

    def block_for_pubchem(self, cid: str) -> str | None:
        key = f"pubchem:{cid}"
        if key in self._cache:
            return self._cache[key]
        block = self._get_txt_inchikey(f"compound/cid/{quote(cid)}/property/InChIKey/TXT")
        self._cache[key] = block
        return block

    def block_for_hmdb(self, hmdb: str) -> str | None:
        key = f"hmdb:{hmdb}"
        if key in self._cache:
            return self._cache[key]
        block = self._get_txt_inchikey(f"compound/xref/RegistryID/{quote(hmdb)}/property/InChIKey/TXT")
        self._cache[key] = block
        return block

    def block_for_name(self, name: str) -> str | None:
        """Independent structure by NAME (PubChem name index) — for name-only panels (e.g. Xu).

        KG-independent: PubChem's name index is a different service than the KG node, so this never
        reads the linking KG node's InChIKey. Returns connectivity-only (block 1), like the id lookups,
        so certification degrades to connectivity — which is what catches wrong-molecule / generic-node
        false positives. Name resolution is fuzzier than id resolution: an ambiguous/absent name yields
        None, so the link is REFUSED (counts-only), never certified off a guess.
        """
        key = f"name:{name}"
        if key in self._cache:
            return self._cache[key]
        block = self._get_txt_inchikey(f"compound/name/{quote(name, safe='')}/property/InChIKey/TXT")
        self._cache[key] = block
        return block

    def block_for_name_status(self, name: str) -> tuple[str | None, str]:
        """Name resolution WITH provenance status: ``(block, success|clean_miss|lookup_failed)``.

        Unlike ``block_for_name`` (block-or-None, which cannot tell a transient PubChem failure from a
        genuine miss), this routes through the status-aware, non-sticky ``_cached_resolve`` so a
        ``lookup_failed`` (5xx / network) is never cached as a terminal ``clean_miss`` — a resumed run
        retries it after the service recovers instead of banking a stale refusal.
        """
        return self._cached_resolve(f"name:{name}", f"compound/name/{quote(name, safe='')}/property/InChIKey/TXT")

    def _cached_resolve(self, cache_key: str, path: str) -> tuple[str | None, str]:
        if cache_key in self._status_cache:
            return self._status_cache[cache_key]
        result = self._resolve_txt(path)
        # Cache only TERMINAL outcomes. A transient ``lookup_failed`` (5xx / network) is never cached, so a
        # later call retries it once the service recovers — a failure is not a sticky verdict.
        if result[1] != "lookup_failed":
            self._status_cache[cache_key] = result
        return result

    def block_for_provided(
        self,
        *,
        inchikey: str | None = None,
        hmdb: str | None = None,
        pubchem: str | None = None,
        name: str | None = None,
    ) -> ProvidedBlock:
        """Resolve a name's INDEPENDENT structure from curator-provided ids, tagged with provenance.

        Order, first success wins: provided InChIKey (offline) -> provided HMDB -> provided PubChem CID
        -> PubChem-by-name. Every source is KG-independent (BioMapper commits via name / RM / CHEBI, not
        via these ids). A transient network failure on any attempted source surfaces as ``lookup_failed``
        when nothing succeeds — never silently ``clean_miss`` — so it cannot masquerade as new coverage.
        """
        # 1) Provided InChIKey — offline, no network, most authoritative.
        if inchikey:
            blk = _first_block(inchikey)
            if blk:
                return ProvidedBlock(blk, "provided-inchikey", "success")

        any_lookup_failed = False
        attempts: list[tuple[str, str, str]] = []  # (source, cache_key, path)
        if hmdb:
            attempts.append(("provided-hmdb", f"hmdb:{hmdb}", f"compound/xref/RegistryID/{quote(hmdb)}/property/InChIKey/TXT"))
        if pubchem:
            attempts.append(("provided-pubchem", f"pubchem:{pubchem}", f"compound/cid/{quote(pubchem)}/property/InChIKey/TXT"))
        if name:
            attempts.append(("pubchem-name", f"name:{name}", f"compound/name/{quote(name, safe='')}/property/InChIKey/TXT"))

        for source, cache_key, path in attempts:
            block, status = self._cached_resolve(cache_key, path)
            if status == "success" and block:
                return ProvidedBlock(block, source, "success")
            if status == "lookup_failed":
                any_lookup_failed = True

        return ProvidedBlock(None, "none", "lookup_failed" if any_lookup_failed else "clean_miss")
