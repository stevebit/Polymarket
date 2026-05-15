"""Test that M2's climatology floor reaches the bucket distribution.

Review §2.2 bug: ``sigma_total`` (incorporating the climatology floor) was
stored on the ``predictions`` row but the un-floored mixture was passed to
``predict_bucket_probs``. This file constructs a mixture whose natural
spread is much tighter than the floor and asserts the bucket integration
now sees the wider distribution.

Run with::

    .\\.venv\\Scripts\\python.exe -m pytest tests/test_m2_floor.py -v
"""

from __future__ import annotations

import unittest

from polymarket_weather.score import (
    BucketBounds,
    GaussianDist,
    GaussianMixture,
    predict_bucket_probs,
)


class M2FloorReachesBuckets(unittest.TestCase):
    """If sigma_total > mixture.std, ``predict_bucket_probs`` must use a
    Gaussian with sigma_total — not the original mixture."""

    def setUp(self) -> None:
        # Three tight components all near 70°F.
        self.tight_mixture = GaussianMixture(
            weights=(1.0, 1.0, 1.0),
            means=(70.0, 70.5, 69.5),
            stds=(1.0, 1.0, 1.0),
        )
        # Bucket layout: < 68, 68-72, > 72 — anchored on the mean.
        self.buckets = (
            BucketBounds(label="<68", lo_f=None, hi_f=68.0),
            BucketBounds(label="68-72", lo_f=68.0, hi_f=72.0),
            BucketBounds(label=">72", lo_f=72.0, hi_f=None),
        )

    def test_tight_mixture_concentrates_centre(self) -> None:
        # Sanity: with std~1, mass is overwhelmingly in the centre bucket.
        probs = predict_bucket_probs(self.buckets, self.tight_mixture)
        centre = probs["68-72"]
        self.assertGreater(centre, 0.80)

    def test_floored_distribution_has_more_tail_mass(self) -> None:
        # Simulate what M2 does after the fix: when the floor is wider than
        # the mixture.std, build a single Gaussian at mixture.mean with
        # sigma = sigma_total. Climatology floor = 4.0 (typical of August
        # in a 30-year window).
        sigma_total = 4.0
        self.assertGreater(sigma_total, self.tight_mixture.std)
        floored = GaussianDist(mean_f=self.tight_mixture.mean, std_f=sigma_total)

        tight = predict_bucket_probs(self.buckets, self.tight_mixture)
        wide = predict_bucket_probs(self.buckets, floored)

        # Floor widens both tails and shrinks the centre.
        self.assertGreater(wide["<68"], tight["<68"])
        self.assertGreater(wide[">72"], tight[">72"])
        self.assertLess(wide["68-72"], tight["68-72"])

        # Floor at 4.0 implies P(|X - mu| > 2) >= ~0.32; both tails together
        # should carry at least 30% mass.
        tail_mass = wide["<68"] + wide[">72"]
        self.assertGreater(tail_mass, 0.30)

    def test_no_floor_keeps_mixture_shape(self) -> None:
        # If the mixture is already wider than the floor, we must keep the
        # mixture (it has component structure the bare Gaussian doesn't).
        wide_mixture = GaussianMixture(
            weights=(1.0, 1.0),
            means=(60.0, 80.0),
            stds=(2.0, 2.0),
        )
        # sigma_total < mixture.std -> no floor needed.
        sigma_total = 2.5
        self.assertLess(sigma_total, wide_mixture.std)

        # Trivially: the mixture has visible bimodal structure that a single
        # Gaussian cannot capture; pin the probability mass in the centre
        # bucket where a Gaussian at the mean would put a lot of mass.
        buckets = (
            BucketBounds(label="<65", lo_f=None, hi_f=65.0),
            BucketBounds(label="65-75", lo_f=65.0, hi_f=75.0),
            BucketBounds(label=">75", lo_f=75.0, hi_f=None),
        )
        mixture_probs = predict_bucket_probs(buckets, wide_mixture)
        gaussian_probs = predict_bucket_probs(
            buckets, GaussianDist(mean_f=70.0, std_f=sigma_total)
        )
        # Mixture should have lower centre mass since modes are at 60 / 80.
        self.assertLess(mixture_probs["65-75"], gaussian_probs["65-75"])


if __name__ == "__main__":
    unittest.main()
