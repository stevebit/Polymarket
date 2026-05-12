"""Stamp paper_trades rows that were written under the pre-fix sizing bug.

Sizing bug context: prior to commit fixing
``polymarket_weather/strategy/sizing.py``, ``size_edge`` divided
``target_dollars`` by ``edge.price`` for both buys AND sells. For YES sells
this under-counted exposure by a factor of ``(1 - price) / price`` — at
price 0.001 that is ~999×. Caps were therefore bypassed.

This script marks every paper_trades row whose ``notional_usd`` differs
materially from the actual loss-if-wrong exposure
(``shares * price`` for buys, ``shares * (1 - price)`` for sells). It does
not delete rows; it appends a tag to the ``notes`` column so paper-summary
queries can filter them out and the audit trail is preserved.

Idempotent: rows already tagged are not re-tagged.

Usage:

    .\\.venv\\Scripts\\python.exe .\\scripts\\quarantine_buggy_paper.py [--apply]
"""

from __future__ import annotations

import argparse

from polymarket_weather.db import with_conn

TAG = "BUGGY_SIZING_PRE_FIX"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually update rows (default: dry-run preview).",
    )
    args = p.parse_args()

    select_sql = """
        SELECT count(*) AS n,
               sum(
                   CASE WHEN side IN ('taker_buy', 'maker_buy')
                        THEN shares * price
                        ELSE shares * (1 - price)
                   END
               ) AS true_expo,
               sum(notional_usd) AS recorded_notional
        FROM paper_trades
        WHERE COALESCE(notes, '') NOT LIKE %s
          AND abs(
              notional_usd
              - CASE WHEN side IN ('taker_buy', 'maker_buy')
                     THEN shares * price
                     ELSE shares * (1 - price)
                END
          ) > 0.01
    """

    update_sql = """
        UPDATE paper_trades
           SET notes = COALESCE(notes || ' ', '') || %s
         WHERE COALESCE(notes, '') NOT LIKE %s
           AND abs(
               notional_usd
               - CASE WHEN side IN ('taker_buy', 'maker_buy')
                      THEN shares * price
                      ELSE shares * (1 - price)
                 END
           ) > 0.01
    """

    with with_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(select_sql, (f"%{TAG}%",))
            n, true_expo, recorded = cur.fetchone()
            n = int(n or 0)
            true_expo = float(true_expo or 0)
            recorded = float(recorded or 0)
            print(
                f"Affected rows (notional_usd differs from real exposure): {n}\n"
                f"  recorded notional_usd sum: ${recorded:,.2f}\n"
                f"  true loss-if-wrong sum:    ${true_expo:,.2f}\n"
                f"  inflation factor:          {true_expo / max(recorded, 0.01):.1f}x"
            )

            if not args.apply:
                print("\nDry-run only. Re-run with --apply to update.")
                return

            cur.execute(update_sql, (TAG, f"%{TAG}%"))
            print(f"\nTagged {cur.rowcount} rows with {TAG!r}.")
            conn.commit()


if __name__ == "__main__":
    main()
