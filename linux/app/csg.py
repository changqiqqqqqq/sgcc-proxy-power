"""南方电网五省代理购电价格目录扫描与 PDF/图片附件保存。"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Page, sync_playwright

from .config import Settings


CSG_HOME_URL = "https://95598.csg.cn/#/gd/serviceInquire/information/list"
CSG_ORIGIN = "https://95598.csg.cn"
CSG_LIST_ENDPOINT = "/ucs/ma/wt/searchService/queryInformationList"
CSG_DETAIL_ENDPOINT = "/ucs/ma/wt/searchService/queryInfoDetail"
CSG_PROVINCES = (
    ("广东省", "030000", "bcdbe6731d0b4352be37e340ca67d346"),
    ("广西壮族自治区", "040000", "4ead135257dc470ba39d24ab3e8848d6"),
    ("云南省", "050000", "1fc640b14ba44a92975e0db4c0dbfe14"),
    ("贵州省", "060000", "725ba6f7efd54a4787a61508b1b6ec0b"),
    ("海南省", "070000", "00ab16ad011b45dbbbedcdbc18238848"),
)
CSG_PRICE_PROVINCES = (
    ("云南省", "050000", "yn", "b50e80ef887f4561bec8fb96e06f4862"),
    ("广东省", "030000", "gd", "c06263b20bf34df9926edd2a40cb1632"),
    ("贵州省", "060000", "gz", "a8d3146e14ed41d1ae18c8a7547c3cdc"),
    ("海南省", "070000", "hn", "47e77bf71fb74f7fbeefd7df2ece0c90"),
    ("广西壮族自治区", "040000", "gx", "6394da157f8349599e1266a7ac502f3f"),
)
PRICE_TITLE_KEYS = ("infoTitle", "knoweleTitle", "title", "fileName", "documentName", "name", "labelName")
PRICE_URL_KEYS = ("pdfAddress", "downloadUrl", "fileUrl", "attachmentUrl", "knoweleContentUrl", "contentUrl", "link", "url", "src")
PROXY_TITLE_RE = re.compile(r"代理\s*购电")


FETCH_JSON_JS = r"""
async ({endpoint, payload, timeoutMs}) => {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), Number(timeoutMs || 30000));
  try {
    const response = await fetch(endpoint, {
      method: 'POST', credentials: 'include',
      headers: {'Content-Type': 'application/json', 'Accept': 'application/json, text/plain, */*'},
      body: JSON.stringify(payload), signal: controller.signal
    });
    return {status: response.status, text: await response.text()};
  } finally { clearTimeout(timer); }
}
"""

FETCH_HTML_JS = r"""
async ({url, timeoutMs}) => {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), Number(timeoutMs || 60000));
  try {
    const response = await fetch(url, {credentials: 'include', signal: controller.signal});
    return {status: response.status, contentType: response.headers.get('content-type') || '', text: await response.text()};
  } finally { clearTimeout(timer); }
}
"""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def absolute_url(value: Any, base: str = CSG_ORIGIN) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    url = urljoin(base.rstrip("/") + "/", text)
    parsed = urlparse(url)
    if parsed.hostname == urlparse(CSG_ORIGIN).hostname and parsed.path.startswith("/info/"):
        url = CSG_ORIGIN + "/ucs/sr" + parsed.path + (("?" + parsed.query) if parsed.query else "")
    return url


def safe_name(value: str, max_chars: int = 180) -> str:
    return (re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", str(value)).strip(" ._") or "南网代理购电公告")[:max_chars]


def stable_item_id(area_code: str, info_id: str, url: str) -> str:
    return hashlib.sha256(f"csg\0{area_code}\0{info_id}\0{url}".encode()).hexdigest()[:20]


def is_proxy_title(value: Any) -> bool:
    return bool(PROXY_TITLE_RE.search(re.sub(r"\s+", "", str(value or ""))))


def _price_text(row: dict[str, Any]) -> str:
    return next((str(row.get(key)).strip() for key in PRICE_TITLE_KEYS if isinstance(row.get(key), str) and row.get(key).strip()), "")


def _price_url(row: dict[str, Any]) -> str:
    for key in PRICE_URL_KEYS:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            url = absolute_url(value)
            if url.startswith(("http://", "https://")):
                return url
    return ""


def walk_price_records(value: Any, parent_title: str = "") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(value, dict):
        title = _price_text(value) or parent_title
        url = _price_url(value)
        if title and url:
            rows.append({**value, "_title": title, "_url": url})
        for child in value.values():
            rows.extend(walk_price_records(child, title))
    elif isinstance(value, list):
        for child in value:
            rows.extend(walk_price_records(child, parent_title))
    return rows


def html_assets(html: str, base_url: str) -> list[tuple[str, str]]:
    output: list[tuple[str, str]] = []
    for raw in re.findall(r"(?:href|src)\s*=\s*[\"']([^\"']+)", html, re.I):
        url = absolute_url(raw, base_url)
        kind, _ext, _pdf = attachment_type(url, {})
        if url.startswith(CSG_ORIGIN + "/") and kind in {"pdf", "image"}:
            output.append((kind, url))
    return list(dict.fromkeys(output))


def attachment_type(url: str, row: dict[str, Any]) -> tuple[str, str, bool]:
    lower = url.lower()
    if "documenttype=pdf" in lower or Path(url.split("?", 1)[0]).suffix.lower() == ".pdf":
        return "pdf", "pdf", True
    suffix = Path(url.split("?", 1)[0]).suffix.lower().lstrip(".")
    if suffix in {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff", "svg"}:
        return "image", suffix, False
    return "html", "html", False


def _decode_json(value: Any) -> Any:
    if not isinstance(value, dict) or not 200 <= int(value.get("status") or 0) < 300:
        raise RuntimeError(f"南网接口 HTTP {value.get('status') if isinstance(value, dict) else '?'}")
    try:
        return json.loads(str(value.get("text") or ""))
    except json.JSONDecodeError as exc:
        raise RuntimeError("南网接口返回非 JSON") from exc


def _rows(payload: Any) -> tuple[int, list[dict[str, Any]], str]:
    data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else {}
    rows = data.get("infoList") if isinstance(data.get("infoList"), list) else []
    return int(data.get("count") or 0), [x for x in rows if isinstance(x, dict)], str(data.get("className") or "资讯公告")


def _find_link(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("link", "url", "infoUrl", "contentUrl", "htmlUrl"):
            if value.get(key):
                return str(value[key])
        for child in value.values():
            found = _find_link(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_link(child)
            if found:
                return found
    return ""


def normalize_item(row: dict[str, Any], province: str, area_code: str, class_id: str) -> dict[str, Any] | None:
    title = str(row.get("infoTitle") or row.get("title") or "").strip()
    url = absolute_url(row.get("link") or row.get("url"))
    if not title or not url:
        return None
    info_id = str(row.get("infoId") or row.get("id") or "")
    kind, extension, is_pdf = attachment_type(url, row)
    return {
        "item_id": stable_item_id(area_code, info_id, url),
        "title": title,
        "url": url,
        "type": kind,
        "format": kind,
        "extension": extension,
        "is_pdf": is_pdf,
        "downloadable": True,
        "province": province,
        "province_code": area_code,
        "network": "csg",
        "area_code": area_code,
        "info_id": info_id,
        "publish_time": str(row.get("publishTime") or row.get("publish_time") or ""),
        "class_id": str(row.get("classId") or class_id),
        "class_name": str(row.get("className") or "资讯公告"),
        "issued_name": str(row.get("issuedName") or ""),
        "raw": row,
    }


class CSGScanner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.timeout = max(5, int(settings.scan_request_timeout_seconds))
        self.page_size = 100
        self.max_pages = 50

    def _fetch_json(self, page: Page, payload: dict[str, Any], timeout_seconds: float | None = None) -> Any:
        return _decode_json(page.evaluate(FETCH_JSON_JS, {
            "endpoint": CSG_LIST_ENDPOINT,
            "payload": payload,
            "timeoutMs": max(1000, int((timeout_seconds or self.timeout) * 1000)),
        }))

    def _resolve_detail_link(self, page: Page, row: dict[str, Any], area_code: str) -> str:
        existing = absolute_url(row.get("link") or row.get("url"))
        if existing:
            return existing
        info_id = str(row.get("infoId") or row.get("id") or "")
        if not info_id:
            return ""
        try:
            decoded = _decode_json(page.evaluate(FETCH_JSON_JS, {
                "endpoint": CSG_DETAIL_ENDPOINT,
                "payload": {"areaCode": area_code, "infoId": info_id, "version": "cn"},
                "timeoutMs": self.timeout * 1000,
            }))
            return absolute_url(_find_link(decoded))
        except Exception:
            return ""

    def _wait_loaded(self, page: Page, deadline: float | None = None) -> None:
        deadline = deadline or time.monotonic() + self.settings.navigation_timeout_seconds
        while time.monotonic() < deadline:
            text = ""
            try:
                text = page.locator("body").inner_text(timeout=1200)
            except Exception:
                pass
            if any(x in text for x in ("安全威胁", "请求已被阻断", "ERR_CONNECTION_CLOSED")):
                raise RuntimeError("南网页面被上游拦截")
            if "资讯公告" in text or "南网在线" in text:
                return
            page.wait_for_timeout(300)
        raise TimeoutError("南网资讯公告页面加载超时")

    def _price_rows(self, page: Page, area_code: str, page_id: str) -> tuple[int, list[dict[str, Any]], int, list[str]]:
        rows: list[dict[str, Any]] = []
        errors: list[str] = []
        total = 0
        page_count = 0
        page_size = 15
        for page_no in range(1, self.max_pages + 1):
            try:
                payload = {"areaCode": area_code[:2], "level": "1", "classId": page_id, "pageNo": page_no, "pageSize": page_size, "version": "cn"}
                total_now, page_rows, _class_name = _rows(self._fetch_json(page, payload))
                page_count = page_no
                total = max(total, total_now)
                rows.extend(page_rows)
                if not page_rows or (total > 0 and len(rows) >= total) or (total <= 0 and len(page_rows) < page_size):
                    break
            except Exception as exc:
                errors.append(f"第 {page_no} 页：{exc}")
                break
        return total, rows, page_count, errors

    def _fetch_html(self, page: Page, url: str) -> str:
        value = page.evaluate(FETCH_HTML_JS, {"url": url, "timeoutMs": self.settings.request_timeout_seconds * 1000})
        if not isinstance(value, dict) or not 200 <= int(value.get("status") or 0) < 300:
            raise RuntimeError(f"详情获取失败：HTTP {value.get('status') if isinstance(value, dict) else '?'}")
        return str(value.get("text") or "")

    def _price_page(self, page: Page, province: str, area_code: str, route_prefix: str, page_id: str) -> dict[str, Any]:
        entry = f"{CSG_ORIGIN}/#/{route_prefix}/businessPage/baseInfo/{page_id}?cname=%E7%94%B5%E4%BB%B7%E5%8F%8A%E6%94%B6%E8%B4%B9%E6%A0%87%E5%87%86"
        page.goto(entry, wait_until="domcontentloaded", timeout=self.settings.navigation_timeout_seconds * 2000)
        total, captured, page_count, errors = self._price_rows(page, area_code, page_id)
        items: list[dict[str, Any]] = []
        for row in captured:
            title = str(row.get("infoTitle") or row.get("title") or "")
            if not is_proxy_title(title):
                continue
            url = absolute_url(row.get("link") or row.get("url")) or self._resolve_detail_link(page, row, area_code[:2])
            if not url:
                errors.append(f"{title}：未取得附件地址")
                continue
            kind, extension, is_pdf = attachment_type(url, row)
            assets: list[tuple[str, str]] = []
            if kind == "html":
                try:
                    assets = html_assets(self._fetch_html(page, url), url)
                except Exception:
                    assets = []
            if kind == "html" and not assets:
                continue
            for asset_kind, asset_url in assets or [(kind, url)]:
                asset_kind, asset_ext, asset_pdf = attachment_type(asset_url, row)
                items.append({
                    "item_id": stable_item_id(area_code, str(row.get("infoId") or row.get("id") or ""), asset_url),
                    "title": title, "url": asset_url, "type": asset_kind, "format": asset_kind, "extension": asset_ext,
                    "is_pdf": asset_pdf, "downloadable": True, "province": province, "province_code": area_code,
                    "network": "csg", "area_code": area_code, "publish_time": str(row.get("publishTime") or row.get("updateTime") or ""),
                    "source_entry": url if assets else entry, "raw": row,
                })
        items = list({item["url"]: item for item in items}.values())
        return {
            "network": "csg", "source": "csg_price_standard", "province": province, "provinceCode": area_code,
            "route_prefix": route_prefix, "page_id": page_id, "region": {"areaCode": area_code, "province": province},
            "category": {"title": "电价及收费标准 / 代理购电", "id": page_id, "url": entry},
            "tab_found": True, "tab_name": "电价及收费标准 / 代理购电", "page_count": page_count,
            "remote_count": total, "raw_count": len(captured),
            "proxy_record_count": sum(is_proxy_title(row.get("infoTitle") or row.get("title")) for row in captured),
            "attachment_count": len(items), "items": items, "errors": errors,
        }

    def _scan_province(self, page: Page, province: str, area_code: str, class_id: str) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        errors: list[str] = []
        total = 0
        class_name = "资讯公告"
        deadline = time.monotonic() + self.settings.province_scan_timeout_seconds
        for page_no in range(1, self.max_pages + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                errors.append("单省目录扫描达到时间上限")
                break
            try:
                payload = {
                    "areaCode": area_code,
                    "level": "2",
                    "classId": class_id,
                    "pageNo": page_no,
                    "pageSize": self.page_size,
                    "version": "cn",
                }
                total_now, page_rows, class_name = _rows(self._fetch_json(page, payload))
                total = max(total, total_now)
                rows.extend(page_rows)
                # The service may cap pageSize below the requested value.  Keep
                # following pageNo while its advertised count is not exhausted.
                if not page_rows or (total > 0 and len(rows) >= total) or (total <= 0 and len(page_rows) < self.page_size):
                    break
            except Exception as exc:
                errors.append(f"第 {page_no} 页：{exc}")
                break
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        resolved_rows: list[dict[str, Any]] = []
        for row in rows:
            if not row.get("link") and not row.get("url"):
                link = self._resolve_detail_link(page, row, area_code)
                if link:
                    row = {**row, "link": link}
            resolved_rows.append(row)
        rows = resolved_rows
        for row in rows:
            if not is_proxy_title(row.get("infoTitle") or row.get("title")):
                continue
            item = normalize_item(row, province, area_code, class_id)
            if item and item["url"] not in seen:
                seen.add(item["url"])
                items.append(item)
        return {
            "network": "csg",
            "source": "csg",
            "province": province,
            "provinceCode": area_code,
            "region": {"areaCode": area_code, "province": province},
            "category": {"title": "资讯公告 / 代理购电", "id": class_id, "url": CSG_HOME_URL},
            "tab_found": True,
            "tab_name": "资讯公告 / 代理购电",
            "class_name": class_name,
            "remote_count": total,
            "raw_count": len(rows),
            "items": items,
            "errors": errors,
        }

    def scan(self, progress: Callable[[int, int, dict[str, Any]], None] | None = None) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        with sync_playwright() as playwright:
            with tempfile.TemporaryDirectory(prefix="csg-scan-") as profile:
                options: dict[str, Any] = {
                    "headless": self.settings.headless,
                    "locale": "zh-CN",
                    "viewport": {"width": 1600, "height": 1100},
                    "ignore_https_errors": True,
                    "args": ["--disable-blink-features=AutomationControlled", "--no-first-run", "--no-default-browser-check"],
                }
                if self.settings.browser_executable:
                    options["executable_path"] = self.settings.browser_executable
                if self.settings.browser_proxy:
                    options["proxy"] = {"server": self.settings.browser_proxy}
                context = playwright.chromium.launch_persistent_context(profile, **options)
                try:
                    for index, (province, area_code, route_prefix, page_id) in enumerate(CSG_PRICE_PROVINCES, 1):
                        page = context.new_page()
                        page.set_default_timeout(self.settings.navigation_timeout_seconds * 1000)
                        try:
                            result = self._price_page(page, province, area_code, route_prefix, page_id)
                        finally:
                            page.close()
                        results.append(result)
                        if progress:
                            progress(index, len(CSG_PRICE_PROVINCES), result)
                    if results and all(row.get("errors") and not row.get("items") for row in results):
                        raise RuntimeError("南网五省接口本轮均未返回有效目录")
                finally:
                    context.close()
        return {"schema_version": "1.0", "network": "csg", "source": CSG_HOME_URL, "scanned_at": now_iso(), "results": results}


def download_html_documents(
    items: list[dict[str, Any]],
    settings: Settings,
    progress: Callable[[int, int, dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """用同一个无头浏览器会话保存南网公告详情 HTML。"""
    output_root = settings.download_dir
    output_root.mkdir(parents=True, exist_ok=True)
    scanner = CSGScanner(settings)
    reports: list[dict[str, Any]] = []
    with sync_playwright() as playwright:
        with tempfile.TemporaryDirectory(prefix="csg-download-") as profile:
            options: dict[str, Any] = {
                "headless": settings.headless,
                "locale": "zh-CN",
                "viewport": {"width": 1600, "height": 1100},
                "ignore_https_errors": True,
                "args": ["--disable-blink-features=AutomationControlled", "--no-first-run"],
            }
            if settings.browser_executable:
                options["executable_path"] = settings.browser_executable
            if settings.browser_proxy:
                options["proxy"] = {"server": settings.browser_proxy}
            context = playwright.chromium.launch_persistent_context(profile, **options)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(CSG_HOME_URL, wait_until="domcontentloaded", timeout=settings.navigation_timeout_seconds * 1000)
                scanner._wait_loaded(page)
                for index, item in enumerate(items, 1):
                    province = str(item.get("province") or "南方电网")
                    title = str(item.get("title") or "代理购电公告")
                    url = absolute_url(item.get("url"))
                    target_dir = output_root / safe_name("南方电网-" + province)
                    target_dir.mkdir(parents=True, exist_ok=True)
                    target = target_dir / f"{safe_name(title)}_{hashlib.sha256(url.encode()).hexdigest()[:10]}.html"
                    record = {"network": "csg", "province": province, "title": title, "url": url, "type": "html", "format": "html"}
                    try:
                        value = page.evaluate(FETCH_HTML_JS, {"url": url, "timeoutMs": settings.request_timeout_seconds * 1000})
                        status = int(value.get("status") or 0)
                        body = str(value.get("text") or "").encode("utf-8")
                        if not 200 <= status < 300 or not body or any(token in body[:5000] for token in ("安全威胁".encode(), "请求已被阻断".encode())):
                            raise RuntimeError(f"公告详情获取失败：HTTP {status}")
                        temporary = target.with_name(target.name + ".part")
                        temporary.write_bytes(body)
                        temporary.replace(target)
                        record.update({"status": "acquired", "file": str(target.relative_to(settings.download_dir)).replace("\\", "/"), "bytes": len(body), "downloaded_at": now_iso()})
                    except Exception as exc:
                        record.update({"status": "failed", "file": "", "bytes": 0, "error": str(exc), "downloaded_at": now_iso()})
                    reports.append(record)
                    if progress:
                        progress(index, len(items), record)
            finally:
                context.close()
    return reports
