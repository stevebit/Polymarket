"""Offline sanity check for the backtest JSON export schema (v2).

Builds a synthetic ``BacktestResult`` populated with a handful of taker and
maker fills spanning multiple stations, lead days, and outcomes, runs
``backtest_result_to_dict``, asserts the new per-fill fields are present,
and (optionally) writes the payload to ``docs/fixtures/backtest_smoke.json``
so the dashboard CLIs can be smoke-tested without Postgres.

Usage:

    .\\.venv\\Scripts\\python.exe .\\scripts\\smoke_backtest_export.py
    .\\.venv\\Scripts\\python.exe .\\scripts\\smoke_backtest_export.py --write-fixture
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

from polymarket_weather.backtest import (
    BacktestResult,
    _Fill,
    backtest_result_to_dict,
)
from polymarket_weather.strategy.edge import Action, FeeSchedule
from polymarket_weather.strategy.sizing import CapsConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "docs" / "fixtures" / "backtest_smoke.json"


def _mk_taker_fill(
    *,
    event_slug: str,
    station: str,
    bucket: str,
    lo_f: float,
    hi_f: float,
    target: dt.date,
    side: Action,
    price: float,
    shares: int,
    p_model: float,
    posted: dt.datetime,
    realised_label: str,
    fee_per_share: float = 0.0175,
) -> _Fill:
    fee = fee_per_share * shares
    if side == Action.TAKER_BUY:
        ev = (p_model - price) - fee_per_share
        per = (1.0 if realised_label == bucket else 0.0) - price
    else:
        ev = (price - p_model) - fee_per_share
        per = price - (1.0 if realised_label == bucket else 0.0)
    return _Fill(
        event_slug=event_slug,
        station_slug=station,
        bucket_label=bucket,
        bucket_lo_f=lo_f,
        bucket_hi_f=hi_f,
        target_date=target,
        side=side,
        price=price,
        shares=shares,
        p_model_at_post=p_model,
        expected_pnl_per_share_at_post=ev,
        fee_usd=fee,
        posted_at=posted,
        filled_at=posted,
        realised_label=realised_label,
        realised_pnl_usd=per * shares,
    )


def _mk_maker_fill(
    *,
    event_slug: str,
    station: str,
    bucket: str,
    lo_f: float,
    hi_f: float,
    target: dt.date,
    side: Action,
    price: float,
    shares: int,
    p_model: float,
    posted: dt.datetime,
    fill_delay_h: int,
    realised_label: str,
) -> _Fill:
    if side == Action.MAKER_BUY:
        ev = p_model - price
        per = (1.0 if realised_label == bucket else 0.0) - price
    else:
        ev = price - p_model
        per = price - (1.0 if realised_label == bucket else 0.0)
    return _Fill(
        event_slug=event_slug,
        station_slug=station,
        bucket_label=bucket,
        bucket_lo_f=lo_f,
        bucket_hi_f=hi_f,
        target_date=target,
        side=side,
        price=price,
        shares=shares,
        p_model_at_post=p_model,
        expected_pnl_per_share_at_post=ev,
        fee_usd=0.0,
        posted_at=posted,
        filled_at=posted + dt.timedelta(hours=fill_delay_h),
        realised_label=realised_label,
        realised_pnl_usd=per * shares,
    )


def build_demo_payload() -> dict:
    """A handful of fills across stations / leads / outcomes for the demo dashboard."""
    UTC = dt.timezone.utc
    fills_taker = [
        _mk_taker_fill(
            event_slug="highest-temperature-in-nyc-on-2026-04-12",
            station="nyc", bucket="78-79", lo_f=78.0, hi_f=79.0,
            target=dt.date(2026, 4, 12),
            side=Action.TAKER_BUY, price=0.35, shares=10, p_model=0.46,
            posted=dt.datetime(2026, 4, 9, 14, 0, tzinfo=UTC),
            realised_label="78-79",
        ),
        _mk_taker_fill(
            event_slug="highest-temperature-in-chicago-on-2026-04-15",
            station="chicago", bucket="68-69", lo_f=68.0, hi_f=69.0,
            target=dt.date(2026, 4, 15),
            side=Action.TAKER_BUY, price=0.18, shares=14, p_model=0.27,
            posted=dt.datetime(2026, 4, 14, 13, 0, tzinfo=UTC),
            realised_label="70-71",
        ),
        _mk_taker_fill(
            event_slug="highest-temperature-in-dallas-on-2026-04-18",
            station="dallas", bucket="86-87", lo_f=86.0, hi_f=87.0,
            target=dt.date(2026, 4, 18),
            side=Action.TAKER_SELL, price=0.42, shares=9, p_model=0.31,
            posted=dt.datetime(2026, 4, 16, 9, 0, tzinfo=UTC),
            realised_label="84-85",
        ),
        _mk_taker_fill(
            event_slug="highest-temperature-in-los-angeles-on-2026-04-22",
            station="los-angeles", bucket="74-75", lo_f=74.0, hi_f=75.0,
            target=dt.date(2026, 4, 22),
            side=Action.TAKER_BUY, price=0.27, shares=12, p_model=0.34,
            posted=dt.datetime(2026, 4, 21, 11, 0, tzinfo=UTC),
            realised_label="74-75",
        ),
        _mk_taker_fill(
            event_slug="highest-temperature-in-miami-on-2026-04-26",
            station="miami", bucket="82-83", lo_f=82.0, hi_f=83.0,
            target=dt.date(2026, 4, 26),
            side=Action.TAKER_BUY, price=0.29, shares=11, p_model=0.41,
            posted=dt.datetime(2026, 4, 24, 16, 0, tzinfo=UTC),
            realised_label="82-83",
        ),
        _mk_taker_fill(
            event_slug="highest-temperature-in-seattle-on-2026-05-02",
            station="seattle", bucket="60-61", lo_f=60.0, hi_f=61.0,
            target=dt.date(2026, 5, 2),
            side=Action.TAKER_SELL, price=0.55, shares=8, p_model=0.39,
            posted=dt.datetime(2026, 4, 30, 12, 0, tzinfo=UTC),
            realised_label="58-59",
        ),
        _mk_taker_fill(
            event_slug="highest-temperature-in-denver-on-2026-05-05",
            station="denver", bucket="72-73", lo_f=72.0, hi_f=73.0,
            target=dt.date(2026, 5, 5),
            side=Action.TAKER_BUY, price=0.18, shares=15, p_model=0.32,
            posted=dt.datetime(2026, 5, 1, 14, 0, tzinfo=UTC),
            realised_label="76-77",
        ),
        _mk_taker_fill(
            event_slug="highest-temperature-in-atlanta-on-2026-05-08",
            station="atlanta", bucket="80-81", lo_f=80.0, hi_f=81.0,
            target=dt.date(2026, 5, 8),
            side=Action.TAKER_BUY, price=0.31, shares=12, p_model=0.42,
            posted=dt.datetime(2026, 5, 6, 10, 0, tzinfo=UTC),
            realised_label="80-81",
        ),
    ]
    fills_maker = [
        _mk_maker_fill(
            event_slug="highest-temperature-in-nyc-on-2026-04-12",
            station="nyc", bucket="76-77", lo_f=76.0, hi_f=77.0,
            target=dt.date(2026, 4, 12),
            side=Action.MAKER_BUY, price=0.22, shares=10, p_model=0.30,
            posted=dt.datetime(2026, 4, 8, 9, 0, tzinfo=UTC),
            fill_delay_h=18,
            realised_label="78-79",
        ),
        _mk_maker_fill(
            event_slug="highest-temperature-in-houston-on-2026-04-30",
            station="houston", bucket="88-89", lo_f=88.0, hi_f=89.0,
            target=dt.date(2026, 4, 30),
            side=Action.MAKER_SELL, price=0.45, shares=8, p_model=0.34,
            posted=dt.datetime(2026, 4, 27, 15, 0, tzinfo=UTC),
            fill_delay_h=22,
            realised_label="88-89",
        ),
    ]

    pnl_taker = sum(f.realised_pnl_usd for f in fills_taker)
    pnl_maker = sum(f.realised_pnl_usd for f in fills_maker)
    fees_paid = sum(f.fee_usd for f in fills_taker)

    by_station: dict[str, dict] = {}
    by_lead: dict[int, dict] = {}
    for f in fills_taker:
        st = by_station.setdefault(f.station_slug, {"n": 0, "pnl": 0.0, "fees": 0.0})
        st["n"] += 1
        st["pnl"] += f.realised_pnl_usd
        st["fees"] += f.fee_usd
        lead = (f.target_date - f.posted_at.date()).days
        ld = by_lead.setdefault(lead, {"n": 0, "pnl": 0.0})
        ld["n"] += 1
        ld["pnl"] += f.realised_pnl_usd

    all_fills = sorted(
        list(fills_taker) + list(fills_maker), key=lambda f: f.filled_at
    )
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    equity_curve = []
    for f in all_fills:
        cum += f.realised_pnl_usd
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)
        equity_curve.append(
            {
                "filled_at": f.filled_at.isoformat(),
                "kind": "taker" if f in fills_taker else "maker",
                "incremental_pnl_usd": round(f.realised_pnl_usd, 6),
                "cumulative_pnl_usd": round(cum, 6),
            }
        )

    snapshot_stats = {
        "n_snapshots": 96,
        "median_hours_between": 4.5,
        "mean_hours_between": 5.2,
        "first_snapshot_at": "2026-04-01T00:00:00+00:00",
        "last_snapshot_at": "2026-05-08T18:00:00+00:00",
        "take_every_n_snapshots": 1,
        "total_snapshots_in_db": 240,
        "n_event_snapshots": 220,
        "n_event_snapshots_with_probs": 198,
        "n_bucket_opportunities": 2178,
        "n_taker_edges_found": 27,
        "n_taker_filled": len(fills_taker),
        "n_maker_quotes_attempted": 198,
        "n_maker_orders_posted": 18,
        "n_maker_orders_filled": len(fills_maker),
        "taker_fill_rate": len(fills_taker) / 27,
        "maker_fill_rate": len(fills_maker) / 18,
    }

    result = BacktestResult(
        n_snapshots=96,
        n_events_resolved=10,
        fills_taker=fills_taker,
        fills_maker=fills_maker,
        pnl_taker_usd=pnl_taker,
        pnl_maker_usd=pnl_maker,
        fees_paid_usd=fees_paid,
        realised_log_loss=2.05,
        by_station=by_station,
        by_lead=by_lead,
        notes=["Synthetic fixture for offline dashboard demos."],
        snapshot_stats=snapshot_stats,
        equity_curve=equity_curve,
        max_drawdown_usd=max_dd,
    )

    return backtest_result_to_dict(
        result,
        model_id="m2_postprocessed_ens",
        start=dt.date(2026, 4, 1),
        end=dt.date(2026, 5, 12),
        strategy="both",
        caps=CapsConfig(
            bankroll_usd=500.0,
            per_bucket_usd=5.0,
            per_event_usd=20.0,
            per_day_usd=100.0,
            per_portfolio_usd=500.0,
            kelly_fraction=0.25,
            min_edge_per_dollar=0.02,
        ),
        fees=FeeSchedule(),
    )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--write-fixture", action="store_true",
        help=f"Overwrite {FIXTURE_PATH.relative_to(REPO_ROOT)} with the demo payload.",
    )
    args = p.parse_args()

    payload = build_demo_payload()

    assert payload["meta"]["export_version"] == 2
    required = {
        "bucket_lo_f", "bucket_hi_f", "lead_days", "notional_usd",
        "expected_pnl_per_share_at_post", "fee_usd",
        "realised_label", "realised_pnl_usd", "settled", "won",
    }
    for key in ("fills_taker", "fills_maker"):
        for f in payload[key]:
            missing = required - f.keys()
            assert not missing, f"{key} missing: {missing}"
    print(
        f"OK: export_version={payload['meta']['export_version']} "
        f"taker_fills={len(payload['fills_taker'])} "
        f"maker_fills={len(payload['fills_maker'])} "
        f"net_pnl=${payload['summary']['net_pnl_usd']:+.2f}"
    )

    if args.write_fixture:
        FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Wrote fixture {FIXTURE_PATH}")


if __name__ == "__main__":
    main()
