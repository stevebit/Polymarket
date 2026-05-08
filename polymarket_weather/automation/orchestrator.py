"""Single-shot orchestrator: ingest delta -> predict -> recommend -> reconcile
-> place / cancel / replace -> snapshot.

Designed to be called every 15 min during the active window per city by
Windows Task Scheduler (or any cron-like runner). All steps are idempotent;
re-running mid-tick should never double-place.

Modes
-----
* ``mode='paper'``: writes to ``paper_trades`` (Phase 4b path).
* ``mode='live'``: writes signed orders via :class:`OrderManager`.
  Requires ``WEATHER_AUTOMATION_ENABLED=1`` and an unset
  ``WEATHER_KILL_SWITCH``. The mode argument is **advisory**; if the
  automation envs aren't set, ``mode='live'`` falls back to ``'paper'``
  with a loud warning so we never accidentally trade.

The orchestrator never bypasses :class:`OrderManager`'s caps. It also tracks
per-tick reconciliation: any local "open" order that the CLOB doesn't
report back is marked ``'unknown'`` for human investigation.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .. import config
from ..data.forecasts import ingest_forecasts
from ..data.live_observations import ingest_live_observations
from ..markets import discover_events, persist_events, snapshot_markets, stations_from_slugs
from ..models.baseline import MODEL_M1, run_predictions
from ..models.m2_postprocessed_ensemble import MODEL_M2, run_m2_predictions
from ..paper import submit_paper_trades
from ..recommend import EventRecommendation, build_recommendations
from ..strategy.edge import Action, FeeSchedule
from ..strategy.sizing import CapsConfig, tiny_bankroll_caps
from . import order_manager, state

log = logging.getLogger(__name__)

Mode = Literal["paper", "live"]


# ---------------------------------------------------------------------------
# Tick result
# ---------------------------------------------------------------------------


@dataclass
class TickResult:
    started_at: dt.datetime
    ended_at: dt.datetime
    mode: Mode
    fallback_to_paper: bool = False
    forecasts_ingested: int = 0
    snapshots_taken: int = 0
    predictions_written: int = 0
    bucket_probs_written: int = 0
    paper_orders: int = 0
    placed_orders: int = 0
    cancelled_orders: int = 0
    cap_breaches: int = 0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _structured_log(record: dict, log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fname = log_dir / f"orch_{dt.date.today().isoformat()}.jsonl"
    with fname.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


def _safe(name: str, fn, *args, errors: list[str], **kwargs):
    """Run ``fn(*args, **kwargs)`` with structured exception capture."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        msg = f"{name} failed: {exc}"
        log.warning("%s\n%s", msg, traceback.format_exc())
        errors.append(msg)
        return None


# ---------------------------------------------------------------------------
# Tick
# ---------------------------------------------------------------------------


