"""Unit tests for ``polymarket_weather.strategy.sizing``.

Pure-Python, no DB. Run with::

    .\\.venv\\Scripts\\python.exe -m pytest tests/test_sizing.py -v

or:

    .\\.venv\\Scripts\\python.exe -m unittest tests.test_sizing
"""

from __future__ import annotations

import unittest

from polymarket_weather.strategy.edge import Action, Edge, Side
from polymarket_weather.strategy.sizing import (
    CapsConfig,
    CapsState,
    _exposure_per_share,
    size_edge,
)


def _caps(
    *,
    bankroll: float = 500.0,
    bucket: float = 5.0,
    event: float = 20.0,
    day: float = 100.0,
    portfolio: float = 500.0,
    kelly: float = 0.25,
    min_edge: float = 0.02,
    min_shares: int = 5,
) -> CapsConfig:
    return CapsConfig(
        bankroll_usd=bankroll,
        per_bucket_usd=bucket,
        per_event_usd=event,
        per_day_usd=day,
        per_portfolio_usd=portfolio,
        kelly_fraction=kelly,
        min_edge_per_dollar=min_edge,
        min_order_shares=min_shares,
    )


def _taker_buy(price: float, p_model: float, fee: float = 0.0175) -> Edge:
    """Construct a taker-buy YES edge consistent with edge.py semantics.

    EV per share = (p_model - price) - fee_per_share (buy YES at ``price``
    with model probability ``p_model`` of winning).
    """
    gross = p_model - price
    ev = gross - fee
    return Edge(
        side=Side.YES,
        action=Action.TAKER_BUY,
        price=price,
        p_model=p_model,
        ev_per_share=ev,
        edge_per_share=gross,
        fee_per_share=fee,
    )


def _taker_sell(price: float, p_model: float, fee: float = 0.0175) -> Edge:
    """Sell YES at ``price`` ↔ buy NO at ``1 - price``.

    EV per share for the seller = (1 - p_model) - (1 - price) - fee.
    """
    gross = (1.0 - p_model) - (1.0 - price)
    ev = gross - fee
    return Edge(
        side=Side.YES,
        action=Action.TAKER_SELL,
        price=price,
        p_model=p_model,
        ev_per_share=ev,
        edge_per_share=gross,
        fee_per_share=fee,
    )


class ExposurePerShareTest(unittest.TestCase):
    def test_buy_exposure_is_price(self) -> None:
        e = _taker_buy(price=0.30, p_model=0.50)
        self.assertAlmostEqual(_exposure_per_share(e), 0.30)

    def test_sell_exposure_is_one_minus_price(self) -> None:
        e = _taker_sell(price=0.05, p_model=0.01)
        self.assertAlmostEqual(_exposure_per_share(e), 0.95)

    def test_sell_at_high_yes_price(self) -> None:
        e = _taker_sell(price=0.95, p_model=0.40)
        self.assertAlmostEqual(_exposure_per_share(e), 0.05)


