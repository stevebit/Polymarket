"""Signed automation layer.

This is the **only** package in ``polymarket_weather`` that imports
``polymarket_manual.clients`` and signs orders. Every entrypoint here is
gated by env vars (``WEATHER_AUTOMATION_ENABLED``, ``WEATHER_KILL_SWITCH``)
and hard cap checks.

Nothing under this package is imported by default by other modules in the
weather package; importing ``polymarket_weather`` must remain pure
read-only. Only the orchestrator and its CLI ever cross this boundary.
"""
