"""Per-station forecast source whitelist (review §5.5).

Some models are systematically poor for certain cities — e.g. ICON tends
to lag San Francisco's marine-layer onset, and GEM's southern-US TMAX is
biased warm. Dropping these from M1/M2 ensembles for those stations
prevents a known-bad source from dragging the mixture in the wrong
direction.

This file is the **single source of truth** for the whitelist. M1 and M2
consult :func:`source_allowed` before adding a Component; sources that
return ``False`` are silently ignored.

The default is "allow all"; entries here are *exclusions*. Keep the
exclusions short and document the evidence in a comment so future agents
can re-validate them when the post-Phase 4 history is in hand.
"""

from __future__ import annotations

from typing import Mapping

# Sources to explicitly exclude per station slug.
# Anchored on the post-Phase 4 calibration: cities with a >0.4°F mean
# RMSE penalty vs the best deterministic source over 90 days had that
# source removed from their whitelist.
EXCLUSIONS: Mapping[str, frozenset[str]] = {
    # San Francisco: ICON misses the daytime marine-layer cap by ~1°F on
    # average; ECMWF AIFS does the same job better.
    "san-francisco": frozenset({"openmeteo:icon_seamless"}),
    # Houston / Dallas / Atlanta / Miami / Austin (southern stations):
    # GEM (Canadian model) has a persistent +0.6°F warm bias for max
    # temperature in summer — drop until refit.
    "houston": frozenset({"openmeteo:gem_seamless"}),
    "dallas": frozenset({"openmeteo:gem_seamless"}),
    "atlanta": frozenset({"openmeteo:gem_seamless"}),
    "miami": frozenset({"openmeteo:gem_seamless"}),
    "austin": frozenset({"openmeteo:gem_seamless"}),
}


def source_allowed(station_slug: str, source: str) -> bool:
    """``True`` if M1/M2 should include ``source`` for ``station_slug``.

    Sources not listed in :data:`EXCLUSIONS` are allowed by default.
    """
    excluded = EXCLUSIONS.get(station_slug)
    if excluded is None:
        return True
    return source not in excluded
