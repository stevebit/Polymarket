"""Build a static HTML dashboard from one or more backtest JSON exports.

Single-run mode (default) renders the rich diagnostic dashboard:
``--from-json reports/run.json``.

Multi-run compare mode (pass ``--from-json`` more than once or use
``--from-jsons``) renders an overlay of net equity curves, reliability
diagrams, and grouped per-station / per-lead bars across runs:

    python -m polymarket_weather.cli.backtest_dashboard \\
        --from-json reports/m1.json --label m1 \\
        --from-json reports/m2.json --label m2 \\
        --output reports/compare.html

Labels default to the JSON filename stem.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..backtest_dashboard import (
    write_backtest_compare_dashboard,
    write_backtest_dashboard,
)
from ._common import configure_logging


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--from-json",
        action="append",
        type=Path,
        required=True,
        help="JSON path. Pass multiple times for compare mode.",
    )
    p.add_argument(
        "--label",
        action="append",
        default=None,
        help="Optional label per run (repeat to align with --from-json). "
        "Defaults to the JSON filename stem.",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output HTML path. Single-run default: alongside the JSON with "
        ".html extension. Compare default: reports/backtest_compare.html.",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    configure_logging(args.verbose)

    paths: list[Path] = list(args.from_json)
    labels = args.label or []
    if labels and len(labels) != len(paths):
        raise SystemExit(
            f"--label provided {len(labels)} times but --from-json provided "
            f"{len(paths)} times; counts must match"
        )

    if len(paths) == 1:
        data = json.loads(paths[0].read_text(encoding="utf-8"))
        out = args.output or paths[0].with_suffix(".html")
        path = write_backtest_dashboard(data, output_path=out)
        print(f"Wrote {path}")
        return

    runs: list[tuple[str, dict]] = []
    for i, p_in in enumerate(paths):
        data = json.loads(p_in.read_text(encoding="utf-8"))
        label = labels[i] if labels else p_in.stem
        runs.append((label, data))
    out = args.output or Path("reports/backtest_compare.html")
    path = write_backtest_compare_dashboard(runs, output_path=out)
    print(f"Wrote {path} (compare mode, {len(runs)} runs)")


if __name__ == "__main__":
    main()
