from __future__ import annotations

import logging
import time
from itertools import chain
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from .catalog import expected_relative_path, valid_file
from .config import Settings


LOGGER = logging.getLogger(__name__)
ALLOWED_HOSTS = {"95598.cn", "www.95598.cn", "95598.csg.cn"}
REDIRECT_CODES = {301, 302, 303, 307, 308}


def validate_download_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
        raise ValueError("附件地址不属于允许的 95598 HTTPS 域名")
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("附件地址包含不允许的认证信息或端口")


def infer_extension(url: str, content_type: str, head: bytes) -> str:
    ext = Path(urlparse(url).path).suffix.lower().lstrip(".")
    if ext in {"html", "htm", "pdf", "png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff", "svg"}:
        return ext
    ctype = content_type.split(";", 1)[0].strip().lower()
    by_type = {
        "text/html": "html",
        "application/xhtml+xml": "html",
        "application/pdf": "pdf",
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/gif": "gif",
        "image/webp": "webp",
        "image/bmp": "bmp",
        "image/tiff": "tif",
        "image/svg+xml": "svg",
    }
    if ctype in by_type:
        return by_type[ctype]
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"\x89PNG"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    return "bin"


class Downloader:
    def __init__(self, settings: Settings, store) -> None:
        self.settings = settings
        self.store = store

    def _request(self, client: httpx.Client, url: str) -> tuple[httpx.Response, str]:
        current = url
        for _ in range(6):
            validate_download_url(current)
            request = client.build_request("GET", current)
            response = client.send(request, stream=True, follow_redirects=False)
            if response.status_code in REDIRECT_CODES:
                location = response.headers.get("location")
                response.close()
                if not location:
                    raise RuntimeError("附件重定向缺少 Location")
                current = urljoin(current, location)
                continue
            response.raise_for_status()
            return response, current
        raise RuntimeError("附件重定向次数过多")

    def download_item(self, item: dict[str, Any]) -> dict[str, Any]:
        url = str(item.get("url") or "")
        title = str(item.get("title") or "未命名资料")
        province = str(item.get("province") or "未知省份")
        validate_download_url(url)
        timeout = httpx.Timeout(self.settings.request_timeout_seconds)
        max_bytes = self.settings.max_download_mb * 1024 * 1024
        last_error = ""
        for attempt in range(3):
            temporary: Path | None = None
            response: httpx.Response | None = None
            try:
                referer = "https://95598.csg.cn/" if str(item.get("network") or "").lower() == "csg" else "https://95598.cn/"
                with httpx.Client(
                    timeout=timeout,
                    headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/132 Safari/537.36", "Referer": referer},
                    proxy=self.settings.browser_proxy or None,
                ) as client:
                    response, final_url = self._request(client, url)
                    declared = int(response.headers.get("content-length", "0") or 0)
                    if declared > max_bytes:
                        raise RuntimeError(f"附件超过 {self.settings.max_download_mb} MB 限制")
                    chunks = response.iter_bytes(1024 * 128)
                    first = next(chunks, b"")
                    if not first:
                        raise RuntimeError("附件响应为空")
                    expected_html = str(item.get("format") or item.get("type") or "").lower() in {"html", "htm"} or str(item.get("network") or "").lower() == "csg"
                    if first.lstrip().lower().startswith((b"<!doctype html", b"<html", b"<head", b"<body")) and not expected_html:
                        raise RuntimeError("附件地址返回网页内容")
                    extension = infer_extension(final_url, response.headers.get("content-type", ""), first[:32])
                    relative = expected_relative_path(province, title, url, extension, str(item.get("network") or ""))
                    target = (self.settings.download_dir / relative).resolve()
                    target.relative_to(self.settings.download_dir.resolve())
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary = target.with_name(target.name + ".part")
                    total = 0
                    with temporary.open("wb") as stream:
                        for chunk in chain((first,), chunks):
                            total += len(chunk)
                            if total > max_bytes:
                                raise RuntimeError(f"附件超过 {self.settings.max_download_mb} MB 限制")
                            stream.write(chunk)
                    fmt = "pdf" if extension == "pdf" else "html" if extension in {"html", "htm"} else "image" if extension in {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff", "svg"} else "other"
                    if not valid_file(temporary, fmt):
                        raise RuntimeError("附件文件签名校验失败")
                    temporary.replace(target)
                    record = {
                        "province": province,
                        "title": title,
                        "url": url,
                        "status": "acquired",
                        "file": relative.as_posix(),
                        "bytes": total,
                        "format": fmt,
                        "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    }
                    self.store.update_download(record)
                    return record
            except Exception as exc:
                last_error = str(exc)
                LOGGER.warning("下载失败（第 %s 次）%s：%s", attempt + 1, url, exc)
                if temporary and temporary.exists():
                    temporary.unlink(missing_ok=True)
                time.sleep(min(2 ** attempt, 4))
            finally:
                if response is not None:
                    response.close()
        record = {
            "province": province,
            "title": title,
            "url": url,
            "status": "failed",
            "file": "",
            "bytes": 0,
            "error": last_error,
            "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        self.store.update_download(record)
        return record
