"""Local stdio MCP server for BioMapper entity resolution (``biomapper-mcp``).

Install with the ``mcp`` extra and launch with one command::

    uvx --from 'biomapper[mcp]' biomapper-mcp

This package is an import gate: it never imports the MCP SDK itself, so ``import biomapper.mcp`` is
safe on a core install, and :func:`main` turns a missing extra into an install hint instead of a
traceback. The server lives in :mod:`biomapper.mcp.server`, which imports the SDK at module level.

stdout is the MCP transport. Nothing in this package writes to it; logs go to stderr.
"""

from __future__ import annotations

import sys

INSTALL_HINT = (
    "biomapper-mcp needs the MCP SDK, which ships in the 'mcp' extra.\n"
    "Install it with:  pip install 'biomapper[mcp]'\n"
    "or launch with:   uvx --from 'biomapper[mcp]' biomapper-mcp\n"
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
