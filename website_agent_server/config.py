from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class Settings:
    host: str = "0.0.0.0"
    port: int = 8000
    headless: bool = True
    ignore_https_errors: bool = False
    allow_private_hosts: bool = False
    locale: str = "zh-CN"
    timezone_id: str = "Asia/Shanghai"
    accept_language: str = "zh-CN,zh;q=0.9,en;q=0.8"
    user_agent: str | None = None
    session_ttl_seconds: int = 600
    navigation_timeout_ms: int = 30000
    frame_interval_seconds: float = 0.18
    screenshot_quality: int = 95
    media_frame_interval_seconds: float = 0.35
    media_screenshot_quality: int = 80
    min_viewport_width: int = 320
    min_viewport_height: int = 240
    max_viewport_width: int = 1920
    max_viewport_height: int = 1600
    data_dir: Path = Path(".agent-data")
    pin: str | None = None
    lock_url: str | None = None
    client_session_cookie_name: str = "session-uuid"
    client_session_cookie_max_age: int = 60 * 60 * 24 * 365
    auth_cookie_name: str = "website_agent_auth"
    auth_cookie_max_age: int = 60 * 60 * 24 * 7

    @property
    def downloads_dir(self) -> Path:
        return self.data_dir / "downloads"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"


settings = Settings()
