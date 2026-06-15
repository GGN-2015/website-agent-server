from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8000
    headless: bool = True
    ignore_https_errors: bool = False
    allow_private_hosts: bool = False
    session_ttl_seconds: int = 900
    navigation_timeout_ms: int = 30000
    frame_interval_seconds: float = 0.18
    screenshot_quality: int = 72
    min_viewport_width: int = 320
    min_viewport_height: int = 240
    max_viewport_width: int = 1920
    max_viewport_height: int = 1600
    data_dir: Path = PROJECT_ROOT / ".agent-data"
    pin: str | None = None
    auth_cookie_name: str = "website_agent_auth"
    auth_cookie_max_age: int = 60 * 60 * 24 * 7

    @property
    def downloads_dir(self) -> Path:
        return self.data_dir / "downloads"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"


settings = Settings()
