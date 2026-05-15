"""Gamma terminal snapshot timestamp parsing."""

from __future__ import annotations

import datetime as dt
import unittest

from polymarket_weather.markets import _parse_gamma_timestamp


class GammaTsTest(unittest.TestCase):
    def test_closed_time_plus00(self) -> None:
        t = _parse_gamma_timestamp("2026-05-10 19:10:19+00")
        self.assertIsNotNone(t)
        assert t is not None
        self.assertEqual(t, dt.datetime(2026, 5, 10, 19, 10, 19, tzinfo=dt.timezone.utc))

    def test_none_empty(self) -> None:
        self.assertIsNone(_parse_gamma_timestamp(None))
        self.assertIsNone(_parse_gamma_timestamp(""))


if __name__ == "__main__":
    unittest.main()
