"""Test that ``predict --as-of`` cannot use data inserted after as_of.

The implementation guarantee (review §2.4): every internal SELECT in the
prediction pipeline that touches ``forecasts``, ``observations``, or
``postprocess_coefs`` includes a ``run_time / ingested_at / fit_at <=
as_of`` clause when ``as_of`` is set.

This test runs the pipeline functions against a tiny fake cursor that
captures SQL + bound params, then asserts the captured params contain the
as_of value and the SQL contains the cutoff clause.

Run with::

    .\\.venv\\Scripts\\python.exe -m pytest tests/test_no_lookahead.py -v
"""

from __future__ import annotations

import datetime as dt
import re
import unittest
from typing import Any

from polymarket_weather.models import baseline, postprocess


class FakeCursor:
    """Captures every execute() call so tests can inspect SQL + params."""

    def __init__(self, rows: list[Any] | None = None) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.rows = rows or []

    def execute(self, sql: str, params: tuple) -> None:
        self.calls.append((sql, params))

    def fetchone(self) -> Any:
        return self.rows.pop(0) if self.rows else None

    def fetchall(self) -> list[Any]:
        out, self.rows = self.rows, []
        return out


class AsOfPassedToForecastSelect(unittest.TestCase):
    AS_OF = dt.datetime(2026, 4, 1, 12, 0, tzinfo=dt.timezone.utc)

    def test_latest_forecasts_for_uses_run_time_cutoff(self) -> None:
        cur = FakeCursor(rows=[])
        baseline._latest_forecasts_for(
            cur, station_id=1, target_date=dt.date(2026, 4, 2), as_of=self.AS_OF
        )
        self.assertEqual(len(cur.calls), 1)
        sql, params = cur.calls[0]
        self.assertIn("run_time", sql)
        self.assertIn("<= %s", sql)
        # The as_of value must be one of the bound params.
        self.assertIn(self.AS_OF, params)

    def test_latest_forecasts_for_no_as_of_omits_cutoff(self) -> None:
        cur = FakeCursor(rows=[])
        baseline._latest_forecasts_for(
            cur, station_id=1, target_date=dt.date(2026, 4, 2)
        )
        sql, params = cur.calls[0]
        # No as_of -> the cutoff clause is absent.
        self.assertNotIn("run_time   <= %s", sql)
        self.assertNotIn(
            dt.datetime(2026, 4, 1, 12, 0, tzinfo=dt.timezone.utc), params
        )

    def test_source_mae_uses_run_time_and_ingested_at_cutoff(self) -> None:
        cur = FakeCursor(rows=[(None, 0)])
        baseline._source_mae(
            cur, station_id=1, source="openmeteo:gfs_seamless",
            lookback_days=30, end_date=dt.date(2026, 3, 31),
            as_of=self.AS_OF,
        )
        sql, params = cur.calls[0]
        # Forecasts cycle cutoff:
        self.assertRegex(sql, r"run_time\s*<=\s*%s")
        # Observations ingested-at cutoff:
        self.assertRegex(sql, r"ingested_at\s*<=\s*%s")
        self.assertEqual(params.count(self.AS_OF), 2)

    def test_station_residual_std_uses_cutoffs(self) -> None:
        cur = FakeCursor(rows=[(None,)])
        baseline._station_residual_std(
            cur, station_id=1, end_date=dt.date(2026, 3, 31),
            lookback_days=90, as_of=self.AS_OF,
        )
        sql, params = cur.calls[0]
        self.assertRegex(sql, r"run_time\s*<=\s*%s")
        self.assertRegex(sql, r"ingested_at\s*<=\s*%s")
        self.assertEqual(params.count(self.AS_OF), 2)

    def test_latest_fit_for_uses_fit_at_cutoff(self) -> None:
        cur = FakeCursor(rows=[])
        postprocess.latest_fit_for(
            cur, station_id=1, source="openmeteo:gfs_seamless", lead_day=2,
            as_of=self.AS_OF,
        )
        sql, params = cur.calls[0]
        self.assertRegex(sql, r"fit_at\s*<=\s*%s")
        self.assertIn(self.AS_OF, params)


class RunTimeAnchored(unittest.TestCase):
    """When as_of is set, the persisted run_time must equal as_of."""

    def test_run_predictions_uses_as_of_for_run_time(self) -> None:
        # We don't have a DB; instead test that ``predict_m0_for`` is called
        # with run_time = as_of by monkey-patching the inner function.
        # The cleaner check is via the public CLI logic: ``run_time =
        # as_of if as_of is not None else now()``. Verify the conditional
        # by introspecting the source — keeps the test hermetic.
        import inspect

        from polymarket_weather.models.baseline import run_predictions
        from polymarket_weather.models.m2_postprocessed_ensemble import (
            run_m2_predictions,
        )

        for fn in (run_predictions, run_m2_predictions):
            src = inspect.getsource(fn)
            self.assertIn("as_of if as_of is not None else", src,
                          f"{fn.__name__} must anchor run_time to as_of")


if __name__ == "__main__":
    unittest.main()
