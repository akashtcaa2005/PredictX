#!/usr/bin/env python3
"""CLI entrypoint for the Polymarket live trading bot.

Usage:
    python run.py            # dry-run by default (ENABLE_LIVE_TRADING unset/false)
    ENABLE_LIVE_TRADING=true python run.py   # arm live trading (real funds)
"""
from __future__ import annotations

import asyncio
import sys

from polybot.main import main

if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
