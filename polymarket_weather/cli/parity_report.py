"""Generate NOAA vs Wunderground parity report."""

from __future__ import annotations

import argparse
from pathlib import Path

from ..parity import compute_station_parity, render_parity_markdown, write_parity_report
from ._common import configure_logging


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--lookback-days", type=int, default=365)
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional output path for markdown report.",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging.")
    args = p.parse_args()
    configure_logging(args.verbose)

    rows = compute_station_parity(args.lookback_days)
    md = render_parity_markdown(rows, args.lookback_days)
    out = write_parity_report(md, out_path=args.output)
    print(f"Parity report written: {out}")
    if rows:
        print("Top station MAE (F):")
        for r in sorted(rows, key=lambda x: x.mean_abs_error, reverse=True)[:5]:
            print(
                f"  {r.slug:14s} n={r.n:4d} "
                f"mean_abs={r.mean_abs_error:.2f} "
                f"exact={100*r.exact_match_rate:.1f}%"
            )


if __name__ == "__main__":
    main()
