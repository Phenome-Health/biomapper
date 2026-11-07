---
title: "Listing a PyPI-shipped stdio MCP server in the MCP registry: the launch command is `uvx <package>==<version>`, so the executable must match the package name and the SDK must be a base dependency"
category: best-practices/
module: biomapper/mcp
date: 2026-11-04
problem_type: best_practice
component: tooling
severity: high
applies_when:
  - "Publishing a local stdio MCP server from an existing PyPI library to registry.modelcontextprotocol.io (registryType pypi, runtimeHint uvx)"
  - "The server's console script name differs from the PyPI package name, or the MCP SDK sits behind an optional extra"
  - "server.json uses runtimeArguments (e.g. --from 'pkg[extra]==X.Y.Z') to work around the launch command"
  - "A release bump rewrites version strings with sed or regex across pyproject.toml and server.json"
symptoms:
  - "Under 1.6.1, the command a registry client builds (`uvx biomapper==1.6.1`) would find neither the MCP SDK (behind the `mcp` extra) nor a `biomapper` executable"
  - "The 1.6.1 bump updated `\"version\": \"1.6.0\"` but left `==1.6.0` inside the runtimeArguments value; the sync test compared only the version fields, so it passed"
root_cause: config_error
resolution_type: config_change
related_components:
  - testing_framework
  - development_workflow
tags:
  - mcp
  - mcp-registry
  - pypi-packaging
  - uvx
  - console-scripts
  - optional-extras
  - release-versioning
  - server-json
---

# Listing a PyPI-shipped stdio MCP server in the MCP registry

## Context

`biomapper` is a PyPI client library first and an MCP server second. 1.6.0 shipped the server
behind an optional `mcp` extra, with a `biomapper-mcp` console script. The documented launch line
was `uvx --from 'biomapper[mcp]' biomapper-mcp`, which works when a human types it.

Just before the first `mcp-publisher publish` we checked how a registry client actually launches
a PyPI package. It builds the command from `server.json` `packages[].identifier` and `version`,
i.e. `uvx biomapper==1.6.1`. The registry docs show no example of an executable name that differs
from the package name, and none that uses extras or `--from`. Whether a given client applies
`runtimeArguments` is undocumented. A registry install of 1.6.1 would therefore have found neither
the MCP SDK (behind the extra) nor a `biomapper` executable: a listing that could not start.

A second defect was already in the shipped 1.6.1. Its `runtimeArguments` `--from` value still said
`biomapper[mcp]==1.6.0`. The release bump had run sed on `"version": "1.6.0"`, and the test
compared only the `version` fields, so the pin inside a string value went unnoticed.

Neither defect reached a user. Both were caught before publishing, and 1.6.2 was released and
listed instead.

## Guidance

1. **Make the literal registry command work: `uvx <identifier>==<version>`.** That means no extras,
   no `--from`, and no `runtimeArguments`.
   - Put the MCP SDK in **base dependencies**.
   - Add a console script named **exactly like the PyPI package**, and keep the old script name as
     an alias.
2. **Remove the extra rather than keeping it as an empty alias.** A Poetry extra can only name
   optional dependencies, and an empty advertised extra is its own trap (`biomapper` 1.5.2 shipped
   four empty extras; see CHANGELOG 1.5.3). Removing it is safe because uv and pip only *warn* on an
   unknown extra and install anyway. The legacy `uvx --from 'biomapper[mcp]' biomapper-mcp` config
   was verified against the built 1.6.2 wheel.
3. **Keep `import <package>` free of the SDK.** Import the SDK lazily inside the server entry point.
   If the repo has a "core install stays light" guard test, update it to allow exactly the new
   dependency, and cite the decision in a comment. Don't delete the guard.
4. **Keep the version only in server.json's `version` fields.** Drop `runtimeArguments`, and test
   that no `==` appears anywhere in the package block. A future bump then cannot leave a stale pin
   behind, whatever tool performs it.
5. **Prove the registry launch line before publishing,** using the recipe below. Run it through a
   real stdio MCP client, not a bare shell.

## Why This Matters

- **A dead registry listing fails quietly.** The user sees a generic "server failed to start" in
  their MCP client, not a packaging error.
- **Fixing it costs a release.** A PyPI version can't be re-uploaded, so the fix needs a version
  bump, a release and a republish.
- **runtimeArguments is untested ground.** Its handling is client behavior that isn't verified, and
  any version written by hand inside it goes stale on the next bump. That happened here within one
  release.
- **The cost is real and should be recorded.** Every `pip install biomapper` now pulls the SDK tree
  (starlette, uvicorn, httpx2, opentelemetry) and its `pydantic>=2.12` floor. It is recorded in
  CHANGELOG 1.6.2 and in the guard test comment, so the next person sees a decision, not an
  accident.

## When to Apply

- Before the first `mcp-publisher publish` of any server whose package is a library first.
- Whenever the server executable isn't named after the package, or its dependencies live in an
  extra.
- On every release that touches `server.json`. Let the test, not the bump tool, enforce
  consistency.

