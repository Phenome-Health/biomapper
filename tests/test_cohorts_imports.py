"""Unit 1: harmonize_cohorts() is public, and a core install (no pandas) can import and pin it."""

from __future__ import annotations

import subprocess
import sys

import biomapper


def test_harmonize_cohorts_is_exported_at_the_package_root() -> None:
    from biomapper.cohorts import CohortHarmonizationReport, harmonize_cohorts

    assert biomapper.harmonize_cohorts is harmonize_cohorts
    assert biomapper.CohortHarmonizationReport is CohortHarmonizationReport
    assert "harmonize_cohorts" in biomapper.__all__
    assert "CohortHarmonizationReport" in biomapper.__all__


def test_root_export_does_not_shadow_the_harmonize_subpackage() -> None:
    """``biomapper.harmonize`` must stay the subpackage (its offline contract is unchanged)."""
    import biomapper.harmonize as sub

    assert biomapper.harmonize is sub
    assert callable(sub.harmonize)


def test_import_works_without_pandas() -> None:
    """A core install has no pandas; importing the package and the new module must not need it."""
    code = (
        "import sys\n"
        "sys.modules['pandas'] = None  # any 'import pandas' now raises ImportError\n"
        "import biomapper\n"
        "import biomapper.cohorts\n"
        "from biomapper._provenance import fetch_kg_build_info, KgBuildInfo\n"
        "assert callable(biomapper.harmonize_cohorts)\n"
        "assert 'pandas' not in [m for m in sys.modules if sys.modules[m] is not None]\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_benchmarks_provenance_reexports_the_moved_helpers() -> None:
    """The move to a pandas-free module must not break the benchmark suite's imports."""
    from biomapper import _provenance
    from biomapper.benchmarks import provenance

    assert provenance.fetch_kg_build_info is _provenance.fetch_kg_build_info
    assert provenance.KgBuildInfo is _provenance.KgBuildInfo
    assert provenance.DEFAULT_KESTREL_URL == _provenance.DEFAULT_KESTREL_URL
    assert provenance.UNKNOWN == _provenance.UNKNOWN
