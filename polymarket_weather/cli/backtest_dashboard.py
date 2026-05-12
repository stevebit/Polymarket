"""Build a static HTML dashboard from ``--export-json`` backtest output."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..backtest_dashboard import write_backtest_dashboard
from ._common import configure_logging


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--from-json",
        type=Path,
        required=True,
        help="Path to JSON written by: python -m polymarket_weather.cli.backtest "
        "... --export-json <path>",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output HTML path (default: same stem as JSON with .html).",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    configure_logging(args.verbose)

    data = json.loads(args.from_json.read_text(encoding="utf-8"))
    out = args.output or args.from_json.with_suffix(".html")
    path = write_backtest_dashboard(data, output_path=out)
    print(f"Wrote {path}")


if __name__ == "__main__":
    main()
