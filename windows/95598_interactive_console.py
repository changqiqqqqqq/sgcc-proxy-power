"""95598 代理购电 PDF 交互式网页控制台。

这个脚本自带本地 HTTP 服务，不依赖当前工作目录，也不依赖另行启动的
``python -m http.server``。运行后会自动在 Edge 中打开控制台页面。

用法：
    python 95598_interactive_console.py
    python 95598_interactive_console.py --no-open

扫描使用一个独立的可见 Edge 用户目录。若网页出现人工验证，扫描线程会
保留浏览器窗口并暂停；用户可在 Edge 完成验证，控制台会自动检测并继续，
也可以点击“验证完成，继续”立即复查。
"""

from __future__ import annotations

import argparse
import copy
import ctypes
import hashlib
import hmac
import io
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import zipfile
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

try:
    import requests
except ImportError:  # 控制台仍可启动，并在下载时给出明确提示。
    requests = None

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:  # 控制台仍可启动，并在开始扫描时给出明确提示。
    PlaywrightTimeoutError = TimeoutError
    sync_playwright = None


APP_DIR = Path(__file__).resolve().parent
CONSOLE_FILE = APP_DIR / "console.html"
STATE_FILE = APP_DIR / "interactive_state.json"
LEGACY_STATE_FILE = APP_DIR / "monitor_state.json"
LOG_FILE = APP_DIR / "interactive_console.log"
URL_FILE = APP_DIR / "console_url.txt"
# The original console stored Beijing PDFs in ``pdf``.  The detailed catalogue
# view uses the verified per-province tree when it downloads PDFs or images,
# while the legacy directory remains available for backwards compatibility.
PDF_DIR = APP_DIR / "pdf"
CATALOG_FILE = APP_DIR / "proxy_power_catalog.json"
CSG_CATALOG_FILE = APP_DIR / "csg_proxy_power_catalog.json"
RICH_CATALOG_FILE = APP_DIR / "all_provinces_pdf_catalog.json"
RAW_CATALOG_FILE = APP_DIR.parent / "work" / "all_provinces_raw.json"
CATALOG_REPORT_FILE = APP_DIR / "proxy_power_download_report.json"
CATALOG_DOWNLOAD_ROOT = APP_DIR / "provinces_verified"
CSG_DOWNLOAD_ROOT = APP_DIR / "csg_documents"
CATALOG_SCANNER = APP_DIR / "sgcc_proxy_power_downloader.py"
CSG_SCANNER = APP_DIR / "csg_proxy_power_downloader.py"
CATALOG_NORMALIZER = APP_DIR / "normalize_proxy_catalog.py"
PROFILE_BASE_DIR = APP_DIR / ".edge_profile_console"

PAGE_URL = "https://95598.cn/osgweb/ipElectrovalenceStandard?partNo=P2019"
APP_ID = "95598-interactive-console-v5"
DEFAULT_PORT = 8765
MAX_PORT_TRIES = 20


class ScanCancelled(RuntimeError):
    """用户主动停止了扫描。"""


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def catalog_item_sort_key(item: dict[str, Any]) -> tuple[int, int, str]:
    match = re.search(r"(20\d{2})年\s*(\d{1,2})月", str(item.get("title") or ""))
    return (int(match.group(1)), int(match.group(2)), str(item.get("title") or "")) if match else (0, 0, str(item.get("title") or ""))


