"""Backfill Polymarket Gamma discovery + terminal market snapshots over a date range.

For each (station, calendar day) in ``[--start, --end]`` (inclusive), calls the
same Gamma slug lookup as :mod:`polymarket_weather.cli.discover`. Persisted rows
update ``pm_events`` / ``pm_buckets``.

Additionally, for **closed** markets, inserts **one** ``pm_market_snapshots``
row per bucket using Gamma's ``outcomePrices`` + ``closedTime`` (see
``persist_gamma_terminal_snapshots``). This does **not** replace high-cadence
live CLOB snapshots from ``discover --no-gamma-terminal`` + ``snapshot_markets``,
but it *does* let backtests and ``market:mid`` calibration see believable books
for every resolved day Polymarket actually listed.

Live / open events: optionally pass ``--clob-snapshot`` to run the read-only CLOB
v2 book pass for markets that are still ``closed=false`` in Gamma.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging

from ..db import init_schema_and_seed, station_id_by_slug
from ..markets import (
    discover_events,
    persist_events,
    persist_gamma_terminal_snapshots,
    snapshot_markets,
    stations_from_slugs,
)
from ._common import (
    add_common_args,
    configure_logging,
    parse_cli_date,
    parse_stations,
)

log = logging.getLogger(__name__)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    add_common_args(p, with_date=False)
    p.add_argument(
        "--start",
        type=parse_cli_date,
        required=True,
        help="First calendar day (inclusive): YYYY-MM-DD or today/yesterday (UTC).",
    )
    p.add_argument(
        "--end",
        type=parse_cli_date,
        default=dt.datetime.now(dt.timezone.utc).date(),
        help="Last calendar day (inclusive). Default today UTC.",
    )
    p.add_argument(
        "--concurrency",
        type=int,
        default=5,
        help="Parallel Gamma requests (default 5).",
    )
    p.add_argument(
        "--no-gamma-terminal",
        action="store_true",
        help="Skip inserting terminal snapshots from Gamma outcomePrices.",
    )
    p.add_argument(
        "--clob-snapshot",
        action="store_true",
        help="After persistence, snapshot **open** events via read-only CLOB.",
    )
    p.add_argument("--no-migrate", action="store_true")
    args = p.parse_args()
    configure_logging(args.verbose)

    if args.end < args.start:
        raise SystemExit(f"--end {args.end} is before --start {args.start}")

    if not args.no_migrate:
        init_schema_and_seed()

    slugs = parse_stations(args.station)
    stations = stations_from_slugs(slugs)
    if not stations:
        raise SystemExit("No valid stations specified.")

    sid_map = station_id_by_slug()
    days_ahead = (args.end - args.start).days
    print(
        f"discover_history: stations={[s.slug for s in stations]} "
        f"[{args.start} .. {args.end}] ({days_ahead + 1} days each) "
        f"concurrency={args.concurrency}"
    )

    events = asyncio.run(
        discover_events(stations, args.start, days_ahead, concurrency=args.concurrency)
    )
    print(f"Found {len(events)} Polymarket events with parsable buckets.")

    counts = persist_events(events, sid_map)
    print(
        f"Persisted pm_events={counts['pm_events']} pm_buckets={counts['pm_buckets']}"
    )

    if not args.no_gamma_terminal and events:
        n_term = persist_gamma_terminal_snapshots(events)
        print(f"Gamma terminal pm_market_snapshots rows inserted: {n_term}")

    if args.clob_snapshot and events:
        open_slugs = [e.slug for e in events if not e.raw.get("closed")]
        if open_slugs:
            n = snapshot_markets(open_slugs)
            print(f"CLOB snapshots for open events (buckets attempted): {n}")
        else:
            print("No open events in this batch; skipping CLOB snapshot.")

    print("discover_history done.")


if __name__ == "__main__":
    main()
