"""Calibration / backtest of bucket probabilities against resolved events.

For each past ``(model_id, event_slug)`` we have ``bucket_probs`` for, identify
the realised bucket from the matching ``observations`` row and compute log-loss
+ Brier score. A reliability diagram is drawn from the *flattened* prediction /
indicator pairs across all buckets.

The special model id ``market:mid`` does not look in ``bucket_probs`` at all —
instead it derives a probability vector per resolved event from the latest
``pm_market_snapshots.mid`` per bucket, normalised across the event. That gives
a like-for-like comparison: how good is the market itself at pricing each
realised outcome, vs the same scoring grid that M0 / M1 / M2 are scored on.
This is the only honest baseline for "do we beat the market".
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import uuid
from dataclasses import dataclass
from typing import Iterable, Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from . import config
from .db import station_id_by_slug, with_conn
from .score import (
    BucketBounds,
    brier_one,
    log_loss_one,
    realised_bucket,
    reliability_bins,
    reliability_to_jsonable,
)
from .stations import REGISTRY

log = logging.getLogger(__name__)

MODEL_MARKET_MID = "market:mid"


@dataclass
class EventScore:
    event_slug: str
    target_date: dt.date
    station_slug: str
    realised_label: str
    realised_prob: float
    log_loss: float
    brier: float


@dataclass
class CalibrationResult:
    run_id: uuid.UUID
    model_id: str
    sample_n: int
    log_loss: float | None
    brier: float | None
    by_station: dict[str, dict[str, float]]
    by_lead: dict[int, dict[str, float]]
    bins_jsonable: list[dict]
    forecast_source_metrics: list[dict]
    report_path: str
    started_at: dt.datetime
    ended_at: dt.datetime


def _fetch_resolved_buckets(
    cur, model_id: str, station_slugs: Sequence[str], lookback_days: int
) -> list[tuple]:
    """Return rows of (event_slug, target_date, station_slug, label,
    lo_f, hi_f, prob, observed_max_f, run_time).

    **Determinism fix (review §2.3):** wrap the ``bucket_probs`` scan in a
    ``DISTINCT ON (event_slug, bucket_label)`` CTE ordered by ``run_time
    DESC`` with the cutoff ``bp.run_time <= e.target_date + interval '12
    hours'`` (Polymarket weather markets close 12:00 UTC). Previously the
    join exploded every historical prediction snapshot into a separate row,
    so log-loss/Brier depended on which run_time the SQL planner chose
    first — i.e. results were non-deterministic.
    """
    cutoff = dt.date.today() - dt.timedelta(days=lookback_days)
    cur.execute(
        """
        WITH latest_bp AS (
            SELECT DISTINCT ON (bp.event_slug, bp.bucket_label)
                bp.event_slug,
                bp.bucket_label,
                bp.prob,
                bp.run_time
            FROM bucket_probs bp
            JOIN pm_events e2 ON e2.event_slug = bp.event_slug
            WHERE bp.model_id   = %s
              AND e2.target_date <= CURRENT_DATE
              AND e2.target_date >= %s
              AND bp.run_time   <= (e2.target_date::timestamp + interval '12 hours')
            ORDER BY bp.event_slug, bp.bucket_label, bp.run_time DESC
        )
        SELECT
            e.event_slug,
            e.target_date,
            s.slug AS station_slug,
            b.bucket_label,
            b.lo_f,
            b.hi_f,
            lb.prob::float8,
            o.observed_max_f::float8,
            lb.run_time
        FROM latest_bp lb
        JOIN pm_events  e ON e.event_slug = lb.event_slug
        JOIN pm_buckets b ON b.event_slug = lb.event_slug
                          AND b.bucket_label = lb.bucket_label
        JOIN stations   s ON s.station_id   = e.station_id
        JOIN observations o
                          ON o.station_id   = e.station_id
                         AND o.obs_date     = e.target_date
                         AND o.observed_max_f IS NOT NULL
                         AND o.finalized   = TRUE
        WHERE s.slug = ANY(%s)
        ORDER BY e.event_slug, b.bucket_label
        """,
        (model_id, cutoff, list(station_slugs)),
    )
    return list(cur.fetchall())


def _fetch_market_baseline_buckets(
    cur, station_slugs: Sequence[str], lookback_days: int
) -> list[tuple]:
    """Same row shape as :func:`_fetch_resolved_buckets`, but ``prob`` is the
    latest ``pm_market_snapshots.mid`` per ``(event_slug, bucket_label)``,
    normalised across the event.

    Snapshot semantics: Polymarket markets close 12:00 UTC on the target day,
    so the "latest" snapshot is effectively the closing-time consensus mid.
    For events that have never been snapshotted we leave ``prob`` NULL and
    they get dropped during normalisation."""
    cutoff = dt.date.today() - dt.timedelta(days=lookback_days)
    cur.execute(
        """
        WITH latest_snap AS (
            SELECT DISTINCT ON (s.event_slug, s.bucket_label)
                s.event_slug,
                s.bucket_label,
                s.mid,
                s.best_bid,
                s.best_ask,
                s.snapshot_at
            FROM pm_market_snapshots s
            ORDER BY s.event_slug, s.bucket_label, s.snapshot_at DESC
        )
        SELECT
            e.event_slug,
            e.target_date,
            sn.slug AS station_slug,
            b.bucket_label,
            b.lo_f,
            b.hi_f,
            COALESCE(
                ls.mid,
                (ls.best_bid + ls.best_ask) / 2.0,
                ls.best_bid,
                ls.best_ask
            )::float8 AS prob_raw,
            o.observed_max_f::float8,
            ls.snapshot_at
        FROM pm_events  e
        JOIN pm_buckets b ON b.event_slug = e.event_slug
        LEFT JOIN latest_snap ls
                          ON ls.event_slug   = b.event_slug
                         AND ls.bucket_label = b.bucket_label
        JOIN stations  sn ON sn.station_id = e.station_id
        JOIN observations o
                          ON o.station_id   = e.station_id
                         AND o.obs_date     = e.target_date
                         AND o.observed_max_f IS NOT NULL
                         AND o.finalized   = TRUE
        WHERE e.target_date <= CURRENT_DATE
          AND e.target_date >= %s
          AND sn.slug = ANY(%s)
        ORDER BY e.event_slug, b.bucket_label
        """,
        (cutoff, list(station_slugs)),
    )
    return list(cur.fetchall())


def _fetch_forecast_source_metrics(
    cur, station_slugs: Sequence[str], lookback_days: int
) -> list[dict]:
    cutoff = dt.date.today() - dt.timedelta(days=lookback_days)
    cur.execute(
        """
        WITH latest AS (
            SELECT DISTINCT ON (f.station_id, f.target_date, f.source)
                f.station_id, f.target_date, f.source, f.predicted_max_f
            FROM forecasts f
            WHERE f.target_date BETWEEN %s AND CURRENT_DATE
              AND f.predicted_max_f IS NOT NULL
            ORDER BY f.station_id, f.target_date, f.source, f.run_time DESC
        )
        SELECT
            s.slug,
            l.source,
            COUNT(*),
            AVG(ABS(l.predicted_max_f - o.observed_max_f))::float8 AS mae,
            SQRT(AVG(POWER(l.predicted_max_f - o.observed_max_f, 2)))::float8 AS rmse
        FROM latest l
        JOIN observations o
          ON o.station_id = l.station_id
         AND o.obs_date   = l.target_date
         AND o.observed_max_f IS NOT NULL
         AND o.finalized = TRUE
        JOIN stations s ON s.station_id = l.station_id
        WHERE s.slug = ANY(%s)
        GROUP BY s.slug, l.source
        ORDER BY s.slug, l.source
        """,
        (cutoff, list(station_slugs)),
    )
    out = []
    for slug, source, n, mae, rmse in cur.fetchall():
        out.append(
            {
                "station": slug,
                "source": source,
                "n": int(n),
                "mae_f": float(mae) if mae is not None else None,
                "rmse_f": float(rmse) if rmse is not None else None,
            }
        )
    return out


def _plot_reliability(bins_jsonable: list[dict], path: str, *, title: str) -> None:
    xs = []
    ys = []
    counts = []
    for b in bins_jsonable:
        if b["count"] == 0:
            continue
        xs.append(b["avg_pred_prob"])
        ys.append(b["empirical_freq"])
        counts.append(b["count"])
    fig, ax = plt.subplots(figsize=(5.5, 5.5), dpi=110)
    ax.plot([0, 1], [0, 1], "--", color="grey", label="perfect")
    if xs:
        sizes = [max(15, c) for c in counts]
        ax.scatter(xs, ys, s=sizes, alpha=0.7, label="bins (size = n)")
    ax.set_xlabel("predicted probability")
    ax.set_ylabel("empirical frequency")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper left")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


INSERT_RUN_SQL = """
INSERT INTO calibration_runs
    (run_id, model_id, station_id, horizon_days, sample_n,
     log_loss, brier, reliability_jsonb, started_at, ended_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
"""


def _bucket_bounds_from_row(label: str, lo, hi) -> BucketBounds:
    return BucketBounds(
        label=label,
        lo_f=None if lo is None else float(lo),
        hi_f=None if hi is None else float(hi),
    )


def run_calibration(
    model_id: str,
    *,
    station_slugs: Sequence[str] | None = None,
    lookback_days: int = 365,
) -> CalibrationResult:
    started_at = dt.datetime.now(dt.timezone.utc)

    if station_slugs is None:
        station_slugs = list(REGISTRY.keys())

    paths = config.paths()
    paths.reports.mkdir(parents=True, exist_ok=True)

    with with_conn() as conn, conn.cursor() as cur:
        if model_id == MODEL_MARKET_MID:
            rows = _fetch_market_baseline_buckets(
                cur, station_slugs, lookback_days
            )
        else:
            rows = _fetch_resolved_buckets(
                cur, model_id, station_slugs, lookback_days
            )
        forecast_metrics = _fetch_forecast_source_metrics(
            cur, station_slugs, lookback_days
        )

    # Group rows by event for per-event log-loss / Brier and to find realised
    # bucket. ``rows`` already constrains buckets to events that have an
    # observation row for the target_date.
    by_event: dict[str, dict] = {}
    pred_pairs: list[tuple[float, int]] = []  # (predicted_prob, indicator)
    for (
        event_slug,
        target_date,
        station_slug,
        label,
        lo,
        hi,
        prob,
        observed,
        run_time,
    ) in rows:
        # ``run_time`` is now the DISTINCT-ON latest bp.run_time per
        # (event, bucket). Use it for forecast-lead-day labelling
        # (review §2.10) instead of (today - target_date) which measured
        # days since resolution.
        e = by_event.setdefault(
            event_slug,
            {
                "target_date": target_date,
                "station_slug": station_slug,
                "observed_max_f": float(observed),
                "buckets": [],
                "probs": {},
                "lead_days": (target_date - run_time.date()).days
                if run_time is not None
                else None,
            },
        )
        bb = _bucket_bounds_from_row(label, lo, hi)
        e["buckets"].append(bb)
        # ``prob`` may be NULL for the market baseline when no snapshot exists
        # for that bucket; treat as 0 so it falls out during renormalisation.
        e["probs"][label] = 0.0 if prob is None else float(prob)

    event_scores: list[EventScore] = []
    event_lead: dict[str, int | None] = {}
    for slug, e in by_event.items():
        realised = realised_bucket(e["buckets"], e["observed_max_f"])
        if realised is None:
            continue
        # Renormalise the saved probs in case rounding drift
        s = sum(e["probs"].values())
        if s <= 0:
            continue
        probs = {k: v / s for k, v in e["probs"].items()}
        p_real = probs.get(realised.label, 0.0)
        ll = log_loss_one(p_real)
        br = brier_one(probs, realised.label)
        event_scores.append(
            EventScore(
                event_slug=slug,
                target_date=e["target_date"],
                station_slug=e["station_slug"],
                realised_label=realised.label,
                realised_prob=p_real,
                log_loss=ll,
                brier=br,
            )
        )
        event_lead[slug] = e.get("lead_days")
        for label, p in probs.items():
            pred_pairs.append((p, 1 if label == realised.label else 0))

    sample_n = len(event_scores)
    if sample_n == 0:
        log.info(
            "No resolved events for model %s in last %d days. "
            "Calibration row will record an empty sample.",
            model_id, lookback_days,
        )

    log_loss_mean = (
        sum(es.log_loss for es in event_scores) / sample_n if sample_n else None
    )
    brier_mean = (
        sum(es.brier for es in event_scores) / sample_n if sample_n else None
    )

    # Slices
    by_station: dict[str, dict[str, float]] = {}
    for es in event_scores:
        bucket = by_station.setdefault(
            es.station_slug, {"n": 0, "log_loss": 0.0, "brier": 0.0}
        )
        bucket["n"] += 1
        bucket["log_loss"] += es.log_loss
        bucket["brier"] += es.brier
    for v in by_station.values():
        if v["n"] > 0:
            v["log_loss"] /= v["n"]
            v["brier"] /= v["n"]

    # Lead-time slice: ``lead = target_date - bp.run_time.date()`` measures
    # the forecast *horizon* the model was working with (review §2.10).
    # Falls back to "days since resolution" only when run_time is absent.
    by_lead: dict[int, dict[str, float]] = {}
    today = dt.date.today()
    for es in event_scores:
        lead = event_lead.get(es.event_slug)
        if lead is None:
            lead = (today - es.target_date).days
        bucket = by_lead.setdefault(lead, {"n": 0, "log_loss": 0.0, "brier": 0.0})
        bucket["n"] += 1
        bucket["log_loss"] += es.log_loss
        bucket["brier"] += es.brier
    for v in by_lead.values():
        if v["n"] > 0:
            v["log_loss"] /= v["n"]
            v["brier"] /= v["n"]

    bins = reliability_bins([p for p, _ in pred_pairs], [y for _, y in pred_pairs])
    bins_jsonable = reliability_to_jsonable(bins)

    run_id = uuid.uuid4()
    img_path = paths.reports / f"calibration_{run_id}_reliability.png"
    if pred_pairs:
        _plot_reliability(
            bins_jsonable, str(img_path), title=f"Reliability — {model_id}"
        )

    report_path = paths.reports / f"calibration_{run_id}.md"
    report_path.write_text(
        _render_report(
            model_id=model_id,
            run_id=run_id,
            sample_n=sample_n,
            log_loss=log_loss_mean,
            brier=brier_mean,
            by_station=by_station,
            by_lead=by_lead,
            bins=bins_jsonable,
            forecast_metrics=forecast_metrics,
            img_relpath=img_path.name if pred_pairs else None,
            event_scores=event_scores,
            lookback_days=lookback_days,
            station_slugs=list(station_slugs),
        ),
        encoding="utf-8",
    )

    ended_at = dt.datetime.now(dt.timezone.utc)
    with with_conn() as conn, conn.cursor() as cur:
        cur.execute(
            INSERT_RUN_SQL,
            (
                str(run_id),
                model_id,
                None,
                None,
                sample_n,
                log_loss_mean,
                brier_mean,
                json.dumps(
                    {
                        "bins": bins_jsonable,
                        "by_station": by_station,
                        "by_lead": {str(k): v for k, v in by_lead.items()},
                        "forecast_source_metrics": forecast_metrics,
                    }
                ),
                started_at,
                ended_at,
            ),
        )

    return CalibrationResult(
        run_id=run_id,
        model_id=model_id,
        sample_n=sample_n,
        log_loss=log_loss_mean,
        brier=brier_mean,
        by_station=by_station,
        by_lead={int(k): v for k, v in by_lead.items()},
        bins_jsonable=bins_jsonable,
        forecast_source_metrics=forecast_metrics,
        report_path=str(report_path),
        started_at=started_at,
        ended_at=ended_at,
    )


def _fmt(x: float | None, p: int = 4) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{x:.{p}f}"


def _render_report(
    *,
    model_id: str,
    run_id: uuid.UUID,
    sample_n: int,
    log_loss: float | None,
    brier: float | None,
    by_station: dict[str, dict[str, float]],
    by_lead: dict[int, dict[str, float]],
    bins: list[dict],
    forecast_metrics: list[dict],
    img_relpath: str | None,
    event_scores: list[EventScore],
    lookback_days: int,
    station_slugs: list[str],
) -> str:
    lines: list[str] = []
    lines.append(f"# Calibration run `{run_id}`")
    lines.append("")
    lines.append(f"- **Model**: `{model_id}`")
    lines.append(f"- **Stations**: {', '.join(station_slugs)}")
    lines.append(f"- **Lookback**: {lookback_days} days")
    lines.append(f"- **Resolved events scored**: {sample_n}")
    lines.append(f"- **Mean log-loss**: {_fmt(log_loss)}")
    lines.append(f"- **Mean Brier**:    {_fmt(brier)}")
    lines.append("")

    if img_relpath:
        lines.append("## Reliability diagram")
        lines.append("")
        lines.append(f"![reliability]({img_relpath})")
        lines.append("")

    lines.append("## By station")
    lines.append("")
    lines.append("| station | n | log-loss | brier |")
    lines.append("|---|---:|---:|---:|")
    for slug in sorted(by_station):
        v = by_station[slug]
        lines.append(
            f"| {slug} | {int(v['n'])} | {_fmt(v['log_loss'])} | {_fmt(v['brier'])} |"
        )
    if not by_station:
        lines.append("| — | — | — | — |")
    lines.append("")

    lines.append("## By forecast lead time (target_date − bp.run_time, days)")
    lines.append("")
    lines.append("| lead_days | n | log-loss | brier |")
    lines.append("|---:|---:|---:|---:|")
    for lead in sorted(by_lead):
        v = by_lead[lead]
        lines.append(
            f"| {lead} | {int(v['n'])} | {_fmt(v['log_loss'])} | {_fmt(v['brier'])} |"
        )
    if not by_lead:
        lines.append("| — | — | — | — |")
    lines.append("")

    lines.append("## Reliability bins (raw)")
    lines.append("")
    lines.append("| bin | count | avg pred prob | empirical freq |")
    lines.append("|---|---:|---:|---:|")
    for b in bins:
        rng = f"[{b['bin_lo']:.2f}, {b['bin_hi']:.2f}]"
        lines.append(
            f"| {rng} | {b['count']} | "
            f"{_fmt(b['avg_pred_prob'])} | {_fmt(b['empirical_freq'])} |"
        )
    lines.append("")

    lines.append("## Forecast source MAE / RMSE vs observations")
    lines.append("")
    lines.append("| station | source | n | MAE (F) | RMSE (F) |")
    lines.append("|---|---|---:|---:|---:|")
    for fm in forecast_metrics:
        lines.append(
            f"| {fm['station']} | {fm['source']} | {fm['n']} | "
            f"{_fmt(fm['mae_f'], 3)} | {_fmt(fm['rmse_f'], 3)} |"
        )
    if not forecast_metrics:
        lines.append("| — | — | — | — | — |")
    lines.append("")

    if event_scores:
        lines.append("## Event-level detail (most recent 25)")
        lines.append("")
        lines.append("| target_date | station | realised label | p(realised) | log-loss | brier |")
        lines.append("|---|---|---|---:|---:|---:|")
        es_sorted = sorted(event_scores, key=lambda e: e.target_date, reverse=True)
        for es in es_sorted[:25]:
            lines.append(
                f"| {es.target_date} | {es.station_slug} | {es.realised_label} | "
                f"{_fmt(es.realised_prob, 3)} | {_fmt(es.log_loss, 3)} | "
                f"{_fmt(es.brier, 3)} |"
            )
        lines.append("")

    return "\n".join(lines) + "\n"