def _catalog_json(path: Path) -> Any:
    """Read a catalogue/report file without allowing a bad file to stop the UI."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _absolute_catalog_url(value: Any) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    if value.startswith("//"):
        return "https:" + value
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return urljoin(PAGE_URL, value)


def _catalog_type(url: str, raw: dict[str, Any] | None = None) -> str:
    """Return a stable, UI-friendly attachment type."""
    path = urlparse(url or "").path.lower()
    suffix = Path(path).suffix.lower().lstrip(".")
    if suffix == "pdf" or bool((raw or {}).get("is_pdf")):
        return "pdf"
    if suffix in {"html", "htm"} or str((raw or {}).get("type") or (raw or {}).get("format") or "").lower() == "html":
        return "html"
    if suffix in {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff", "svg"}:
        return "image"
    # Existing records may have been collected before the URL was normalized.
    raw_type = str((raw or {}).get("type", "")).lower()
    if raw_type == "pdf":
        return "pdf"
    if raw_type == "image" or raw_type in {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff", "svg", "image_or_other"}:
        return "image" if suffix in {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff", "svg"} else "other"
    return "other"


def _catalog_extension(url: str, content_type: str = "", data: bytes = b"") -> str:
    """Infer a safe file extension for PDF/image attachments."""
    ext = Path(urlparse(url or "").path).suffix.lower()
    if ext in {".html", ".htm", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".svg"}:
        return ext
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    by_type = {
        "text/html": ".html",
        "application/xhtml+xml": ".html",
        "application/pdf": ".pdf",
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
        "image/tiff": ".tif",
        "image/svg+xml": ".svg",
    }
    if ctype in by_type:
        return by_type[ctype]
    if data.startswith(b"%PDF"):
        return ".pdf"
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    return ".bin"


def _is_probably_html(data: bytes, content_type: str = "") -> bool:
    ctype = (content_type or "").lower()
    if "text/html" in ctype or "application/xhtml" in ctype:
        return True
    head = data[:300].lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html", b"<head", b"<body"))


def default_data() -> dict[str, Any]:
    return {"items": [], "downloaded": {}, "last_scan": ""}


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def load_data() -> dict[str, Any]:
    # 首次运行时自动迁移旧版 monitor_state.json，避免丢失已有下载记录。
    state = _read_json(STATE_FILE)
    if state is None:
        state = _read_json(LEGACY_STATE_FILE)
    if state is None:
        state = default_data()
    state.setdefault("items", [])
    state.setdefault("downloaded", {})
    state.setdefault("last_scan", "")
    return state


def save_data(state: dict[str, Any]) -> None:
    """原子保存状态，避免网页轮询时读到半个 JSON 文件。"""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(STATE_FILE)


def append_log(message: str) -> None:
    with LOG_FILE.open("a", encoding="utf-8") as stream:
        stream.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {message}\n")


def safe_filename(title: str, url: str = "") -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", title).strip().rstrip(".")
    if not name:
        name = "代理购电价格公示表"
    # Windows 单个文件名上限通常为 255；给扩展名和偶发重名后缀留余量。
    name = name[:210]
    return f"{name}.pdf"


def safe_component(value: str) -> str:
    return (re.sub(r'[\\/:*?"<>|\r\n]+', "_", value).strip(" ._") or "未命名")[:210]


def find_edge() -> Path:
    candidates = [
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
        / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Microsoft/Edge/Application/msedge.exe",
    ]
    found = shutil.which("msedge")
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise RuntimeError("未找到 Microsoft Edge。请确认 Edge 已安装。")


def choose_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _is_pdf_url(url: str) -> bool:
    return ".pdf" in urlparse(url).path.lower()


def _visible_exact(page, text: str):
    """返回第一个可见的精确文本元素；不存在时返回 None。"""
    locator = page.get_by_text(text, exact=True)
    try:
        count = min(locator.count(), 20)
    except Exception:
        return None
    candidates = []
    for index in range(count):
        item = locator.nth(index)
        try:
            if item.is_visible():
                box = item.bounding_box()
                area = (box or {}).get("width", 9999) * (box or {}).get("height", 9999)
                candidates.append((area, item))
        except Exception:
            continue
    return min(candidates, key=lambda value: value[0])[1] if candidates else None


def _window_text(hwnd: int) -> str:
    user32 = ctypes.windll.user32
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def activate_edge_window(preferred_pid: int | set[int] | None = None) -> bool:
    """尽力将受控 Edge 窗口恢复并切到前台。"""
    if os.name != "nt":
        return False
    user32 = ctypes.windll.user32
    windows: list[tuple[int, int, str]] = []
    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    preferred_pids: set[int] = set()
    if isinstance(preferred_pid, set):
        preferred_pids = preferred_pid
    elif preferred_pid:
        preferred_pids = {preferred_pid}

    @enum_proc
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _window_text(hwnd)
        process_id = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        score = 0
        if process_id.value in preferred_pids:
            score += 100
        if not title and score == 0:
            return True
        lowered = title.lower()
        if "95598" in lowered or "电价" in title or "国家电网" in title:
            score += 30
        if "edge" in lowered:
            score += 10
        if score:
            windows.append((score, int(hwnd), title))
        return True

    user32.EnumWindows(callback, 0)
    if not windows:
        return False
    _score, hwnd, _title = max(windows, key=lambda item: item[0])
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    # Windows 会限制后台进程抢前台；临时把当前线程接到前台/目标线程，
    # 再设置焦点，通常比单独调用 SetForegroundWindow 更可靠。
    attached: list[tuple[int, int]] = []
    try:
        current_thread = ctypes.windll.kernel32.GetCurrentThreadId()
        foreground = user32.GetForegroundWindow()
        foreground_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
        target_thread = user32.GetWindowThreadProcessId(hwnd, None)
        for other_thread in (foreground_thread, target_thread):
            if other_thread and other_thread != current_thread:
                if user32.AttachThreadInput(current_thread, other_thread, True):
                    attached.append((current_thread, other_thread))
        user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
        user32.BringWindowToTop(hwnd)
        user32.SetFocus(hwnd)
        user32.SetActiveWindow(hwnd)
        foreground_result = bool(user32.SetForegroundWindow(hwnd))
        if hasattr(user32, "SwitchToThisWindow"):
            user32.SwitchToThisWindow(hwnd, True)
        return foreground_result or user32.GetForegroundWindow() == hwnd
    finally:
        for current_thread, other_thread in reversed(attached):
            user32.AttachThreadInput(current_thread, other_thread, False)


class ScannerController:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.data = load_data()
        self.runtime: dict[str, Any] = {
            "phase": "idle",
            "message": "就绪。点击“开始扫描”后会打开受控的可见 Edge 窗口。",
            "scan_running": False,
            "download_running": False,
            "needs_validation": False,
            "browser_open": False,
            "updated_at": now_text(),
            "current_item": "",
            "last_error": "",
        }
        self.scan_thread: threading.Thread | None = None
        self.download_thread: threading.Thread | None = None
        self.continue_event = threading.Event()
        self.cancel_event = threading.Event()
        self.browser_pid: int | None = None
        self.browser_process: subprocess.Popen | None = None
        self.browser_profile: Path | None = None
        self.browser_cookies: list[dict[str, Any]] = []
        self.browser_user_agent = "Mozilla/5.0"
        # Detailed all-province catalogue jobs are separate from the original
        # Beijing interactive scan, so a user can inspect/download the existing
        # catalogue while a fresh scan is running.
        self.catalog_scan_thread: threading.Thread | None = None
        self.catalog_scan_process: subprocess.Popen | None = None
        self.catalog_download_thread: threading.Thread | None = None
        self.catalog_runtime: dict[str, Any] = {
            "running": False,
            "phase": "idle",
            "message": "尚未执行全省目录扫描。",
            "current_province": "",
            "completed": 0,
            "total": 0,
            "started_at": "",
            "finished_at": "",
            "last_error": "",
        }
        save_data(self.data)

    def _set_runtime(self, *, phase: str | None = None, message: str | None = None, **values: Any) -> None:
        with self.lock:
            if phase is not None:
                self.runtime["phase"] = phase
            if message is not None:
                self.runtime["message"] = message
            self.runtime.update(values)
            self.runtime["updated_at"] = now_text()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            runtime = copy.deepcopy(self.runtime)
            data = copy.deepcopy(self.data)
            catalog_runtime = copy.deepcopy(self.catalog_runtime)
        items = []
        downloaded_count = 0
        failed_count = 0
        for item in data.get("items", []):
            entry = dict(item)
            record = data.get("downloaded", {}).get(entry.get("url", ""), {})
            file_name = record.get("path", "")
            exists = bool(file_name and (PDF_DIR / file_name).is_file())
            if exists:
                local_status = "downloaded"
                downloaded_count += 1
            elif record.get("error"):
                local_status = "failed"
                failed_count += 1
            else:
                local_status = "missing"
            entry.update(
                {
                    "local_status": local_status,
                    "path": file_name,
                    "download_time": record.get("time", ""),
                    "download_error": record.get("error", ""),
                }
            )
            items.append(entry)
        runtime.update(
            {
                "app": APP_ID,
                "items": items,
                "last_scan": data.get("last_scan", ""),
                "counts": {
                    "total": len(items),
                    "downloaded": downloaded_count,
                    "missing": len(items) - downloaded_count - failed_count,
                    "failed": failed_count,
                },
                "download_dir": str(PDF_DIR),
            }
        )
        # The detailed UI uses the same status banner for both the legacy
        # Beijing operation and the new all-province catalogue jobs.
        catalog_is_newer = str(catalog_runtime.get("updated_at") or "") >= str(runtime.get("updated_at") or "")
        if catalog_runtime.get("running") or catalog_runtime.get("download_running") or catalog_is_newer:
            runtime.update(
                {
                    "phase": catalog_runtime.get("phase", runtime.get("phase")),
                    "message": catalog_runtime.get("message", runtime.get("message")),
                    "scan_running": bool(catalog_runtime.get("running")) and catalog_runtime.get("phase") == "scanning",
                    "download_running": bool(catalog_runtime.get("download_running")),
                    "current_item": catalog_runtime.get("current_item") or catalog_runtime.get("current_province") or "",
                    "download_total": catalog_runtime.get("download_total", 0),
                    "download_done": catalog_runtime.get("download_completed", 0),
                    "updated_at": catalog_runtime.get("updated_at") or runtime.get("updated_at"),
                }
            )
        runtime["catalog_scan"] = catalog_runtime
        runtime["catalog_available"] = CATALOG_FILE.is_file() or RICH_CATALOG_FILE.is_file() or CSG_CATALOG_FILE.is_file()
        runtime["catalog_download_dir"] = str(CATALOG_DOWNLOAD_ROOT)
        return runtime

    def start_scan(self) -> tuple[bool, str]:
        with self.lock:
            if self.scan_thread and self.scan_thread.is_alive():
                return False, "扫描正在进行中。"
            self.cancel_event.clear()
            self.continue_event.clear()
            self.runtime.update(
                {
                    "phase": "queued",
                    "message": "扫描任务已启动，正在准备可见 Edge…",
                    "scan_running": True,
                    "needs_validation": False,
                    "last_error": "",
                    "updated_at": now_text(),
                }
            )
            self.scan_thread = threading.Thread(target=self._scan_worker, name="95598-scan", daemon=True)
            self.scan_thread.start()
        return True, "扫描任务已启动。"

    def continue_after_validation(self) -> tuple[bool, str]:
        with self.lock:
            if not self.runtime.get("scan_running"):
                return False, "当前没有进行中的扫描。"
            if not self.runtime.get("needs_validation"):
                return False, "当前未处于等待验证状态；扫描会自行继续。"
        self.continue_event.set()
        return True, "已通知扫描程序复查验证状态。"

    def stop_scan(self) -> tuple[bool, str]:
        with self.lock:
            if not self.runtime.get("scan_running"):
                return False, "当前没有进行中的扫描。"
        self.cancel_event.set()
        self.continue_event.set()
        self._set_runtime(phase="stopping", message="正在停止扫描…")
        return True, "已发出停止请求。"

    def show_browser(self) -> tuple[bool, str]:
        with self.lock:
            browser_open = bool(self.runtime.get("browser_open"))
            pid = self.browser_pid
            profile = self.browser_profile
        if not browser_open:
            return False, "受控 Edge 尚未打开；请先点击“开始扫描”。"
        pids = {pid} if pid else set()
        if profile is not None and os.name == "nt":
            pids.update(self._profile_process_ids(profile))
        shown = activate_edge_window(pids)
        if shown:
            return True, "已尝试将受控 Edge 切到前台。"
        # SetForegroundWindow 可能被 Windows 的前台锁拒绝。再次用同一 profile
        # 请求新窗口会由正在运行的受控 Edge 接管，能可靠地让用户看到页面，
        # 同时仍属于 Playwright 已连接的浏览器上下文。
        if profile is not None:
            try:
                edge_path = find_edge()
                subprocess.Popen(
                    [str(edge_path), f"--user-data-dir={profile}", "--new-window", PAGE_URL],
                    cwd=str(APP_DIR),
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                )
                return True, "已请求 Edge 重新显示受控页面。"
            except Exception:
                pass
        return False, "Edge 已打开，请从任务栏选择该窗口。"

    @staticmethod
    def _profile_process_ids(profile: Path) -> set[int]:
        """找出本次随机 profile 对应的 Edge 主进程和子进程。"""
        if os.name != "nt":
            return set()
        try:
            needle = str(profile).replace("'", "''")
            ps = (
                "$needle='" + needle + "'; "
                "$p=Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" "
                "| Where-Object { $_.CommandLine -like ('*' + $needle + '*') } "
                "| Select-Object -ExpandProperty ProcessId; $p"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True,
                text=True,
                timeout=6,
                check=False,
            )
            values: set[int] = set()
            for line in result.stdout.splitlines():
                try:
                    values.add(int(line.strip()))
                except ValueError:
                    continue
            return values
        except Exception:
            return set()

    def open_download_folder(self) -> tuple[bool, str]:
        CATALOG_DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
        CSG_DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
        download_parent = CATALOG_DOWNLOAD_ROOT.parent
        try:
            if os.name == "nt":
                os.startfile(str(download_parent))  # type: ignore[attr-defined]
            else:
                webbrowser.open(download_parent.as_uri())
            return True, "已打开下载根目录（国家电网与南方电网文件夹均在其中）。"
        except OSError as exc:
            return False, f"打开目录失败：{exc}"

    def download_missing(self) -> tuple[bool, str]:
        if requests is None:
            return False, "缺少 requests 组件，请先执行：python -m pip install requests"
        with self.lock:
            if self.download_thread and self.download_thread.is_alive():
                return False, "下载任务正在进行中。"
            missing = []
            for item in self.data.get("items", []):
                record = self.data.get("downloaded", {}).get(item.get("url", ""), {})
                path = record.get("path", "")
                if not path or not (PDF_DIR / path).is_file():
                    missing.append(dict(item))
            if not missing:
                return False, "没有待下载文件。"
            self.runtime.update(
                {
                    "download_running": True,
                    "phase": "downloading",
                    "message": f"准备下载 {len(missing)} 个文件…",
                    "updated_at": now_text(),
                }
            )
            self.download_thread = threading.Thread(
                target=self._download_worker,
                args=(missing,),
                name="95598-download",
                daemon=True,
            )
            self.download_thread.start()
        return True, f"已开始下载 {len(missing)} 个文件。"

    def download_item(self, url: str) -> tuple[bool, str]:
        """启动单个条目的下载，URL 必须已经由本次扫描发现。"""
        if requests is None:
            return False, "缺少 requests 组件，请先执行：python -m pip install requests"
        with self.lock:
            if self.download_thread and self.download_thread.is_alive():
                return False, "下载任务正在进行中。"
            item = next((entry for entry in self.data.get("items", []) if entry.get("url") == url), None)
            if item is None:
                return False, "这个 PDF 地址不在扫描记录中。"
            record = self.data.get("downloaded", {}).get(url, {})
            path = record.get("path", "")
            if path and (PDF_DIR / path).is_file():
                return False, "这个 PDF 已经存在于本地。"
            self.runtime.update(
                {
                    "download_running": True,
                    "phase": "downloading",
                    "message": f"准备下载：{item.get('title', '')}",
                    "current_item": item.get("title", ""),
                    "updated_at": now_text(),
                }
            )
            self.download_thread = threading.Thread(
                target=self._download_worker,
                args=([dict(item)],),
                name="95598-download-one",
                daemon=True,
            )
            self.download_thread.start()
        return True, "已开始下载该 PDF。"

    # ------------------------------------------------------------------
    # Detailed all-province catalogue API
    # ------------------------------------------------------------------
    def _catalog_candidates(self) -> tuple[list[dict[str, Any]], str, str]:
        """Load and merge the available catalogue snapshots.

        ``proxy_power_catalog.json`` is the current scanner output.  The
        verified ``all_provinces_pdf_catalog.json`` and (when present) the raw
        work snapshot are useful fallbacks when a transient page response
        returned zero rows for a province.  A candidate with more items wins;
        ties prefer the newest primary scanner output.
        """
        sources: list[tuple[str, int, Any]] = [
            ("proxy_power_catalog", 30, _catalog_json(CATALOG_FILE)),
            ("csg_proxy_power_catalog", 30, _catalog_json(CSG_CATALOG_FILE)),
            ("all_provinces_pdf_catalog", 20, _catalog_json(RICH_CATALOG_FILE)),
            ("all_provinces_raw", 10, _catalog_json(RAW_CATALOG_FILE)),
        ]
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        order: list[tuple[str, str]] = []

        def add_candidate(raw: Any, source: str, rank: int) -> None:
            if not isinstance(raw, dict):
                return
            province_value = raw.get("province", "")
            if isinstance(province_value, dict):
                province_name = str(province_value.get("codeName") or province_value.get("name") or "")
                province_code = str(province_value.get("codeValue") or raw.get("provinceCode") or "")
            else:
                province_name = str(province_value or raw.get("province_name") or "").strip()
                province_code = str(raw.get("provinceCode") or raw.get("province_code") or "")
            if not province_name:
                return
            network = str(raw.get("network") or ("csg" if source.startswith("csg") else "sgcc")).lower()
            codes = raw.get("codes") if isinstance(raw.get("codes"), dict) else {}
            province_code = province_code or str(codes.get("provinceCode") or "")
            region = raw.get("region") if isinstance(raw.get("region"), dict) else {}
            if not region and codes:
                region = dict(codes)
            category_raw = raw.get("category") if isinstance(raw.get("category"), dict) else {}
            category_title = str(
                category_raw.get("title")
                or category_raw.get("knoweleTitle")
                or category_raw.get("className")
                or category_raw.get("labelName")
                or ""
            ).strip()
            category_url_value = ""
            if category_raw:
                category_url_value = (
                    category_raw.get("url")
                    or category_raw.get("knoweleContentUrl")
                    or (category_raw.get("raw") or {}).get("knoweleContentUrl")
                    or ""
                )
            category_url = _absolute_catalog_url(category_url_value)
            raw_items = raw.get("items")
            if not isinstance(raw_items, list):
                raw_items = raw.get("child") if isinstance(raw.get("child"), list) else []
            items: list[dict[str, Any]] = []
            seen_urls: set[str] = set()
            for item_raw in raw_items:
                if not isinstance(item_raw, dict):
                    continue
                nested = item_raw.get("raw") if isinstance(item_raw.get("raw"), dict) else {}
                title = str(
                    item_raw.get("title")
                    or item_raw.get("knoweleTitle")
                    or item_raw.get("electricpriceTitle")
                    or item_raw.get("infoTitle")
                    or item_raw.get("className")
                    or item_raw.get("labelName")
                    or ""
                ).strip()
                url = _absolute_catalog_url(
                    item_raw.get("url")
                    or item_raw.get("pdfAddress")
                    or item_raw.get("electricpriceInfo")
                    or item_raw.get("classContentUrl")
                    or item_raw.get("classContent")
                    or item_raw.get("link")
                    or nested.get("pdfAddress")
                    or nested.get("electricpriceInfo")
                    or nested.get("classContentUrl")
                    or nested.get("classContent")
                    or nested.get("link")
                )
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                kind = _catalog_type(url, {**nested, **item_raw})
                items.append(
                    {
                        "title": title or Path(urlparse(url).path).stem,
                        "url": url,
                        "type": kind,
                        "format": Path(urlparse(url).path).suffix.lower().lstrip(".") or kind,
                        "is_pdf": kind == "pdf",
                        "raw": nested or item_raw,
                        "category_id": str(item_raw.get("category_id") or item_raw.get("categoryId") or ""),
                        "info_id": str(item_raw.get("info_id") or item_raw.get("infoId") or (nested or {}).get("infoId") or ""),
                        "publish_time": str(item_raw.get("publish_time") or item_raw.get("publishTime") or (nested or {}).get("publishTime") or ""),
                        "discovered_at": str(item_raw.get("discovered_at") or raw.get("scanned_at") or ""),
                    }
                )
            candidate = {
                "province": province_name,
                "provinceCode": province_code,
                "network": network,
                "region": region,
                "category": {"title": category_title, "url": category_url},
                "items": items,
                "source": source,
                "rank": rank,
            }
            group_key = (network, province_name)
            grouped.setdefault(group_key, []).append(candidate)
            if group_key not in order:
                order.append(group_key)

        for source, rank, payload in sources:
            if isinstance(payload, dict):
                rows = payload.get("results")
                if isinstance(rows, list):
                    for row in rows:
                        add_candidate(row, source, rank)
            elif isinstance(payload, list):
                for row in payload:
                    add_candidate(row, source, rank)

        merged: list[dict[str, Any]] = []
        scanned_at = ""
        source_url = PAGE_URL
        primary = _catalog_json(CATALOG_FILE)
        if isinstance(primary, dict):
            scanned_at = str(primary.get("scanned_at") or "")
            source_url = str(primary.get("source") or PAGE_URL)
        rich = _catalog_json(RICH_CATALOG_FILE)
        if not scanned_at and isinstance(rich, dict):
            scanned_at = str(rich.get("scanned_at") or "")
            source_url = str(rich.get("source") or PAGE_URL)

        for group_key in order:
            candidates = grouped.get(group_key, [])
            if not candidates:
                continue
            best = max(candidates, key=lambda x: (int(bool(x.get("items"))), x.get("rank", 0), len(x.get("items", []))))
            category = dict(best.get("category") or {})
            region = dict(best.get("region") or {})
            if not category.get("title"):
                for candidate in candidates:
                    if (candidate.get("category") or {}).get("title"):
                        category = dict(candidate["category"])
                        break
            if not region:
                for candidate in candidates:
                    if candidate.get("region"):
                        region = dict(candidate["region"])
                        break
            best = dict(best)
            best["category"] = category
            best["network"] = group_key[0]
            best["tab_found"] = bool(category.get("title"))
            best["region"] = region
            merged.append(best)
        return merged, scanned_at, source_url

    @staticmethod
    def _catalog_report_map() -> dict[str, dict[str, Any]]:
        payload = _catalog_json(CATALOG_REPORT_FILE)
        if not isinstance(payload, dict):
            return {}
        rows = payload.get("items")
        if not isinstance(rows, list):
            return {}
        return {str(row.get("url")): dict(row) for row in rows if isinstance(row, dict) and row.get("url")}

    @staticmethod
    def _catalog_local_path(value: Any) -> Path | None:
        value = str(value or "").strip()
        if not value:
            return None
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = APP_DIR / candidate
        try:
            resolved = candidate.resolve()
            roots = [CATALOG_DOWNLOAD_ROOT.resolve(), CSG_DOWNLOAD_ROOT.resolve(), PDF_DIR.resolve()]
            for root in roots:
                try:
                    resolved.relative_to(root)
                    return resolved
                except ValueError:
                    continue
        except (OSError, ValueError):
            return None
        return None

    def _decorate_catalog_item(self, province: str, network: str, item: dict[str, Any], reports: dict[str, dict[str, Any]]) -> dict[str, Any]:
        entry = dict(item)
        url = str(entry.get("url") or "")
        record = dict(reports.get(url) or {})
        local_path = self._catalog_local_path(record.get("file") or record.get("path") or entry.get("local_file"))
        exists = bool(local_path and local_path.is_file())
        if exists:
            status = "downloaded"
            bytes_count = local_path.stat().st_size
        elif record.get("status") == "failed" or record.get("error") or entry.get("local_status") == "failed":
            status = "failed"
            bytes_count = 0
        else:
            status = "missing"
            bytes_count = 0
        relative = ""
        if local_path:
            try:
                relative = str(local_path.relative_to(APP_DIR))
            except ValueError:
                relative = str(local_path)
        entry.update(
            {
                "id": hashlib.sha256(url.encode("utf-8")).hexdigest()[:16],
                "province": province,
                "network": network,
                "type": entry.get("type") or _catalog_type(url, entry),
                "is_pdf": bool(entry.get("is_pdf") or entry.get("type") == "pdf"),
                "local_status": status,
                "status": status,
                "path": relative,
                "local_file": relative,
                "local_url": "/pdf/" + relative.replace("\\", "/") if relative and exists else "",
                "bytes": bytes_count,
                "download_time": str(record.get("time") or ""),
                "download_error": str(record.get("error") or ""),
            }
        )
        return entry

    def catalog_snapshot(
        self,
        province_filter: str = "",
        status_filter: str = "",
        type_filter: str = "",
        include_items: bool = True,
    ) -> dict[str, Any]:
        """Return province summaries and (optionally) expanded item details."""
        with self.lock:
            runtime = copy.deepcopy(self.catalog_runtime)
        groups, scanned_at, source_url = self._catalog_candidates()
        reports = self._catalog_report_map()
        provinces: list[dict[str, Any]] = []
        total_items = total_pdf = total_images = total_html = total_downloaded = total_missing = total_failed = 0
        for group in groups:
            name = str(group.get("province") or "")
            if province_filter and province_filter not in name:
                continue
            network = str(group.get("network") or "sgcc").lower()
            ordered_items = sorted(group.get("items", []), key=catalog_item_sort_key, reverse=True)
            details = [self._decorate_catalog_item(name, network, item, reports) for item in ordered_items]
            if status_filter:
                details = [x for x in details if x.get("local_status") == status_filter]
            if type_filter:
                details = [x for x in details if x.get("type") == type_filter]
            all_details = [self._decorate_catalog_item(name, network, item, reports) for item in ordered_items]
            pdf_count = sum(1 for x in all_details if x.get("type") == "pdf")
            image_count = sum(1 for x in all_details if x.get("type") == "image")
            html_count = sum(1 for x in all_details if x.get("type") == "html")
            downloaded_count = sum(1 for x in all_details if x.get("local_status") == "downloaded")
            missing_count = sum(1 for x in all_details if x.get("local_status") == "missing")
            failed_count = sum(1 for x in all_details if x.get("local_status") == "failed")
            total_items += len(all_details)
            total_pdf += pdf_count
            total_images += image_count
            total_html += html_count
            total_downloaded += downloaded_count
            total_missing += missing_count
            total_failed += failed_count
            tab_found = bool(group.get("tab_found", (group.get("category") or {}).get("title")))
            tab_name = str((group.get("category") or {}).get("title") or "")
            category_payload = dict(group.get("category") or {"title": "", "url": ""})
            category_payload["found"] = tab_found
            province = {
                "province": name,
                "name": name,
                "provinceCode": str(group.get("provinceCode") or ""),
                "network": network,
                "grid": "南方电网" if network == "csg" else "国家电网",
                "province_key": f"{network}:{name}",
                "region": group.get("region") or {},
                "category": category_payload,
                "category_title": tab_name,
                "tab_name": tab_name,
                "tab_found": tab_found,
                "item_count": len(all_details),
                "directory_count": len(all_details),
                "pdf_count": pdf_count,
                "image_count": image_count,
                "html_count": html_count,
                "other_count": max(0, len(all_details) - pdf_count - image_count - html_count),
                "downloaded_count": downloaded_count,
                "missing_count": missing_count,
                "failed_count": failed_count,
                "source": group.get("source", ""),
            }
            if include_items:
                province["items"] = details
            provinces.append(province)
        return {
            "ok": True,
            "app": APP_ID,
            "source": source_url,
            "scanned_at": scanned_at,
            "updated_at": now_text(),
            "scan": runtime,
            "provinces": provinces,
            "totals": {
                "province_count": len(provinces),
                "item_count": total_items,
                "directory_count": total_items,
                "pdf_count": total_pdf,
                "image_count": total_images,
                "html_count": total_html,
                "downloaded_count": total_downloaded,
                "missing_count": total_missing,
                "failed_count": total_failed,
            },
        }

    def catalog_refresh_status(self) -> tuple[bool, str]:
        snapshot = self.catalog_snapshot(include_items=False)
        totals = snapshot.get("totals", {})
        return True, f"状态已刷新：已获取 {totals.get('downloaded_count', 0)}，未获取 {totals.get('missing_count', 0)}。"

    def export_catalog_items(self, urls: list[str]) -> tuple[bytes, str, int]:
        requested = {str(value) for value in urls if str(value)}
        if not requested:
            raise ValueError("请至少选择一个文件。")
        snapshot = self.catalog_snapshot(include_items=True)
        entries = [
            item
            for province in snapshot.get("provinces", [])
            for item in province.get("items", [])
            if str(item.get("url") or "") in requested
        ]
        if len(entries) != len(requested):
            raise ValueError("部分所选文件已不在当前目录，请刷新页面后重试。")
        missing = [item for item in entries if item.get("local_status") != "downloaded"]
        if missing:
            raise ValueError(f"所选文件中有 {len(missing)} 个尚未获取，请先下载后再导出。")
        buffer = io.BytesIO()
        used: set[str] = set()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for item in entries:
                source = self._catalog_local_path(item.get("local_file") or item.get("path"))
                if not source or not source.is_file():
                    raise ValueError(f"本地文件不存在：{item.get('title') or item.get('url')}")
                grid = "南方电网" if str(item.get("network") or "sgcc").lower() == "csg" else "国家电网"
                province = safe_component(str(item.get("province") or "未知省份"))
                filename = safe_component(source.name)
                arcname = f"{grid}/{province}/{filename}"
                stem, suffix = Path(filename).stem, Path(filename).suffix
                number = 2
                while arcname.lower() in used:
                    arcname = f"{grid}/{province}/{stem}_{number}{suffix}"
                    number += 1
                used.add(arcname.lower())
                archive.write(source, arcname)
        filename = f"95598代理购电资料_{datetime.now():%Y%m%d_%H%M%S}.zip"
        return buffer.getvalue(), filename, len(entries)

    def start_catalog_scan(self, _province: str = "") -> tuple[bool, str]:
        """Run the reusable all-province scanner asynchronously."""
        with self.lock:
            if self.catalog_scan_thread and self.catalog_scan_thread.is_alive():
                return False, "全省目录扫描正在进行中。"
            missing_scripts = [path.name for path in (CATALOG_SCANNER, CSG_SCANNER) if not path.is_file()]
            if missing_scripts:
                return False, f"找不到扫描脚本：{', '.join(missing_scripts)}"
            self.cancel_event.clear()
            self.catalog_runtime.update(
                {
                    "running": True,
                    "phase": "scanning",
                    "message": "正在扫描国家电网和南方电网代理购电目录…",
                    "current_province": "",
                    "completed": 0,
                    "total": 32,
                    "started_at": now_text(),
                    "updated_at": now_text(),
                    "finished_at": "",
                    "last_error": "",
                }
            )
            self.catalog_scan_thread = threading.Thread(target=self._catalog_scan_worker, name="95598-catalog-scan", daemon=True)
            self.catalog_scan_thread.start()
        return True, "全省目录扫描已开始。"

    def _catalog_scan_worker(self) -> None:
        process: subprocess.Popen | None = None
        try:
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            jobs = [
                ("国家电网", [sys.executable, str(CATALOG_SCANNER), "--scan"], 0),
                ("南方电网", [sys.executable, str(CSG_SCANNER), "--scan", "--visible"], 27),
            ]
            completed_total = 0
            errors: list[str] = []
            for network_name, command, offset in jobs:
                if self.cancel_event.is_set():
                    break
                self._set_catalog_runtime(message=f"正在扫描{network_name}代理购电目录…", current_province="")
                process = subprocess.Popen(
                    command,
                    cwd=str(APP_DIR),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=env,
                    creationflags=flags,
                )
                with self.lock:
                    self.catalog_scan_process = process
                if process.stdout is not None:
                    for line in process.stdout:
                        line = line.strip()
                        if not line:
                            continue
                        state_match = re.search(r"(\d+)\s*/\s*(\d+)\s+(.+?):\s*目录\s*(\d+)，PDF\s*(\d+)", line)
                        csg_match = re.search(r"CSG\s+(\d+)\s*/\s*(\d+)\s+(.+?):\s*目录\s*(\d+)，网页\s*(\d+)", line)
                        if state_match:
                            done, _total, province, item_count, pdf_count = state_match.groups()
                            completed_total = offset + int(done)
                            self._set_catalog_runtime(
                                message=f"{network_name} · {province}：目录 {item_count}，PDF {pdf_count}",
                                current_province=province,
                                completed=completed_total,
                                total=32,
                            )
                        elif csg_match:
                            done, _total, province, item_count, html_count = csg_match.groups()
                            completed_total = offset + int(done)
                            self._set_catalog_runtime(
                                message=f"{network_name} · {province}：目录 {item_count}，网页 {html_count}",
                                current_province=province,
                                completed=completed_total,
                                total=32,
                            )
                        else:
                            self._set_catalog_runtime(message=line)
                code = process.wait(timeout=30)
                if code != 0:
                    errors.append(f"{network_name}扫描脚本退出码 {code}")
            if self.cancel_event.is_set():
                self._set_catalog_runtime(
                    running=False,
                    phase="stopped",
                    message="全省目录扫描已停止。",
                    finished_at=now_text(),
                    current_province="",
                )
                return
            if errors and not (CATALOG_FILE.is_file() or CSG_CATALOG_FILE.is_file()):
                raise RuntimeError("；".join(errors))
            self._refresh_catalog_view_file()
            snapshot = self.catalog_snapshot(include_items=False)
            totals = snapshot.get("totals", {})
            message = f"双电网目录扫描完成：{totals.get('province_count', 0)} 个省份条目，共 {totals.get('item_count', 0)} 条资料。"
            if errors:
                message += " " + "；".join(errors)
            self._set_catalog_runtime(
                running=False,
                phase="warning" if errors else "complete",
                message=message,
                finished_at=now_text(),
                current_province="",
            )
            append_log(message)
        except Exception as exc:
            append_log(f"全省目录扫描失败：{exc}")
            self._set_catalog_runtime(
                running=False,
                phase="error",
                message=f"全省目录扫描失败：{exc}",
                finished_at=now_text(),
                last_error=str(exc),
            )
        finally:
            with self.lock:
                self.catalog_scan_process = None

    @staticmethod
    def _refresh_catalog_view_file() -> None:
        """Keep the standalone normalized JSON/CSV views in sync when present."""
        if not CATALOG_NORMALIZER.is_file():
            return
        try:
            subprocess.run(
                [sys.executable, str(CATALOG_NORMALIZER)],
                cwd=str(APP_DIR),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=90,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            append_log(f"刷新标准化目录视图时出现提示：{exc}")

    def _set_catalog_runtime(self, **values: Any) -> None:
        with self.lock:
            self.catalog_runtime.update(values)
            self.catalog_runtime["updated_at"] = now_text()

    def stop_catalog_scan(self) -> tuple[bool, str]:
        with self.lock:
            process = self.catalog_scan_process
            running = bool(self.catalog_runtime.get("running"))
        if not running:
            return False, "当前没有进行中的全省目录扫描。"
        self.cancel_event.set()
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        self._set_catalog_runtime(running=False, phase="stopped", message="全省目录扫描已停止。", finished_at=now_text())
        return True, "已发出停止扫描请求。"

    def stop_active_scan(self) -> tuple[bool, str]:
        with self.lock:
            catalog_running = bool(self.catalog_runtime.get("running"))
        return self.stop_catalog_scan() if catalog_running else self.stop_scan()

    def start_catalog_download(self, urls: list[str] | None = None, province: str = "", all_missing: bool = True, network: str = "") -> tuple[bool, str]:
        if requests is None:
            return False, "缺少 requests 组件，请先执行：python -m pip install requests"
        snapshot = self.catalog_snapshot(include_items=True)
        all_items = [item for group in snapshot.get("provinces", []) for item in group.get("items", [])]
        wanted = {str(url) for url in (urls or []) if str(url)}
        if wanted:
            entries = [item for item in all_items if item.get("url") in wanted]
        else:
            entries = [item for item in all_items if (not province or province in str(item.get("province", ""))) and (not network or str(item.get("network") or "sgcc").lower() == network.lower()) and item.get("local_status") != "downloaded"]
            if not all_missing and not entries:
                return False, "没有选择待下载资料。"
        if not entries:
            return False, "没有找到可下载的资料。"
        with self.lock:
            if self.catalog_download_thread and self.catalog_download_thread.is_alive():
                return False, "目录下载任务正在进行中。"
            self.catalog_runtime.update(
                {
                    "phase": "downloading",
                    "message": f"准备下载 {len(entries)} 个资料…",
                    "download_running": True,
                    "download_total": len(entries),
                    "download_completed": 0,
                    "updated_at": now_text(),
                }
            )
            self.catalog_download_thread = threading.Thread(target=self._catalog_download_worker, args=(entries,), name="95598-catalog-download", daemon=True)
            self.catalog_download_thread.start()
        return True, f"已开始下载 {len(entries)} 个资料。"

    def start_catalog_download_item(self, url: str) -> tuple[bool, str]:
        return self.start_catalog_download([url], all_missing=False)

    def _catalog_download_worker(self, entries: list[dict[str, Any]]) -> None:
        assert requests is not None
        CATALOG_DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
        CSG_DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
        session = requests.Session()
        session.headers.update({"User-Agent": self.browser_user_agent or "Mozilla/5.0", "Referer": PAGE_URL, "Accept": "*/*"})
        for cookie in self.browser_cookies:
            try:
                session.cookies.set(cookie.get("name", ""), cookie.get("value", ""), domain=cookie.get("domain") or None, path=cookie.get("path") or "/")
            except Exception:
                continue
        reports = self._catalog_report_map()
        success = failed = 0
        csg_entries = [
            item for item in entries
            if (str(item.get("network") or "").lower() == "csg" or urlparse(str(item.get("url") or "")).hostname == "95598.csg.cn")
            and str(item.get("type") or item.get("format") or "").lower() == "html"
        ]
        sgcc_entries = [item for item in entries if item not in csg_entries]
        if csg_entries:
            try:
                from csg_proxy_power_downloader import fetch_html_documents

                def csg_progress(index: int, total: int, record: dict[str, Any]) -> None:
                    nonlocal success, failed
                    title = str(record.get("title") or "南网代理购电公告")
                    if record.get("status") == "downloaded":
                        success += 1
                    else:
                        failed += 1
                    reports[str(record.get("url") or "")] = record
                    self._write_catalog_reports(reports)
                    self._set_catalog_runtime(
                        message=f"正在保存南网网页 {index}/{total}：{title}",
                        current_item=title,
                        download_completed=index,
                    )

                fetch_html_documents(
                    csg_entries,
                    output_root=CSG_DOWNLOAD_ROOT,
                    headless=False,
                    executable_path=find_edge(),
                    progress=csg_progress,
                )
            except Exception as exc:
                append_log(f"南网网页批量保存失败：{exc}")
                for item in csg_entries:
                    url = str(item.get("url") or "")
                    record = {
                        "network": "csg",
                        "province": item.get("province", ""),
                        "title": item.get("title", ""),
                        "url": url,
                        "type": "html",
                        "status": "failed",
                        "file": "",
                        "bytes": 0,
                        "time": now_text(),
                        "error": str(exc),
                    }
                    reports[url] = record
                    failed += 1
                self._write_catalog_reports(reports)
        offset = len(csg_entries)
        for relative_index, item in enumerate(sgcc_entries, 1):
            index = offset + relative_index
            title = str(item.get("title") or "代理购电资料")
            url = str(item.get("url") or "")
            if urlparse(url).hostname == "95598.csg.cn":
                session.headers["Referer"] = "https://95598.csg.cn/"
            else:
                session.headers["Referer"] = PAGE_URL
            self._set_catalog_runtime(message=f"正在下载 {index}/{len(entries)}：{title}", current_item=title, download_completed=index - 1)
            network = str(item.get("network") or ("csg" if urlparse(url).hostname == "95598.csg.cn" else "sgcc")).lower()
            record: dict[str, Any] = {"network": network, "province": item.get("province", ""), "title": title, "url": url, "is_pdf": item.get("type") == "pdf"}
            try:
                response = session.get(url, timeout=90, allow_redirects=True)
                response.raise_for_status()
                data = response.content
                content_type = response.headers.get("content-type", "")
                expected_html = str(item.get("type") or item.get("format") or "").lower() == "html" or str(item.get("network") or "").lower() == "csg"
                if not data:
                    raise RuntimeError("服务器返回空内容")
                if _is_probably_html(data, content_type) and not expected_html:
                    raise RuntimeError("服务器返回网页，不是资料文件")
                ext = _catalog_extension(url, content_type, data)
                if item.get("type") == "pdf" and not data.startswith(b"%PDF"):
                    raise RuntimeError("服务器返回的内容不是 PDF")
                root = CSG_DOWNLOAD_ROOT if network == "csg" else CATALOG_DOWNLOAD_ROOT
                province_dir = root / safe_filename(str(item.get("province") or "未标注省份")).rsplit(".", 1)[0]
                province_dir.mkdir(parents=True, exist_ok=True)
                base = safe_filename(title).rsplit(".", 1)[0]
                target = province_dir / f"{base}{ext}"
                old = reports.get(url, {})
                old_path = self._catalog_local_path(old.get("file") or old.get("path"))
                if old_path and old_path.is_file():
                    target = old_path
                elif target.exists():
                    suffix = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
                    target = target.with_name(f"{target.stem}_{suffix}{target.suffix}")
                temporary = target.with_name(target.name + ".part")
                temporary.write_bytes(data)
                temporary.replace(target)
                relative = str(target.relative_to(APP_DIR))
                record.update({"status": "downloaded", "file": relative, "bytes": len(data), "time": now_text(), "type": item.get("type") or _catalog_type(url, item)})
                success += 1
                append_log(f"目录资料下载完成：{relative}")
            except Exception as exc:
                record.update({"status": "failed", "file": "", "bytes": 0, "time": now_text(), "error": str(exc), "type": item.get("type") or _catalog_type(url, item)})
                failed += 1
                append_log(f"目录资料下载失败：{title}：{exc}")
            reports[url] = record
            self._write_catalog_reports(reports)
        self._set_catalog_runtime(
            running=False,
            download_running=False,
            phase="complete" if not failed else "download_complete",
            message=f"目录下载完成：成功 {success} 个，失败 {failed} 个。",
            current_item="",
            download_completed=len(entries),
        )
        self._refresh_catalog_view_file()

    @staticmethod
    def _write_catalog_reports(reports: dict[str, dict[str, Any]]) -> None:
        payload = {"finished_at": now_text(), "items": sorted(reports.values(), key=lambda x: (str(x.get("province", "")), str(x.get("title", "")), str(x.get("url", ""))))}
        temporary = CATALOG_REPORT_FILE.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(CATALOG_REPORT_FILE)

    def _check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise ScanCancelled("用户停止扫描")

    def _page_has_validation(self, page) -> bool:
        signals = ("拖动下方拼图", "安全验证", "请完成验证", "验证错误", "滑块验证")
        # 只把“可见”的提示判定为验证码。挑战完成后 DOM 中可能仍保留
        # 隐藏的提示文字，直接搜索 body 文本会把扫描永久卡在等待状态。
        frames = list(getattr(page, "frames", [])) or [page]
        for frame in frames:
            for signal in signals:
                try:
                    locator = frame.get_by_text(signal, exact=False)
                    for index in range(min(locator.count(), 20)):
                        if locator.nth(index).is_visible():
                            return True
                except Exception:
                    continue
            try:
                # 某些验证框只有 iframe URL 标记，没有可读文本。
                frame_url = (frame.url or "").lower()
                if any(token in frame_url for token in ("captcha", "verify", "security")):
                    body = frame.locator("body")
                    if body.count() and body.is_visible():
                        box = body.bounding_box()
                        if box and box.get("width", 0) > 80 and box.get("height", 40) > 40:
                            return True
            except Exception:
                continue
        return False

    def _wait_for_page_content(self, page, timeout_ms: int = 10000) -> None:
        """等待地区弹窗/代理购电栏目等异步内容出现，同时及时停在验证页。"""
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            self._check_cancelled()
            if self._page_has_validation(page):
                self._wait_if_validation(page, "打开页面")
                # 验证通过后，站点通常还需要几秒重新渲染。
                deadline = max(deadline, time.monotonic() + 5)
            try:
                body = page.locator("body").inner_text(timeout=1200)
            except Exception:
                body = ""
            if any(token in body for token in ("请选择您所在地区", "代理购电", "电价标准")):
                return
            page.wait_for_timeout(300)

    def _wait_if_validation(self, page, stage: str) -> None:
        if not self._page_has_validation(page):
            return
        self.continue_event.clear()
        self._set_runtime(
            phase="waiting_validation",
            message=f"{stage}时页面要求人工验证。请在 Edge 完成验证；控制台会自动检测，也可点击“验证完成，继续”。",
            needs_validation=True,
        )
        try:
            page.bring_to_front()
        except Exception:
            pass
        activate_edge_window(self.browser_pid)
        while True:
            self._check_cancelled()
            # 自动检测验证页面是否消失，同时允许按钮触发立即复查。
            self.continue_event.wait(0.8)
            self.continue_event.clear()
            if not self._page_has_validation(page):
                break
            self._set_runtime(
                phase="waiting_validation",
                message="仍检测到验证页面。请在受控 Edge 中完成后再继续。",
                needs_validation=True,
            )
        self._set_runtime(
            phase="resuming",
            message="验证页面已通过，正在继续扫描…",
            needs_validation=False,
        )

    def _launch_controlled_edge(self, edge_path: Path, debug_port: int) -> subprocess.Popen:
        # 每次扫描使用独立 profile。Edge 只允许一个进程持有同一 profile，
        # 固定目录会让第二次启动把 URL 转交给旧窗口，新的 CDP 端口随即退出。
        PROFILE_BASE_DIR.mkdir(parents=True, exist_ok=True)
        profile_dir = PROFILE_BASE_DIR / f"run-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(4)}"
        profile_dir.mkdir(parents=True, exist_ok=True)
        self.browser_profile = profile_dir
        command = [
            str(edge_path),
            f"--remote-debugging-port={debug_port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={profile_dir}",
            "--new-window",
            "--start-maximized",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-popup-blocking",
            "--disable-session-crashed-bubble",
            PAGE_URL,
        ]
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return subprocess.Popen(command, cwd=str(APP_DIR), creationflags=flags)

    def _wait_for_cdp(self, port: int, process: subprocess.Popen, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        endpoint = f"http://127.0.0.1:{port}/json/version"
        last_error = ""
        while time.monotonic() < deadline:
            self._check_cancelled()
            if process.poll() is not None:
                raise RuntimeError("Edge 在控制通道建立前退出。请关闭旧的“95598”受控窗口后重试。")
            try:
                with urllib.request.urlopen(endpoint, timeout=1) as response:
                    if response.status == 200:
                        return
            except (OSError, urllib.error.URLError) as exc:
                last_error = str(exc)
            time.sleep(0.25)
        raise RuntimeError(f"Edge 已打开，但控制通道未在 {int(timeout)} 秒内就绪。{last_error}")

    def _select_beijing(self, page) -> None:
        self._set_runtime(phase="selecting_region", message="正在确认地区：北京…")
        self._wait_for_page_content(page, timeout_ms=10000)
        self._wait_if_validation(page, "选择地区")

        try:
            body_text = page.locator("body").inner_text(timeout=4000)
        except Exception:
            body_text = ""

        # 首次访问通常默认显示北京的地区弹窗，只需选择市辖区和一个区。
        # 如果已经保存过北京地区，则这些元素不会出现，直接继续即可。
        if "请选择您所在地区" in body_text:
            city = _visible_exact(page, "市辖区")
            if city is not None:
                city.click(timeout=4000)
                page.wait_for_timeout(700)
                self._wait_if_validation(page, "选择北京市")
            district = _visible_exact(page, "东城区")
            if district is not None:
                district.click(timeout=4000)
                page.wait_for_timeout(1200)
                self._wait_if_validation(page, "选择东城区")

    def _open_proxy_category(self, page) -> None:
        self._set_runtime(phase="opening_category", message="正在打开“代理购电”栏目…")
        self._wait_if_validation(page, "打开代理购电栏目")
        exact_names = (
            "工商业用户代理购电",
            "工商业代理购电",
            "代理购电",
        )
        for name in exact_names:
            category = _visible_exact(page, name)
            if category is not None:
                try:
                    category.click(timeout=4000)
                    page.wait_for_timeout(1200)
                except Exception:
                    pass
                return

        # 页面脚本可能尚未完成异步渲染；短暂轮询可避免把“尚未出现”误报为
        # “栏目不存在”，也给验证组件足够时间显示出来。
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            self._check_cancelled()
            self._wait_if_validation(page, "打开代理购电栏目")
            for name in exact_names:
                category = _visible_exact(page, name)
                if category is not None:
                    category.click(timeout=4000)
                    page.wait_for_timeout(1200)
                    return
            page.wait_for_timeout(400)

        # 各省名称可能不同，选择短文本且不是具体月份文件的“代理购电”项。
        locator = page.get_by_text(re.compile("代理购电"))
        options: list[tuple[int, Any]] = []
        for index in range(min(locator.count(), 100)):
            item = locator.nth(index)
            try:
                text = " ".join(item.inner_text(timeout=1000).split())
                if item.is_visible() and 2 <= len(text) <= 45 and not re.search(r"20\d{2}年\d{1,2}月", text):
                    options.append((len(text), item))
            except Exception:
                continue
        if not options:
            raise RuntimeError("页面中没有找到名称包含“代理购电”的栏目。")
        options.sort(key=lambda pair: pair[0])
        options[0][1].click(timeout=4000)
        page.wait_for_timeout(1200)

    @staticmethod
    def _looks_like_document_title(text: str) -> bool:
        normalized = " ".join(text.split())
        return (
            "代理购电" in normalized
            and bool(re.search(r"20\d{2}年\s*\d{1,2}月", normalized))
            and len(normalized) <= 180
        )

    def _collect_titles(self, page) -> list[str]:
        titles: list[str] = []
        seen: set[str] = set()
        for selector in ("tr", "li", "a", "[role='row']"):
            locator = page.locator(selector)
            try:
                count = min(locator.count(), 1000)
            except Exception:
                continue
            for index in range(count):
                try:
                    text = " ".join(locator.nth(index).inner_text(timeout=800).split())
                except Exception:
                    continue
                if self._looks_like_document_title(text) and text not in seen:
                    seen.add(text)
                    titles.append(text)
        return titles

    def _discover_items(self, page, context) -> list[dict[str, str]]:
        found: dict[str, dict[str, str]] = {}
        active_title = {"value": ""}

        def record(url: str, title: str = "") -> None:
            if not url or not url.lower().startswith(("http://", "https://")):
                return
            found.setdefault(
                url,
                {
                    "title": title or active_title["value"] or Path(urlparse(url).path).stem,
                    "url": url,
                },
            )

        def capture_response(response) -> None:
            try:
                content_type = response.headers.get("content-type", "").lower()
                if _is_pdf_url(response.url) or "application/pdf" in content_type:
                    record(response.url)
            except Exception:
                return

        def capture_download(download) -> None:
            try:
                record(download.url, download.suggested_filename.rsplit(".", 1)[0])
                download.cancel()
            except Exception:
                return

        page.on("response", capture_response)
        page.on("download", capture_download)

        titles = self._collect_titles(page)
        if not titles:
            page.locator("body").evaluate("element => element.scrollTo(0, element.scrollHeight)")
            page.wait_for_timeout(1000)
            titles = self._collect_titles(page)
        if not titles:
            raise RuntimeError("已打开代理购电栏目，但没有读取到带月份的文件条目。")

        self._set_runtime(
            phase="scanning",
            message=f"已识别 {len(titles)} 个月份条目，正在提取 PDF 地址…",
        )

        # 优先读取页面上已有的 href，无需打开新标签页。
        anchors = page.locator("a")
        for index in range(min(anchors.count(), 1500)):
            anchor = anchors.nth(index)
            try:
                text = " ".join(anchor.inner_text(timeout=700).split())
                href = anchor.get_attribute("href") or ""
            except Exception:
                continue
            if self._looks_like_document_title(text) and href:
                absolute = urljoin(page.url, href)
                if _is_pdf_url(absolute):
                    record(absolute, text)

        # 对没有直接 href 的条目逐一点击，从响应、新页面或下载事件中捕获地址。
        for index, title in enumerate(titles, 1):
            self._check_cancelled()
            if any(item.get("title") == title for item in found.values()):
                continue
            active_title["value"] = title
            self._set_runtime(
                phase="scanning",
                message=f"正在读取 PDF 地址 {index}/{len(titles)}",
                current_item=title,
            )
            target = page.get_by_text(title, exact=True)
            visible_target = None
            for target_index in range(min(target.count(), 10)):
                try:
                    if target.nth(target_index).is_visible():
                        visible_target = target.nth(target_index)
                        break
                except Exception:
                    continue
            if visible_target is None:
                continue

            old_pages = list(context.pages)
            base_url = page.url
            try:
                visible_target.scroll_into_view_if_needed(timeout=2000)
                visible_target.click(timeout=3500)
                page.wait_for_timeout(650)
            except PlaywrightTimeoutError:
                continue
            except Exception:
                continue

            for opened in list(context.pages):
                if opened in old_pages or opened is page:
                    continue
                try:
                    opened.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    pass
                opened_url = opened.url
                if _is_pdf_url(opened_url):
                    record(opened_url, title)
                try:
                    opened.close()
                except Exception:
                    pass

            # 少数页面会在当前标签导航到 PDF，提取后回退到列表。
            if page.url != base_url:
                if _is_pdf_url(page.url):
                    record(page.url, title)
                try:
                    page.go_back(wait_until="domcontentloaded", timeout=10000)
                    page.wait_for_timeout(700)
                except Exception:
                    page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=30000)
                    self._select_beijing(page)
                    self._open_proxy_category(page)

            self._wait_if_validation(page, "读取 PDF 地址")

        active_title["value"] = ""
        if not found:
            raise RuntimeError("识别到了月份条目，但没有捕获到 PDF 地址。可在受控 Edge 中确认任一月份是否能正常打开。")

        # 尽量保持页面月份顺序；未能按标题对应的响应排在末尾。
        ordered: list[dict[str, str]] = []
        used: set[str] = set()
        for title in titles:
            for url, item in found.items():
                if url not in used and item.get("title") == title:
                    ordered.append(item)
                    used.add(url)
                    break
        ordered.extend(item for url, item in found.items() if url not in used)
        return ordered

    def _scan_worker(self) -> None:
        browser = None
        process = None
        try:
            if sync_playwright is None:
                raise RuntimeError("缺少 playwright 组件，请先执行：python -m pip install playwright")
            edge_path = find_edge()
            debug_port = choose_free_port()
            self._set_runtime(phase="starting_browser", message="正在启动可见 Edge…", browser_open=False)
            process = self._launch_controlled_edge(edge_path, debug_port)
            with self.lock:
                self.browser_process = process
                self.browser_pid = process.pid
            self._wait_for_cdp(debug_port, process)
            self._set_runtime(
                phase="connecting_browser",
                message="Edge 已打开，正在连接页面…",
                browser_open=True,
            )
            activate_edge_window(process.pid)

            with sync_playwright() as playwright:
                browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{debug_port}")
                if not browser.contexts:
                    raise RuntimeError("Edge 已启动，但没有取得浏览器上下文。")
                context = browser.contexts[0]
                pages = context.pages
                page = next((candidate for candidate in pages if "95598.cn" in candidate.url), None)
                page = page or (pages[0] if pages else context.new_page())
                page.bring_to_front()
                if PAGE_URL not in page.url:
                    self._set_runtime(phase="loading_page", message="正在打开 95598 电价标准页面…")
                    page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=30000)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    pass
                page.wait_for_timeout(1200)
                self._wait_if_validation(page, "打开页面")
                self._select_beijing(page)
                self._open_proxy_category(page)
                self._wait_if_validation(page, "展开栏目")
                items = self._discover_items(page, context)

                try:
                    self.browser_cookies = context.cookies()
                    self.browser_user_agent = page.evaluate("navigator.userAgent") or "Mozilla/5.0"
                except Exception:
                    pass

                with self.lock:
                    previous_downloads = self.data.setdefault("downloaded", {})
                    self.data["items"] = [
                        {
                            "title": item["title"],
                            "url": item["url"],
                            "province": "北京",
                            "discovered_at": now_text(),
                        }
                        for item in items
                    ]
                    self.data["downloaded"] = previous_downloads
                    self.data["last_scan"] = now_text()
                    save_data(self.data)
                append_log(f"扫描完成：读取到 {len(items)} 个 PDF 地址")
                self._set_runtime(
                    phase="complete",
                    message=f"扫描完成：读取到 {len(items)} 个 PDF 地址。",
                    current_item="",
                )
        except ScanCancelled:
            append_log("扫描已由用户停止")
            self._set_runtime(phase="stopped", message="扫描已停止。", current_item="")
        except Exception as exc:
            append_log(f"扫描失败：{exc}")
            self._set_runtime(
                phase="error",
                message=f"扫描未完成：{exc}",
                last_error=str(exc),
                current_item="",
            )
        finally:
            self._set_runtime(scan_running=False, needs_validation=False)
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
            self._terminate_controlled_edge(process)
            with self.lock:
                self.browser_process = None
                self.browser_pid = None
                self.browser_profile = None
            self._set_runtime(browser_open=False)

    def _terminate_controlled_edge(self, process: subprocess.Popen | None) -> None:
        """结束本次启动的 Edge 进程树，不触碰用户的其他 Edge 窗口。

        Edge 在 Windows 上可能把最初的 Popen 进程替换成带有 renderer 的
        子进程；因此除了 Popen PID，还按本次随机 profile 目录查询进程。
        """
        profile = self.browser_profile
        if os.name == "nt" and profile is not None:
            try:
                # 仅查询命令行中包含本次随机目录的进程，路径由本程序生成。
                needle = str(profile).replace("'", "''")
                ps = (
                    "$needle='" + needle + "'; "
                    "$p=Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" "
                    "| Where-Object { $_.CommandLine -like ('*' + $needle + '*') } "
                    "| Select-Object -ExpandProperty ProcessId; $p"
                )
                query = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps],
                    capture_output=True,
                    text=True,
                    timeout=8,
                    check=False,
                )
                pids = []
                for line in query.stdout.splitlines():
                    try:
                        pids.append(int(line.strip()))
                    except ValueError:
                        continue
                if process is not None and process.poll() is None:
                    pids.append(process.pid)
                for pid in sorted(set(pids)):
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        capture_output=True,
                        timeout=8,
                        check=False,
                    )
                return
            except Exception as exc:
                append_log(f"结束受控 Edge 进程树时出现提示：{exc}")
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                pass

    def _download_worker(self, entries: list[dict[str, Any]]) -> None:
        assert requests is not None
        PDF_DIR.mkdir(parents=True, exist_ok=True)
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": self.browser_user_agent or "Mozilla/5.0",
                "Referer": PAGE_URL,
                "Accept": "application/pdf,*/*;q=0.8",
            }
        )
        for cookie in self.browser_cookies:
            try:
                session.cookies.set(
                    cookie.get("name", ""),
                    cookie.get("value", ""),
                    domain=cookie.get("domain") or None,
                    path=cookie.get("path") or "/",
                )
            except Exception:
                continue

        succeeded = 0
        failed = 0
        for index, item in enumerate(entries, 1):
            title = item.get("title", "代理购电价格公示表")
            url = item.get("url", "")
            self._set_runtime(
                phase="downloading",
                message=f"正在下载 {index}/{len(entries)}：{title}",
                current_item=title,
            )
            try:
                response = session.get(url, timeout=60, allow_redirects=True)
                response.raise_for_status()
                if not response.content.startswith(b"%PDF"):
                    raise RuntimeError("服务器返回的内容不是 PDF")
                target = PDF_DIR / safe_filename(title, url)
                # 标题碰撞时，用 URL 的短摘要区分，避免覆盖不同文件。
                with self.lock:
                    other_url_same_path = any(
                        key != url and record.get("path") == target.name
                        for key, record in self.data.get("downloaded", {}).items()
                    )
                if other_url_same_path:
                    suffix = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
                    target = target.with_name(f"{target.stem}_{suffix}.pdf")
                temporary = target.with_suffix(".pdf.part")
                temporary.write_bytes(response.content)
                temporary.replace(target)
                with self.lock:
                    self.data.setdefault("downloaded", {})[url] = {
                        "path": target.name,
                        "time": now_text(),
                    }
                    save_data(self.data)
                append_log(f"下载完成：{target.name}")
                succeeded += 1
            except Exception as exc:
                with self.lock:
                    self.data.setdefault("downloaded", {})[url] = {
                        "path": "",
                        "time": now_text(),
                        "error": str(exc),
                    }
                    save_data(self.data)
                append_log(f"下载失败：{title}：{exc}")
                failed += 1

        self._set_runtime(
            phase="complete" if not failed else "download_complete",
            message=f"下载任务完成：成功 {succeeded} 个，失败 {failed} 个。",
            download_running=False,
            current_item="",
        )


class ConsoleHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, controller: ScannerController, token: str):
        super().__init__(address, handler)
        self.controller = controller
        self.control_token = token


class ConsoleHandler(BaseHTTPRequestHandler):
    server: ConsoleHTTPServer

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _send_bytes(self, payload: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def _send_download(self, payload: bytes, filename: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(filename)}")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        self._send_bytes(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
        )

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-95598-Token", "")
        return hmac.compare_digest(supplied, self.server.control_token)

    def _require_authorized(self) -> bool:
        if self._authorized():
            return True
        self._send_json({"ok": False, "message": "本地控制令牌无效，请刷新控制台页面。"}, HTTPStatus.FORBIDDEN)
        return False

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 命名约定
        parsed_url = urlparse(self.path)
        path = unquote(parsed_url.path)
        query = parse_qs(parsed_url.query)
        if path == "/api/health":
            host, port = self.server.server_address
            self._send_json(
                {
                    "ok": True,
                    "app": APP_ID,
                    "console_url": f"http://127.0.0.1:{port}/console.html",
                }
            )
            return
        if path in ("/", "/console.html"):
            try:
                html = CONSOLE_FILE.read_text(encoding="utf-8")
            except OSError as exc:
                self._send_json({"ok": False, "message": f"读取 console.html 失败：{exc}"}, 500)
                return
            html = html.replace("__CONTROL_TOKEN__", self.server.control_token)
            self._send_bytes(html.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/state":
            if self._require_authorized():
                self._send_json(self.server.controller.snapshot())
            return
        if path in {"/api/catalog", "/api/provinces"}:
            if not self._require_authorized():
                return
            province = str((query.get("province") or [""])[0])
            status = str((query.get("status") or [""])[0])
            kind = str((query.get("type") or [""])[0])
            include_items = path == "/api/catalog"
            self._send_json(
                self.server.controller.catalog_snapshot(
                    province_filter=province,
                    status_filter=status,
                    type_filter=kind,
                    include_items=include_items,
                )
            )
            return
        if path.startswith("/pdf/"):
            # Serve only files under either managed download root.  The route
            # name is retained for UI compatibility, but images are supported.
            relative = path[len("/pdf/") :]
            target = (APP_DIR / relative).resolve()
            allowed = False
            for root in (CATALOG_DOWNLOAD_ROOT.resolve(), CSG_DOWNLOAD_ROOT.resolve(), PDF_DIR.resolve()):
                try:
                    target.relative_to(root)
                    allowed = True
                    break
                except ValueError:
                    continue
            # Old Beijing entries store only a basename relative to ``pdf``.
            if not allowed and len(Path(relative).parts) == 1:
                target = (PDF_DIR / relative).resolve()
                try:
                    target.relative_to(PDF_DIR.resolve())
                    allowed = True
                except ValueError:
                    pass
            if not allowed:
                self._send_json({"ok": False, "message": "文件路径无效。"}, 400)
                return
            if not target.is_file():
                self._send_json({"ok": False, "message": "文件不存在。"}, 404)
                return
            content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            self._send_bytes(target.read_bytes(), content_type)
            return
        self._send_json({"ok": False, "message": "页面不存在。"}, 404)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 命名约定
        if not self._require_authorized():
            return
        path = urlparse(self.path).path
        payload: dict[str, Any] = {}
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(min(length, 2 * 1024 * 1024)) if length else b"{}"
            parsed = json.loads(raw.decode("utf-8"))
            payload = parsed if isinstance(parsed, dict) else {}
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        if path == "/api/catalog/export":
            try:
                urls = [str(value) for value in payload.get("urls", [])] if isinstance(payload.get("urls"), list) else []
                body, filename, _count = self.server.controller.export_catalog_items(urls)
                self._send_download(body, filename)
            except ValueError as exc:
                self._send_json({"ok": False, "message": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                append_log(f"导出所选资料失败：{exc}")
                self._send_json({"ok": False, "message": f"导出失败：{exc}"}, 500)
            return
        actions = {
            "/api/scan/start": self.server.controller.start_scan,
            "/api/scan/continue": self.server.controller.continue_after_validation,
            "/api/scan/stop": self.server.controller.stop_active_scan,
            "/api/browser/show": self.server.controller.show_browser,
            "/api/download/start": self.server.controller.download_missing,
            "/api/download/open-folder": self.server.controller.open_download_folder,
            "/api/catalog/refresh-status": self.server.controller.catalog_refresh_status,
            "/api/catalog/scan": lambda: self.server.controller.start_catalog_scan(str(payload.get("province") or "")),
            "/api/catalog/scan/stop": self.server.controller.stop_catalog_scan,
            "/api/catalog/download": lambda: self.server.controller.start_catalog_download(
                [str(value) for value in payload.get("urls", [])] if isinstance(payload.get("urls"), list) else None,
                str(payload.get("province") or ""),
                bool(payload.get("all_missing", True)),
                str(payload.get("network") or ""),
            ),
            "/api/catalog/download/item": lambda: self.server.controller.start_catalog_download_item(str(payload.get("url", ""))),
        }
        action = actions.get(path)
        if path == "/api/download/item":
            action = lambda: self.server.controller.download_item(str(payload.get("url", "")))
        if action is None:
            self._send_json({"ok": False, "message": "操作不存在。"}, 404)
            return
        try:
            ok, message = action()
            self._send_json({"ok": ok, "message": message})
        except Exception as exc:
            append_log(f"控制台操作失败 {path}：{exc}")
            self._send_json({"ok": False, "message": f"操作失败：{exc}"}, 500)


def find_existing_console() -> str | None:
    for port in range(DEFAULT_PORT, DEFAULT_PORT + MAX_PORT_TRIES):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.25) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("app") == APP_ID:
                return payload.get("console_url")
        except Exception:
            continue
    return None


def make_server(controller: ScannerController, preferred_port: int) -> ConsoleHTTPServer:
    token = secrets.token_urlsafe(24)
    for port in range(preferred_port, preferred_port + MAX_PORT_TRIES):
        try:
            return ConsoleHTTPServer(("127.0.0.1", port), ConsoleHandler, controller, token)
        except OSError:
            continue
    raise RuntimeError(f"端口 {preferred_port} 到 {preferred_port + MAX_PORT_TRIES - 1} 均被占用。")


def open_console(url: str) -> None:
    try:
        edge = find_edge()
        subprocess.Popen(
            [str(edge), "--new-window", url],
            cwd=str(APP_DIR),
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except Exception:
        webbrowser.open_new_tab(url)


def main() -> None:
    parser = argparse.ArgumentParser(description="95598 代理购电 PDF 交互式网页控制台")
    parser.add_argument("--no-open", action="store_true", help="启动服务但不自动打开浏览器")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="首选本地端口，默认 8765")
    args = parser.parse_args()

    existing = find_existing_console()
    if existing:
        URL_FILE.write_text(existing, encoding="utf-8")
        print(f"控制台已在运行：{existing}")
        if not args.no_open:
            open_console(existing)
        return

    controller = ScannerController()
    server = make_server(controller, args.port)
    port = int(server.server_address[1])
    console_url = f"http://127.0.0.1:{port}/console.html"
    URL_FILE.write_text(console_url, encoding="utf-8")
    print(f"95598 控制台：{console_url}")
    print("保持此窗口运行；关闭窗口会停止本地控制台服务。")
    append_log(f"本地控制台启动：{console_url}")
    if not args.no_open:
        threading.Timer(0.5, open_console, args=(console_url,)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\n正在关闭控制台…")
    finally:
        controller.stop_active_scan()
        server.server_close()


if __name__ == "__main__":
    main()