## Examples

**server.json package block**

Before (`git show v1.6.1:server.json`, note the stale pin):

```json
"identifier": "biomapper",
"version": "1.6.1",
"runtimeHint": "uvx",
"runtimeArguments": [
  {
    "type": "named",
    "name": "--from",
    "value": "biomapper[mcp]==1.6.0",
    "description": "Install the mcp extra, which carries the MCP SDK"
  }
],
"transport": { "type": "stdio" },
```

After (1.6.2):

```json
"identifier": "biomapper",
"version": "1.6.2",
"runtimeHint": "uvx",
"transport": { "type": "stdio" },
```

**pyproject.toml (Poetry)**

Before:

```toml
mcp          = {version = ">=2.3,<3", optional = true}

[tool.poetry.extras]
mcp        = ["mcp"]
all        = [..., "defusedxml", "mcp"]

[tool.poetry.scripts]
biomapper-mcp = "biomapper.mcp:main"
```

After:

```toml
[tool.poetry.dependencies]
# A base dependency since 1.6.2, not an extra: MCP registry clients launch `uvx <package>` and
# there is no documented way to make them install an extra.
mcp           = ">=2.3,<3"

[tool.poetry.extras]
all        = [..., "defusedxml"]          # no `mcp` extra

[tool.poetry.scripts]
biomapper     = "biomapper.mcp:main"      # what `uvx biomapper` runs
biomapper-mcp = "biomapper.mcp:main"      # 1.6.0/1.6.1 alias
```

**Tests** (`tests/mcp_server/test_packaging_mcp.py`, `tests/test_packaging_extras.py`)

```python
# server.json: nothing hand-pinned outside `version`
assert "runtimeArguments" not in package
assert "==" not in json.dumps(package), "no hand-written version pins outside `version`"

# built wheel: the SDK is required unconditionally, and both scripts exist
assert not re.search(r"^Provides-Extra: mcp$", metadata, re.MULTILINE)
lines = re.findall(r"^Requires-Dist: (mcp\b.*)$", metadata, re.MULTILINE)
assert len(lines) == 1 and "extra ==" not in lines[0]

# core-light guard: allow exactly the deliberate addition
    "python-dotenv",
    # Deliberate, 1.6.2 (Trent, 2026-11-04): MCP registry clients launch `uvx biomapper` and
    # cannot install an extra, so the SDK must be in core. See CHANGELOG 1.6.2.
    "mcp",
```

**Verification recipe: prove the listing will launch, before publishing**

1. Build the wheel: `poetry build --format wheel`.
2. Launch it with the identifier as the command, exactly as the registry will, from inside a real
   stdio MCP client. Assert on `list_tools()`:

   ```python
   params = StdioServerParameters(command="uvx", args=["--from", WHEEL, "biomapper"])
   async with Client(params) as c:
       assert len((await c.list_tools()).tools) == 10
   ```

3. Check that legacy configs still work against the same wheel:
   `uvx --from 'biomapper[mcp] @ file://<abs-wheel-path>' biomapper-mcp` should warn about the
   unknown extra and still list the tools.
4. Upload to PyPI, then repeat step 2 with the literal registry line, `uvx biomapper==<ver>`.
   Pre-warm with `uvx --refresh` first (see below). Only then run `mcp-publisher publish`.

For 1.6.2, all four steps passed (10 tools each), then `mcp-publisher publish` succeeded and the
registry entry `io.github.trentleslie/biomapper` showed `status: active`.

**Gotchas hit along the way**

- **uv's index is stale right after a PyPI upload.** For a few minutes uv reports that the new
  version does not exist. Use `--refresh`.
- **The first cold install can be slow.** With `--refresh` and nothing cached, the first launch
  failed once with "Connection closed" inside the test client, apparently an init timeout; an
  immediate retry passed. This was observed once, not measured. Pre-warm before a client test, and
  expect a slow first start for users.
- **The registry ownership check reads the published README.** The `mcp-name:
  io.github.<user>/<name>` marker must be in the README of the version already on PyPI. GitHub
  login only grants the `io.github.<user>/*` namespace.
- **zsh array subscripts can silently change what you test.** `"$W[mcp]"` is a zsh array
  subscript, so a smoke test built that way ran from the source tree instead of the wheel extra
  and still passed. Build the requirement spec in a language that doesn't expand it, or quote the
  whole thing (`"biomapper[mcp] @ file://${W}"`), and check which install actually ran.

## Related

- `docs/solutions/best-practices/mcp-server-file-jobs-review-gap-2026-11-03.md`: the same module's
  1.6.0 build. Its "Packaging" line and its open `mcp-publisher` item are superseded by this doc.
- `docs/plans/2026-11-03-001-feat-mcp-server-plan.md`: the plan anticipated this risk. Its fallback
  ("if server.json cannot express the extra, promote `mcp` to a base dependency") is the branch
  that fired.
- CHANGELOG 1.5.3 (empty extras) and 1.6.2 (this change); fork PR #37, org PR #8.
