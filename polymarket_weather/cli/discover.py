"""Discover Polymarket daily-temperature events for the configured stations.

For each station and each day in ``[--date, --date + --days-ahead]`` (inclusive)
look up the Gamma event by slug, parse buckets, persist to ``pm_events`` and
``pm_buckets``, and (unless ``--no-snapshot``) snapshot the YES order book of
each bucket via the read-only CLOB v2 client.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt

from ..db import init_schema_and_seed, station_id_by_slug
from ..markets import (
    discover_events,
    persist_events,
    snapshot_markets,
    stations_from_slugs,
)
from ._common import add_common_args, configure_logging, parse_stations


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    add_common_args(p)
    p.add_argument(
        "--days-ahead",
        type=int,
        default=7,
        help="How many days past --date to enumerate (inclusive).",
    )
    p.add_argument(
        "--no-snapshot",
        action="store_true",
        help="Skip the order-book snapshot step.",
    )
    p.add_argument(
        "--no-migrate",
        action="store_true",
        help="Skip the apply-migrations bootstrap step.",
    )
    args = p.parse_args()
    configure_logging(args.verbose)

    if not args.no_migrate:
        init_schema_and_seed()

    slugs = parse_stations(args.station)
    stations = stations_from_slugs(slugs)
    if not stations:
        raise SystemExit("No valid stations specified.")

    sid_map = station_id_by_slug()

    print(
        f"Discovering events: stations={[s.slug for s in stations]} "
        f"date_window=[{args.date} .. {args.date + dt.timedelta(days=args.days_ahead)}]"
    )

    events = asyncio.run(
        discover_events(stations, args.date, args.days_ahead)
    )
    print(f"Found {len(events)} matching Polymarket events.")

    counts = persist_events(events, sid_map)
    print(
        f"Persisted: pm_events={counts['pm_events']} "
        f"pm_buckets={counts['pm_buckets']}"
    )

    if args.no_snapshot or not events:
        return

    n_snap = snapshot_markets([e.slug for e in events])
    print(f"Snapshotted {n_snap} buckets into pm_market_snapshots.")


if __name__ == "__main__":
    main()
