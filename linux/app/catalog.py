from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import threading
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse


# Current province-wide tariff page.  The legacy route redirects to an older
# component whose list stops before recently published months.
PAGE_URL = "https://95598.cn/osgweb/ipElectrovalenceStandard"
SITE_ORIGIN = "https://95598.cn"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".svg"}
NETWORK_NAMES = {"sgcc": "国家电网", "csg": "南方电网"}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def atomic_json_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return deepcopy(default)


def absolute_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return urljoin(SITE_ORIGIN + "/", text).replace("/omg-static//omg-static/", "/omg-static/")


def attachment_format(url: str, raw: dict[str, Any] | None = None) -> tuple[str, str]:
    extension = Path(urlparse(url).path).suffix.lower()
    if extension == ".pdf" or bool((raw or {}).get("is_pdf")):
        return "pdf", "pdf"
    if extension in {".html", ".htm"} or str((raw or {}).get("format") or (raw or {}).get("type") or "").lower() in {"html", "htm"}:
        return "html", "html"
    if extension in IMAGE_EXTENSIONS:
        return "image", extension.lstrip(".")
    raw_format = str((raw or {}).get("format") or (raw or {}).get("type") or "").lower()
    if raw_format == "pdf":
        return "pdf", extension.lstrip(".") or "pdf"
    if raw_format in {"image", "png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff", "svg"}:
        return "image", extension.lstrip(".") or raw_format
    return "other", extension.lstrip(".") or "bin"


def safe_name(value: str, max_chars: int = 150) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", str(value)).strip(" ._") or "未命名"
    return name[:max_chars]


def item_id(province_code: str, title: str, url: str) -> str:
    return hashlib.sha256(f"{province_code}\0{title}\0{url}".encode("utf-8")).hexdigest()[:20]


def expected_relative_path(province: str, title: str, url: str, extension: str, network: str = "") -> Path:
    suffix = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
    ext = "." + (extension or "bin").lstrip(".")
    folder = safe_name(f"{NETWORK_NAMES.get(network.lower(), network)}-{province}") if network else safe_name(province)
    return Path(folder) / f"{safe_name(title)}_{suffix}{ext}"


def valid_file(path: Path, fmt: str = "") -> bool:
    if not path.is_file() or path.stat().st_size <= 0:
        return False
    try:
        with path.open("rb") as stream:
            head = stream.read(16)
    except OSError:
        return False
    if fmt == "pdf" or path.suffix.lower() == ".pdf":
        return head.startswith(b"%PDF")
    if fmt == "html" or path.suffix.lower() in {".html", ".htm"}:
        lower = head.lstrip().lower()
        return bool(lower) and (lower.startswith((b"<!doctype", b"<html", b"<?xml", b"<head", b"<body")) or path.stat().st_size > 0)
    if fmt == "image" or path.suffix.lower() in IMAGE_EXTENSIONS:
        return head.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"RIFF", b"BM", b"II*\x00", b"MM\x00*", b"<svg"))
    return not head.lstrip().lower().startswith((b"<!doctype", b"<html"))


