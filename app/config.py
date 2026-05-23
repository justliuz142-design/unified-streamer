"""Runtime configuration loaded from environment variables.

All values are read once at process start. Cookies for YouTube are read from
``YOUTUBE_COOKIES`` (raw Netscape cookie file contents) — never encrypted, as
required for Railway deployments.
"""
from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger("config")


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("Invalid int for %s=%r, using default %d", key, raw, default)
        return default


@dataclass
class Settings:
    host: str = "0.0.0.0"
    port: int = 8081
    static_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent / "ui" / "src")
    download_dir: Path = field(default_factory=lambda: Path(os.environ.get("DOWNLOAD_DIR", tempfile.gettempdir())) / "lunar_downloads")
    state_dir: Path = field(default_factory=lambda: Path(os.environ.get("STATE_DIR", tempfile.gettempdir())) / "lunar_state")
    ytdl_cache_dir: Path = field(default_factory=lambda: Path(os.environ.get("YTDL_CACHE_DIR", tempfile.gettempdir())) / "lunar_ytdl_cache")
    max_concurrent: int = field(default_factory=lambda: _env_int("MAX_CONCURRENT_DOWNLOADS", 3))
    cleanup_seconds: int = field(default_factory=lambda: _env_int("CLEANUP_AFTER_SECONDS", 3 * 60 * 60))
    cleanup_interval: int = field(default_factory=lambda: _env_int("CLEANUP_SCAN_INTERVAL", 5 * 60))
    cors_origins: str = field(default_factory=lambda: os.environ.get("CORS_ALLOWED_ORIGINS", "*"))
    log_level: str = field(default_factory=lambda: os.environ.get("LOGLEVEL", "INFO"))
    youtube_cookies: Optional[str] = field(default_factory=lambda: os.environ.get("YOUTUBE_COOKIES") or None)
    proxy_url: Optional[str] = field(default_factory=lambda: os.environ.get("PROXY_URL") or None)

    def __post_init__(self) -> None:
        port_env = os.environ.get("PORT")
        if port_env:
            try:
                self.port = int(port_env)
            except ValueError:
                pass
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.ytdl_cache_dir.mkdir(parents=True, exist_ok=True)

    @property
    def cookies_file(self) -> Optional[Path]:
        """Materialise the cookie env var into a file yt-dlp can read."""
        if not self.youtube_cookies:
            return None
        path = self.state_dir / "cookies.txt"
        # Always rewrite so rotated cookies are picked up without a restart edge.
        try:
            path.write_text(self.youtube_cookies, encoding="utf-8")
            return path
        except OSError as exc:
            log.warning("Cannot write cookies file %s: %s", path, exc)
            return None


settings = Settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
