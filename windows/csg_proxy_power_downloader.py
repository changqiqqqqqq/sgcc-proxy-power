"""南方电网五省代理购电资料扫描器。

工具打开每个省份提供的“电价及收费标准”入口，调用页面实际使用的
``queryInformationList`` 接口，依据返回的 ``count`` 自动遍历全部分页，筛选标题含
“代理购电”的记录，并解析详情中的 PDF/图片附件。

该模块只使用浏览器页面上下文发起接口请求。这样既能复用网站的会话，也避免把
浏览器中可用的请求误当成普通 requests 请求。命令行示例：

    python csg_proxy_power_downloader.py --scan --visible

默认输出 ``csg_proxy_power_catalog.json``；扫描后可用页面或下载函数保存附件。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import time
try:
    import winreg
except ImportError:
    winreg = None
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Page, sync_playwright


ROOT = Path(__file__).resolve().parent
CSG_HOME_URL = "https://95598.csg.cn/#/gd/serviceInquire/information/list"
CSG_ORIGIN = "https://95598.csg.cn"
CSG_LIST_ENDPOINT = "/ucs/ma/wt/searchService/queryInformationList"
CSG_ADDRESS_ENDPOINT = "/ucs/ma/wt/user/queryStructuredAddress"
CSG_DETAIL_ENDPOINT = "/ucs/ma/wt/searchService/queryInfoDetail"
CSG_PROVINCES = (
    ("广东省", "030000", "bcdbe6731d0b4352be37e340ca67d346"),
    ("广西壮族自治区", "040000", "4ead135257dc470ba39d24ab3e8848d6"),
    ("云南省", "050000", "1fc640b14ba44a92975e0db4c0dbfe14"),
    ("贵州省", "060000", "725ba6f7efd54a4787a61508b1b6ec0b"),
    ("海南省", "070000", "00ab16ad011b45dbbbedcdbc18238848"),
)
# “电价及收费标准”页面入口。页面是动态 SPA，不能只依赖资讯公告接口；
# 扫描时会打开这些入口、监听 JSON 响应，并继续翻页/展开代理购电节点。
CSG_PRICE_PROVINCES = (
    ("云南省", "050000", "yn", "b50e80ef887f4561bec8fb96e06f4862"),
    ("广东省", "030000", "gd", "c06263b20bf34df9926edd2a40cb1632"),
    ("贵州省", "060000", "gz", "a8d3146e14ed41d1ae18c8a7547c3cdc"),
    ("海南省", "070000", "hn", "47e77bf71fb74f7fbeefd7df2ece0c90"),
    ("广西壮族自治区", "040000", "gx", "6394da157f8349599e1266a7ac502f3f"),
)
CSG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36 Edg/152.0.0.0",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
PROXY_TITLE_RE = re.compile(r"代理\s*购电")
VALID_EXTENSIONS = {".html", ".htm", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".svg"}
PRICE_TITLE_KEYS = ("infoTitle", "knoweleTitle", "title", "fileName", "documentName", "name", "labelName")
PRICE_URL_KEYS = ("pdfAddress", "downloadUrl", "fileUrl", "attachmentUrl", "knoweleContentUrl", "contentUrl", "link", "url", "src")


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
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", str(value)).strip(" ._")
    return (value or "南网代理购电公告")[:max_chars]


def stable_item_id(area_code: str, info_id: str, url: str) -> str:
    return hashlib.sha256(f"csg\0{area_code}\0{info_id}\0{url}".encode("utf-8")).hexdigest()[:20]


def is_proxy_title(title: Any) -> bool:
    return bool(PROXY_TITLE_RE.search(re.sub(r"\s+", "", str(title or ""))))


def _first_text(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _first_url(row: dict[str, Any]) -> str:
    for key in PRICE_URL_KEYS:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            candidate = absolute_url(value)
            if urlparse(candidate).scheme in {"http", "https"}:
                return candidate
    return ""


def _walk_price_records(value: Any, parent_title: str = "") -> list[dict[str, Any]]:
    """从动态接口任意层级提取带标题和附件地址的记录。"""
    records: list[dict[str, Any]] = []
    if isinstance(value, dict):
        title = _first_text(value, PRICE_TITLE_KEYS) or parent_title
        url = _first_url(value)
        if title and url:
            records.append({**value, "_title": title, "_url": url})
        for child in value.values():
            records.extend(_walk_price_records(child, title))
    elif isinstance(value, list):
        for child in value:
            records.extend(_walk_price_records(child, parent_title))
    return records


def _html_assets(html: str, base_url: str) -> list[tuple[str, str]]:
    assets: list[tuple[str, str]] = []
    for raw in re.findall(r"(?:href|src)\s*=\s*[\"']([^\"']+)", html, re.I):
        url = absolute_url(raw, base_url)
        if urlparse(url).hostname != urlparse(CSG_ORIGIN).hostname:
            continue
        kind, _ext, _pdf = attachment_type(url, {})
        if kind in {"pdf", "image"}:
            assets.append((kind, url))
    return list(dict.fromkeys(assets))


def attachment_type(url: str, row: dict[str, Any]) -> tuple[str, str, bool]:
    lower = url.lower()
    if "documenttype=pdf" in lower or Path(urlparse(url).path).suffix.lower() == ".pdf":
        return "pdf", "pdf", True
    suffix = Path(urlparse(url).path).suffix.lower().lstrip(".")
    if suffix in {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff", "svg"}:
        return "image", suffix, False
    return "html", "html", False


def _edge_candidates() -> list[Path]:
    values = [
        os.getenv("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        os.getenv("PROGRAMFILES", r"C:\\Program Files"),
        os.getenv("LOCALAPPDATA", ""),
    ]
    paths = [
        Path(values[0]) / "Microsoft/Edge/Application/msedge.exe",
        Path(values[1]) / "Microsoft/Edge/Application/msedge.exe",
        Path(values[2]) / "Microsoft/Edge/Application/msedge.exe",
    ]
    found = os.environ.get("CSG_BROWSER_EXECUTABLE")
    if found:
        paths.insert(0, Path(found))
    return paths


def find_edge() -> Path:
    for path in _edge_candidates():
        if path.is_file():
            return path.resolve()
    raise FileNotFoundError("未找到 Microsoft Edge；可设置 CSG_BROWSER_EXECUTABLE")


def browser_proxy() -> str:
    explicit = os.getenv("CSG_BROWSER_PROXY", "").strip()
    if explicit:
        return explicit
    if winreg is not None:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
                enabled = int(winreg.QueryValueEx(key, "ProxyEnable")[0])
                server = str(winreg.QueryValueEx(key, "ProxyServer")[0]).strip()
            if enabled and server:
                if ";" in server:
                    server = server.split(";", 1)[0].split("=", 1)[-1]
                return server if "://" in server else "http://" + server
        except (OSError, ValueError):
            pass
    return ""


FETCH_JSON_JS = r"""
async ({endpoint, payload, timeoutMs}) => {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), Number(timeoutMs || 30000));
  try {
    const response = await fetch(endpoint, {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'Accept': 'application/json, text/plain, */*'},
      body: JSON.stringify(payload),
      credentials: 'include',
      signal: controller.signal
    });
    return {status: response.status, contentType: response.headers.get('content-type') || '', text: await response.text()};
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