class CapAccountingTest(unittest.TestCase):
    """The whole point of the fix: caps interpret as max-loss-if-wrong dollars."""

    def test_buy_per_bucket_cap_respected(self) -> None:
        # Strong edge at low price: model says 0.50, market is 0.10.
        e = _taker_buy(price=0.10, p_model=0.50)
        sized = size_edge(e, caps=_caps(bucket=5.0))
        self.assertIsNotNone(sized)
        # Loss-if-wrong = shares * price <= bucket cap.
        self.assertLessEqual(sized.shares * e.price, 5.0 + 1e-9)
        self.assertEqual(sized.binding_cap, "per_bucket")

    def test_sell_per_bucket_cap_respected_mid_yes_price(self) -> None:
        # Pre-fix bug: any sell at low yes_price would shares=cap/price,
        # producing huge real exposure. With ``per_bucket_usd=5`` and a
        # mid-price (0.30), exposure_per_share = 0.70, so shares <= 5/0.70.
        e = _taker_sell(price=0.30, p_model=0.05)
        sized = size_edge(e, caps=_caps(bucket=5.0))
        self.assertIsNotNone(sized)
        true_exposure = sized.shares * (1.0 - e.price)
        self.assertLessEqual(true_exposure, 5.0 + 1e-9)
        self.assertEqual(sized.binding_cap, "per_bucket")

    def test_sell_per_bucket_cap_respected_high_yes_price(self) -> None:
        # Sell at price=0.95 ↔ buy NO at 0.05. A $5 cap should let us buy
        # ~100 NO shares (5 / 0.05).
        e = _taker_sell(price=0.95, p_model=0.40)
        sized = size_edge(e, caps=_caps(bucket=5.0))
        self.assertIsNotNone(sized)
        true_exposure = sized.shares * (1.0 - e.price)
        self.assertLessEqual(true_exposure, 5.0 + 1e-9)
        # And we should size meaningfully (not be sub-min): roughly 100.
        self.assertGreaterEqual(sized.shares, 50)

    def test_per_event_cap_caps_below_per_bucket(self) -> None:
        e = _taker_buy(price=0.10, p_model=0.50)
        # Event cap of $3 with $5 bucket cap: per_event binds.
        sized = size_edge(e, caps=_caps(bucket=5.0, event=3.0))
        self.assertIsNotNone(sized)
        self.assertLessEqual(sized.shares * e.price, 3.0 + 1e-9)
        self.assertEqual(sized.binding_cap, "per_event")

    def test_state_used_subtracted(self) -> None:
        e = _taker_buy(price=0.10, p_model=0.50)
        # Already used $4 of a $5 bucket cap → only $1 left.
        sized = size_edge(
            e, caps=_caps(bucket=5.0), state=CapsState(used_per_bucket=4.0),
        )
        self.assertIsNotNone(sized)
        self.assertLessEqual(sized.shares * e.price, 1.0 + 1e-9)


class FilterTest(unittest.TestCase):
    def test_zero_or_negative_ev_returns_none(self) -> None:
        e = Edge(
            side=Side.YES, action=Action.TAKER_BUY, price=0.5, p_model=0.4,
            ev_per_share=-0.05, edge_per_share=-0.10, fee_per_share=0.05,
        )
        self.assertIsNone(size_edge(e, caps=_caps()))

    def test_below_min_edge_per_dollar(self) -> None:
        # Tiny edge: 0.001 per share at $0.10 price is 1% per dollar at risk,
        # below the default 2% threshold.
        e = Edge(
            side=Side.YES, action=Action.TAKER_BUY, price=0.10, p_model=0.118,
            ev_per_share=0.001, edge_per_share=0.018, fee_per_share=0.0175,
        )
        self.assertIsNone(size_edge(e, caps=_caps()))

    def test_min_shares_filter(self) -> None:
        # Strong edge but bucket cap so tight that shares < 5.
        e = _taker_buy(price=0.50, p_model=0.99)
        self.assertIsNone(size_edge(e, caps=_caps(bucket=1.0, min_shares=5)))


class RegressionTest(unittest.TestCase):
    """Specific bad fills from reports/m1_run.json — they should now size sanely."""

    def test_regression_5000_share_cold_tail_sell_now_rejected(self) -> None:
        # Pre-fix this returned shares=4999 with notional reported as $5
        # but true exposure $4994. Post-fix the trade is correctly rejected
        # entirely: at yes_price=0.001 the cash proceeds (≈ EV ceiling) are
        # too small relative to the ``1 - price`` exposure for the
        # ``min_edge_per_dollar`` floor of 2% to pass.
        e = _taker_sell(price=0.001, p_model=1e-9)
        sized = size_edge(e, caps=_caps(bucket=5.0))
        self.assertIsNone(sized)

    def test_realistic_sell_fills_within_cap(self) -> None:
        # And a realistic sell (e.g. NO buy at price 0.20 = sell YES at 0.80)
        # with a real edge still fills under the cap.
        e = _taker_sell(price=0.80, p_model=0.50)
        sized = size_edge(e, caps=_caps(bucket=5.0))
        self.assertIsNotNone(sized)
        self.assertLessEqual(sized.shares * (1.0 - e.price), 5.0 + 1e-9)


if __name__ == "__main__":
    unittest.main()
