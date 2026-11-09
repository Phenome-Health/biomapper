"""Offline identifier check: do a row's submitter-declared IDs agree with its chosen node?

A cohort table often carries its own identifiers (HMDB, KEGG, InChIKey, ...). In the names-only
arm they are not sent to the mapper, so they are independent evidence about the node a name was
mapped to. :func:`check_identifiers` compares them with that node:

* Each declared ``vocabulary:code`` is normalized like the linker's CURIEs (:func:`normalize_curie`,
  so ``KEGG.COMPOUND`` equals ``KEGG``) and legacy 5-digit HMDB codes are zero-padded. A cell that
  already carries its prefix (``CHEBI:15422`` under CHEBI) is used as it is.
* The result is compared with the node's identifier-only CURIE set (``curie_set``: the chosen ID
  plus its equivalents). Declared InChIKeys are compared, full key, with the node's ``INCHIKEY``
  equivalents, because ``curie_set`` drops structure namespaces.
* ``MappingResult.identifiers`` (engine-assigned annotations) is never read.

Statuses: ``confirmed`` (some declared ID is among the node's identifiers), ``not_confirmed``
(declared IDs were comparable and none matched), ``no_declared_id`` (nothing comparable was
declared; a reason says when a vocabulary could not be compared), ``errored`` (the mapping call
failed). ``not_confirmed`` does NOT mean contradicted: the node may simply lack that
cross-reference. Pure and offline.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from biomapper.harmonize.curies import curie_set, normalize_curie, normalize_hmdb
from biomapper.models import MappingResult

__all__ = ["COMPARISON_RULE", "ID_CHECK_STATUSES", "IdCheck", "check_identifiers"]

COMPARISON_RULE = "declared_curie_in_node_curie_set+inchikey_full_key/v1"
ID_CHECK_STATUSES: tuple[str, ...] = ("confirmed", "not_confirmed", "no_declared_id", "errored")

_KG_PREFIX = re.compile(r"^[A-Z0-9][A-Z0-9._-]*$")
_UNCOMPARED_STRUCTURE = frozenset({"INCHI", "SMILES"})


@dataclass(frozen=True)
class IdCheck:
    status: str
    declared: tuple[str, ...] = ()  # the comparable declared CURIEs, normalized
    matched: tuple[str, ...] = ()
    reason: str | None = None
    rule: str = COMPARISON_RULE


def _declared_curie(vocabulary: str, code: str) -> str | None:
    code = code.strip()
    if code.upper().startswith(f"{vocabulary.upper()}:"):
        curie = normalize_curie(code)
    else:
        curie = normalize_curie(f"{vocabulary}:{code}")
    return normalize_hmdb(curie) if curie is not None else None


def _node_inchikeys(result: MappingResult) -> frozenset[str]:
    out: set[str] = set()
    for prefix, values in result.kg_equivalent_ids.items():
        if prefix.strip().upper() == "INCHIKEY":
            out.update(str(v).strip().upper().removeprefix("INCHIKEY:") for v in values)
    return frozenset(out)


def check_identifiers(
    declared: Mapping[str, Sequence[str]], result: MappingResult
) -> IdCheck:
    """Compare one row's declared identifiers (``{vocabulary: codes}``) with its mapping result.

    ``declared`` is :attr:`biomapper.cohorts.CohortRecord.identifiers`: vocabularies already
    upper-cased and folded by ``normalize_vocabulary``.
    """
    if result.error:
        return IdCheck(status="errored", reason=result.error)
    comparable: list[str] = []
    inchikeys: list[str] = []
    skipped: list[str] = []
    for vocabulary, codes in declared.items():
        vocab = vocabulary.strip().upper()
        if not codes:
            continue
        if vocab == "INCHIKEY":
            inchikeys.extend(c.strip().upper().removeprefix("INCHIKEY:") for c in codes)
        elif vocab in _UNCOMPARED_STRUCTURE:
            skipped.append(f"{vocabulary} (structure vocabulary, not compared)")
        elif not _KG_PREFIX.match(vocab):
            skipped.append(f"{vocabulary} (unmappable vocabulary)")
        else:
            comparable.extend(c for c in (_declared_curie(vocab, code) for code in codes) if c)
    reason = "; ".join(skipped) or None
    declared_all = tuple(dict.fromkeys([*comparable, *(f"INCHIKEY:{k}" for k in inchikeys)]))
    if not declared_all:
        return IdCheck(status="no_declared_id", reason=reason)
    node = frozenset(normalize_hmdb(c) for c in curie_set(result.chosen_kg_id,
                                                          result.kg_equivalent_ids))
    node_keys = _node_inchikeys(result)
    matched = tuple(
        [c for c in dict.fromkeys(comparable) if c in node]
        + [f"INCHIKEY:{k}" for k in dict.fromkeys(inchikeys) if k in node_keys]
    )
    return IdCheck(
        status="confirmed" if matched else "not_confirmed",
        declared=declared_all,
        matched=matched,
        reason=reason,
    )
