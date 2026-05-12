"""Hourly breakdown of paper_trades rows so we can identify a single tick."""

from polymarket_weather.db import with_conn

with with_conn() as conn:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT date_trunc('hour', posted_at) AS h,
                   count(*) AS n,
                   sum(notional_usd) AS notional_col,
                   sum(
                       CASE WHEN side IN ('taker_buy', 'maker_buy')
                            THEN shares * price
                            ELSE shares * (1 - price)
                       END
                   ) AS true_expo
            FROM paper_trades
            GROUP BY h
            ORDER BY h DESC
            LIMIT 12
            """
        )
        for h, n, notional, true_expo in cur.fetchall():
            print(
                f"  {h}  n={int(n):>4}  "
                f"notional_col=${float(notional or 0):>10,.2f}  "
                f"true_exposure=${float(true_expo or 0):>14,.2f}"
            )
