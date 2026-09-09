"""标准化 95598 代理购电目录，供网页界面读取。

这个脚本只处理已经保存的扫描结果，不访问网站。它会合并当前扫描结果、
完整原始扫描快照和旧版扁平清单，按省份选择条目最完整的来源，然后为每个
省份和资料补充统一字段、文件类型以及本地“已获取/未获取”状态。

运行（在本文件所在目录）：

    python normalize_proxy_catalog.py
    python normalize_proxy_catalog.py --check

输出：

    proxy_power_catalog_view.json       网页用的分层目录
    proxy_power_items_view.json         网页用的扁平资料列表
    proxy_power_catalog_view.csv        可用 Excel 打开的摘要
    proxy_power_catalog_validation.json  来源和字段校验报告

注意：标准化不会下载文件，也不会删除或覆盖已有 PDF/图片。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urljoin, urlparse


ROOT = Path(__file__).resolve().parent
PAGE_URL = "https://95598.cn/osgweb/ipElectrovalenceStandard"
SITE_ORIGIN = "https://95598.cn"
CSG_PAGE_URL = "https://95598.csg.cn/#/gd/serviceInquire/information/list"

CATALOG_OUTPUT = ROOT / "proxy_power_catalog_view.json"
ITEMS_OUTPUT = ROOT / "proxy_power_items_view.json"
CSV_OUTPUT = ROOT / "proxy_power_catalog_view.csv"
VALIDATION_OUTPUT = ROOT / "proxy_power_catalog_validation.json"
REPORT_FILE = ROOT / "proxy_power_download_report.json"

# 来源优先级只用于条目数和时间相同时的裁决。当前扫描优先，其次为完整原始
# 快照，最后为旧版已经扁平化的清单。
SOURCE_SPECS = (
    ("live", ROOT / "proxy_power_catalog.json", 3),
    ("csg_live", ROOT / "csg_proxy_power_catalog.json", 3),
    ("raw_snapshot", ROOT.parent / "work" / "all_provinces_raw.json", 2),
    ("flat_snapshot", ROOT / "all_provinces_pdf_catalog.json", 1),
)

IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
    ".svg",
}
DOWNLOAD_ROOTS = (
    ROOT / "provinces_verified",
    ROOT / "provinces",
    ROOT / "pdf",
    ROOT / "csg_documents",
)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def read_json(path: Path) -> Any:
    """读取 UTF-8 JSON，并给出包含文件名的清晰错误。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path.name} 不是有效 UTF-8 JSON：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} JSON 格式错误（第 {exc.lineno} 行）：{exc.msg}") from exc


def source_records(payload: Any) -> tuple[list[dict[str, Any]], str]:
    """适配三种历史文件包装格式，返回 (records, scan_time)。"""
    if isinstance(payload, dict):
        records = payload.get("results")
        stamp = as_text(payload.get("scanned_at") or payload.get("time"))
        if isinstance(records, list):
            return [x for x in records if isinstance(x, dict)], stamp
        # 兼容意外保存成单个省份对象的情况。
        if payload.get("province") or payload.get("codes"):
            return [payload], stamp
        return [], stamp
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)], ""
    return [], ""