def _decode_json_response(value: Any) -> Any:
    if not isinstance(value, dict):
        raise RuntimeError("南网接口返回格式异常")
    status = int(value.get("status") or 0)
    text = str(value.get("text") or "")
    if status < 200 or status >= 300:
        raise RuntimeError(f"南网接口 HTTP {status}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"南网接口返回非 JSON：{text[:160]}") from exc


def _info_rows(payload: Any) -> tuple[int, list[dict[str, Any]], str]:
    if not isinstance(payload, dict):
        return 0, [], ""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
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


def normalize_item(row: dict[str, Any], area_code: str, province: str, class_id: str) -> dict[str, Any] | None:
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
        "area_code": area_code,
        "info_id": info_id,
        "publish_time": str(row.get("publishTime") or row.get("publish_time") or ""),
        "class_id": str(row.get("classId") or class_id),
        "class_name": str(row.get("className") or "资讯公告"),
        "issued_name": str(row.get("issuedName") or ""),
        "raw": row,
    }


def normalize_result(province: str, area_code: str, class_id: str, rows: list[dict[str, Any]], class_name: str, errors: list[str] | None = None) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not is_proxy_title(row.get("infoTitle") or row.get("title")):
            continue
        item = normalize_item(row, area_code, province, class_id)
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
        "items": items,
        "errors": list(errors or []),
    }


