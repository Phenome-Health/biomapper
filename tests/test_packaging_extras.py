"""Every declared extra must actually carry dependencies into the built distribution.

Background. 1.5.2 shipped a `benchmarks` extra that installed nothing. The extras were declared
correctly in ``[tool.poetry.extras]``, but the packages they named were declared only in
``[tool.poetry.group.*.dependencies]``. Poetry groups are a local development concept and are never
written into wheel metadata, so every extra published with no ``Requires-Dist`` at all.
``pip install 'biomapper[benchmarks]'`` therefore produced a package that could not import pandas.

CI did not catch it, and the reason matters: ``poetry install --all-extras`` also installs all
non-optional groups, so the group declarations satisfied the test suite locally and in CI while the
published artifact was broken. Green CI was actively misleading here.

These tests read ``pyproject.toml`` directly, which is the one artifact that governs what gets
published, rather than relying on the installed environment.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text())


def _main_dependencies() -> dict:
    return _pyproject()["tool"]["poetry"]["dependencies"]


def _extras() -> dict:
    return _pyproject()["tool"]["poetry"].get("extras", {})


def test_extras_are_declared() -> None:
    """Guard the fixture itself: if extras vanish, the rest of this file silently passes."""
    extras = _extras()
    assert extras, "no extras declared; these tests would vacuously pass"
    assert "benchmarks" in extras


@pytest.mark.parametrize("extra", sorted(_extras()))
def test_extra_packages_are_declared_in_main_dependencies(extra: str) -> None:
    """Each package named by an extra must be an optional main dependency.

    This is the check that was missing. A package named by an extra but declared only in a group is
    invisible to the wheel, so the extra publishes empty.
    """
    main = _main_dependencies()
    missing = [pkg for pkg in _extras()[extra] if pkg not in main]
    assert not missing, (
        f"extra {extra!r} names {missing}, which are not in [tool.poetry.dependencies]. "
        f"Poetry groups are not published in wheel metadata, so declaring them only in a "
        f"[tool.poetry.group.*] block makes this extra install nothing."
    )


@pytest.mark.parametrize("extra", sorted(_extras()))
def test_extra_packages_are_marked_optional(extra: str) -> None:
    """An extra's packages must be ``optional = true``, or they become mandatory for everyone.

    The opposite failure to the one that shipped: a non-optional declaration would drag pandas and
    rdkit into every core install.
    """
    main = _main_dependencies()
    not_optional = []
    for pkg in _extras()[extra]:
        spec = main.get(pkg)
        if not isinstance(spec, dict) or not spec.get("optional", False):
            not_optional.append(pkg)
    assert not not_optional, (
        f"extra {extra!r} names {not_optional}, which are not marked optional=true. "
        f"They would be installed for every user, not just those requesting the extra."
    )


def test_benchmark_extra_covers_what_the_suite_imports() -> None:
    """The benchmarks extra must name every third-party package the suite actually needs.

    Pinned explicitly rather than derived, because the failure mode is a missing entry and a test
    that derives the list from the same source cannot detect an omission.
    """
    required = {"pandas", "openpyxl", "requests", "rdkit", "defusedxml"}
    declared = set(_extras()["benchmarks"])
    assert required <= declared, f"benchmarks extra is missing {sorted(required - declared)}"


def test_all_extra_is_the_union_of_the_others() -> None:
    """``all`` must not silently drift behind the extras it claims to aggregate."""
    extras = _extras()
    union: set[str] = set()
    for name, packages in extras.items():
        if name != "all":
            union |= set(packages)
    assert set(extras["all"]) == union, (
        f"the 'all' extra is not the union of the others; "
        f"missing {sorted(union - set(extras['all']))}, "
        f"extra {sorted(set(extras['all']) - union)}"
    )


def _make_wheel(tmp_path: Path, metadata: str) -> Path:
    """A minimal wheel carrying just the METADATA the checker reads."""
    import zipfile

    wheel = tmp_path / "biomapper-0.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as zf:
        zf.writestr("biomapper-0.0.0.dist-info/METADATA", metadata)
    return wheel


def _run_checker(wheel: Path) -> int:
    import importlib.util

    script = REPO_ROOT / "scripts" / "check_wheel_extras.py"
    spec = importlib.util.spec_from_file_location("check_wheel_extras", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return int(module.main(["check_wheel_extras.py", str(wheel)]))


def test_wheel_checker_rejects_an_empty_extra(tmp_path: Path) -> None:
    """The 1.5.2 shape: extras advertised, nothing gated on them.

    This is the exact artifact that shipped, reconstructed, so the checker is proven against the
    failure rather than only against the fix.
    """
    metadata = (
        "Metadata-Version: 2.1\nName: biomapper\nVersion: 0.0.0\n"
        "Provides-Extra: benchmarks\n"
        "Requires-Dist: httpx (>=0.27,<0.28)\n"
    )
    assert _run_checker(_make_wheel(tmp_path, metadata)) == 1


def test_wheel_checker_rejects_a_partially_populated_extra(tmp_path: Path) -> None:
    """An extra that gates some but not all of its declared packages must also fail."""
    metadata = (
        "Metadata-Version: 2.1\nName: biomapper\nVersion: 0.0.0\n"
        "Provides-Extra: benchmarks\n"
        'Requires-Dist: pandas (>=2.0,<3.0) ; extra == "benchmarks"\n'
    )
    assert _run_checker(_make_wheel(tmp_path, metadata)) == 1


def test_wheel_checker_accepts_a_fully_gated_extra(tmp_path: Path) -> None:
    """And it must pass when every declared package is gated, or it is useless."""
    lines = ["Metadata-Version: 2.1", "Name: biomapper", "Version: 0.0.0"]
    for extra, packages in _extras().items():
        lines.append(f"Provides-Extra: {extra}")
        for pkg in packages:
            lines.append(f'Requires-Dist: {pkg} ; extra == "{extra}"')
    assert _run_checker(_make_wheel(tmp_path, "\n".join(lines) + "\n")) == 0


def test_core_install_stays_light() -> None:
    """The non-optional dependency set must stay small.

    A core `pip install biomapper` is an async HTTP client plus models. If pandas or rdkit ever
    becomes mandatory, that is a significant regression for library consumers and should be a
    deliberate decision rather than a side effect of fixing an extra.
    """
    main = _main_dependencies()
    mandatory = {
        name
        for name, spec in main.items()
        if name != "python" and not (isinstance(spec, dict) and spec.get("optional", False))
    }
    assert mandatory == {
        "httpx",
        "pydantic",
        "python-dotenv",
    }, f"core dependency set changed to {sorted(mandatory)}; heavy packages must stay optional"
