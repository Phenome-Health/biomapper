"""MCP server tests never touch the network.

Every route is respx-mocked, but the server's startup pin probe runs in a worker thread that is
deliberately abandoned on shutdown (so a slow ``/health`` never delays exit). Such a thread can
outlive its test's respx context and would then attempt a real DNS lookup. Name resolution is
blocked for the whole session so that can never reach a real host, prod included.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any

import pytest

_LOCAL = {"localhost", "127.0.0.1", "::1"}


@pytest.fixture(autouse=True, scope="session")
def _no_real_dns() -> Iterator[None]:
    real = socket.getaddrinfo

    def guarded(host: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        if host in _LOCAL or host is None:
            return real(host, *args, **kwargs)
        raise socket.gaierror(socket.EAI_NONAME, f"DNS blocked in MCP tests: {host}")

    socket.getaddrinfo = guarded
    try:
        yield
    finally:
        socket.getaddrinfo = real