def run_tick(
    *,
    mode: Mode = "paper",
    station_slugs: list[str] | None = None,
    days_ahead: int = 7,
    caps: CapsConfig | None = None,
    fees: FeeSchedule = FeeSchedule(),
    do_ingest: bool = True,
    do_snapshot: bool = True,
    log_dir: Path | None = None,
) -> TickResult:
    started = dt.datetime.now(dt.timezone.utc)
    station_slugs = station_slugs or config.station_slugs()
    caps = caps or tiny_bankroll_caps()
    log_dir = log_dir or (config.paths().repo_root / "logs")
    errors: list[str] = []

    # 1. Mode resolution
    fallback = False
    if mode == "live" and not order_manager.is_automation_enabled():
        log.warning(
            "mode='live' requested but %s is not set; falling back to paper mode.",
            order_manager.ENV_AUTOMATION,
        )
        mode = "paper"
        fallback = True
    if mode == "live" and order_manager.is_kill_switch_set():
        log.warning(
            "%s is set; falling back to paper mode for this tick.",
            order_manager.ENV_KILL_SWITCH,
        )
        mode = "paper"
        fallback = True

    # 2. Ingest delta
    if do_ingest:
        _safe(
            "ingest_forecasts",
            ingest_forecasts,
            station_slugs,
            past_days=2,
            forecast_days=days_ahead + 1,
            errors=errors,
        )
        _safe(
            "ingest_live_observations",
            ingest_live_observations,
            station_slugs,
            hours_back=36,
            errors=errors,
        )

    # 3. Discover events + snapshot markets (read-only CLOB)
    if do_snapshot:
        today = dt.date.today()
        from ..db import station_id_by_slug

        sid_map = station_id_by_slug()
        stations = stations_from_slugs(station_slugs)
        import asyncio
        events = _safe(
            "discover_events",
            lambda: asyncio.run(discover_events(stations, today, days_ahead)),
            errors=errors,
        )
        if events:
            _safe(
                "persist_events",
                persist_events,
                events,
                sid_map,
                errors=errors,
            )
            n_snap = _safe(
                "snapshot_markets",
                snapshot_markets,
                [e.slug for e in events],
                errors=errors,
            )
        else:
            n_snap = 0
    else:
        n_snap = 0

    # 4. Predict (M1 always, M2 best-effort)
    today = dt.date.today()
    targets = [today + dt.timedelta(days=i) for i in range(days_ahead + 1)]
    preds_m1 = _safe(
        "predict_m1",
        run_predictions,
        station_slugs,
        targets,
        errors=errors,
        models=(MODEL_M1,),
    ) or {"predictions": 0, "bucket_probs": 0}
    preds_m2 = _safe(
        "predict_m2",
        run_m2_predictions,
        station_slugs,
        targets,
        errors=errors,
    ) or {"predictions": 0, "bucket_probs": 0}

    # 5. Recommendations
    recs: list[EventRecommendation] = _safe(
        "build_recommendations",
        build_recommendations,
        station_slugs=station_slugs,
        days_ahead=days_ahead,
        primary_model=MODEL_M2,
        fallback_model=MODEL_M1,
        caps=caps,
        fees=fees,
        errors=errors,
    ) or []

    paper_n = 0
    placed = 0
    cancelled = 0
    cap_breaches = 0

    if mode == "paper":
        paper_n = _safe(
            "submit_paper_trades",
            submit_paper_trades,
            recs,
            errors=errors,
        ) or 0
    else:  # live
        omgr = order_manager.OrderManager(caps=caps, fees=fees)
        # 5a. Reconcile open orders
        open_local = state.list_open_orders()
        try:
            open_remote = omgr.list_open()
            open_remote_ids = {
                str(o.get("id") or o.get("orderId") or o.get("order_id"))
                for o in open_remote
                if isinstance(o, dict)
            }
            for o in open_local:
                if o["order_id"] not in open_remote_ids:
                    state.update_order_status(o["order_id"], status="unknown")
                    log.warning(
                        "Order %s missing from CLOB list; marked unknown.",
                        o["order_id"],
                    )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"reconcile failed: {exc}")

        # 5b. Place new orders for each recommendation
        for r in recs:
            if r.flagged_arb:
                # Phase 6: arb-only execution requires extra audit. Skip for now.
                log.info(
                    "Skipping %s: neg-risk arb flagged (sum_yes_asks=%.3f)",
                    r.event_slug, r.sum_yes_asks,
                )
                continue
            for b in r.buckets:
                _, yes_token = _resolve_token_ids(r.event_slug, b.bucket_label)
                for sized, side in _orders_from_bucket(b):
                    ctx = order_manager.PlacementContext(
                        event_slug=r.event_slug,
                        bucket_label=b.bucket_label,
                        target_date=r.target_date,
                        sized=sized,
                        p_model=b.p_model,
                        model_id=r.model_id,
                        model_run_time=r.model_run_time,
                        yes_token_id=yes_token,
                    )
                    try:
                        omgr.place_limit(ctx)
                        placed += 1
                    except order_manager.CapBreach as exc:
                        log.info(
                            "Cap breach on %s/%s: %s — skipping.",
                            r.event_slug, b.bucket_label, exc,
                        )
                        cap_breaches += 1
                    except (
                        order_manager.OrderManagerDisabled,
                        order_manager.OrderManagerKilled,
                    ) as exc:
                        errors.append(str(exc))
                        break
                    except Exception as exc:  # noqa: BLE001
                        errors.append(
                            f"place_limit failed for {r.event_slug}/"
                            f"{b.bucket_label}: {exc}"
                        )

    # 6. Refresh daily PnL aggregate
    _safe("refresh_daily_pnl", state.refresh_daily_pnl, today, errors=errors)

    ended = dt.datetime.now(dt.timezone.utc)
    result = TickResult(
        started_at=started,
        ended_at=ended,
        mode=mode,
        fallback_to_paper=fallback,
        forecasts_ingested=preds_m1.get("predictions", 0),
        snapshots_taken=int(n_snap or 0),
        predictions_written=preds_m1.get("predictions", 0)
        + preds_m2.get("predictions", 0),
        bucket_probs_written=preds_m1.get("bucket_probs", 0)
        + preds_m2.get("bucket_probs", 0),
        paper_orders=int(paper_n),
        placed_orders=placed,
        cancelled_orders=cancelled,
        cap_breaches=cap_breaches,
        errors=errors,
    )
    _structured_log(
        {
            "started_at": started.isoformat(),
            "ended_at": ended.isoformat(),
            "mode": mode,
            "fallback_to_paper": fallback,
            "snapshots_taken": result.snapshots_taken,
            "predictions_written": result.predictions_written,
            "paper_orders": result.paper_orders,
            "placed_orders": result.placed_orders,
            "cap_breaches": result.cap_breaches,
            "errors": result.errors,
        },
        log_dir,
    )
    return result


# ---------------------------------------------------------------------------
# Helpers used in live placement
# ---------------------------------------------------------------------------


def _resolve_token_ids(event_slug: str, bucket_label: str) -> tuple[str | None, str | None]:
    from ..db import with_conn

    with with_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT yes_token_id, no_token_id FROM pm_buckets "
            "WHERE event_slug=%s AND bucket_label=%s",
            (event_slug, bucket_label),
        )
        row = cur.fetchone()
    if not row:
        return None, None
    return row[0], row[1]


def _orders_from_bucket(b):
    """Yield (sized, action) tuples for the orders the live mode should
    attempt for this bucket."""
    if b.best_taker is not None:
        yield b.best_taker, b.best_taker.edge.action
    if b.maker_buy is not None:
        yield b.maker_buy, Action.MAKER_BUY
    if b.maker_sell is not None:
        yield b.maker_sell, Action.MAKER_SELL


# ---------------------------------------------------------------------------
# Continuous loop (optional)
# ---------------------------------------------------------------------------


def run_loop(
    *,
    interval_seconds: int = 900,
    max_iterations: int | None = None,
    **tick_kwargs,
) -> None:
    """Long-running daemon. Prefer Windows Task Scheduler over this for
    durability — but useful for development."""
    i = 0
    while True:
        try:
            result = run_tick(**tick_kwargs)
            log.info(
                "Tick %d done: mode=%s placed=%d paper=%d errors=%d",
                i, result.mode, result.placed_orders,
                result.paper_orders, len(result.errors),
            )
        except KeyboardInterrupt:
            log.info("Interrupted; exiting.")
            return
        except Exception as exc:  # noqa: BLE001
            log.error("Tick crashed: %s", exc, exc_info=True)
        i += 1
        if max_iterations is not None and i >= max_iterations:
            return
        time.sleep(interval_seconds)
