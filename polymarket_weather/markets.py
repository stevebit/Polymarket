"""Polymarket Gamma discovery + bucket parsing + CLOB read-only snapshots.

This module is **read only**: it never signs, never places orders, never imports
``polymarket_manual.clients``. The CLOB is queried for order books only via the
public read-only endpoints (no API credentials).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable

import httpx

from . import config
from .stations import REGISTRY, Station

log = logging.getLogger(__name__)

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"

# Tail bucket: ``55F or below``, ``74F or higher`` (degree symbol optional).
_TAIL_RE = re.compile(
    r"^\s*(\d+)\s*[°*o]?\s*F\s*or\s*(below|higher)\s*$",
    re.IGNORECASE,
)
# Interior bucket: ``56-57F``.
_INTERIOR_RE = re.compile(
    r"^\s*(\d+)\s*-\s*(\d+)\s*[°*o]?\s*F\s*$",
    re.IGNORECASE,
)


def _normalise_label(s: str) -> str:
    """Replace unicode degree sign (and look-alikes) with empty so the regex
    matches regardless of UTF-8 quirks."""
    if not s:
        return s
    return (
        s.replace("\u00b0", "")  # degree sign
        .replace("\u00ba", "")  # masculine ordinal
        .replace("\ufffd", "")  # replacement char
        .strip()
    )


@dataclass(frozen=True)
class ParsedBucket:
    label: str
    lo_f: float | None  # inclusive integer lower bound, None if open
    hi_f: float | None  # inclusive integer upper bound, None if open

    @property
    def is_low_tail(self) -> bool:
        return self.lo_f is None and self.hi_f is not None

    @property
    def is_high_tail(self) -> bool:
        return self.lo_f is not None and self.hi_f is None


def parse_bucket_label(label: str) -> ParsedBucket | None:
    """Parse a Polymarket bucket label like ``56-57F`` or ``55F or below``."""
    if not label:
        return None
    cleaned = _normalise_label(label)
    m = _INTERIOR_RE.match(cleaned)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        return ParsedBucket(label=label, lo_f=float(lo), hi_f=float(hi))
    m = _TAIL_RE.match(cleaned)
    if m:
        n = int(m.group(1))
        kind = m.group(2).lower()
        if kind == "below":
            return ParsedBucket(label=label, lo_f=None, hi_f=float(n))
        return ParsedBucket(label=label, lo_f=float(n), hi_f=None)
    return None


def slug_for(station: Station, target: dt.date) -> str:
    """Build the Gamma slug ``highest-temperature-in-{city}-on-{month}-{day}-{year}``."""
    return (
        f"highest-temperature-in-{station.polymarket_city_slug}"
        f"-on-{target.strftime('%B').lower()}-{target.day}-{target.year}"
    )


async def _get_event(client: httpx.AsyncClient, slug: str) -> dict[str, Any] | None:
    r = await client.get(GAMMA_EVENTS_URL, params={"slug": slug}, timeout=15.0)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list) and data:
        return data[0]
    return None


def _coerce_json_field(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return v
    return v


@dataclass
class DiscoveredEvent:
    slug: str
    station: Station
    target_date: dt.date
    gamma_event_id: str | None
    raw: dict[str, Any]
    buckets: list[dict[str, Any]]  # ready for INSERT


def _build_buckets(event: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in event.get("markets") or []:
        label = m.get("groupItemTitle") or ""
        parsed = parse_bucket_label(label)
        if parsed is None:
            log.warning(
                "Could not parse bucket label %r in event %s", label, event.get("slug")
            )
            continue
        token_ids = _coerce_json_field(m.get("clobTokenIds")) or []
        outcomes = _coerce_json_field(m.get("outcomes")) or []
        yes_token = None
        no_token = None
        if isinstance(outcomes, list) and isinstance(token_ids, list):
            for outcome, tok in zip(outcomes, token_ids):
                if str(outcome).lower() == "yes":
                    yes_token = tok
                elif str(outcome).lower() == "no":
                    no_token = tok
        # Fallback: assume YES = first if outcomes ordering is missing.
        if yes_token is None and isinstance(token_ids, list) and token_ids:
            yes_token = token_ids[0]
        if no_token is None and isinstance(token_ids, list) and len(token_ids) > 1:
            no_token = token_ids[1]

        out.append(
            {
                "bucket_label": label,
                "lo_f": parsed.lo_f,
                "hi_f": parsed.hi_f,
                "yes_token_id": yes_token,
                "no_token_id": no_token,
                "condition_id": m.get("conditionId"),
                "tick_size": m.get("orderPriceMinTickSize"),
            }
        )
    return out


async def _discover_one(
    client: httpx.AsyncClient,
    station: Station,
    target: dt.date,
) -> DiscoveredEvent | None:
    slug = slug_for(station, target)
    try:
        ev = await _get_event(client, slug)
    except httpx.HTTPError as exc:
        log.warning("Gamma fetch failed for %s: %s", slug, exc)
        return None
    if ev is None:
        return None
    buckets = _build_buckets(ev)
    if not buckets:
        log.info("No buckets parsed for %s — skipping persistence.", slug)
        return None
    return DiscoveredEvent(
        slug=slug,
        station=station,
        target_date=target,
        gamma_event_id=str(ev.get("id")) if ev.get("id") is not None else None,
        raw=ev,
        buckets=buckets,
    )


async def discover_events(
    stations: Iterable[Station],
    start_date: dt.date,
    days_ahead: int,
    *,
    concurrency: int = 5,
) -> list[DiscoveredEvent]:
    targets: list[tuple[Station, dt.date]] = []
    for s in stations:
        for d in range(days_ahead + 1):
            targets.append((s, start_date + dt.timedelta(days=d)))

    sem = asyncio.Semaphore(concurrency)
    headers = {"User-Agent": config.http_user_agent()}

    async with httpx.AsyncClient(headers=headers) as client:
        async def _runner(station: Station, target: dt.date) -> DiscoveredEvent | None:
            async with sem:
                return await _discover_one(client, station, target)

        results = await asyncio.gather(
            *(_runner(s, t) for s, t in targets), return_exceptions=False
        )
    return [r for r in results if r is not None]


# ---------------------------------------------------------------------------
# Persistence (sync, uses pooled connection).
# ---------------------------------------------------------------------------

UPSERT_EVENT_SQL = """
INSERT INTO pm_events (event_slug, station_id, target_date, gamma_event_id, raw)
VALUES (%s, %s, %s, %s, %s::jsonb)
ON CONFLICT (event_slug) DO UPDATE SET
    station_id     = EXCLUDED.station_id,
    target_date    = EXCLUDED.target_date,
    gamma_event_id = EXCLUDED.gamma_event_id,
    raw            = EXCLUDED.raw,
    fetched_at     = now()
