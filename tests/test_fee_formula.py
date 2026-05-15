"""Unit tests for the Polymarket CLOB v2 symmetric fee formula.

The fee formula is::

    fee_per_share = rate * p * (1 - p)

where ``p`` is the trade price in ``[0, 1]``. This file pins concrete
endpoints so the bug-fix from review §2.1 cannot silently regress.

Run with::

    .\\.venv\\Scripts\\python.exe -m pytest tests/test_fee_formula.py -v
"""

from __future__ import annotations

import unittest

from polymarket_weather.strategy.edge import (
    FeeSchedule,
    fee_per_share,
    maker_quote_yes_edge,
    taker_buy_no_edge,
    taker_buy_yes_edge,
    taker_sell_yes_edge,
)
from polymarket_weather.strategy.edge import Action


WEATHER_TAKER = 0.05
MAKER = 0.0


class FormulaShape(unittest.TestCase):
    """Pin the symmetric fee curve: rate * p * (1 - p)."""

    def test_endpoints_zero(self) -> None:
        self.assertEqual(fee_per_share(0.0, WEATHER_TAKER), 0.0)
        self.assertEqual(fee_per_share(1.0, WEATHER_TAKER), 0.0)

    def test_centre_is_peak(self) -> None:
        peak = fee_per_share(0.5, WEATHER_TAKER)
        self.assertAlmostEqual(peak, 0.0125, places=6)
        # Off-centre is strictly smaller.
        self.assertLess(fee_per_share(0.4, WEATHER_TAKER), peak)
        self.assertLess(fee_per_share(0.6, WEATHER_TAKER), peak)

    def test_symmetry(self) -> None:
        for p in (0.10, 0.20, 0.30, 0.40):
            self.assertAlmostEqual(
                fee_per_share(p, WEATHER_TAKER),
                fee_per_share(1.0 - p, WEATHER_TAKER),
                places=12,
            )

    def test_concrete_values(self) -> None:
        # These are the values shown in docs/REVIEW_2026_05_12.md §2.1.
        cases = [
            (0.10, 0.0045),
            (0.30, 0.0105),
            (0.50, 0.0125),
            (0.70, 0.0105),
            (0.90, 0.0045),
            (0.95, 0.002375),
        ]
        for price, expected in cases:
            self.assertAlmostEqual(
                fee_per_share(price, WEATHER_TAKER), expected, places=6,
                msg=f"fee at price={price}",
            )

    def test_zero_rate_short_circuits(self) -> None:
        # Maker fee defaults to 0 -> no fee regardless of price.
        self.assertEqual(fee_per_share(0.5, 0.0), 0.0)


class NotLinear(unittest.TestCase):
    """Guard against the old ``fee = rate * price`` linear approximation."""

    def test_high_price_not_linear(self) -> None:
        # The old code returned 0.0475 at price=0.95; the right answer is
        # 0.002375. The ratio is 20x.
        linear = 0.05 * 0.95
        actual = fee_per_share(0.95, WEATHER_TAKER)
        self.assertLess(actual * 5, linear)  # at least 5x smaller


class EdgeUsesFormula(unittest.TestCase):
    """The four taker/maker edge constructors must consume the new formula."""

    def test_taker_buy_yes_fee(self) -> None:
        edge = taker_buy_yes_edge(p_yes=0.6, ask_yes=0.5)
        self.assertIsNotNone(edge)
        self.assertAlmostEqual(edge.fee_per_share, 0.0125, places=6)
        self.assertAlmostEqual(edge.edge_per_share, 0.6 - 0.5, places=12)
        self.assertAlmostEqual(edge.ev_per_share, 0.1 - 0.0125, places=6)

    def test_taker_sell_yes_fee_at_high_price(self) -> None:
        edge = taker_sell_yes_edge(p_yes=0.3, bid_yes=0.9)
        self.assertIsNotNone(edge)
        self.assertAlmostEqual(edge.fee_per_share, 0.0045, places=6)

    def test_taker_buy_no_fee(self) -> None:
        edge = taker_buy_no_edge(p_yes=0.4, ask_no=0.7)
        self.assertIsNotNone(edge)
        self.assertAlmostEqual(edge.fee_per_share, 0.0105, places=6)

    def test_maker_buy_no_fee_default(self) -> None:
        # Makers don't pay fees with the default schedule.
        edge = maker_quote_yes_edge(
            p_yes=0.5, quote_price=0.45, side=Action.MAKER_BUY,
            fill_prob=0.5,
        )
        self.assertIsNotNone(edge)
        self.assertEqual(edge.fee_per_share, 0.0)

    def test_custom_rate(self) -> None:
        # Sports markets are 0.03.
        sports = FeeSchedule(taker_fee=0.03)
        edge = taker_buy_yes_edge(p_yes=0.6, ask_yes=0.5, fees=sports)
        self.assertIsNotNone(edge)
        self.assertAlmostEqual(edge.fee_per_share, 0.03 * 0.5 * 0.5, places=6)


if __name__ == "__main__":
    unittest.main()
