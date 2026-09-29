"""Run provenance: what actually served the answers.

This is the reason the suite points at a deployment instead of an engine checkout. A local
git SHA describes the *client*; ``GET {kestrel}/health`` describes the graph that produced
the numbers. Every manifest records ``kestrel_version``, ``kg_version``,
``biolink_version``, ``build_timestamp``, ``git_commit``, the full ``source_versions`` map,
the dataset source SHA, the package version, and the run id.

Ported from ``biomapper2.provenance``; the ``/health`` read is unchanged.

**Never hardcode a build.** Notes claiming KRAKEN 2.0.1 and a docstring claiming 2.1.0 are
both stale; ``/health`` is the only authority. Do not read the service version from
``/openapi.json`` either: its ``info.version`` is a stale framework default (``0.1.0``).

The ``sources`` list doubles as the **circularity register**. Four of KRAKEN's ingested
sources are gold sources for arms in this suite — ``lipidmaps`` (LMSD), ``refmet`` (RefMet),
``loinc`` (clinical labs), ``ncbigene`` (the gene arms) — so those arms measure coverage,
not independent accuracy. :func:`circularity_notes` surfaces that per run rather than
leaving it to be argued from memory.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from biomapper._version import resolve_version

DEFAULT_KESTREL_URL = "https://kestrel.krakenkg.com/api"
HEALTH_TIMEOUT = 15.0

UNKNOWN = "unknown"

# Arms whose gold source is ingested into the graph being measured. Value is the KRAKEN
# source name to look for in /health's `sources`, so the label is derived from what the
# build actually reports rather than asserted from memory.
GOLD_SOURCE_IN_GRAPH: dict[str, str] = {
    "lmsd": "lipidmaps",
    "refmet": "refmet",
    "nlmgene": "ncbigene",
    "hgnc": "ncbigene",
}

logger = logging.getLogger(__name__)


class KgBuildInfo(BaseModel):
    """KG build metadata as served by Kestrel ``/health`` (originates in KRAKEN build_info).

    ``extra="allow"`` deliberately: operational fields (``steps_run``,
    ``build_duration_minutes``) and future enrichments pass through untyped rather than
    being dropped. For a provenance record, silently discarding a field the upstream gained
    is worse than carrying one we do not model.
    """

    model_config = ConfigDict(extra="allow")

    kg_version: str = UNKNOWN
    kraken_package_version: str = UNKNOWN
    biolink_version: str = UNKNOWN
    build_timestamp: str = UNKNOWN
    git_commit: str = UNKNOWN
    sources: list[str] = Field(default_factory=list)
    source_versions: dict[str, str] = Field(default_factory=dict)
    kg_label: str | None = None


class RunProvenance(BaseModel):
    """Everything needed to interpret and reproduce a run. Stamped into every manifest."""

    run_id: str
    biomapper_version: str
    api_endpoint: str
    kestrel_url: str
    kestrel_version: str = UNKNOWN
    run_timestamp: str = ""
    kg_build: KgBuildInfo = Field(default_factory=KgBuildInfo)
    health_error: str | None = None
    # The CLIENT commit, distinct from ``kg_build.git_commit`` (the graph build). Captured at run
    # start, because a long suite can outlive the checkout it started from: a 17-hour run on an
    # editable install took 20+ commits mid-flight, and ``biomapper_version`` could not have
    # detected that, since the installed metadata does not move when the working tree does.
    client_git_commit: str = UNKNOWN
    client_git_dirty: bool | None = None

    @property
    def pinned(self) -> bool:
        """Whether this provenance actually names a build, rather than recording 'unknown'.

        A suite run is by definition live, so ``pinned is False`` means the numbers cannot be
        attributed to a graph. That is worse than having no pins at all, because 'unknown'
        still *looks* like provenance — callers surface it rather than passing it off.
        """
        return self.kestrel_version != UNKNOWN and self.kg_build.kg_version != UNKNOWN


def package_version() -> str:
    """The client package version recorded in every run manifest.

    Reads the installed distribution metadata, the same source ``biomapper.__version__`` uses, so a
    manifest and the importing code can never disagree about which client produced a number. Do not
    reintroduce a literal here or in ``biomapper/__init__.py``: those two literals drifted once
    already (pyproject 1.5.1 against a hardcoded 1.4.0) and a manifest cannot be audited against a
    version string that two files answer differently.

    Caveat worth knowing when reading an old manifest: this is the *installed* version, which in an
    editable checkout goes stale against ``pyproject.toml`` until the package is reinstalled. It
    answers "what code ran" only as precisely as the install is fresh, which is why the manifest
    also records ``client_git_commit``.
    """
    return resolve_version()


def client_git_state() -> tuple[str, bool | None]:
    """Return ``(commit_sha, is_dirty)`` for the checkout this package is imported from.

    Returns ``(UNKNOWN, None)`` for an installed wheel, which has no repository, and that is a
    correct answer rather than a failure: a wheel's version string IS its identity. The pair only
    carries information for a source or editable install, which is exactly the case where
    ``package_version()`` can go stale against the working tree.

    Two guards keep a wheel from being attributed to somebody else's repository. ``git`` searches
    parent directories, so a wheel installed into a project-local virtualenv nested inside an
    unrelated checkout would otherwise report THAT checkout's commit and dirty flag. A manifest
    naming a commit from a different project is worse than one naming no commit, because it looks
    like provenance and a reader cannot tell it is wrong. So: refuse any path inside a
    ``site-packages`` / ``dist-packages`` tree, and require the discovered repository root to BE
    the package root rather than merely contain it.

    Never raises. Provenance capture must not be able to abort a run that is otherwise fine.
    """
    import subprocess

    here = Path(__file__).resolve()
    if any(part in {"site-packages", "dist-packages"} for part in here.parts):
        return UNKNOWN, None

    # .../<repo>/src/biomapper/benchmarks/provenance.py -> <repo>
    repo = here.parent.parent.parent.parent
    try:
        toplevel = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
        if not toplevel or Path(toplevel).resolve() != repo:
            # A repository was found, but it is an ancestor rather than this package's own
            # checkout. That is somebody else's project; report no commit.
            return UNKNOWN, None
        sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "-C", str(repo), "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            ).stdout.strip()
        )
        return (sha or UNKNOWN), dirty
    except Exception:  # noqa: BLE001 - no git, not a repo, or git unavailable; all mean "no commit"
        return UNKNOWN, None


def utc_stamp() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def new_run_id(prefix: str = "run") -> str:
    """A run id that sorts chronologically and is unique within a second.

    Timestamp alone collides when two arms start in the same second; a bare uuid does not
    sort. Both, so the id is a usable directory name and a usable join key.
    """
    return f"{prefix}_{utc_stamp()}_{uuid.uuid4().hex[:8]}"


def fetch_kg_build_info(
    kestrel_url: str = DEFAULT_KESTREL_URL,
) -> tuple[str, KgBuildInfo, str | None]:
    """Return ``(kestrel_version, KgBuildInfo, error)`` from Kestrel ``/health``.

    Keyless by design: the public KRAKEN endpoint takes no key and must never be sent one.

    Degrades rather than raising — the caller always gets a usable object, so a manifest
    records that provenance was unavailable instead of omitting the field. The third element
    is the error text, present precisely so "we could not read the build" is distinguishable
    from "the build self-reports unknown".
    """
    url = f"{kestrel_url.rstrip('/')}/health"
    try:
        with httpx.Client(timeout=HEALTH_TIMEOUT, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:  # noqa: BLE001 — provenance must never abort a run
        message = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "Could not read Kestrel /health at %s (%s); recording 'unknown'", url, message
        )
        return UNKNOWN, KgBuildInfo(), message

    kestrel_version = data.get("kestrel_version", UNKNOWN)
    kg_build_raw = data.get("kg_build") or {}
    if not kg_build_raw:
        message = f"Kestrel /health at {url} returned an empty kg_build — KG provenance unavailable"
        logger.warning(message)
        return kestrel_version, KgBuildInfo(), message
    return kestrel_version, KgBuildInfo.model_validate(kg_build_raw), None


def build_run_provenance(
    *,
    api_endpoint: str,
    kestrel_url: str = DEFAULT_KESTREL_URL,
    run_id: str | None = None,
    probe_live: bool = True,
) -> RunProvenance:
    """Assemble a :class:`RunProvenance`.

    ``probe_live=False`` keeps construction offline for unit tests. Live run paths leave it
    on and read the build once, then share it across arms: sampling per-arm would attribute
    every result to whatever build happened to be serving when that arm ran.
    """
    kestrel_version, kg_build, error = (
        fetch_kg_build_info(kestrel_url)
        if probe_live
        else (UNKNOWN, KgBuildInfo(), "probe_live=False")
    )
    client_sha, client_dirty = client_git_state()
    return RunProvenance(
        run_id=run_id or new_run_id(),
        biomapper_version=package_version(),
        api_endpoint=api_endpoint,
        kestrel_url=kestrel_url,
        kestrel_version=kestrel_version,
        run_timestamp=dt.datetime.now(dt.UTC).isoformat(),
        kg_build=kg_build,
        health_error=error,
        client_git_commit=client_sha,
        client_git_dirty=client_dirty,
    )


def circularity_notes(kg_build: KgBuildInfo, datasets: list[str]) -> dict[str, dict[str, Any]]:
    """Per-arm accuracy-vs-coverage labels, derived from the build's own ``sources`` list.

    An arm whose gold source is ingested into the graph is measuring **coverage**: its gold
    identifier and BioMapper's answer come from the same place. Reported per run against the
    live source list, so the label tracks the build rather than a note that can go stale.

    Absence of a source is not evidence of independence, only of non-circularity through
    *that* source, so the label is ``accuracy_candidate`` rather than ``accuracy``: the full
    verdict also depends on the resolution path, which this function cannot see.
    """
    present = {s.lower() for s in kg_build.sources}
    versions = {k.lower(): v for k, v in kg_build.source_versions.items()}
    notes: dict[str, dict[str, Any]] = {}
    for dataset in datasets:
        source = GOLD_SOURCE_IN_GRAPH.get(dataset)
        if source and source.lower() in present:
            notes[dataset] = {
                "label": "coverage",
                "reason": (
                    f"gold source {source!r} is ingested into the graph being measured "
                    f"(version {versions.get(source.lower(), UNKNOWN)}), so the gold identifier "
                    f"and "
                    f"BioMapper's answer share a source. Report as coverage, not accuracy."
                ),
                "gold_source_in_graph": source,
            }
        else:
            notes[dataset] = {
                "label": "accuracy_candidate",
                "reason": (
                    "no gold source for this arm appears in the graph's ingested source list. "
                    "Not a full independence verdict: circularity also depends on whether the "
                    "gold's service sits in the resolution path."
                ),
                "gold_source_in_graph": None,
            }
    return notes
