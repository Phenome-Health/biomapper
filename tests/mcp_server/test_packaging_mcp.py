"""Unit 2 (and 5): the MCP server ships in the core install with a ``biomapper`` console script.

Since 1.6.2 the launch line is ``uvx biomapper``: MCP registry clients run ``uvx <package>`` and
have no documented way to install an extra, so the SDK is a base dependency and the console script
is named after the package. ``biomapper-mcp`` stays as an alias for 1.6.0/1.6.1 configs.
``import biomapper`` must still never import the SDK, and a broken install (SDK missing) must fail
with an install hint rather than a traceback.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _poetry() -> dict:
    return tomllib.loads(PYPROJECT.read_text())["tool"]["poetry"]


def test_mcp_is_a_base_dependency_on_the_2x_sdk() -> None:
    spec = _poetry()["dependencies"]["mcp"]
    version = spec if isinstance(spec, str) else spec["version"]
    assert not (isinstance(spec, dict) and spec.get("optional")), "uvx biomapper needs it in core"
    assert version.startswith(">=2.3")
    assert "<3" in version


def test_no_mcp_extra_and_both_console_scripts_are_declared() -> None:
    poetry = _poetry()
    # An extra naming a non-optional dependency would advertise an empty extra; uv and pip only
    # warn on an unknown extra, so 1.6.0-era `biomapper[mcp]` configs keep installing.
    assert "mcp" not in poetry["extras"]
    assert "mcp" not in poetry["extras"]["all"]
    assert poetry["scripts"]["biomapper"] == "biomapper.mcp:main"
    assert poetry["scripts"]["biomapper-mcp"] == "biomapper.mcp:main"


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    poetry_bin = shutil.which("poetry")
    if poetry_bin is None:
        pytest.skip("poetry is not on PATH; the CI wheel-extras step covers the built artifact")
    out = tmp_path_factory.mktemp("dist")
    subprocess.run(
        [poetry_bin, "build", "--format", "wheel", "--output", str(out)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    wheels = list(out.glob("biomapper-*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def _wheel_file(wheel: Path, suffix: str) -> str:
    with zipfile.ZipFile(wheel) as zf:
        name = next(n for n in zf.namelist() if n.endswith(suffix))
        return zf.read(name).decode()


def test_built_wheel_requires_mcp_unconditionally(built_wheel: Path) -> None:
    metadata = _wheel_file(built_wheel, ".dist-info/METADATA")
    assert not re.search(r"^Provides-Extra: mcp$", metadata, re.MULTILINE)
    lines = re.findall(r"^Requires-Dist: (mcp\b.*)$", metadata, re.MULTILINE)
    assert len(lines) == 1, lines
    assert "extra ==" not in lines[0], "mcp must not be gated on an extra"


def test_built_wheel_declares_both_console_scripts(built_wheel: Path) -> None:
    entry_points = _wheel_file(built_wheel, ".dist-info/entry_points.txt")
    for script in ("biomapper", "biomapper-mcp"):
        assert re.search(rf"^{script}\s*=\s*biomapper\.mcp:main$", entry_points, re.MULTILINE)


def test_main_without_the_sdk_exits_with_an_install_hint() -> None:
    code = (
        "import sys\n"
        "sys.modules['mcp'] = None  # simulate a core install without the extra\n"
        "from biomapper.mcp import main\n"
        "main()\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 1
    assert "uvx biomapper" in proc.stderr
    assert "Traceback" not in proc.stderr
    assert proc.stdout == "", "stdout is the MCP transport; nothing else may write to it"


def test_importing_biomapper_does_not_import_the_sdk() -> None:
    code = (
        "import sys\n"
        "import biomapper\n"
        "import biomapper.mcp\n"
        "print(json.dumps(sorted(m for m in sys.modules if m == 'mcp' or m.startswith('mcp.'))))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", "import json\n" + code], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == []


def test_package_root_does_not_bind_the_mcp_subpackage() -> None:
    """``biomapper.mcp`` must stay reachable only as a submodule import (no root re-export)."""
    import biomapper

    assert "mcp" not in biomapper.__all__


async def test_the_stdio_server_starts_and_lists_its_tools(tmp_path: Path) -> None:
    """End to end over real stdio: the module entry point speaks MCP on stdout and nothing else.

    The API and Kestrel URLs point at a closed local port, so the startup probe fails fast and
    nothing leaves the machine.
    """
    import os

    from mcp import Client, StdioServerParameters

    env = {
        **os.environ,
        "BIOMAPPER_BASE_URL": "http://127.0.0.1:9/api/v1",
        "BIOMAPPER_KESTREL_URL": "http://127.0.0.1:9/api",
        "BIOMAPPER_MCP_HOME": str(tmp_path / "home"),
    }
    env.pop("BIOMAPPER_API_KEY", None)
    params = StdioServerParameters(command=sys.executable, args=["-m", "biomapper.mcp"], env=env)
    async with Client(params) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert {"map_entity", "map_batch", "map_file", "job_status", "resume_job", "cancel_job",
            "list_entity_types", "list_vocabularies", "list_annotators", "server_info"} <= names


# ---------------------------------------------------------------------------
# Unit 5: registry metadata and the README marker
# ---------------------------------------------------------------------------

SERVER_JSON = REPO_ROOT / "server.json"
README = REPO_ROOT / "README.md"
REGISTRY_NAME = "io.github.trentleslie/biomapper"


def test_server_json_matches_pyproject() -> None:
    meta = json.loads(SERVER_JSON.read_text())
    version = _poetry()["version"]
    assert meta["name"] == REGISTRY_NAME
    assert meta["version"] == version
    assert len(meta["description"]) <= 100
    (package,) = meta["packages"]
    assert package["registryType"] == "pypi"
    assert package["identifier"] == _poetry()["name"]
    assert package["version"] == version
    assert package["transport"] == {"type": "stdio"}
    assert package["runtimeHint"] == "uvx"
    # Clients build the launch line from identifier + version; anything in runtimeArguments is
    # unverified client behaviour, and 1.6.1 shipped a stale `==1.6.0` pin in there unnoticed.
    assert "runtimeArguments" not in package
    assert "==" not in json.dumps(package), "no hand-written version pins outside `version`"
    secrets = {v["name"] for v in package["environmentVariables"] if v["isSecret"]}
    assert secrets == {"BIOMAPPER_API_KEY"}


def test_readme_carries_the_registry_marker_exactly_once() -> None:
    text = README.read_text()
    hits = re.findall(r"mcp-name: (\S+)(\s|-->)", text)
    assert len(hits) == 1, hits
    token, after = hits[0]
    assert token == REGISTRY_NAME, f"marker token {token!r} has trailing punctuation or a typo"
    assert after.isspace() or after == "-->"
