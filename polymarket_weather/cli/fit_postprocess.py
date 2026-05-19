"""Fit per-(station, source, lead_day) EMOS coefficients and persist them.

Run this after observations and forecasts are caught up. Coefficients are
versioned by ``fit_at`` so the most recent row wins at predict time, and old
fits remain for audit / rollback.
"""

from __future__ import annotations

import argparse
import datetime as dt

from ..db import init_schema_and_seed
from ..models.postprocess import (
    DEFAULT_LEAD_DAYS,
    DEFAULT_SOURCES,
    fit_postprocess,
)
from ._common import (
    add_common_args,
    configure_logging,
    parse_cli_date,
    parse_stations,
)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    add_common_args(p, with_date=False)
    p.add_argument(
        "--lookback-days",
        type=int,
        default=365,
        help="Training window ending at --end-date.",
    )
    p.add_argument(
        "--end-date",
        type=parse_cli_date,
        default=None,
        help=(
            "Last observation date in training: YYYY-MM-DD or today/yesterday "
            "(UTC). Default today UTC."
        ),
    )
    p.add_argument(
        "--sources",
        default=",".join(DEFAULT_SOURCES),
        help="Comma-separated list of forecast sources to fit.",
    )
    p.add_argument(
        "--leads",
        default=",".join(str(d) for d in DEFAULT_LEAD_DAYS),
        help="Comma-separated lead days (forecast issued N days before target).",
    )
    p.add_argument("--no-migrate", action="store_true")
    p.add_argument(
        "--with-neighbors",
        action="store_true",
        help="Enable neighbor TMAX stats (mean/std/range/distance-weighted) as extra regressors or spread modulators in EMOS training (requires prior neighbor backfill).",
    )
    args = p.parse_args()
    configure_logging(args.verbose)

    if not args.no_migrate:
        init_schema_and_seed()

    sources = tuple(s.strip() for s in args.sources.split(",") if s.strip())
    leads = tuple(int(s.strip()) for s in args.leads.split(",") if s.strip())

    end_date = args.end_date
    if end_date is None:
        end_date = dt.datetime.now(dt.timezone.utc).date()

    counts = fit_postprocess(
        parse_stations(args.station),
        sources=sources,
        lead_days=leads,
        end_date=end_date,
        lookback_days=args.lookback_days,
        with_neighbors=args.with_neighbors,
    )
    print(
        f"Fits={counts['fits']} skipped_too_few={counts['skipped_too_few']} "
        f"skipped_no_data={counts['skipped_no_data']}"
    )


if __name__ == "__main__":
    main()