def _category(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    nested = raw.get("raw") if isinstance(raw.get("raw"), dict) else {}
    title = str(raw.get("title") or raw.get("name") or raw.get("knoweleTitle") or nested.get("knoweleTitle") or "").strip()
    category_id = str(raw.get("id") or raw.get("classifyId") or nested.get("classifyId") or "")
    url = absolute_url(raw.get("url") or raw.get("knoweleContentUrl") or nested.get("knoweleContentUrl"))
    return {"title": title, "id": category_id, "url": url, "found": bool(title)}


def _raw_items(record: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("items", "child", "pdf"):
        value = record.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
    return []


def normalize_catalog(raw: Any, download_dir: Path | None = None, report: dict[str, Any] | None = None) -> dict[str, Any]:
    if isinstance(raw, list):
        records = raw
        scanned_at = ""
        source = PAGE_URL
    elif isinstance(raw, dict):
        records = raw.get("results") or raw.get("provinces") or []
        scanned_at = str(raw.get("scanned_at") or raw.get("generated_at") or "")
        source = str(raw.get("source") or PAGE_URL)
    else:
        records, scanned_at, source = [], "", PAGE_URL
    report_map: dict[str, dict[str, Any]] = {}
    if isinstance(report, dict):
        rows = report.get("items") or []
        if isinstance(rows, list):
            report_map = {absolute_url(x.get("url")): x for x in rows if isinstance(x, dict) and x.get("url")}
        elif isinstance(report.get("downloads"), dict):
            report_map = {absolute_url(k): v for k, v in report["downloads"].items() if isinstance(v, dict)}
    provinces: list[dict[str, Any]] = []
    grand = {"province_count": 0, "tab_count": 0, "directory_count": 0, "item_count": 0, "pdf_count": 0, "image_count": 0, "html_count": 0, "other_count": 0, "acquired_count": 0, "missing_count": 0, "failed_count": 0}
    for record in records:
        if not isinstance(record, dict):
            continue
        pobj = record.get("province") if isinstance(record.get("province"), dict) else {}
        province = str(pobj.get("codeName") or record.get("province") or record.get("name") or record.get("province_name") or "未知省份")
        codes = record.get("codes") if isinstance(record.get("codes"), dict) else {}
        province_code = str(record.get("provinceCode") or record.get("province_code") or codes.get("provinceCode") or pobj.get("codeValue") or "")
        network = str(record.get("network") or record.get("source_network") or ("csg" if str(record.get("source") or "").lower().startswith("csg") else "sgcc")).lower()
        region = record.get("region") if isinstance(record.get("region"), dict) else dict(codes)
        category = _category(record.get("category"))
        tab_found = bool(category and category.get("found"))
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, value in enumerate(_raw_items(record)):
            nested = value.get("raw") if isinstance(value.get("raw"), dict) else {}
            title = str(value.get("title") or value.get("knoweleTitle") or value.get("electricpriceTitle") or value.get("infoTitle") or nested.get("knoweleTitle") or nested.get("electricpriceTitle") or nested.get("infoTitle") or "").strip()
            url = absolute_url(value.get("url") or value.get("pdfAddress") or value.get("electricpriceInfo") or value.get("classContentUrl") or value.get("link") or nested.get("pdfAddress") or nested.get("electricpriceInfo") or nested.get("classContentUrl") or nested.get("link"))
            if not url or url in seen:
                continue
            seen.add(url)
            fmt, extension = attachment_format(url, {**nested, **value})
            iid = item_id(province_code, title, url)
            expected = expected_relative_path(province, title, url, extension, network if network == "csg" else "")
            record_info = report_map.get(url, {})
            local_value = record_info.get("file") or record_info.get("path") or value.get("local_file") or value.get("local_path") or ""
            local_path: Path | None = None
            if download_dir is not None:
                candidate = Path(str(local_value)) if local_value else expected
                candidate = candidate if candidate.is_absolute() else download_dir / candidate
                try:
                    resolved = candidate.resolve()
                    resolved.relative_to(download_dir.resolve())
                    local_path = resolved
                except (OSError, ValueError):
                    local_path = None
            acquired = bool(local_path and valid_file(local_path, fmt))
            failed = bool(record_info.get("status") == "failed" or record_info.get("error")) and not acquired
            status = "acquired" if acquired else "failed" if failed else "missing"
            items.append({
                "item_id": iid,
                "index": index,
                "province": province,
                "province_code": province_code,
                "network": network,
                "grid": NETWORK_NAMES.get(network, network),
                "title": title or Path(urlparse(url).path).stem,
                "url": url,
                "publish_time": str(value.get("publish_time") or value.get("publishTime") or nested.get("publishTime") or ""),
                "info_id": str(value.get("info_id") or value.get("infoId") or nested.get("infoId") or ""),
                "format": fmt,
                "type": fmt,
                "extension": extension,
                "is_pdf": fmt == "pdf",
                "downloadable": True,
                "local_status": status,
                "status": status,
                "local_file": str((Path(str(local_value)) if local_value else expected).as_posix()) if acquired else "",
                "bytes": local_path.stat().st_size if acquired and local_path else 0,
                "error": str(record_info.get("error") or ""),
            })
        counts = {
            "directory_count": len(items),
            "item_count": len(items),
            "pdf_count": sum(x["format"] == "pdf" for x in items),
            "image_count": sum(x["format"] == "image" for x in items),
            "other_count": sum(x["format"] == "other" for x in items),
            "html_count": sum(x["format"] == "html" for x in items),
            "acquired_count": sum(x["local_status"] == "acquired" for x in items),
            "missing_count": sum(x["local_status"] == "missing" for x in items),
            "failed_count": sum(x["local_status"] == "failed" for x in items),
        }
        if items and not tab_found:
            inferred_title = str(record.get("tab_name") or record.get("categoryTitle") or "代理购电")
            category = {"title": inferred_title, "id": "", "url": "", "found": True, "inferred": True}
            tab_found = True
        province_view = {
            "province": province,
            "name": province,
            "province_code": province_code,
            "provinceCode": province_code,
            "network": network,
            "grid": NETWORK_NAMES.get(network, network),
            "province_key": f"{network}:{province}",
            "region": region,
            "tab_found": tab_found,
            "tab_name": category.get("title") if category else "",
            "tab_id": category.get("id") if category else "",
            "tab_url": category.get("url") if category else "",
            "category": category,
            "counts": counts,
            "items": items,
            "errors": list(record.get("errors") or []) + list(record.get("scan_errors") or []),
            "stale": bool(record.get("stale")),
        }
        provinces.append(province_view)
        grand["province_count"] += 1
        grand["tab_count"] += int(tab_found)
        grand["directory_count"] += counts["directory_count"]
        for key in ("item_count", "pdf_count", "image_count", "html_count", "other_count", "acquired_count", "missing_count", "failed_count"):
            grand[key] += counts[key]
    return {"ok": True, "schema_version": "1.0", "generated_at": now_iso(), "scanned_at": scanned_at, "source": source, "summary": grand, "totals": grand, "provinces": provinces}


def merge_scan_results(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Retain the previous province when a transient scan returns no rows."""
    old_rows = previous.get("results", []) if isinstance(previous, dict) else []
    new_rows = current.get("results", []) if isinstance(current, dict) else []
    old_by_code = {(str(x.get("network") or "sgcc"), str(x.get("provinceCode") or x.get("province_code") or "")): x for x in old_rows if isinstance(x, dict)}
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in new_rows:
        if not isinstance(row, dict):
            continue
        code = (str(row.get("network") or "sgcc"), str(row.get("provinceCode") or row.get("province_code") or ""))
        seen.add(code)
        if not _raw_items(row) and code in old_by_code and _raw_items(old_by_code[code]):
            fallback = deepcopy(old_by_code[code])
            fallback["stale"] = True
            fallback["scan_errors"] = list(row.get("errors") or [])
            merged.append(fallback)
        else:
            merged.append(row)
    for code, row in old_by_code.items():
        if code not in seen:
            fallback = deepcopy(row)
            fallback["stale"] = True
            merged.append(fallback)
    result = dict(current)
    result["results"] = merged
    return result


class CatalogStore:
    def __init__(self, catalog_file: Path | str, download_dir: Path | str, report_file: Path | str | None = None, **_: Any) -> None:
        self.catalog_file = Path(catalog_file)
        self.download_dir = Path(download_dir)
        self.report_file = Path(report_file) if report_file else self.catalog_file.with_name("download-report.json")
        self.lock = threading.RLock()
        self.catalog_file.parent.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        with self.lock:
            return read_json(self.catalog_file, {"scanned_at": "", "source": PAGE_URL, "results": []})

    def save(self, raw: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            atomic_json_write(self.catalog_file, raw)
            return self.snapshot()

    def load_report(self) -> dict[str, Any]:
        with self.lock:
            return read_json(self.report_file, {"updated_at": "", "items": []})

    def save_report(self, report: dict[str, Any]) -> None:
        with self.lock:
            atomic_json_write(self.report_file, report)

    def report_map(self) -> dict[str, dict[str, Any]]:
        report = self.load_report()
        return {absolute_url(x.get("url")): x for x in report.get("items", []) if isinstance(x, dict) and x.get("url")}

    def update_download(self, record: dict[str, Any]) -> None:
        with self.lock:
            report = self.load_report()
            rows = {absolute_url(x.get("url")): x for x in report.get("items", []) if isinstance(x, dict) and x.get("url")}
            rows[absolute_url(record.get("url"))] = record
            atomic_json_write(self.report_file, {"updated_at": now_iso(), "items": sorted(rows.values(), key=lambda x: (str(x.get("province", "")), str(x.get("title", ""))))})

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return normalize_catalog(self.load(), self.download_dir, self.load_report())

    def refresh_status(self) -> dict[str, Any]:
        return self.snapshot()

    def get_item(self, iid: str) -> dict[str, Any] | None:
        for province in self.snapshot().get("provinces", []):
            for item in province.get("items", []):
                if item.get("item_id") == iid:
                    return item
        return None