class CSGScanner:
    def __init__(
        self,
        *,
        headless: bool = True,
        executable_path: str | Path | None = None,
        request_timeout_seconds: int = 30,
        page_size: int = 100,
        max_pages: int = 50,
    ) -> None:
        self.headless = headless
        self.executable_path = str(executable_path or find_edge())
        self.request_timeout_seconds = max(5, int(request_timeout_seconds))
        self.page_size = max(5, min(100, int(page_size)))
        self.max_pages = max(1, int(max_pages))

    def _fetch_json(self, page: Page, endpoint: str, payload: dict[str, Any], timeout_seconds: float | None = None) -> Any:
        return _decode_json_response(page.evaluate(FETCH_JSON_JS, {
            "endpoint": endpoint,
            "payload": payload,
            "timeoutMs": max(1000, int((timeout_seconds or self.request_timeout_seconds) * 1000)),
        }))

    def _wait_loaded(self, page: Page, deadline: float | None = None) -> None:
        deadline = deadline or time.monotonic() + self.request_timeout_seconds * 2
        while time.monotonic() < deadline:
            try:
                text = page.locator("body").inner_text(timeout=1500)
            except Exception:
                text = ""
            if "安全威胁" in text or "请求已被阻断" in text or "ERR_CONNECTION_CLOSED" in text:
                raise RuntimeError("南网页面被上游拦截，请稍后重试或使用可见浏览器检查")
            if "资讯公告" in text or "南网在线" in text:
                return
            page.wait_for_timeout(300)
        raise TimeoutError("南网资讯公告页面加载超时")

    def _fetch_page(self, page: Page, url: str) -> dict[str, Any]:
        value = page.evaluate(FETCH_HTML_JS, {"url": url, "timeoutMs": self.request_timeout_seconds * 2000})
        if not isinstance(value, dict) or int(value.get("status") or 0) < 200 or int(value.get("status") or 0) >= 300:
            raise RuntimeError(f"公告详情获取失败：HTTP {value.get('status') if isinstance(value, dict) else '?'}")
        return value

    def _price_rows(self, page: Page, area_code: str, page_id: str) -> tuple[int, list[dict[str, Any]], int, list[str]]:
        rows: list[dict[str, Any]] = []
        errors: list[str] = []
        total = 0
        page_count = 0
        page_size = 15
        for page_no in range(1, self.max_pages + 1):
            try:
                decoded = self._fetch_json(page, CSG_LIST_ENDPOINT, {
                    "areaCode": area_code[:2], "level": "1", "classId": page_id,
                    "pageNo": page_no, "pageSize": page_size, "version": "cn",
                })
                current_total, current_rows, _class_name = _info_rows(decoded)
                page_count = page_no
                total = max(total, current_total)
                rows.extend(current_rows)
                if not current_rows or (total > 0 and len(rows) >= total) or (total <= 0 and len(current_rows) < page_size):
                    break
            except Exception as exc:
                errors.append(f"第 {page_no} 页：{exc}")
                break
        return total, rows, page_count, errors

    def _price_page(self, page: Page, province: str, area_code: str, route_prefix: str, page_id: str) -> dict[str, Any]:
        """采集“电价及收费标准”入口中的代理购电节点和全部附件。"""
        url = f"{CSG_ORIGIN}/#/{route_prefix}/businessPage/baseInfo/{page_id}?cname=%E7%94%B5%E4%BB%B7%E5%8F%8A%E6%94%B6%E8%B4%B9%E6%A0%87%E5%87%86"
        page.goto(url, wait_until="domcontentloaded", timeout=self.request_timeout_seconds * 2000)
        total, captured, page_count, errors = self._price_rows(page, area_code, page_id)
        rows: list[dict[str, Any]] = []
        for row in captured:
            title = str(row.get("infoTitle") or row.get("title") or "")
            if not is_proxy_title(title):
                continue
            link = absolute_url(row.get("link") or row.get("url"))
            if not link:
                link = self._resolve_detail_link(page, row, area_code[:2])
            if not link:
                errors.append(f"{title}：未取得附件地址")
                continue
            kind, extension, is_pdf = attachment_type(link, row)
            if kind == "html":
                try:
                    html = str(self._fetch_page(page, link).get("text") or "")
                    assets = _html_assets(html, link)
                except Exception:
                    assets = []
                if assets:
                    for asset_kind, asset_url in assets:
                        asset_kind, asset_ext, asset_pdf = attachment_type(asset_url, {})
                        rows.append({
                            "item_id": stable_item_id(area_code, str(row.get("infoId") or row.get("id") or ""), asset_url),
                            "title": title, "url": asset_url, "type": asset_kind, "format": asset_kind,
                            "extension": asset_ext, "is_pdf": asset_pdf, "downloadable": True,
                            "province": province, "province_code": area_code, "area_code": area_code,
                            "publish_time": str(row.get("publishTime") or row.get("updateTime") or ""),
                            "source_entry": link, "raw": row,
                        })
                # 用户需要 PDF/图片；没有可提取附件的普通网页不进入资料目录。
                continue
            rows.append({
                "item_id": stable_item_id(area_code, str(row.get("infoId") or row.get("id") or ""), link),
                "title": title,
                "url": link,
                "type": kind,
                "format": kind,
                "extension": extension,
                "is_pdf": is_pdf,
                "downloadable": True,
                "province": province,
                "province_code": area_code,
                "area_code": area_code,
                "publish_time": str(row.get("publishTime") or row.get("updateTime") or ""),
                "source_entry": url,
                "raw": row,
            })
        unique: dict[str, dict[str, Any]] = {item["url"]: item for item in rows}
        return {
            "network": "csg",
            "source": "csg_price_standard",
            "province": province,
            "provinceCode": area_code,
            "route_prefix": route_prefix,
            "page_id": page_id,
            "region": {"areaCode": area_code, "province": province},
            "category": {"title": "电价及收费标准 / 代理购电", "id": page_id, "url": url},
            "tab_found": True,
            "tab_name": "电价及收费标准 / 代理购电",
            "items": list(unique.values()),
            "page_count": page_count,
            "remote_count": total,
            "raw_count": len(captured),
            "proxy_record_count": sum(is_proxy_title(row.get("infoTitle") or row.get("title")) for row in captured),
            "attachment_count": len(unique),
            "errors": errors,
        }

    def _resolve_detail_link(self, page: Page, row: dict[str, Any], area_code: str) -> str:
        existing = absolute_url(row.get("link") or row.get("url"))
        if existing:
            return existing
        info_id = str(row.get("infoId") or row.get("id") or "")
        if not info_id:
            return ""
        try:
            decoded = self._fetch_json(page, CSG_DETAIL_ENDPOINT, {"areaCode": area_code, "infoId": info_id, "version": "cn"})
            return absolute_url(_find_link(decoded))
        except Exception:
            return ""

    def _province(self, page: Page, province: str, area_code: str, class_id: str) -> dict[str, Any]:
        all_rows: list[dict[str, Any]] = []
        errors: list[str] = []
        total = 0
        class_name = "资讯公告"
        deadline = time.monotonic() + max(45, self.request_timeout_seconds * 2)
        for page_no in range(1, self.max_pages + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                errors.append("单省目录扫描达到时间上限")
                break
            payload = {
                "areaCode": area_code,
                "level": "2",
                "classId": class_id,
                "pageNo": page_no,
                "pageSize": self.page_size,
                "version": "cn",
            }
            try:
                decoded = self._fetch_json(page, CSG_LIST_ENDPOINT, payload)
                current_total, rows, class_name = _info_rows(decoded)
                total = max(total, current_total)
                all_rows.extend(rows)
                # Some deployments cap pageSize below the requested value.  The
                # advertised count, rather than a short page, is authoritative.
                if not rows or (total > 0 and len(all_rows) >= total) or (total <= 0 and len(rows) < self.page_size):
                    break
            except Exception as exc:
                errors.append(f"第 {page_no} 页：{exc}")
                break
        resolved_rows: list[dict[str, Any]] = []
        for row in all_rows:
            if not row.get("link") and not row.get("url"):
                link = self._resolve_detail_link(page, row, area_code)
                if link:
                    row = {**row, "link": link}
            resolved_rows.append(row)
        result = normalize_result(province, area_code, class_id, resolved_rows, class_name, errors)
        result["remote_count"] = total
        result["page_count"] = max(1, (total + self.page_size - 1) // self.page_size) if total else 0
        result["raw_count"] = len(all_rows)
        return result

    def scan(self, progress: Callable[[int, int, dict[str, Any]], None] | None = None) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        with sync_playwright() as playwright:
            with tempfile.TemporaryDirectory(prefix="csg-playwright-") as profile_dir:
                launch_options: dict[str, Any] = {
                    "headless": self.headless,
                    "executable_path": self.executable_path,
                    "locale": "zh-CN",
                    "viewport": {"width": 1600, "height": 1100},
                    "ignore_https_errors": True,
                    "args": ["--disable-blink-features=AutomationControlled", "--no-first-run", "--no-default-browser-check"],
                }
                proxy = browser_proxy()
                if proxy:
                    launch_options["proxy"] = {"server": proxy}
                context = playwright.chromium.launch_persistent_context(profile_dir, **launch_options)
                try:
                    for index, (province, area_code, route_prefix, page_id) in enumerate(CSG_PRICE_PROVINCES, 1):
                        page = context.new_page()
                        page.set_default_timeout(self.request_timeout_seconds * 1000)
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
        return {
            "schema_version": "1.0",
            "network": "csg",
            "source": CSG_HOME_URL,
            "scanned_at": now_iso(),
            "results": results,
        }


def fetch_html_documents(
    items: list[dict[str, Any]],
    *,
    output_root: Path,
    headless: bool = True,
    executable_path: str | Path | None = None,
    request_timeout_seconds: int = 60,
    progress: Callable[[int, int, dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """在浏览器上下文中保存南网公告 HTML；返回每条结果。"""
    output_root.mkdir(parents=True, exist_ok=True)
    scanner = CSGScanner(headless=headless, executable_path=executable_path, request_timeout_seconds=request_timeout_seconds, page_size=100)
    reports: list[dict[str, Any]] = []
    with sync_playwright() as playwright:
        with tempfile.TemporaryDirectory(prefix="csg-download-") as profile_dir:
            launch_options: dict[str, Any] = {
                "headless": headless,
                "executable_path": scanner.executable_path,
                "locale": "zh-CN",
                "viewport": {"width": 1600, "height": 1100},
                "ignore_https_errors": True,
                "args": ["--disable-blink-features=AutomationControlled", "--no-first-run"],
            }
            proxy = browser_proxy()
            if proxy:
                launch_options["proxy"] = {"server": proxy}
            context = playwright.chromium.launch_persistent_context(profile_dir, **launch_options)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(CSG_HOME_URL, wait_until="domcontentloaded", timeout=request_timeout_seconds * 2000)
                scanner._wait_loaded(page)
                for index, item in enumerate(items, 1):
                    province = str(item.get("province") or "南方电网")
                    title = str(item.get("title") or "代理购电公告")
                    url = absolute_url(item.get("url"))
                    target_dir = output_root / safe_name(province)
                    target_dir.mkdir(parents=True, exist_ok=True)
                    item_type = str(item.get("type") or item.get("format") or "html").lower()
                    extension = str(item.get("extension") or ("pdf" if item_type == "pdf" else "html"))
                    target = target_dir / f"{safe_name(title)}_{hashlib.sha256(url.encode()).hexdigest()[:10]}.{extension}"
                    record: dict[str, Any] = {"network": "csg", "province": province, "title": title, "url": url, "type": item_type, "format": item_type}
                    try:
                        if item_type in {"html", "htm"}:
                            value = scanner._fetch_page(page, url)
                            body = str(value.get("text") or "").encode("utf-8")
                            if not body or "安全威胁".encode("utf-8") in body[:5000]:
                                raise RuntimeError("公告详情返回空白或拦截页")
                        else:
                            response = page.request.get(url, headers={"Referer": CSG_HOME_URL, "User-Agent": CSG_HEADERS["User-Agent"]})
                            if not response.ok:
                                raise RuntimeError(f"附件 HTTP {response.status}")
                            body = response.body()
                            if item_type == "pdf" and not body.startswith(b"%PDF"):
                                raise RuntimeError("返回内容不是 PDF")
                        temporary = target.with_name(target.name + ".part")
                        temporary.write_bytes(body)
                        temporary.replace(target)
                        record.update({"status": "downloaded", "file": str(target.relative_to(ROOT)).replace("\\", "/"), "bytes": len(body), "time": now_iso()})
                    except Exception as exc:
                        record.update({"status": "failed", "file": "", "bytes": 0, "error": str(exc), "time": now_iso()})
                    reports.append(record)
                    if progress:
                        progress(index, len(items), record)
            finally:
                context.close()
    return reports


def atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def scan_to_file(output: Path, *, headless: bool = False, executable_path: str | Path | None = None) -> dict[str, Any]:
    scanner = CSGScanner(headless=headless, executable_path=executable_path)

    def progress(index: int, total: int, result: dict[str, Any]) -> None:
        print(f"CSG {index}/{total} {result['province']}: 目录 {len(result.get('items', []))}，网页 {len(result.get('items', []))}", flush=True)

    value = scanner.scan(progress)
    atomic_write(output, value)
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", action="store_true", help="扫描五个南方电网省份")
    parser.add_argument("--visible", action="store_true", help="使用可见 Edge 扫描")
    parser.add_argument("--output", type=Path, default=ROOT / "csg_proxy_power_catalog.json")
    args = parser.parse_args()
    if not args.scan:
        args.scan = True
    scan_to_file(args.output, headless=not args.visible)
    print(f"南方电网目录已保存：{args.output}")


if __name__ == "__main__":
    main()
