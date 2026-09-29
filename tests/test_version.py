"""The version has exactly one source of truth, and these tests fail if it grows a second.

Background. ``pyproject.toml`` said ``1.5.1`` while ``biomapper/__init__.py`` said ``1.4.0``, and
the newest tag was ``v1.4.0``. Three answers to "what version is this?" meant no run manifest could
name its own package version unambiguously, and a benchmark number whose client cannot be
identified is a number that cannot be audited.

The fix is that ``pyproject.toml`` is the only place a version literal may appear. Everything else
reads the installed distribution metadata that Poetry builds from it. These tests enforce that,
because a convention nobody checks is a convention that drifts back.
"""

from __future__ import annotations

import re
import tomllib
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as dist_version
from pathlib import Path

import pytest

import biomapper
from biomapper._version import UNINSTALLED_VERSION, resolve_version
from biomapper.benchmarks.provenance import UNKNOWN, client_git_state, package_version

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
INIT_PY = REPO_ROOT / "src" / "biomapper" / "__init__.py"


def _pyproject_version() -> str:
    return tomllib.loads(PYPROJECT.read_text())["tool"]["poetry"]["version"]


def test_dunder_version_matches_installed_distribution() -> None:
    """``biomapper.__version__`` is the installed version, not an independent literal."""
    assert biomapper.__version__ == dist_version("biomapper")


def test_package_version_helper_agrees_with_dunder_version() -> None:
    """The manifest's version field and the importable one cannot disagree.

    They are separate call sites (``provenance.package_version`` and ``biomapper.__version__``);
    this pins them to the same answer so a manifest can be checked against the code that wrote it.
    """
    assert package_version() == biomapper.__version__


def test_installed_version_matches_pyproject() -> None:
    """The installed metadata tracks ``pyproject.toml``.

    A failure here usually means the working tree bumped the version and the environment was not
    reinstalled, which is the stale-editable-install case. That is worth failing on in CI: it is the
    exact condition under which a run records a version that does not describe the code that ran.
    """
    assert dist_version("biomapper") == _pyproject_version()


def test_init_py_contains_no_version_literal() -> None:
    """No hardcoded ``__version__ = "x.y.z"`` may reappear in ``__init__.py``.

    This is the regression that actually happened, so it gets a test that looks for the shape of the
    mistake rather than for one specific stale number.
    """
    literal = re.search(
        r'^__version__\s*=\s*[\'"][0-9]+\.[0-9]+', INIT_PY.read_text(), flags=re.MULTILINE
    )
    assert literal is None, (
        "__init__.py reintroduced a hardcoded version literal. pyproject.toml is the single "
        "source; derive __version__ from importlib.metadata instead."
    )


VERSION_PY = REPO_ROOT / "src" / "biomapper" / "_version.py"


def test_only_pyproject_declares_a_version_literal() -> None:
    """Sweep the package for any other module asserting its own version number.

    The narrow ``__init__.py`` test above catches the exact regression that happened; this catches
    the same mistake made anywhere else in the package, including indented inside a try/except.
    """
    offenders = []
    for path in (REPO_ROOT / "src").rglob("*.py"):
        for match in re.finditer(
            r'^\s*(?:__version__|VERSION)\s*=\s*[\'"]([^\'"]+)[\'"]',
            path.read_text(),
            re.MULTILINE,
        ):
            if match.group(1) == UNINSTALLED_VERSION:
                continue
            if re.match(r"^[0-9]+\.[0-9]+", match.group(1)):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {match.group(1)!r}")
    assert not offenders, f"version literals outside pyproject.toml: {offenders}"


def test_uninstalled_sentinel_is_not_a_plausible_release() -> None:
    """The fallback must be unmistakably not-a-version.

    If it were ever changed to something that looks real, a manifest produced from a bare checkout
    would carry a confident version string describing nothing, which is the failure this whole
    module exists to prevent.
    """
    assert "+" in UNINSTALLED_VERSION
    assert UNINSTALLED_VERSION.startswith("0.0.0")
    assert UNINSTALLED_VERSION in VERSION_PY.read_text()


