"""The single source of the package version, and of the sentinel used when there isn't one.

Lives in its own module with no heavy imports so that both ``biomapper/__init__.py`` and
``biomapper.benchmarks.provenance`` can read it without importing each other. Two call sites
resolving the version independently is how the original drift happened; two call sites resolving
the *fallback* independently is how it nearly happened again.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

#: Returned when no installed distribution metadata exists, i.e. a bare source checkout that was
#: never installed. Deliberately PEP 440 valid so version parsers do not choke, and deliberately
#: ``0.0.0`` with a local segment so it sorts below every real release and cannot be mistaken for
#: one. A fallback that looks like a plausible version is worse than no version at all: it puts a
#: confident number in a provenance field that describes nothing.
UNINSTALLED_VERSION = "0.0.0+unknown"


def resolve_version() -> str:
    """The installed distribution version, or :data:`UNINSTALLED_VERSION`.

    Every consumer must route through here. ``biomapper.__version__`` and
    ``provenance.package_version()`` previously had separate fallbacks (``0.0.0+unknown`` against
    ``unknown``), so in the uninstalled case a run manifest recorded a version that disagreed with
    the importable attribute, which is the exact ambiguity this module exists to remove.
    """
    try:
        return _dist_version("biomapper")
    except PackageNotFoundError:
        return UNINSTALLED_VERSION
