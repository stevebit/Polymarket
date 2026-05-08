"""Trading strategy layer.

Pure-functional modules that translate calibrated bucket probabilities and
order-book snapshots into actionable, fee-aware, neg-risk-coherent,
fractional-Kelly-sized recommendations.

Nothing in here signs or places orders; that lives in
``polymarket_weather.automation``.
"""
