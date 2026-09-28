"""CLI for the external benchmark suite.

``--endpoint`` defaults to **production**, because a benchmark that measures a dev checkout is
not measuring the service a reader can call. ``dev`` is available for future testing.

The API key is read from ``BIOMAPPER_API_KEY`` and never taken on the command line: argv is
visible to every process on the host and lands in shell history. A deployment with no keys
configured is open, and the suite runs against it unauthenticated without needing a placeholder.

There is no ``--no-save``. The expensive part of a run is live API traffic, and a flag that
discards it is not an acceptable failure mode; ``--out`` overrides *where* results land, never
*whether* they do.

For the same reason there is no silent subset. ``all --only`` / ``all --skip`` must account for
every suite arm left out: each one is written into the manifest as ``status="skipped"`` with the
operator's reason, and the command refuses to start if any omitted arm has none.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from biomapper.benchmarks.config import SUITE_DATASETS, SUITE_SKIPPED
from biomapper.benchmarks.provenance import DEFAULT_KESTREL_URL
from biomapper.benchmarks.suite import ENDPOINTS, resolve_omissions, run_suite


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m biomapper.benchmarks",
        description="Run the BioMapper external benchmark suite against a deployment (API only).",
    )
    parser.add_argument(
        "--endpoint",
        default="production",
        help=(
            f"API endpoint: one of {sorted(ENDPOINTS)} or a full http(s) URL. "
            f"Default: production."
        ),
    )
    parser.add_argument(
        "--kestrel-url",
        default=DEFAULT_KESTREL_URL,
        help=(
            "Kestrel base URL, read for run provenance (/health) and for node-name lookups in the "
            "structure oracle's name-fallback path. The public host is keyless and is never sent a "
            f"key. Default: {DEFAULT_KESTREL_URL}"
        ),
    )
    parser.add_argument(
        "--out", default=None, help="Override the output dir (default: timestamped)."
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=20,
        help="Entities per /map/batch request. Default: 20.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")

    sub = parser.add_subparsers(dest="command", required=True)

    all_parser = sub.add_parser("all", help=f"Run all {len(SUITE_DATASETS)} arms.")
    all_parser.add_argument(
        "--only",
        nargs="+",
        default=None,
        choices=SUITE_DATASETS,
        help=(
            "Restrict to these arms (still writes one suite manifest). Every suite arm left out "
            "needs a reason via --skip or --exclusions, or the command refuses to run."
        ),
    )
    all_parser.add_argument(
        "--skip",
        action="append",
        default=[],
        metavar="ARM=REASON",
        help=(
            "Leave ARM out of this run and record it in the manifest as skipped with REASON. "
            "Repeatable. Without --only, the run covers every arm not skipped."
        ),
    )
    all_parser.add_argument(
        "--exclusions",
        default=None,
        metavar="FILE",
        help=(
            'JSON list of {"arm": ..., "reason": ..., ...} records (the excluded_arms.json '
            "shape). Each arm is skipped with its reason and the rest of the record is embedded "
            "in the manifest entry as evidence."
        ),
    )

    arm_parser = sub.add_parser("arm", help="Run a single arm.")
    arm_parser.add_argument("name", choices=SUITE_DATASETS, help="Arm to run.")

    sub.add_parser("list", help="List the arms and the deliberate skips, then exit.")

    return parser


def _parse_skip(value: str) -> tuple[str, str]:
    arm, sep, reason = value.partition("=")
    if not sep or not arm.strip() or not reason.strip():
        raise ValueError(f"--skip expects ARM=REASON with a non-empty reason, got {value!r}")
    return arm.strip(), reason.strip()


def _load_exclusions(path: str) -> dict[str, dict[str, Any]]:
    records = json.loads(Path(path).read_text())
    if isinstance(records, dict):
        records = [records]
    out: dict[str, dict[str, Any]] = {}
    for record in records:
        arm = record.get("arm") if isinstance(record, dict) else None
        if not isinstance(arm, str) or not arm:
            raise ValueError(f"{path}: every exclusion record needs an 'arm', got {record!r}")
        if arm in out:
            raise ValueError(f"{path}: arm {arm!r} is listed more than once")
        out[arm] = record
    return out


def _selection(
    args: argparse.Namespace,
) -> tuple[list[str], dict[str, str | dict[str, Any]]]:
    """The arms to run and the operator's reason for every suite arm left out.

    ``arm NAME`` is an explicit single-arm invocation, so the reason for the other arms is the
    invocation itself and is recorded as such. ``all`` takes reasons only from the operator.
    """
    if args.command == "arm":
        reason = f"single-arm invocation (`arm {args.name}`); not part of this run"
        return [args.name], {k: reason for k in SUITE_DATASETS if k != args.name}

    omitted: dict[str, str | dict[str, Any]] = {}
    if args.exclusions:
        omitted.update(_load_exclusions(args.exclusions))
    for value in args.skip:
        arm, reason = _parse_skip(value)
        if arm in omitted:
            raise ValueError(f"arm {arm!r} is given a skip reason more than once")
        omitted[arm] = reason
    if args.only is not None:
        return list(args.only), omitted
    return [k for k in SUITE_DATASETS if k not in omitted], omitted


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.command == "list":
        print(f"Suite arms ({len(SUITE_DATASETS)}):")
        for key in SUITE_DATASETS:
            print(f"  {key}")
        print(f"\nDeliberate skips ({len(SUITE_SKIPPED)}), recorded in every manifest:")
        for key, reason in SUITE_SKIPPED.items():
            print(f"  {key}: {reason}")
        return 0

    try:
        datasets, omitted = _selection(args)
        # Validate up front so a refusal is an argparse error (exit 2) before any network call.
        resolve_omissions(datasets, omitted)
    except (ValueError, OSError) as exc:
        build_parser().error(str(exc))

    # Read from the environment only. See the module docstring on why not from argv.
    api_key = os.getenv("BIOMAPPER_API_KEY")

    outcome = run_suite(
        out_dir=Path(args.out) if args.out else None,
        datasets=datasets,
        omitted=omitted,
        endpoint=args.endpoint,
        api_key=api_key,
        kestrel_url=args.kestrel_url,
        batch_size=args.batch_size,
    )
    manifest = outcome["manifest"]
    print(json.dumps({k: v for k, v in manifest.items() if k != "datasets"}, indent=2, default=str))
    print(f"\nResults saved to: {outcome['out_dir']}")
    for entry in manifest["datasets"]:
        note = entry.get("reason") or entry.get("error") or ""
        print(f"  {entry['status']:8s} {entry['dataset']:24s} {note}")
    if not manifest.get("complete", True):
        print(
            "\nINCOMPLETE: at least one arm failed or completed only part of its sub-arms. "
            "The numbers above do not cover the full benchmark."
        )

    # A failed OR PARTIAL arm is a non-zero exit, so a scheduled run is not reported as green when
    # part of the declared benchmark is missing. A SKIP is not a failure: it is a recorded,
    # deliberate outcome with a reason attached.
    return 1 if (manifest["n_failed"] or manifest["n_partial"]) else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