"""

UPSERT_BUCKET_SQL = """
INSERT INTO pm_buckets
    (event_slug, bucket_label, lo_f, hi_f, yes_token_id, no_token_id,
     condition_id, tick_size)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (event_slug, bucket_label) DO UPDATE SET
    lo_f         = EXCLUDED.lo_f,
    hi_f         = EXCLUDED.hi_f,
    yes_token_id = EXCLUDED.yes_token_id,
    no_token_id  = EXCLUDED.no_token_id,
    condition_id = EXCLUDED.condition_id,
    tick_size    = EXCLUDED.tick_size
"""

INSERT_SNAPSHOT_SQL = """
INSERT INTO pm_market_snapshots
    (event_slug, bucket_label, snapshot_at, best_bid, best_ask,
     last_trade, mid, depth_jsonb)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
ON CONFLICT (event_slug, bucket_label, snapshot_at) DO NOTHING
"""


def persist_events(
    events: list[DiscoveredEvent], station_ids: dict[str, int]
) -> dict[str, int]:
    """Upsert ``pm_events`` and ``pm_buckets`` rows. Returns counters."""
    from .db import with_conn

    n_ev = 0
    n_bk = 0
    with with_conn() as conn, conn.cursor() as cur:
        for ev in events:
            sid = station_ids[ev.station.slug]
            cur.execute(
                UPSERT_EVENT_SQL,
                (
                    ev.slug,
                    sid,
                    ev.target_date,
                    ev.gamma_event_id,
                    json.dumps(ev.raw),
                ),
            )
            n_ev += 1
            for b in ev.buckets:
                cur.execute(
                    UPSERT_BUCKET_SQL,
                    (
                        ev.slug,
                        b["bucket_label"],
                        b["lo_f"],
                        b["hi_f"],
                        b["yes_token_id"],
                        b["no_token_id"],
                        b["condition_id"],
                        b["tick_size"],
                    ),
                )
                n_bk += 1
    return {"pm_events": n_ev, "pm_buckets": n_bk}


# ---------------------------------------------------------------------------
# Market snapshots (read-only CLOB).
# ---------------------------------------------------------------------------


def _load_buckets_for_snapshot(event_slugs: list[str]) -> list[tuple[str, str, str]]:
    """Return ``(event_slug, bucket_label, yes_token_id)`` tuples."""
    if not event_slugs:
        return []
    from .db import with_conn

    with with_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT event_slug, bucket_label, yes_token_id
            FROM pm_buckets
            WHERE event_slug = ANY(%s)
              AND yes_token_id IS NOT NULL
            """,
            (event_slugs,),
        )
        return list(cur.fetchall())


