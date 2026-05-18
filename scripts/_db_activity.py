"""Show pg_stat_activity rows and table sizes — diagnostic only."""

import os

import psycopg
from dotenv import load_dotenv


def main() -> None:
    load_dotenv()
    url = os.environ["WEATHER_POSTGRES_URL"]
    with psycopg.connect(url, connect_timeout=15, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pid,
                       state,
                       wait_event_type,
                       wait_event,
                       LEFT(query, 80)        AS q,
                       NOW() - query_start    AS age
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND state IS NOT NULL
                  AND state <> 'idle'
                ORDER BY age DESC NULLS LAST
                LIMIT 30
                """
            )
            rows = cur.fetchall()
            print("=== pg_stat_activity (non-idle) ===")
            for r in rows:
                print(r)

            cur.execute(
                """
                SELECT relname,
                       n_live_tup,
                       n_dead_tup,
                       last_autovacuum,
                       last_autoanalyze
                FROM pg_stat_user_tables
                WHERE relname IN (
                    'bucket_probs', 'pm_market_snapshots', 'pm_buckets',
                    'forecasts', 'forecast_members', 'observations'
                )
                ORDER BY relname
                """
            )
            rows = cur.fetchall()
            print("\n=== table stats ===")
            for r in rows:
                print(r)

            cur.execute("SELECT COUNT(*) FROM bucket_probs")
            print("\nbucket_probs total rows:", cur.fetchone()[0])
            cur.execute("SELECT COUNT(*) FROM pm_market_snapshots")
            print("pm_market_snapshots total rows:", cur.fetchone()[0])


if __name__ == "__main__":
    main()
