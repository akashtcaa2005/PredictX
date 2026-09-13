#!/usr/bin/env python3
"""CLI entrypoint for the Polybot dashboard service.

Reads ``env.dashboard.txt``, serves the API + SPA, and streams live state over a
websocket. Runs independently of the trading engines: starting, stopping or
crashing an engine never takes the dashboard down.

Usage:
    python run_dashboard.py                  # serve
    python run_dashboard.py --check          # validate config and exit
    python run_dashboard.py --gen-secret     # print a session secret
"""
from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from dashboard.config import DashboardSettings, generate_secret
from dashboard.server import create_app
from polybot.config.settings import ConfigError


def _configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    # The live-account sync polls every few seconds; httpx logs one INFO line per
    # request, which would swamp the journal and the disk. Warnings still surface.
    for noisy in ("httpx", "httpcore", "websockets.client", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Polybot dashboard server")
    parser.add_argument("--check", action="store_true",
                        help="validate configuration and exit")
    parser.add_argument("--gen-secret", action="store_true",
                        help="print a fresh DASHBOARD_SECRET_KEY and exit")
    parser.add_argument("--host", default=None, help="override the bind address")
    parser.add_argument("--port", type=int, default=None, help="override the port")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    if args.gen_secret:
        print(generate_secret())
        return 0

    _configure_logging(args.log_level)
    try:
        settings = DashboardSettings.load()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port

    if args.check:
        print("configuration OK")
        print(f"  bind:        {settings.host}:{settings.port}")
        print(f"  tls:         {'yes' if settings.tls_enabled else 'no (terminate at nginx)'}")
        print(f"  data root:   {settings.data_root}")
        print(f"  paper db:    {settings.engine_db('paper')}")
        print(f"  live db:     {settings.engine_db('live')}")
        print(f"  live sync:   {'enabled' if settings.live_sync_enabled else 'disabled'}")
        print(f"  control:     {'enabled' if settings.control_enabled else 'disabled'}")
        return 0

    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=args.log_level,
        ssl_certfile=settings.tls_certfile if settings.tls_enabled else None,
        ssl_keyfile=settings.tls_keyfile if settings.tls_enabled else None,
        proxy_headers=settings.behind_proxy,
        forwarded_allow_ips="*" if settings.behind_proxy else None,
        access_log=False,          # the audit log is the record that matters
        timeout_keep_alive=30,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
