"""Polybot dashboard — a 24/7 monitoring and control plane for the trading bot.

Runs as its own systemd service on the VPS, independently of the trading
engines. It never places orders: it reads each engine's telemetry database,
synchronises live account state from Polymarket read-only, and drives the
engines through systemd.
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
