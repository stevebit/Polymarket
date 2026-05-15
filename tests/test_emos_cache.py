"""LRU cache for ``latest_fit_for`` (review §5.8).

Confirms the process-local cache:

1. Avoids a second DB round-trip on a repeat lookup for the same key.
2. Differentiates live (``as_of=None``) from historical (``as_of=<dt>``).
3. Caches "no row" outcomes so the predict path doesn't hammer the DB.
4. Gets cleared by ``_cache_latest_fit_invalidate`` (called from
   ``fit_postprocess`` so a refit invalidates the in-memory copy).

Run with::

    .\\.venv\\Scripts\\python.exe -m pytest tests/test_emos_cache.py -v
"""

from __future__ import annotations

import datetime as dt
import unittest
from unittest.mock import MagicMock

from polymarket_weather.models import postprocess as pp


class EmosCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        pp._cache_latest_fit_invalidate()

    def tearDown(self) -> None:
        pp._cache_latest_fit_invalidate()

    def _cur_returning(self, row):
        cur = MagicMock()
        cur.fetchone.return_value = row
        return cur

    def test_repeat_hits_cache(self) -> None:
        cur = self._cur_returning((0.5, 1.0, 1.5, None, 100))
        first = pp.latest_fit_for(
            cur, station_id=1, source="openmeteo:gfs_seamless", lead_day=0
        )
        self.assertIsNotNone(first)
        self.assertEqual(cur.execute.call_count, 1)
        second = pp.latest_fit_for(
            cur, station_id=1, source="openmeteo:gfs_seamless", lead_day=0
        )
        self.assertIs(second, first)
        self.assertEqual(cur.execute.call_count, 1)

    def test_none_is_cached(self) -> None:
        cur = self._cur_returning(None)
        a = pp.latest_fit_for(cur, station_id=2, source="openmeteo:icon_seamless", lead_day=1)
        b = pp.latest_fit_for(cur, station_id=2, source="openmeteo:icon_seamless", lead_day=1)
        self.assertIsNone(a)
        self.assertIsNone(b)
        self.assertEqual(cur.execute.call_count, 1)

    def test_as_of_keys_are_distinct(self) -> None:
        cur = self._cur_returning((0.2, 0.9, 1.1, None, 50))
        as_of = dt.datetime(2026, 1, 1, 12, 0, 0)
        pp.latest_fit_for(
            cur, station_id=3, source="openmeteo:hrrr_conus", lead_day=0, as_of=None
        )
        pp.latest_fit_for(
            cur, station_id=3, source="openmeteo:hrrr_conus", lead_day=0, as_of=as_of
        )
        self.assertEqual(cur.execute.call_count, 2)

    def test_invalidate_clears_cache(self) -> None:
        cur = self._cur_returning((0.3, 1.1, 1.2, None, 40))
        pp.latest_fit_for(cur, station_id=4, source="openmeteo:bestmatch", lead_day=0)
        self.assertEqual(cur.execute.call_count, 1)
        pp._cache_latest_fit_invalidate()
        pp.latest_fit_for(cur, station_id=4, source="openmeteo:bestmatch", lead_day=0)
        self.assertEqual(cur.execute.call_count, 2)


if __name__ == "__main__":
    unittest.main()
