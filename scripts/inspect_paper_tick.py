"""Inspect paper_trades rows from the most recent run_loop tick.

Reads ``WEATHER_POSTGRES_URL`` and prints diagnostic stats for paper_trades
rows ``posted_at`` after a given UTC timestamp.

Usage:

    .\\.venv\\Scripts\\python.exe .\\scripts\\inspect_paper_tick.py 2026-05-12T08:00:00Z
    .\\.venv\\Scripts\\python.exe .\\scripts\\inspect_paper_tick.py 2026-05-12T08:00:00Z --delete

The ``--delete`` flag removes the rows after asking for stdin confirmation.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from polymarket_weather.db import with_conn


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("since", help="UTC ISO timestamp, e.g. 2026-05-12T08:00:00Z")
    p.add_argument("--delete", action="store_true", help="Delete after preview")
    args = p.parse_args()

    since = datetime.fromisoformat(args.since.replace("Z", "+00:00"))
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    with with_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT side,
                       count(*) AS n,
                       sum(notional_usd) AS sum_notional_col,
                       sum(shares * price) AS sum_yes_cost,
                       sum(
                           CASE WHEN side IN ('taker_buy','maker_buy')
                                THEN shares*price
                                ELSE shares*(1-price)
                           END
                       ) AS sum_true_exposure,
                       max(shares) AS max_shares,
                       max(shares * price) AS max_yes_cost,
                       max(
                           CASE WHEN side IN ('taker_buy','maker_buy')
                                THEN shares*price
                                ELSE shares*(1-price)
                           END
                       ) AS max_true_exposure
                FROM paper_trades
                WHERE posted_at >= %s
                GROUP BY side
                ORDER BY side
                """,
                (since,),
            )
            rows = cur.fetchall()
            print(f"paper_trades rows posted_at >= {since.isoformat()}:")
            print(
                f"  {'side':<12} {'n':>5} "
                f"{'notional_usd_col':>18} "
                f"{'shares*price':>14} "
                f"{'true_exposure':>14} "
                f"{'max_shares':>10}"
            )
            total_n = 0
            for r in rows:
                total_n += int(r[1])
                print(
                    f"  {r[0]:<12} {r[1]:>5} "
                    f"${float(r[2] or 0):>17,.2f} "
                    f"${float(r[3] or 0):>13,.2f} "
                    f"${float(r[4] or 0):>13,.2f} "
                    f"{int(r[5] or 0):>10}"
                )
            print(f"  total rows: {total_n}")

            if args.delete:
                resp = input(f"\nDELETE all {total_n} rows? type yes to confirm: ")
                if resp.strip().lower() != "yes":
                    print("aborted")
                    sys.exit(1)
                cur.execute(
                    "DELETE FROM paper_trades WHERE posted_at >= %s",
                    (since,),
                )
                print(f"deleted {cur.rowcount} rows")
                conn.commit()


if __name__ == "__main__":
    main()