def first_value(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return ""


def object_name(value: Any) -> str:
    if isinstance(value, dict):
        return as_text(first_value(value.get("codeName"), value.get("name"), value.get("title")))
    return as_text(value)


def province_name(record: dict[str, Any]) -> str:
    value = record.get("province")
    if isinstance(value, dict):
        return object_name(value)
    codes = record.get("codes") if isinstance(record.get("codes"), dict) else {}
    return as_text(first_value(value, codes.get("province"), record.get("provinceName")))


def province_code(record: dict[str, Any]) -> str:
    value = record.get("province")
    pobj = value if isinstance(value, dict) else {}
    codes = record.get("codes") if isinstance(record.get("codes"), dict) else {}
    return as_text(
        first_value(
            record.get("provinceCode"),
            codes.get("provinceCode"),
            pobj.get("codeValue"),
            pobj.get("content1"),
        )
    )


def region_info(record: dict[str, Any], pcode: str) -> dict[str, str]:
    codes = record.get("codes") if isinstance(record.get("codes"), dict) else {}
    region = record.get("region") if isinstance(record.get("region"), dict) else {}
    city_obj = record.get("city") if isinstance(record.get("city"), dict) else {}
    county_obj = record.get("county") if isinstance(record.get("county"), dict) else {}
    city_name = as_text(first_value(codes.get("city"), region.get("city"), object_name(city_obj)))
    county_name = as_text(first_value(codes.get("county"), region.get("county"), object_name(county_obj)))
    city_code = as_text(
        first_value(
            codes.get("cityCode"),
            region.get("cityCode"),
            city_obj.get("codeValue"),
            city_obj.get("content1"),
        )
    )
    county_code = as_text(
        first_value(
            codes.get("countyCode"),
            region.get("countyCode"),
            county_obj.get("codeValue"),
            county_obj.get("content1"),
        )
    )
    return {
        "province_code": pcode,
        "city_code": city_code,
        "county_code": county_code,
        "city": city_name,
        "county": county_name,
    }


def absolute_url(value: Any) -> str:
    text = as_text(value)
    if not text:
        return ""
    return urljoin(SITE_ORIGIN + "/", text).replace(
        "/omg-static//omg-static/", "/omg-static/"
    )


def newest_item_key(item: dict[str, Any]) -> tuple[int, int, str]:
    match = re.search(r"(20\d{2})年\s*(\d{1,2})月", str(item.get("title") or ""))
    return (int(match.group(1)), int(match.group(2)), str(item.get("title") or "")) if match else (0, 0, str(item.get("title") or ""))


def category_id(category: dict[str, Any]) -> str:
    raw = category.get("raw") if isinstance(category.get("raw"), dict) else {}
    return as_text(first_value(category.get("classifyId"), category.get("category_id"), raw.get("classifyId")))


def category_title(category: dict[str, Any]) -> str:
    raw = category.get("raw") if isinstance(category.get("raw"), dict) else {}
    return as_text(
        first_value(
            category.get("title"),
            category.get("knoweleTitle"),
            category.get("electricpriceTitle"),
            category.get("className"),
            raw.get("knoweleTitle"),
        )
    )


def normalize_category(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    raw = value.get("raw") if isinstance(value.get("raw"), dict) else value
    title = category_title(value)
    url = absolute_url(
        first_value(
            value.get("url"),
            value.get("knoweleContentUrl"),
            value.get("classContentUrl"),
            raw.get("knoweleContentUrl"),
        )
    )
    return {
        "name": title,
        "id": category_id(value),
        "url": url,
        "found": bool(title),
        "matches_proxy_power": bool(re.search(r"代理购电", title)),
        "raw": raw,
    }


def item_list(record: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("items", "child", "pdf"):
        value = record.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
    return []


def item_title(value: dict[str, Any]) -> str:
    raw = value.get("raw") if isinstance(value.get("raw"), dict) else value
    return as_text(
        first_value(
            value.get("title"),
            value.get("knoweleTitle"),
            value.get("electricpriceTitle"),
            value.get("className"),
            value.get("labelName"),
            value.get("infoTitle"),
            raw.get("knoweleTitle"),
            raw.get("infoTitle"),
        )
    )


def item_url(value: dict[str, Any]) -> str:
    raw = value.get("raw") if isinstance(value.get("raw"), dict) else value
    return absolute_url(
        first_value(
            value.get("url"),
            value.get("pdfAddress"),
            value.get("electricpriceInfo"),
            value.get("classContentUrl"),
            value.get("classContent"),
            value.get("link"),
            raw.get("pdfAddress"),
            raw.get("electricpriceInfo"),
            raw.get("link"),
        )
    )


def extension_for_url(url: str) -> str:
    path = unquote(urlparse(url).path)
    return Path(path).suffix.lower()


def media_type(extension: str, value: dict[str, Any]) -> str:
    if extension == ".pdf" or bool(value.get("is_pdf")):
        return "pdf"
    if extension in IMAGE_EXTENSIONS:
        return "image"
    if extension in {".html", ".htm"} or as_text(value.get("type") or value.get("format")).lower() in {"html", "htm"}:
        return "html"
    declared = as_text(value.get("type")).lower()
    if declared in {"pdf", "application/pdf"}:
        return "pdf"
    if declared in {"image", "image_or_other", "png", "jpg", "jpeg"}:
        return "image"
    return "other"


def item_key(pcode: str, title: str, url: str) -> str:
    digest = hashlib.sha256(f"{pcode}|{url}|{title}".encode("utf-8")).hexdigest()
    return digest[:20]


def normalize_item(
    value: dict[str, Any],
    *,
    pcode: str,
    tab_id: str,
    index: int,
) -> dict[str, Any]:
    raw = value.get("raw") if isinstance(value.get("raw"), dict) else value
    title = item_title(value)
    url = item_url(value)
    if not title:
        title = Path(unquote(urlparse(url).path)).stem or f"未命名资料-{index + 1}"
    extension = extension_for_url(url)
    kind = media_type(extension, value)
    source_item_id = as_text(first_value(value.get("source_item_id"), value.get("classifyId"), raw.get("classifyId")))
    return {
        "item_id": item_key(pcode, title, url),
        "index": index,
        "title": title,
        "url": url,
        "extension": extension.lstrip("."),
        "format": kind,
        "type": kind,
        "is_pdf": kind == "pdf",
        "category_id": tab_id,
        "source_item_id": source_item_id,
        "downloadable": bool(url),
        "raw": raw,
    }


def normalize_result(record: dict[str, Any], source_name: str, source_time: str) -> dict[str, Any]:
    pname = province_name(record)
    pcode = province_code(record)
    region = region_info(record, pcode)
    category = normalize_category(record.get("category") or record.get("tab"))
    items = [normalize_item(x, pcode=pcode, tab_id=(category or {}).get("id", ""), index=i) for i, x in enumerate(item_list(record))]
    items.sort(key=newest_item_key, reverse=True)
    # 旧版扁平清单没有 category；如果资料标题本身带有“代理购电”，仍保留
    # 条目，但把 tab_found 标为 false，方便界面区分“无页签”与“有页签无资料”。
    return {
        "province": pname or "未命名省份",
        "province_code": pcode,
        "region": region,
        "tab": category,
        "items": items,
        "source": source_name,
        "network": as_text(record.get("network") or ("csg" if source_name.startswith("csg") else "sgcc")),
        "source_scanned_at": source_time,
        "source_item_count": len(items),
        "source_record": record,
    }


def source_timestamp(path: Path, declared: str) -> str:
    if declared:
        return declared
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        return ""


def source_sort_key(candidate: dict[str, Any]) -> tuple[int, int, str, int, int, int]:
    # A successful current scan is authoritative even when the new P2019
    # catalogue has fewer rows than a legacy snapshot. Empty current records
    # still fall back to older data.
    items = candidate["items"]
    valid_urls = sum(bool(x.get("url")) for x in items)
    has_tab = int(bool(candidate.get("tab") and candidate["tab"].get("name")))
    return (
        int(bool(items)),
        int(candidate.get("source_rank", 0)),
        as_text(candidate.get("source_scanned_at")),
        len(items),
        has_tab,
        valid_urls,
    )


def safe_filename(value: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", value).strip(" ._")
    return (name or "代理购电价格公示表")[:210]


def is_within(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def file_signature(path: Path) -> bytes:
    try:
        with path.open("rb") as stream:
            return stream.read(16)
    except OSError:
        return b""


def valid_local_file(path: Path, kind: str) -> bool:
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            return False
    except OSError:
        return False
    signature = file_signature(path)
    if kind == "pdf":
        return signature.startswith(b"%PDF")
    if kind == "image":
        return (
            signature.startswith(b"\x89PNG")
            or signature.startswith(b"\xff\xd8\xff")
            or signature.startswith((b"GIF87a", b"GIF89a", b"BM", b"II*\x00", b"MM\x00*"))
            or signature[0:4] == b"RIFF" and signature[8:12] == b"WEBP"
            or signature.startswith(b"<?xml")
        )
    return True


def load_download_report() -> dict[str, dict[str, Any]]:
    if not REPORT_FILE.exists():
        return {}
    try:
        payload = read_json(REPORT_FILE)
    except ValueError:
        return {}
    records = payload.get("items", []) if isinstance(payload, dict) else []
    output: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        url = absolute_url(record.get("url"))
        if url:
            output[url] = record
    return output


def report_path(record: dict[str, Any]) -> Path | None:
    value = as_text(record.get("file") or record.get("path"))
    if not value:
        return None
    candidate = (ROOT / value.replace("\\", "/")).resolve()
    if not is_within(candidate, ROOT) or not candidate.exists():
        return None
    return candidate


def find_local_file(
    item: dict[str, Any],
    province: str,
    report: dict[str, dict[str, Any]],
) -> Path | None:
    kind = item["format"]
    url = item["url"]
    reported = report_path(report[url]) if url in report else None
    if reported and valid_local_file(reported, kind):
        return reported

    # 其次按省份目录和标题查找，兼容没有下载报告的旧文件。
    title_base = safe_filename(item["title"])
    expected_ext = "." + item["extension"] if item.get("extension") else ""
    url_stem = Path(unquote(urlparse(url).path)).stem
    url_suffix = url_stem[-10:] if url_stem else ""
    for root in DOWNLOAD_ROOTS:
        province_dir = root / safe_filename(province)
        if not province_dir.is_dir():
            continue
        try:
            candidates = list(province_dir.iterdir())
        except OSError:
            continue
        # 精确标题优先。
        ordered = sorted(candidates, key=lambda p: (0 if p.stem == title_base else 1, p.name.casefold()))
        for path in ordered:
            if not path.is_file():
                continue
            if expected_ext and path.suffix.lower() != expected_ext:
                continue
            if path.stem == title_base or (url_suffix and url_suffix in path.stem):
                if valid_local_file(path, kind):
                    return path

    # 不能把同名文件跨省匹配。多个省份可能都发布“2026年7月代理购电
    # 电价”之类的同名资料；脱离省份目录只按文件名查找会把未获取项误报为
    # 已获取。没有 URL 报告或对应省份目录时，保持 missing 更可靠。
    return None


def attach_local_status(
    province: dict[str, Any], report: dict[str, dict[str, Any]]
) -> None:
    acquired = 0
    for item in province["items"]:
        path = find_local_file(item, province["province"], report)
        if path is None:
            item.update(
                {
                    "local_status": "missing",
                    "download_status": "missing",
                    "status": "missing",
                    "local_file": "",
                    "local_path": "",
                    "bytes": 0,
                    "downloaded_at": "",
                }
            )
            continue
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        rel = path.relative_to(ROOT).as_posix()
        old = report.get(item["url"], {})
        item.update(
            {
                "local_status": "acquired",
                "download_status": "acquired",
                "status": "acquired",
                "local_file": rel,
                "local_path": rel,
                "bytes": size,
                "downloaded_at": as_text(old.get("time") or old.get("downloaded_at")),
            }
        )
        acquired += 1

    counts = Counter(item["format"] for item in province["items"])
    status_counts = Counter(item["local_status"] for item in province["items"])
    province["counts"] = {
        "directory_count": len(province["items"]),
        "item_count": len(province["items"]),
        "pdf_count": counts.get("pdf", 0),
        "image_count": counts.get("image", 0),
        "html_count": counts.get("html", 0),
        "other_count": counts.get("other", 0),
        "acquired_count": status_counts.get("acquired", 0),
        "missing_count": status_counts.get("missing", 0),
        "by_type": {
            "pdf": counts.get("pdf", 0),
            "image": counts.get("image", 0),
            "html": counts.get("html", 0),
            "other": counts.get("other", 0),
        },
    }
    tab = province.get("tab") or {}
    province["tab_name"] = tab.get("name", "")
    province["tab_id"] = tab.get("id", "")
    province["tab_url"] = tab.get("url", "")
    province["tab_found"] = bool(tab.get("name"))
    province["tab_matches_proxy_power"] = bool(tab.get("matches_proxy_power"))


def validate(provinces: list[dict[str, Any]], source_meta: list[dict[str, Any]]) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    seen_urls: dict[str, list[str]] = defaultdict(list)
    replacement = "\ufffd"
    for province in provinces:
        pname = province["province"]
        if replacement in pname or replacement in province.get("tab_name", ""):
            warnings.append({"code": "encoding_replacement", "province": pname, "message": "名称中出现 Unicode 替换字符"})
        if province.get("fallback_used"):
            warnings.append(
                {
                    "code": "fallback_used",
                    "province": pname,
                    "source": province.get("source", ""),
                    "message": "当前扫描条目不完整，标准化视图已采用条目更完整的历史扫描快照",
                }
            )
        if not province.get("tab_found") and province["items"]:
            warnings.append({"code": "items_without_tab", "province": pname, "message": "有资料但没有识别到代理购电页签"})
        if not province["items"]:
            warnings.append({"code": "no_items", "province": pname, "message": "该省本次来源没有资料条目；可能是网站无资料或扫描未加载完成"})
        for item in province["items"]:
            url = item["url"]
            if not url:
                errors.append({"code": "missing_url", "province": pname, "title": item["title"]})
            else:
                seen_urls[url].append(pname)
            if item["format"] == "other":
                warnings.append({"code": "unknown_format", "province": pname, "title": item["title"], "url": url})
            if item["format"] == "pdf" and item["extension"] and item["extension"].lower() != "pdf":
                warnings.append({"code": "pdf_extension_mismatch", "province": pname, "title": item["title"], "url": url})
    for url, owners in seen_urls.items():
        if len(owners) > 1:
            errors.append({"code": "duplicate_url", "url": url, "provinces": owners})
    for meta in source_meta:
        if meta.get("exists") and not meta.get("valid_shape"):
            warnings.append({"code": "source_shape", "source": meta["name"], "message": "文件存在但没有可识别的 results/records 数组"})
    return {
        "schema_version": "1.0",
        "validated_at": now_iso(),
        "ok": not errors,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors,
        "warnings": warnings,
        "sources": source_meta,
    }


def build_view() -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    source_meta: list[dict[str, Any]] = []
    for name, path, rank in SOURCE_SPECS:
        meta: dict[str, Any] = {"name": name, "path": str(path), "exists": path.exists(), "rank": rank}
        source_meta.append(meta)
        if not path.exists():
            continue
        try:
            payload = read_json(path)
            records, declared_time = source_records(payload)
        except ValueError as exc:
            meta["error"] = str(exc)
            continue
        stamp = source_timestamp(path, declared_time)
        meta.update({"valid_shape": bool(records), "record_count": len(records), "scanned_at": stamp})
        for record in records:
            normalized = normalize_result(record, name, stamp)
            normalized["source_rank"] = rank
            key = (normalized.get("network") or "sgcc", normalized["province_code"] or normalized["province"])
            if key:
                candidates[key].append(normalized)

    selected: list[dict[str, Any]] = []
    for key, options in candidates.items():
        chosen = max(options, key=source_sort_key)
        # 不把内部完整 source_record/raw 带入网页文件，避免 JSON 过大且暴露
        # 不需要的 Vue 字段；标准化前的来源仍保存在原始扫描文件中。
        chosen = dict(chosen)
        chosen.pop("source_record", None)
        chosen.pop("source_rank", None)
        candidate_counts = {
            option["source"]: len(option["items"])
            for option in options
        }
        chosen["source_candidates"] = candidate_counts
        chosen["fallback_used"] = chosen["source"] != "live"
        chosen["data_status"] = (
            "empty_unconfirmed"
            if not chosen["items"]
            else "fallback"
            if chosen["fallback_used"]
            else "current"
        )
        selected.append(chosen)
    selected.sort(key=lambda x: (x.get("province_code", ""), x.get("province", "")))

    report = load_download_report()
    for province in selected:
        attach_local_status(province, report)
        # 兼容页面直接使用 province.items 的简短状态字段。
        for item in province["items"]:
            item.pop("raw", None)

    summary_counts = Counter()
    status_counts = Counter()
    for province in selected:
        summary_counts.update(item["format"] for item in province["items"])
        status_counts.update(item["local_status"] for item in province["items"])
    view = {
        "schema_version": "1.0",
        "generated_at": now_iso(),
        "source": {
            "page": PAGE_URL,
            "selection_policy": "每省选择条目数最多、同量时页签/URL/时间更优的来源",
            "source_files": source_meta,
        },
        "summary": {
            "province_count": len(selected),
            "tab_found_count": sum(bool(x.get("tab_found")) for x in selected),
            "province_with_items": sum(bool(x["items"]) for x in selected),
            "fallback_province_count": sum(bool(x.get("fallback_used")) for x in selected),
            "directory_count": sum(len(x["items"]) for x in selected),
            "item_count": sum(len(x["items"]) for x in selected),
            "pdf_count": summary_counts.get("pdf", 0),
            "image_count": summary_counts.get("image", 0),
            "html_count": summary_counts.get("html", 0),
            "other_count": summary_counts.get("other", 0),
            "acquired_count": status_counts.get("acquired", 0),
            "missing_count": status_counts.get("missing", 0),
            "by_type": {
                "pdf": summary_counts.get("pdf", 0),
                "image": summary_counts.get("image", 0),
                "html": summary_counts.get("html", 0),
                "other": summary_counts.get("other", 0),
            },
        },
        "provinces": selected,
    }
    flat: list[dict[str, Any]] = []
    for province in selected:
        for item in province["items"]:
            flat.append(
                {
                    "item_id": item["item_id"],
                    "province": province["province"],
                    "province_code": province["province_code"],
                    "city": province["region"].get("city", ""),
                    "county": province["region"].get("county", ""),
                    "tab_name": province.get("tab_name", ""),
                    "tab_id": province.get("tab_id", ""),
                    "title": item["title"],
                    "url": item["url"],
                    "extension": item["extension"],
                    "format": item["format"],
                    "network": province.get("network", "sgcc"),
                    "local_status": item["local_status"],
                    "local_file": item["local_file"],
                    "bytes": item["bytes"],
                }
            )
    validation = validate(selected, source_meta)
    return view, flat, validation


def write_outputs(view: dict[str, Any], flat: list[dict[str, Any]], validation: dict[str, Any]) -> None:
    CATALOG_OUTPUT.write_text(json.dumps(view, ensure_ascii=False, indent=2), encoding="utf-8")
    ITEMS_OUTPUT.write_text(json.dumps(flat, ensure_ascii=False, indent=2), encoding="utf-8")
    VALIDATION_OUTPUT.write_text(json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = [
        "province",
        "province_code",
        "tab_name",
        "directory_count",
        "pdf_count",
        "image_count",
        "html_count",
        "other_count",
        "acquired_count",
        "missing_count",
        "city",
        "county",
    ]
    with CSV_OUTPUT.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for province in view["provinces"]:
            row = {
                "province": province["province"],
                "province_code": province["province_code"],
                "tab_name": province.get("tab_name", ""),
                **province["counts"],
                "city": province["region"].get("city", ""),
                "county": province["region"].get("county", ""),
            }
            writer.writerow({field: row.get(field, "") for field in fields})


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="生成文件后，如果有字段错误则返回非零退出码")
    parser.add_argument("--no-write", action="store_true", help="只打印摘要，不写输出文件")
    args = parser.parse_args(list(argv) if argv is not None else None)
    view, flat, validation = build_view()
    if not args.no_write:
        write_outputs(view, flat, validation)
    summary = view["summary"]
    print(
        "标准化完成："
        f"省份 {summary['province_count']}，页签 {summary['tab_found_count']}，"
        f"目录 {summary['directory_count']}，PDF {summary['pdf_count']}，"
        f"图片 {summary['image_count']}，网页 {summary['html_count']}，其他 {summary['other_count']}，"
        f"已获取 {summary['acquired_count']}，未获取 {summary['missing_count']}"
    )
    print(f"校验：错误 {validation['error_count']}，警告 {validation['warning_count']}")
    if not args.no_write:
        print(f"目录视图：{CATALOG_OUTPUT}")
        print(f"校验报告：{VALIDATION_OUTPUT}")
    return 1 if args.check and validation["error_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