@pytest.mark.parametrize("field", ["client_git_commit", "client_git_dirty"])
def test_run_provenance_exposes_the_client_commit(field: str) -> None:
    """The manifest identifies the client by commit, not by version alone.

    ``biomapper_version`` comes from installed metadata, which does not move when the working tree
    does. A long suite run on an editable checkout can therefore outlive the code it started from
    while still reporting a single, confident-looking version string. The commit is what closes that
    gap, so its presence on the provenance model is pinned here.
    """
    from biomapper.benchmarks.provenance import build_run_provenance

    provenance = build_run_provenance(api_endpoint="https://example.invalid/api", probe_live=False)
    assert hasattr(provenance, field)


def test_client_git_state_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provenance capture must not be able to abort an otherwise healthy run."""
    import subprocess

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("git is not installed")

    monkeypatch.setattr(subprocess, "run", boom)
    sha, dirty = client_git_state()
    assert sha == UNKNOWN
    assert dirty is None


def test_both_version_fallbacks_agree_when_metadata_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``__version__`` and ``package_version()`` must not disagree in the uninstalled case.

    Greptile P1 on PR #10: the two had separate fallbacks (``0.0.0+unknown`` against ``unknown``),
    so a run from a bare source checkout recorded a manifest version that contradicted the
    importable attribute. Both now route through ``_version.resolve_version``.
    """
    import biomapper._version as version_module

    def not_installed(_name: str) -> str:
        raise PackageNotFoundError("biomapper")

    monkeypatch.setattr(version_module, "_dist_version", not_installed)
    assert resolve_version() == UNINSTALLED_VERSION
    assert package_version() == UNINSTALLED_VERSION


def test_client_git_state_refuses_a_site_packages_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A wheel must never be attributed to an enclosing, unrelated repository.

    Greptile P1 on PR #10: ``git`` searches parent directories, so a wheel installed into a
    virtualenv nested inside someone else's checkout would report THAT project's commit. A manifest
    naming a foreign commit looks like provenance and cannot be spotted as wrong by a reader, which
    is strictly worse than recording no commit at all.
    """
    import biomapper.benchmarks.provenance as provenance_module

    fake = tmp_path / "venv" / "lib" / "python3.12" / "site-packages" / "biomapper"
    fake.mkdir(parents=True)
    installed = fake / "benchmarks" / "provenance.py"
    installed.parent.mkdir(parents=True)
    installed.write_text("")
    monkeypatch.setattr(provenance_module, "__file__", str(installed))

    sha, dirty = client_git_state()
    assert sha == UNKNOWN, "a site-packages install must report no commit"
    assert dirty is None


def test_client_git_state_refuses_an_ancestor_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A repo that merely *contains* the package is not the package's own checkout.

    Covers the non-site-packages half of the same defect: a source tree vendored inside another
    repository resolves a toplevel that is an ancestor, not the package root.
    """
    import subprocess

    import biomapper.benchmarks.provenance as provenance_module

    nested = tmp_path / "outer-repo" / "vendored" / "src" / "biomapper" / "benchmarks"
    nested.mkdir(parents=True)
    module = nested / "provenance.py"
    module.write_text("")
    monkeypatch.setattr(provenance_module, "__file__", str(module))

    real_run = subprocess.run

    def fake_run(cmd: list[str], **kwargs: object) -> object:
        if "--show-toplevel" in cmd:

            class Result:
                stdout = str(tmp_path / "outer-repo") + "\n"

            return Result()
        return real_run(cmd, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(subprocess, "run", fake_run)
    sha, dirty = client_git_state()
    assert sha == UNKNOWN, "an ancestor repository must not be reported as the client checkout"
    assert dirty is None
