"""Terminate stale ``idle in transaction`` connections left by killed clients."""

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
                SELECT pid, state, application_name,
                       NOW() - state_change AS idle_for
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND pid <> pg_backend_pid()
                  AND state IN ('idle in transaction',
                                'idle in transaction (aborted)')
                """
            )
            stale = cur.fetchall()
            if not stale:
                print("No stale idle-in-transaction connections.")
                return
            for pid, state, app, idle_for in stale:
                print(f"terminating pid={pid} state={state} app={app!r} idle_for={idle_for}")
                cur.execute("SELECT pg_terminate_backend(%s)", (pid,))
            print(f"Terminated {len(stale)} connections.")


if __name__ == "__main__":
    main()
