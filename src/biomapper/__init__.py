"""biomapper — Python client for the BioMapper2 API.

Quick start::

    from biomapper import map_entity, map_entities, BioMapperClient

    # Single lookup (synchronous)
    result = map_entity("L-Histidine")
    print(result.primary_curie)      # RM:0129894
    print(result.confidence_tier)    # high

    # Batch (synchronous, with progress bar)
    results = map_entities(
        [{"name": "L-Histidine"}, {"name": "Glucose"}],
        progress=True,
    )

    # File-based (synchronous, with progress bar)
    from pathlib import Path
    from biomapper import map_dataset_file_sync

    result = map_dataset_file_sync(
        Path("compounds.tsv"),
        name_column="name",
        provided_id_columns=["hmdb_id"],
        progress=True,
    )
    result.raise_for_error()  # opt-in: raise if the stream truncated
    print(f"resolved {sum(1 for r in result.results if r.resolved)}")

    # Async (in an async context)
    async with BioMapperClient() as client:
        result = await client.map_entity("L-Histidine")

    # Harmonize two already-resolved cohorts (local, offline, no extra requests).
    # `harmonize` is deliberately NOT re-exported at the package root: the name would
    # shadow the `biomapper.harmonize` submodule and break `biomapper.harmonize.curie_set`.
    from biomapper.harmonize import harmonize

    report = harmonize(ukbb_results, arivale_results, a_label="ukbb", b_label="arivale")
    print(report.n_links, report.a_unresolved)
"""

from biomapper._version import resolve_version as _resolve_version
from biomapper.client import BioMapperClient
from biomapper.dataset import map_dataset_file_sync
from biomapper.exceptions import (
    BioMapperAuthError,
    BioMapperConfigError,
    BioMapperError,
    BioMapperRateLimitError,
    BioMapperServerError,
    BioMapperTimeoutError,
)
from biomapper.mapper import (
    list_annotators,
    list_entity_types,
    list_vocabularies,
    map_entities,
    map_entity,
    summarize,
)
from biomapper.models import (
    AnnotatorInfo,
    DatasetMappingResult,
    EntityTypeInfo,
    KestrelRequestParams,
    KestrelSearchResult,
    LipidResolution,
    MappingResult,
    MappingSummary,
    ResolutionCertificate,
    VocabularyInfo,
)

# Single-sourced from the installed distribution metadata, which Poetry builds from
# ``pyproject.toml``. It is deliberately NOT a literal here: a second literal is a second source of
# truth, and the two drifted (pyproject 1.5.1 against a hardcoded 1.4.0) for long enough that no run
# manifest could name its own package version unambiguously. ``pyproject.toml`` is the one source;
# ``tests/test_version.py`` fails the build if this module ever reintroduces a literal.
#
# Resolution AND the uninstalled fallback both live in ``biomapper._version`` so that this attribute
# and ``provenance.package_version()`` cannot answer differently in any case, including the
# no-metadata one.
__version__ = _resolve_version()

__all__ = [
    # Client
    "BioMapperClient",
    # Sync helpers
    "map_entity",
    "map_entities",
    "map_dataset_file_sync",
    "list_entity_types",
    "list_annotators",
    "list_vocabularies",
    "summarize",
    # Models
    "MappingResult",
    "MappingSummary",
    "DatasetMappingResult",
    "EntityTypeInfo",
    "AnnotatorInfo",
    "VocabularyInfo",
    "ResolutionCertificate",
    "LipidResolution",
    "KestrelSearchResult",
    "KestrelRequestParams",
    # Exceptions
    "BioMapperError",
    "BioMapperAuthError",
    "BioMapperConfigError",
    "BioMapperRateLimitError",
    "BioMapperServerError",
    "BioMapperTimeoutError",
]
