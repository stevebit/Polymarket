"""CLI date keyword parsing (today / yesterday in UTC)."""

from __future__ import annotations

import datetime as dt
import unittest

from polymarket_weather.cli._common import parse_cli_date


class ParseCliDateTest(unittest.TestCase):
    def test_iso_still_works(self) -> None:
        self.assertEqual(parse_cli_date("2026-05-12"), dt.date(2026, 5, 12))
        self.assertEqual(parse_cli_date(" 2026-05-12 "), dt.date(2026, 5, 12))

    def test_today_yesterday_keywords_match_utc_calendar(self) -> None:
        utc_today = dt.datetime.now(dt.timezone.utc).date()
        self.assertEqual(parse_cli_date("today"), utc_today)
        self.assertEqual(parse_cli_date("TODAY"), utc_today)
        self.assertEqual(parse_cli_date("yesterday"), utc_today - dt.timedelta(days=1))


if __name__ == "__main__":
    unittest.main()
