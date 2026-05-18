#!/usr/bin/env python3
"""Orchestrate a research DB rebuild (Azure Postgres).

Run from repo root with ``WEATHER_POSTGRES_URL`` set. Typical order:

1. ``discover_history`` — Polymarket Gamma events + terminal ``outcomePrices`` snapshots
2. ``backfill_open_meteo_history`` — historical Open-Meteo rows (incl. ``gfs_hrrr``)
3. ``ingest_hourly_observations`` — Iowa Mesonet ASOS (primary + neighbours)
4. ``fit_postprocess`` — EMOS refit
5. ``predict_history`` — honest ``bucket_probs`` / ``predictions`` replay

Each step is idempotent (upserts). Adjust dates for your window.

Usage::

    .venv\\Scripts\\python.exe scripts/rebuild_research_data.py --dry-run
    .venv\\Scripts\\python.exe scripts/rebuild_research_data.py \\
        --hist-start 2025-11-01 --hist-end today --hourly-start 2023-01-01

The script runs subprocesses so you can Ctrl+C between phases safely.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"
if not PY.exists():
    PY = Path(sys.executable)


def run(args: list[str], *, dry: bool) -> None:
    cmd = [str(PY), *args]
    print("+", " ".join(cmd))
    if dry:
        return
    subprocess.run(cmd, cwd=str(ROOT), check=True)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--hist-start", default="2025-11-01")
    p.add_argument("--hist-end", default="today")
    p.add_argument("--hourly-start", default="2023-01-01")
    p.add_argument("--hourly-end", default="today")
    p.add_argument(
        "--skip-discover",
        action="store_true",
        help="Skip Polymarket Gamma discover_history.",
    )
    p.add_argument(
        "--skip-weather-backfill",
        action="store_true",
        help="Skip Open-Meteo historical forecast backfill.",
    )
    p.add_argument(
        "--skip-hourly",
        action="store_true",
        help="Skip Mesonet hourly_observations ingest.",
    )
    p.add_argument(
        "--skip-emos",
        action="store_true",
        help="Skip fit_postprocess.",
    )
    p.add_argument(
        "--skip-predict-history",
        action="store_true",
        help="Skip predict_history replay.",
    )
    p.add_argument(
        "--predict-book-alignment",
        choices=("from-cutoff-hour", "utc-midnight", "utc-noon"),
        default="from-cutoff-hour",
        help="Forwarded to predict_history --book-alignment (utc-midnight for intraday CLOB).",
    )
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if not args.skip_discover:
        run(
            [
                "-m",
                "polymarket_weather.cli.discover_history",
                "--start",
                args.hist_start,
                "--end",
                args.hist_end,
            ],
            dry=args.dry_run,
        )
    if not args.skip_weather_backfill:
        run(
            [
                str(ROOT / "scripts" / "backfill_open_meteo_history.py"),
                "--start",
                args.hist_start,
                "--end",
                args.hist_end,
            ],
            dry=args.dry_run,
        )
    if not args.skip_hourly:
        run(
            [
                "-m",
                "polymarket_weather.cli.ingest_hourly_observations",
                "--start",
                args.hourly_start,
                "--end",
                args.hourly_end,
            ],
            dry=args.dry_run,
        )
    if not args.skip_emos:
        run(
            [
                "-m",
                "polymarket_weather.cli.fit_postprocess",
                "--lookback-days",
                "365",
                "--end-date",
                args.hist_end,
            ],
            dry=args.dry_run,
        )
    if not args.skip_predict_history:
        run(
            [
                "-m",
                "polymarket_weather.cli.predict_history",
                "--start",
                args.hist_start,
                "--end",
                args.hist_end,
                "--book-alignment",
                args.predict_book_alignment,
            ],
            dry=args.dry_run,
        )


if __name__ == "__main__":
    main()