def _summarise_book(book: Any) -> dict[str, Any]:
    """Reduce a CLOB order book to ``best_bid / best_ask / mid / depth``.

    The v2 CLOB client returns an ``OrderBookSummary`` with ``bids`` /
    ``asks`` lists of ``OrderSummary(price, size)``. We tolerate dict-like
    shapes too in case the library changes.
    """
    def _entries(side: Any) -> list[tuple[float, float]]:
        out: list[tuple[float, float]] = []
        for e in side or []:
            if hasattr(e, "price") and hasattr(e, "size"):
                p, s = e.price, e.size
            elif isinstance(e, dict):
                p, s = e.get("price"), e.get("size")
            else:
                continue
            try:
                out.append((float(p), float(s)))
            except (TypeError, ValueError):
                continue
        return out

    bids = _entries(getattr(book, "bids", None) or (book.get("bids") if isinstance(book, dict) else []))
    asks = _entries(getattr(book, "asks", None) or (book.get("asks") if isinstance(book, dict) else []))

    # Polymarket order books are typically sorted bids desc / asks asc, but
    # we don't rely on that — pick best from both sides explicitly.
    best_bid = max((p for p, _ in bids), default=None)
    best_ask = min((p for p, _ in asks), default=None)
    mid = None
    if best_bid is not None and best_ask is not None:
        mid = (best_bid + best_ask) / 2.0

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "depth": {"bids": bids[:10], "asks": asks[:10]},
    }


def snapshot_markets(event_slugs: list[str]) -> int:
    """Snapshot best-bid/ask for the YES side of every bucket. Returns row count."""
    rows = _load_buckets_for_snapshot(event_slugs)
    if not rows:
        return 0

    # Lazy import: keeps the module importable in environments where the CLOB
    # client is unavailable (e.g. CI lint).
    from py_clob_client_v2.client import ClobClient

    client = ClobClient(host="https://clob.polymarket.com", chain_id=137)

    from .db import with_conn

    inserted = 0
    now = dt.datetime.now(dt.timezone.utc)
    with with_conn() as conn, conn.cursor() as cur:
        for event_slug, bucket_label, yes_token in rows:
            try:
                book = client.get_order_book(yes_token)
            except Exception as exc:  # noqa: BLE001 — net errors should not crash
                log.warning(
                    "get_order_book failed for %s/%s: %s",
                    event_slug,
                    bucket_label,
                    exc,
                )
                continue
            summary = _summarise_book(book)
            cur.execute(
                INSERT_SNAPSHOT_SQL,
                (
                    event_slug,
                    bucket_label,
                    now,
                    summary["best_bid"],
                    summary["best_ask"],
                    None,
                    summary["mid"],
                    json.dumps(summary["depth"]),
                ),
            )
            inserted += 1
    return inserted


def stations_from_slugs(slugs: Iterable[str]) -> list[Station]:
    out: list[Station] = []
    for s in slugs:
        st = REGISTRY.get(s)
        if st is None:
            log.warning("Unknown station slug %r — skipping", s)
            continue
        out.append(st)
    return out
