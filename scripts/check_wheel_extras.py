#!/usr/bin/env python3
"""Assert the BUILT wheel's metadata actually gates dependencies on each declared extra.

``tests/test_packaging_extras.py`` checks ``pyproject.toml``, which is the input to publishing.
This checks the output. Both are needed, and the reason is the bug that prompted them: 1.5.2's
``pyproject.toml`` declared four extras and the wheel published them with zero ``Requires-Dist``,
because the packages lived in Poetry groups. A configuration-level test catches that particular
mistake, but any future change to the build backend, the lockfile, or Poetry itself could
reintroduce the same *symptom* from a different cause, and a source-only check would stay green.

The rule this enforces: an extra that is advertised must be able to install something. An empty
advertised extra is worse than an absent one, because ``pip install pkg[extra]`` succeeds silently
and the user discovers the gap at first import.

Usage:
    python scripts/check_wheel_extras.py                 # builds into dist/ then checks
    python scripts/check_wheel_extras.py path/to/x.whl   # checks an existing wheel
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def declared_extras() -> dict[str, list[str]]:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    return data["tool"]["poetry"].get("extras", {})


def build_wheel() -> Path:
    subprocess.run(
        ["poetry", "build", "--format", "wheel"], cwd=REPO_ROOT, check=True, capture_output=True
    )
    version = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["tool"]["poetry"]["version"]
    wheel = REPO_ROOT / "dist" / f"biomapper-{version}-py3-none-any.whl"
    if not wheel.exists():
        raise SystemExit(f"expected a wheel at {wheel} after build, found none")
    return wheel


def wheel_metadata(wheel: Path) -> str:
    with zipfile.ZipFile(wheel) as zf:
        name = next(n for n in zf.namelist() if n.endswith(".dist-info/METADATA"))
        return zf.read(name).decode()


def main(argv: list[str]) -> int:
    wheel = Path(argv[1]) if len(argv) > 1 else build_wheel()
    metadata = wheel_metadata(wheel)

    provided = set(re.findall(r"^Provides-Extra:\s*(.+)$", metadata, re.MULTILINE))
    # Requires-Dist lines carry an environment marker naming the extras that pull them in.
    gated: dict[str, set[str]] = {}
    for line in re.findall(r"^Requires-Dist:\s*(.+)$", metadata, re.MULTILINE):
        package = re.split(r"[\s(<>=!;\[]", line.strip(), maxsplit=1)[0]
        for extra in re.findall(r'extra\s*==\s*[\'"]([^\'"]+)[\'"]', line):
            gated.setdefault(extra, set()).add(package)

    expected = declared_extras()
    problems: list[str] = []

    missing_advert = set(expected) - provided
    if missing_advert:
        problems.append(
            f"pyproject declares extras absent from the wheel: {sorted(missing_advert)}"
        )

    for extra, packages in sorted(expected.items()):
        got = gated.get(extra, set())
        if not got:
            problems.append(
                f"extra {extra!r} is advertised by the wheel but gates NO requirements. "
                f"'pip install biomapper[{extra}]' would install nothing extra."
            )
            continue
        absent = sorted(set(packages) - got)
        if absent:
            problems.append(f"extra {extra!r} is missing {absent} in the wheel metadata")

    print(f"wheel: {wheel.name}")
    print(f"advertised extras: {sorted(provided)}")
    for extra in sorted(expected):
        print(f"  {extra:11s} gates {sorted(gated.get(extra, set()))}")

    if problems:
        print("\nFAIL")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\nOK: every declared extra gates its dependencies in the built wheel")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
