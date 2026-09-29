"""External benchmark suite for BioMapper, run against a deployment over the REST API.

Why API-only: running the harness in-process against an engine checkout measures a *library*,
not the deployed service the paper describes. Pointing at a deployment also pins provenance to
the backend that actually served the answers (Kestrel ``/health``: service version, KG build,
biolink version, build commit, per-source versions) rather than to a client git SHA, and it makes
the suite pip-installable and public — so a reader can ``pip install 'biomapper[benchmarks]'``
and re-run against the keyless public endpoint instead of cloning a fork branch.

Usage::

    python -m biomapper.benchmarks all                 # 11 arms, production endpoint
    python -m biomapper.benchmarks arm hajjar          # one arm
    python -m biomapper.benchmarks all --endpoint dev  # dev, for future testing

What deliberately stayed in the engine repo: the CI regression gates (``gate.py``,
``conflation_gate.py``, ``test_regression_gate.py``, ``kg-regression.yml``), which guard merges
and need engine internals; and the ``--resolver-mode {weighted,vote}`` A/B, which toggles a
resolver constructor argument that is not on the API surface and should not be put there.

Requires the extra: ``pip install 'biomapper[benchmarks]'``.
"""

from biomapper.benchmarks.provenance import RunProvenance, build_run_provenance, fetch_kg_build_info
from biomapper.benchmarks.suite import DEV_ENDPOINT, PRODUCTION_ENDPOINT, run_suite

__all__ = [
    "DEV_ENDPOINT",
    "PRODUCTION_ENDPOINT",
    "RunProvenance",
    "build_run_provenance",
    "fetch_kg_build_info",
    "run_suite",
]
