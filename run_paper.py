#!/usr/bin/env python3
"""CLI entrypoint for the Polymarket PAPER trading engine.

Runs the real strategy against real order books with virtual money. Reads
``env.paper.txt``, writes to ``data/paper/``, and never loads a wallet key.

Usage:
    python run_paper.py
"""
from __future__ import annotations

import asyncio
import sys

from polybot.paper.main import main

if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
