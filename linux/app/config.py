from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    host: str = "0.0.0.0"
    port: int = 8765
    data_dir: Path = Path("/data")
    headless: bool = True
    browser_executable: str = ""
    browser_proxy: str = ""
    scan_enabled: bool = True
    scan_cron: str = "0 3 * * *"
    scan_on_start: bool = False
    download_after_scan: bool = True
    download_workers: int = 4
    request_timeout_seconds: int = 90
    navigation_timeout_seconds: int = 60
    scan_request_timeout_seconds: int = 15
    province_scan_timeout_seconds: int = 45
    max_region_candidates: int = 12
    max_download_mb: int = 200
    admin_token: str = ""
    timezone: str = "Asia/Shanghai"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=os.getenv("SGCC_HOST", "0.0.0.0"),
            port=_int_env("SGCC_PORT", 8765),
            data_dir=Path(os.getenv("SGCC_DATA_DIR", "/data")).expanduser().resolve(),
            headless=_bool_env("SGCC_HEADLESS", True),
            browser_executable=os.getenv("SGCC_BROWSER_EXECUTABLE", "").strip(),
            browser_proxy=os.getenv("SGCC_BROWSER_PROXY", "").strip(),
            scan_enabled=_bool_env("SGCC_SCAN_ENABLED", True),
            scan_cron=os.getenv("SGCC_SCAN_CRON", "0 3 * * *").strip(),
            scan_on_start=_bool_env("SGCC_SCAN_ON_START", False),
            download_after_scan=_bool_env("SGCC_DOWNLOAD_AFTER_SCAN", True),
            download_workers=_int_env("SGCC_DOWNLOAD_WORKERS", 4),
            request_timeout_seconds=_int_env("SGCC_REQUEST_TIMEOUT_SECONDS", 90),
            navigation_timeout_seconds=_int_env("SGCC_NAVIGATION_TIMEOUT_SECONDS", 60),
            scan_request_timeout_seconds=_int_env("SGCC_SCAN_REQUEST_TIMEOUT_SECONDS", 15),
            province_scan_timeout_seconds=_int_env("SGCC_PROVINCE_SCAN_TIMEOUT_SECONDS", 45),
            max_region_candidates=_int_env("SGCC_MAX_REGION_CANDIDATES", 12),
            max_download_mb=_int_env("SGCC_MAX_DOWNLOAD_MB", 200),
            admin_token=os.getenv("SGCC_ADMIN_TOKEN", "").strip(),
            timezone=os.getenv("SGCC_TIMEZONE", "Asia/Shanghai").strip(),
        )

    @property
    def catalog_file(self) -> Path:
        return self.data_dir / "catalog.json"

    @property
    def report_file(self) -> Path:
        return self.data_dir / "download-report.json"

    @property
    def schedule_file(self) -> Path:
        return self.data_dir / "schedule.json"

    @property
    def download_dir(self) -> Path:
        return self.data_dir / "downloads"

    @property
    def log_file(self) -> Path:
        return self.data_dir / "logs" / "service.log"

    def prepare(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
