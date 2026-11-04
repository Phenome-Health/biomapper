"""Local stdio MCP server for BioMapper entity resolution (``biomapper``, alias ``biomapper-mcp``).

The MCP SDK is a base dependency (since 1.6.2), so one command launches it::

    uvx biomapper

This package still never imports the SDK itself, so ``import biomapper`` stays light, and
:func:`main` turns a broken install (SDK missing) into an install hint instead of a traceback.
The server lives in :mod:`biomapper.mcp.server`, which imports the SDK at module level.

stdout is the MCP transport. Nothing in this package writes to it; logs go to stderr.
"""

from __future__ import annotations

import sys

INSTALL_HINT = (
    "The BioMapper MCP server needs the MCP SDK (`mcp`), which this install is missing.\n"
    "Reinstall with:  pip install --upgrade 'biomapper>=1.6.2'\n"
    "or launch with:  uvx biomapper\n"
)


def main() -> None:
    """Console-script entry point: run the stdio server, or exit 1 with an install hint."""
    try:
        import mcp.server.mcpserver  # noqa: F401 — presence check only
    except ImportError:
        sys.stderr.write(INSTALL_HINT)
        raise SystemExit(1) from None

    from biomapper.mcp.server import run

    run()
