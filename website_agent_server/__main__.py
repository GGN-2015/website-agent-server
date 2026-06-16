from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import threading
from pathlib import Path
from types import FrameType
from collections.abc import Callable, Generator

import uvicorn
from uvicorn.server import HANDLED_SIGNALS

from .config import settings
from .url_policy import HostAccessPolicy


class WebsiteAgentServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, request_stop: Callable[[], None]) -> None:
        super().__init__(config)
        self._request_stop = request_stop

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        self._request_stop()
        super().handle_exit(sig, frame)

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        original_handlers = {sig: signal.signal(sig, self.handle_exit) for sig in HANDLED_SIGNALS}
        try:
            yield
        finally:
            for sig, handler in original_handlers.items():
                signal.signal(sig, handler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="website-agent-server",
        description="Run the server-side browser proxy.",
    )
    parser.add_argument("--host", default=settings.host, help="Server bind host.")
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
        "--locale",
        default=settings.locale,
        help="Browser locale exposed to remote sites.",
    )
    parser.add_argument(
        "--timezone-id",
        default=settings.timezone_id,
        help="Browser timezone ID exposed to remote sites.",
    )
    parser.add_argument(
        "--accept-language",
        default=settings.accept_language,
        help="Accept-Language header sent by browser contexts.",
    )
    parser.add_argument(
        "--user-agent",
        default=settings.user_agent,
        help=(
            "Desktop browser User-Agent. By default the server derives a normal "
            "Chrome UA from the bundled Chromium version instead of exposing "
            "HeadlessChrome."
        ),
    )
    parser.add_argument(
        "--session-ttl-seconds",
        type=int,
        default=settings.session_ttl_seconds,
        help="Idle session lifetime in seconds.",
    )
    parser.add_argument(
        "--shutdown-timeout-seconds",
        type=float,
        default=settings.shutdown_timeout_seconds,
        help="Maximum graceful shutdown wait for active HTTP/WebSocket connections.",
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
        help="Screenshot quality from 1 to 100. Values below 100 use JPEG; 100 uses PNG.",
    )
    parser.add_argument(
        "--media-frame-interval-seconds",
        type=float,
        default=settings.media_frame_interval_seconds,
        help="Screenshot streaming interval while remote media is playing.",
    )
    parser.add_argument(
        "--media-screenshot-quality",
        type=int,
        default=settings.media_screenshot_quality,
        help="JPEG screenshot quality while remote media is playing. Ignored when screenshot quality is 100.",
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
    parser.add_argument(
        "--lock-url",
        default=None,
        help="Lock the UI to this initial URL and disable browser option controls.",
    )
    return parser


async def apply_args(args: argparse.Namespace) -> None:
    settings.host = args.host
    settings.port = args.port
    settings.headless = not args.headed
    settings.ignore_https_errors = args.ignore_https_errors
    settings.allow_private_hosts = args.allow_private_hosts
    settings.locale = args.locale
    settings.timezone_id = args.timezone_id
    settings.accept_language = args.accept_language
    settings.user_agent = args.user_agent
    settings.session_ttl_seconds = args.session_ttl_seconds
    settings.shutdown_timeout_seconds = max(0.1, args.shutdown_timeout_seconds)
    settings.navigation_timeout_ms = args.navigation_timeout_ms
    settings.frame_interval_seconds = args.frame_interval_seconds
    settings.screenshot_quality = max(1, min(100, args.screenshot_quality))
    settings.media_frame_interval_seconds = max(0.05, args.media_frame_interval_seconds)
    settings.media_screenshot_quality = max(1, min(99, args.media_screenshot_quality))
    settings.min_viewport_width = args.min_viewport_width
    settings.min_viewport_height = args.min_viewport_height
    settings.max_viewport_width = args.max_viewport_width
    settings.max_viewport_height = args.max_viewport_height
    settings.data_dir = args.data_dir.resolve()
    settings.pin = args.pin
    if args.lock_url:
        lock_url_policy = HostAccessPolicy(settings.allow_private_hosts)
        settings.lock_url = await lock_url_policy.ensure_navigation_url_allowed(
            args.lock_url,
            verify_https=not settings.ignore_https_errors,
        )
    else:
        settings.lock_url = None


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        asyncio.run(apply_args(args))
    except ValueError as exc:
        parser.error(str(exc))
    from .main import app, manager

    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        reload=False,
        ws_ping_interval=None,
        timeout_graceful_shutdown=settings.shutdown_timeout_seconds,
    )
    server = WebsiteAgentServer(config, manager.request_stop)
    server.run()


if __name__ == "__main__":
    main()
