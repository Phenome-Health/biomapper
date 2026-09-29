"""Structure oracle built from the API response instead of an in-process StructureResolver.

The mapping response already carries what the oracle needs, so ``StructureResolver`` does not
have to be imported to score the structure arms:

* ``kg_equivalent_ids["INCHIKEY"]`` is the chosen node's graph-asserted InChIKey list, in the
  graph's own order. ``[0]`` is therefore the same ``keys[0]`` the engine's
  ``inchikey_block`` reads, and the set of first blocks is the same set
  ``inchikey_blocks`` returns.
* ``resolution_certificate.node_inchikey_blocks`` is that same set, sorted. It is used here as
  a **cross-check**, not as the source: it is sorted, so ``keys[0]`` cannot be recovered from
  it, and the strict metric needs ``keys[0]``.

Verified against production over the Hajjar-100 set on 2026-09-23: the certificate's
``node_inchikey_blocks`` equalled the ``kg_equivalent_ids["INCHIKEY"]`` first-block union on
100/100 rows. :meth:`ApiStructureOracle.integrity` re-runs that check every time and reports
disagreements rather than preferring one silently.

One genuine gap: the response does not carry the chosen **node's** name (``result.name`` is
the *query* name), and the engine's name fallback keys off the node name. So when the graph
asserts no structure for the chosen node, this oracle looks the node name up through
:class:`NodeNameResolver` (Kestrel ``/get-nodes``, keyless) before falling back. Which Kestrel
served that lookup is recorded in the manifest, because an oracle reading a different graph
than the mapper did would be a silent provenance break.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from biomapper.benchmarks.structure import (
    NameStructureResolver,
    first_block,
    neutralize_first_block,
)

DEFAULT_KESTREL_URL = "https://kestrel.krakenkg.com/api"
GET_NODES_TIMEOUT_S = 60.0
GET_NODES_BATCH = 100

logger = logging.getLogger(__name__)


class NodeNameResolver:
    """KG node id -> node name, via Kestrel ``/get-nodes``. Keyless; batched; memoized.

    Only needed for the name-fallback path, which fires for the minority of rows where the
    graph asserts no InChIKey for the chosen node. Failures degrade to ``None`` (the row then
    has no resolvable structure and is reported as such) rather than aborting an arm.
    """

    def __init__(
        self, kestrel_url: str = DEFAULT_KESTREL_URL, *, timeout: float = GET_NODES_TIMEOUT_S
    ) -> None:
        self.kestrel_url = kestrel_url.rstrip("/")
        self._timeout = timeout
        self._cache: dict[str, str | None] = {}
        self._errors: list[str] = []

    def prime(self, node_ids: list[str]) -> None:
        """Batch-fetch names for ``node_ids`` up front.

        Called once per arm with every node that needs a fallback, so the oracle does not
        issue one request per row.
        """
        missing = sorted({n for n in node_ids if n and n not in self._cache})
        for i in range(0, len(missing), GET_NODES_BATCH):
            self._fetch(missing[i : i + GET_NODES_BATCH])

    def name(self, node_id: str | None) -> str | None:
        if not node_id:
            return None
        if node_id not in self._cache:
            self._fetch([node_id])
        return self._cache.get(node_id)

    def stats(self) -> dict[str, Any]:
        return {
            "kestrel_url": self.kestrel_url,
            "nodes_looked_up": len(self._cache),
            "names_found": sum(1 for v in self._cache.values() if v),
            "errors": list(self._errors),
        }

    def _fetch(self, node_ids: list[str]) -> None:
        if not node_ids:
            return
        try:
            with httpx.Client(timeout=self._timeout, follow_redirects=True) as client:
                response = client.post(
                    f"{self.kestrel_url}/get-nodes",
                    json={"curies": node_ids, "slim": False, "truncate_long_fields": False},
                )
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:  # noqa: BLE001 — enrichment, never fatal
            message = f"{type(exc).__name__}: {exc}"
            self._errors.append(message)
            logger.warning("Kestrel /get-nodes failed for %d node(s): %s", len(node_ids), message)
            # Cache the miss so a dead endpoint costs one timeout per batch, not per row.
            for node_id in node_ids:
                self._cache.setdefault(node_id, None)
            return
        for node_id in node_ids:
            node = payload.get(node_id)
            self._cache[node_id] = (node or {}).get("name") if isinstance(node, dict) else None


class ApiStructureOracle:
    """Row-keyed structure oracle over API mapping output.

    Satisfies the same three-plus-one method surface the engine's ``KGStructureOracle``
    exposes (``kg_block`` / ``resolved_block`` / ``resolved_blocks`` / ``neutral_block``), so
    the ported scorers consume it unchanged. The difference is the lookup key: the engine's
    oracle takes a node id and re-queries Kestrel, while this one takes a node id and reads
    the per-node structure the mapping response already delivered.

    ``name_fallback=None`` disables the MW/PubChem hop entirely. Then a node the graph
    asserts no structure for resolves to ``None`` — reported as unverifiable, which is
    honest, rather than guessed.
    """

    def __init__(
        self,
        node_structures: dict[str, dict[str, list[str]]],
        *,
        name_fallback: NameStructureResolver | None = None,
        node_names: NodeNameResolver | None = None,
        certificate_blocks: dict[str, list[str]] | None = None,
    ) -> None:
        self._equiv = node_structures
        self._name_fallback = name_fallback
        self._node_names = node_names
        self._certificate_blocks = certificate_blocks or {}
        self._fallback_used: dict[str, str | None] = {}

    # ------------------------------------------------------------------
    # Construction from mapper output
    # ------------------------------------------------------------------

    @classmethod
    def from_rows(
        cls,
        rows: list[dict[str, Any]],
        *,
        name_fallback: NameStructureResolver | None = None,
        node_names: NodeNameResolver | None = None,
    ) -> ApiStructureOracle:
        """Build from ``ApiMapper`` row dicts (``chosen_kg_id`` + ``kg_equivalent_ids``).

        Node-name lookups for every structure-absent node are primed in one batch here, so
        the scorer's per-row loop never triggers a network round-trip of its own.
        """
        equiv: dict[str, dict[str, list[str]]] = {}
        cert_blocks: dict[str, list[str]] = {}
        needs_name: list[str] = []
        for row in rows:
            node_id = row.get("chosen_kg_id")
            if not node_id:
                continue
            node_id = str(node_id)
            equiv.setdefault(node_id, row.get("kg_equivalent_ids") or {})
            certificate = row.get("certificate") or {}
            blocks = (
                certificate.get("node_inchikey_blocks") if isinstance(certificate, dict) else None
            )
            if blocks is not None:
                cert_blocks.setdefault(node_id, list(blocks))
            if not (equiv[node_id] or {}).get("INCHIKEY"):
                needs_name.append(node_id)
        if node_names is not None and needs_name:
            node_names.prime(needs_name)
        return cls(
            equiv,
            name_fallback=name_fallback,
            node_names=node_names,
            certificate_blocks=cert_blocks,
        )

    # ------------------------------------------------------------------
    # StructureOracle surface
    # ------------------------------------------------------------------

    def kg_block(self, node_id: str) -> str | None:
        """First block of the graph's FIRST asserted InChIKey, or ``None``.

        Mirrors ``StructureResolver.inchikey_block``'s KG branch: ``keys[0]``, not a sorted
        set. The ordering is the graph's and is arbitrary, which is precisely why the
        equivalence-set metric exists alongside this one — but the strict metric is defined on
        ``keys[0]``, so reproducing it requires preserving that order.
        """
        keys = self._inchikeys(node_id)
        return first_block(keys[0]) if keys else None

    def resolved_block(self, node_id: str) -> str | None:
        """Full layered path: graph ``keys[0]``, else the node-name fallback.

        Mirrors ``StructureResolver.inchikey_block`` in full. The gap between this and
        :meth:`kg_block` is the fallback-segregation signal the scorer buckets.
        """
        block = self.kg_block(node_id)
        if block is not None:
            return block
        return self._fallback_block(node_id)

    def resolved_blocks(self, node_id: str) -> set[str]:
        """EVERY graph-asserted first block, else the singleton fallback.

        Mirrors ``StructureResolver.inchikey_blocks``. This is what fixes the ``keys[0]``
        artifact: a gold structure may match a non-first entry, because a node's INCHIKEY
        list is multi-valued (neutral parent, conjugate anion, salt, stereoisomers) and its
        order is arbitrary. Set membership, not equality against one arbitrary representation.

        The loosening cannot cross node boundaries: a wrong entity only matches if its own
        node asserts equivalence to the gold structure, so there is no free inflation.
        """
        blocks = {b for b in (first_block(k) for k in self._inchikeys(node_id)) if b}
        if blocks:
            return blocks
        single = self._fallback_block(node_id)
        return {single} if single else set()

    def neutral_block(self, node_id: str) -> str | None:
        """Charge/protonation-normalized first block of the prediction.

        Mirrors ``KGStructureOracle.neutral_block``: neutralize the graph's SMILES before
        hashing, and when the node carries no SMILES to neutralize, fall back to the strict
        resolved block (a hash cannot be neutralized).
        """
        smiles_list = (self._equiv.get(node_id) or {}).get("SMILES") or []
        smiles = smiles_list[0] if smiles_list else None
        block = neutralize_first_block(smiles) if smiles else None
        return block if block is not None else self.resolved_block(node_id)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def integrity(self) -> dict[str, Any]:
        """Cross-check the derived block sets against the certificate's own.

        A disagreement means the response's ``kg_equivalent_ids`` and its certificate
        describe different things, which would invalidate the premise that
        ``StructureResolver`` is redundant here. Reported, never silently resolved in favour
        of either side.
        """
        agree = 0
        disagree: list[dict[str, Any]] = []
        for node_id, blocks in self._certificate_blocks.items():
            derived = sorted({b for b in (first_block(k) for k in self._inchikeys(node_id)) if b})
            if derived == sorted(blocks):
                agree += 1
            else:
                disagree.append(
                    {"node": node_id, "derived": derived, "certificate": sorted(blocks)}
                )
        return {
            "nodes_with_certificate": len(self._certificate_blocks),
            "certificate_agrees_with_kg_equivalent_ids": agree,
            "disagreements": disagree,
        }

    def fallback_report(self) -> dict[str, Any]:
        """Which nodes needed the name fallback, and whether it resolved them."""
        resolved = {k: v for k, v in self._fallback_used.items() if v}
        return {
            "nodes_needing_fallback": len(self._fallback_used),
            "nodes_resolved_by_fallback": len(resolved),
            "unresolved_nodes": sorted(k for k, v in self._fallback_used.items() if not v),
            "name_resolver": self._name_fallback.stats() if self._name_fallback else None,
            "node_name_resolver": self._node_names.stats() if self._node_names else None,
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _inchikeys(self, node_id: str) -> list[str]:
        keys = (self._equiv.get(node_id) or {}).get("INCHIKEY") or []
        return [str(k) for k in keys if k]

    def _fallback_block(self, node_id: str) -> str | None:
        if self._name_fallback is None:
            return None
        if node_id in self._fallback_used:
            return self._fallback_used[node_id]
        node_name = self._node_names.name(node_id) if self._node_names else None
        block = self._name_fallback.block(node_name) if node_name else None
        self._fallback_used[node_id] = block
        return block
