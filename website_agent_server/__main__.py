from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from .config import settings


DEFAULT_HOST = settings.host


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="website-agent-server",
        description="Run the server-side browser proxy.",
    )
    parser.add_argument("--host", default=None, help="Server bind host.")
    parser.add_argument(
        "--allow-lan",
        action="store_true",
        help="Allow LAN clients by binding to 0.0.0.0 unless --host is set.",
    )
    parser.add_argument("--port", type=int, default=settings.port, help="Server port.")
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run Chromium with a visible browser window.",
    )
    parser.add_argument(
        "--ignore-https-errors",
        action="store_true",
        help="Ignore remote TLS certificate errors.",
    )
    parser.add_argument(
        "--allow-private-hosts",
        action="store_true",
        help="Allow private, local, and reserved network targets.",
    )
    parser.add_argument(
        "--session-ttl-seconds",
        type=int,
        default=settings.session_ttl_seconds,
        help="Idle session lifetime in seconds.",
    )
    parser.add_argument(
        "--navigation-timeout-ms",
        type=int,
        default=settings.navigation_timeout_ms,
        help="Navigation timeout in milliseconds.",
    )
    parser.add_argument(
        "--frame-interval-seconds",
        type=float,
        default=settings.frame_interval_seconds,
        help="Screenshot streaming interval in seconds.",
    )
    parser.add_argument(
        "--screenshot-quality",
        type=int,
        default=settings.screenshot_quality,
        help="JPEG screenshot quality from 1 to 100.",
    )
    parser.add_argument(
        "--min-viewport-width",
        type=int,
        default=settings.min_viewport_width,
        help="Minimum remote viewport width.",
    )
    parser.add_argument(
        "--min-viewport-height",
        type=int,
        default=settings.min_viewport_height,
        help="Minimum remote viewport height.",
    )
    parser.add_argument(
        "--max-viewport-width",
        type=int,
        default=settings.max_viewport_width,
        help="Maximum remote viewport width.",
    )
    parser.add_argument(
        "--max-viewport-height",
        type=int,
        default=settings.max_viewport_height,
        help="Maximum remote viewport height.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=settings.data_dir,
        help="Runtime downloads and temporary uploads directory.",
    )
    parser.add_argument(
        "--pin",
        default=None,
        help="Require this PIN before clients can use the proxy.",
    )
    return parser


def apply_args(args: argparse.Namespace) -> None:
    settings.host = args.host or ("0.0.0.0" if args.allow_lan else DEFAULT_HOST)
    settings.port = args.port
    settings.headless = not args.headed
    settings.ignore_https_errors = args.ignore_https_errors
    settings.allow_private_hosts = args.allow_private_hosts
    settings.session_ttl_seconds = args.session_ttl_seconds
    settings.navigation_timeout_ms = args.navigation_timeout_ms
    settings.frame_interval_seconds = args.frame_interval_seconds
    settings.screenshot_quality = max(1, min(100, args.screenshot_quality))
    settings.min_viewport_width = args.min_viewport_width
    settings.min_viewport_height = args.min_viewport_height
    settings.max_viewport_width = args.max_viewport_width
    settings.max_viewport_height = args.max_viewport_height
    settings.data_dir = args.data_dir.resolve()
    settings.pin = args.pin


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    apply_args(args)
    uvicorn.run(
        "website_agent_server.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
