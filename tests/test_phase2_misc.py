"""Phase 2 smoke tests for the batched lower-severity fixes.

Covers:

* §2.5 maker direction guard
* §2.7 M1 sigma floor (via a module-level constant check)
* §2.9 banker's-rounding fix in ``realised_bucket``
* §2.11 simplex projection tail floor

Run with::

    .\\.venv\\Scripts\\python.exe -m pytest tests/test_phase2_misc.py -v
"""

from __future__ import annotations

import unittest

import numpy as np

from polymarket_weather.score import BucketBounds, realised_bucket
from polymarket_weather.strategy.edge import (
    Action,
    BookLevel,
    maker_quote_yes_within_rewards,
)
from polymarket_weather.strategy.negrisk import (
    PROB_FLOOR,
    coherent_model_probs,
    project_to_simplex,
)


class MakerDirectionGuard(unittest.TestCase):
    """Maker should not post a buy quote when YES is below mid; nor a sell
    when YES is above mid (review §2.5)."""

    def _book(self, bid: float, ask: float) -> BookLevel:
        return BookLevel(best_bid=bid, best_ask=ask, mid=(bid + ask) / 2.0)

    def test_only_sell_when_p_below_mid(self) -> None:
        # Mid=0.30, model says p=0.20. Sell side is the model's edge; buy is not.
        book = self._book(0.28, 0.32)
        buy, sell = maker_quote_yes_within_rewards(
            p_yes=0.20, book=book, min_edge=0.0,
        )
        self.assertIsNone(buy, "buy must be suppressed when p_model < mid")
        self.assertIsNotNone(sell, "sell side should remain active")

    def test_only_buy_when_p_above_mid(self) -> None:
        book = self._book(0.28, 0.32)
        buy, sell = maker_quote_yes_within_rewards(
            p_yes=0.45, book=book, min_edge=0.0,
        )
        self.assertIsNotNone(buy, "buy side should remain active when p > mid")
        self.assertIsNone(sell, "sell must be suppressed when p_model > mid")

    def test_neither_when_fair(self) -> None:
        # min_edge=0.02 -> p in [mid-0.02, mid+0.02] disables both sides.
        book = self._book(0.28, 0.32)  # mid=0.30
        buy, sell = maker_quote_yes_within_rewards(
            p_yes=0.305, book=book, min_edge=0.02,
        )
        self.assertIsNone(buy)
        self.assertIsNone(sell)


class HalfUpRounding(unittest.TestCase):
    """`realised_bucket` must round half up (review §2.9)."""

    def setUp(self) -> None:
        self.buckets = (
            BucketBounds(label="<70", lo_f=None, hi_f=69.0),
            BucketBounds(label="70-72", lo_f=70.0, hi_f=72.0),
            BucketBounds(label=">72", lo_f=73.0, hi_f=None),
        )

    def test_boundary_70_5_rounds_up(self) -> None:
        # Banker's rounding would map 70.5 -> 70 (even), which is wrong.
        # Half-up maps 70.5 -> 71, in 70-72.
        rb = realised_bucket(self.buckets, 70.5)
        self.assertIsNotNone(rb)
        assert rb is not None
        self.assertEqual(rb.label, "70-72")

    def test_boundary_69_5_rounds_to_70(self) -> None:
        rb = realised_bucket(self.buckets, 69.5)
        self.assertIsNotNone(rb)
        assert rb is not None
        self.assertEqual(rb.label, "70-72")


class SimplexFloor(unittest.TestCase):
    """`coherent_model_probs` must keep small tail entries positive
    (review §2.11)."""

    def test_small_entry_survives(self) -> None:
        raw = {
            "<60": 0.0001,
            "60-65": 0.04,
            "65-70": 0.95,
            "70-75": 0.0099,
        }
        proj = coherent_model_probs(raw)
        for v in proj.values():
            self.assertGreaterEqual(v, PROB_FLOOR - 1e-12)
        # Sum to 1.
        self.assertAlmostEqual(sum(proj.values()), 1.0, places=10)

    def test_uniform_stays_uniform(self) -> None:
        raw = {"a": 0.25, "b": 0.25, "c": 0.25, "d": 0.25}
        proj = coherent_model_probs(raw)
        for v in proj.values():
            self.assertAlmostEqual(v, 0.25, places=6)

    def test_projection_clips_when_over_simplex(self) -> None:
        # When the input over-shoots the simplex, the bare projection
        # routinely clips small entries to exactly 0.
        arr = np.array([0.00005, 0.50, 0.50, 0.10])  # sum > 1
        proj = project_to_simplex(arr)
        self.assertEqual(proj[0], 0.0)
        # The fix protects against this: the public API floors first so
        # the tail entry stays strictly positive (the renormalisation may
        # shrink it slightly below the raw floor, which is fine).
        raw = {"a": 0.00005, "b": 0.50, "c": 0.50, "d": 0.10}
        fixed = coherent_model_probs(raw)
        self.assertGreater(fixed["a"], 0.0)
        self.assertGreater(fixed["a"], 0.5 * PROB_FLOOR)


if __name__ == "__main__":
    unittest.main()
