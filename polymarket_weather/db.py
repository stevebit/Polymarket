"""Postgres connection helpers and migration runner.

Uses ``psycopg`` v3 with a small connection pool. All callers go through
``with_conn()`` so connections are returned to the pool deterministically and
exceptions roll back. The migration runner discovers numbered ``.sql`` files
in the ``migrations/`` directory and applies each one in a single transaction,
recording the applied version in ``schema_migrations``.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Tuple

import psycopg
from psycopg_pool import ConnectionPool

from . import config
from .stations import REGISTRY

log = logging.getLogger(__name__)

_POOL: ConnectionPool | None = None
_POOL_LOCK = threading.Lock()


def _get_pool() -> ConnectionPool:
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = ConnectionPool(
                conninfo=config.postgres_url(),
                min_size=1,
                max_size=4,
                timeout=30,
                kwargs={"connect_timeout": 15},
            )
            _POOL.wait()
        return _POOL


def close_pool() -> None:
    global _POOL
    with _POOL_LOCK:
        if _POOL is not None:
            _POOL.close()
            _POOL = None


@contextmanager
def with_conn() -> Iterator[psycopg.Connection]:
    """Borrow a pooled connection. Commits on success, rolls back on error."""
    pool = _get_pool()
    with pool.connection() as conn:
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise


def _list_migration_files() -> List[Tuple[str, Path]]:
    mig_dir = config.paths().migrations
    if not mig_dir.exists():
        return []
    files = sorted(mig_dir.glob("*.sql"))
    return [(p.stem, p) for p in files]


def _ensure_versions_table(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    conn.commit()


def applied_versions(conn: psycopg.Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_migrations")
        return {r[0] for r in cur.fetchall()}


def apply_migrations() -> List[str]:
    """Apply pending SQL files in numeric order. Returns versions newly applied."""
    applied_now: List[str] = []
    with with_conn() as conn:
        _ensure_versions_table(conn)
        already = applied_versions(conn)
        for version, path in _list_migration_files():
            if version in already:
                continue
            sql = path.read_text(encoding="utf-8")
            log.info("Applying migration %s", version)
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(sql)
                    cur.execute(
                        "INSERT INTO schema_migrations(version) VALUES (%s)",
                        (version,),
                    )
            applied_now.append(version)
    if applied_now:
        log.info("Applied %d migrations: %s", len(applied_now), applied_now)
    else:
        log.info("No migrations pending.")
    return applied_now


def seed_stations() -> int:
    """Idempotently UPSERT the station registry. Returns rowcount touched."""
    rows = [
        (
            s.slug,
            s.polymarket_city_slug,
            s.icao,
            s.ghcn_id,
            s.lat,
            s.lon,
            s.tz,
            s.display_name,
        )
        for s in REGISTRY.values()
    ]
    sql = """
        INSERT INTO stations
            (slug, polymarket_city_slug, icao, ghcn_id, lat, lon, tz, display_name)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (slug) DO UPDATE SET
            polymarket_city_slug = EXCLUDED.polymarket_city_slug,
            icao                 = EXCLUDED.icao,
            ghcn_id              = EXCLUDED.ghcn_id,
            lat                  = EXCLUDED.lat,
            lon                  = EXCLUDED.lon,
            tz                   = EXCLUDED.tz,
            display_name         = EXCLUDED.display_name
    """
    with with_conn() as conn, conn.cursor() as cur:
        cur.executemany(sql, rows)
        return cur.rowcount or 0


def station_id_by_slug() -> dict[str, int]:
    with with_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT slug, station_id FROM stations")
        return {row[0]: row[1] for row in cur.fetchall()}


def init_schema_and_seed() -> None:
    """Convenience: apply migrations + seed stations. Safe to call repeatedly."""
    apply_migrations()
    seed_stations()


def table_counts(tables: list[str] | None = None) -> dict[str, int]:
    """Return ``{table: row_count}`` for the requested tables. Used by smoke run."""
    default = [
        "stations",
        "pm_events",
        "pm_buckets",
        "pm_market_snapshots",
        "forecasts",
        "observations",
        "climatology",
        "predictions",
        "bucket_probs",
        "calibration_runs",
    ]
    out: dict[str, int] = {}
    with with_conn() as conn, conn.cursor() as cur:
        for tbl in tables or default:
            cur.execute(f"SELECT count(*) FROM {tbl}")
            out[tbl] = cur.fetchone()[0]
    return out
