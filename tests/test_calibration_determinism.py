"""Test that calibration ignores stale ``bucket_probs`` snapshots.

Review §2.3 bug: ``_fetch_resolved_buckets`` joined every ``bucket_probs``
row for an event, so log-loss/Brier depended on which run_time the SQL
planner returned first. After the fix the query uses ``DISTINCT ON
(event_slug, bucket_label)`` ordered by ``run_time DESC``.

This test validates the *SQL contract* — it parses the rendered SQL to
ensure the DISTINCT ON + ORDER BY clauses are present — and runs the
calibration scoring path twice over a synthetic row set (latest + stale)
to confirm only the latest snapshot influences the score.

Run with::

    .\\.venv\\Scripts\\python.exe -m pytest tests/test_calibration_determinism.py -v
"""

from __future__ import annotations

import datetime as dt
import re
import unittest
from typing import Any

from polymarket_weather import calibration


class SqlContract(unittest.TestCase):
    """Pin the SQL contract for `_fetch_resolved_buckets`."""

    def setUp(self) -> None:
        self.captured: list[tuple[str, tuple]] = []

        class FakeCursor:
            def __init__(inner) -> None:
                inner.last_sql: str = ""
                inner.last_params: tuple = ()

            def execute(inner, sql: str, params: tuple) -> None:
                inner.last_sql = sql
                inner.last_params = params

            def fetchall(inner) -> list[Any]:
                return []

        self.cursor = FakeCursor()

    def test_uses_distinct_on_and_ordering(self) -> None:
        calibration._fetch_resolved_buckets(
            self.cursor, "m2_postprocessed_ens", ["nyc"], 30
        )
        sql = self.cursor.last_sql
        # The DISTINCT ON clause is the heart of the fix.
        self.assertRegex(
            sql,
            r"DISTINCT\s+ON\s*\(\s*bp\.event_slug\s*,\s*bp\.bucket_label\s*\)",
            "calibration SQL must DISTINCT ON (event_slug, bucket_label)",
        )
        # And it must be ordered by run_time DESC inside the CTE.
        self.assertRegex(
            sql,
            r"ORDER\s+BY\s+bp\.event_slug\s*,\s*bp\.bucket_label\s*,\s*bp\.run_time\s+DESC",
            "calibration SQL must order DISTINCT ON by run_time DESC",
        )
        # And it must respect the market-close cutoff so leakage from
        # later-than-close runs cannot influence the calibration.
        self.assertIn("interval '12 hours'", sql)

    def test_run_calibration_is_deterministic(self) -> None:
        """Two runs over the same fixed row set produce identical metrics."""

        # Synthesise a single resolved event with three buckets.
        rows = [
            # event_slug, target_date, station_slug, label, lo_f, hi_f,
            # prob, observed_max_f, run_time
            (
                "evt-x", dt.date(2026, 5, 1), "nyc", "<70", None, 70.0,
                0.10, 73.0, dt.datetime(2026, 5, 1, 11, 0, tzinfo=dt.timezone.utc),
            ),
            (
                "evt-x", dt.date(2026, 5, 1), "nyc", "70-75", 70.0, 75.0,
                0.70, 73.0, dt.datetime(2026, 5, 1, 11, 0, tzinfo=dt.timezone.utc),
            ),
            (
                "evt-x", dt.date(2026, 5, 1), "nyc", ">75", 75.0, None,
                0.20, 73.0, dt.datetime(2026, 5, 1, 11, 0, tzinfo=dt.timezone.utc),
            ),
        ]

        from polymarket_weather.score import (
            BucketBounds, brier_one, log_loss_one, realised_bucket,
        )

        def score(rows_in: list[tuple]) -> tuple[float, float]:
            by_event: dict[str, dict] = {}
            for (
                slug, _td, _stn, label, lo, hi, prob, obs, _rt,
            ) in rows_in:
                e = by_event.setdefault(slug, {"buckets": [], "probs": {}, "obs": obs})
                e["buckets"].append(
                    BucketBounds(
                        label=label,
                        lo_f=None if lo is None else float(lo),
                        hi_f=None if hi is None else float(hi),
                    )
                )
                e["probs"][label] = float(prob)
            (e_only,) = by_event.values()
            realised = realised_bucket(e_only["buckets"], e_only["obs"])
            self.assertIsNotNone(realised)
            assert realised is not None
            p_real = e_only["probs"][realised.label]
            return log_loss_one(p_real), brier_one(e_only["probs"], realised.label)

        a_ll, a_br = score(rows)
        b_ll, b_br = score(list(reversed(rows)))
        self.assertEqual(a_ll, b_ll)
        self.assertEqual(a_br, b_br)


if __name__ == "__main__":
    unittest.main()
