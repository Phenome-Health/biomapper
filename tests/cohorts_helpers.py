"""Shared helpers for the harmonize_cohorts() tests: a scriptable fake mapper and result builder."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from biomapper.models import MappingResult


def mr(
    name: str,
    chosen: str | None = None,
    equivalents: dict[str, list[str]] | None = None,
    error: str | None = None,
) -> MappingResult:
    """A MappingResult with just the fields harmonization reads."""
    return MappingResult(
        query_name=name,
        resolved=bool(chosen),
        chosen_kg_id=chosen,
        kg_equivalent_ids=equivalents or {},
        error=error,
    )


Resolver = Callable[[str, dict[str, list[str]], str, str], MappingResult]


@dataclass
class Call:
    records: list[dict[str, Any]]
    entity_type: str
    annotation_mode: str


@dataclass
class FakeMapper:
    """Answers each request with ``resolve(name, identifiers, annotation_mode, entity_type)``.

    Logs every call so a test can assert exactly which requests were sent (and which were not).
    """

    resolve: Resolver
    calls: list[Call] = field(default_factory=list)

    def __call__(
        self, records: list[dict[str, Any]], *, entity_type: str, annotation_mode: str
    ) -> list[MappingResult]:
        self.calls.append(Call([dict(r) for r in records], entity_type, annotation_mode))
        return [
            self.resolve(
                r["name"],
                {k: list(v) for k, v in (r.get("identifiers") or {}).items()},
                annotation_mode,
                entity_type,
            )
            for r in records
        ]

    @property
    def sent(self) -> list[tuple[str, dict[str, list[str]], str]]:
        """Every (name, identifiers, annotation_mode) the mapper was asked for, in order."""
        return [
            (r["name"], dict(r.get("identifiers") or {}), c.annotation_mode)
            for c in self.calls
            for r in c.records
        ]


def by_name(table: dict[str, MappingResult | None]) -> Resolver:
    """Resolve by name only, ignoring identifiers; unknown names resolve to nothing."""

    def resolve(name: str, ids: dict[str, list[str]], mode: str, etype: str) -> MappingResult:
        hit = table.get(name)
        return hit.model_copy(update={"query_name": name}) if hit is not None else mr(name)

    return resolve
