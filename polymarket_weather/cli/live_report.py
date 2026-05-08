"""Daily live + paper trading review report.

Read-only CLI that snapshots:

* live ``orders`` / ``fills`` / ``daily_pnl``,
* the day's ``paper_trades`` plus realised / expected calibration over a
  configurable lookback,
* most-recent ``calibration_runs`` per model,
* open exposure by event,

and writes ``reports/live_<date>.md``. Run every morning before deciding
whether to keep ``WEATHER_AUTOMATION_ENABLED=1`` set.

This CLI **never** signs or places orders. It is safe to schedule blindly.
"""

from __future__ import annotations

import argparse
import datetime as dt

from ..live_report import (
    build_live_report,
    render_live_report_markdown,
    write_live_report,
)
from ._common import configure_logging


def _parse_date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--date", type=_parse_date, default=dt.date.today(),
        help="Report date (default today UTC).",
    )
    p.add_argument(
        "--paper-lookback-days", type=int, default=14,
        help="Window of settled paper trades to summarise (default 14).",
    )
    p.add_argument(
        "--print-only", action="store_true",
        help="Print to stdout instead of writing reports/live_<date>.md.",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    configure_logging(args.verbose)

    report = build_live_report(
        report_date=args.date,
        paper_lookback_days=args.paper_lookback_days,
    )
    if args.print_only:
        print(render_live_report_markdown(report))
        return
    path = write_live_report(report)
    print(f"Wrote {path}")


if __name__ == "__main__":
    main()
