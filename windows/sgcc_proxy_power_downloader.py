"""国家电网 95598 各省代理购电资料扫描与 PDF 下载工具。

示例：
  python sgcc_proxy_power_downloader.py --all
  python sgcc_proxy_power_downloader.py --scan
  python sgcc_proxy_power_downloader.py --download
  python sgcc_proxy_power_downloader.py --download --province 北京市

扫描阶段使用本机 Microsoft Edge 加载 95598 页面，并调用页面自身已经解密的
Vue 数据组件；下载阶段直接校验并保存 PDF 二进制文件。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parent
CATALOG_FILE = ROOT / "proxy_power_catalog.json"
REPORT_FILE = ROOT / "proxy_power_download_report.json"
# Verified page-component results are kept separate from the earlier exploratory
# click run, so province assignment and duplicate titles remain unambiguous.
DOWNLOAD_ROOT = ROOT / "provinces_verified"
# P2019 is the current "执行的电价和收费标准" view.  The legacy route
# silently redirects to the older component and misses recently published rows.
PAGE_URL = "https://95598.cn/osgweb/ipElectrovalenceStandard?partNo=P2019"
REGION_BASE = "https://95598.cn/pubcode/app/divisionGb"
EDGE_CANDIDATES = [
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
]
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/152 Safari/537.36 Edg/152",
    "Accept": "application/json, text/plain, */*",
    "Referer": PAGE_URL,
}
ORG_CODE_BY_PROVINCE = {
    "110000": "11102", "120000": "12101", "130000": "13102", "140000": "14101",
    "150000": "15101", "210000": "21102", "220000": "22101", "230000": "23101",
    "310000": "31102", "320000": "32101", "330000": "33101", "340000": "34101",
    "350000": "35101", "360000": "36101", "370000": "37101", "410000": "41101",
    "420000": "42102", "430000": "43101", "500000": "50101", "510000": "51101",
    "520000": "52101", "540000": "54101", "610000": "61102", "620000": "62101",
    "630000": "63101", "640000": "64101", "650000": "65101",
}


COLLECT_JS = r"""
async (codes) => {
  const root = document.querySelector('#app')?.__vue__;
  if (!root?.$tools?.apiPost || !root?.$api?.infoPublicNew || !root?.$api?.infoPublicNewDetails) {
    return {error:'未找到当前信息公开公告接口'};
  }
  let header = null, listVm = null; const listCandidates = [], seen = new Set();
  function walk(vm, depth=0) {
    if (!vm || seen.has(vm) || depth > 45) return;
    seen.add(vm);
    if (Array.isArray(vm.cityList) && vm.cityList.length) header = vm;
    if (typeof vm.getPublickInfo === 'function' && Array.isArray(vm.listInformation)) listCandidates.push(vm);
    (vm.$children || []).forEach(child => walk(child, depth + 1));
  }
  walk(root);
  listVm = listCandidates.sort((a, b) => {
    const score = x => (x.listInformation || []).filter(row => row?.releaseInfoId && row?.title).length;
    return score(b) - score(a);
  })[0] || null;
  const province = (header?.cityList || []).find(x => String(x.codeValue || x.content1) === String(codes.provinceCode));
  const orgCode = String(codes.orgCode || province?.content2 || province?.content3 || province?.content6 || '');
  if (!orgCode) return {error:'未找到省级组织代码', categories:[], category:null, items:[]};
  if (!listVm) return {error:'未找到当前公告列表组件', categories:[], category:null, items:[]};
  const pageSize = Number(listVm.pageCount || 10);
  const isProxyPurchase = value => /代理.*购电|购电.*代理/.test(String(value || ''));
  const rows = (listVm.listInformation || []).filter(x => isProxyPurchase(x.title));
  let count = 0;
  for (let pageIndex=1; pageIndex<=200; pageIndex++) {
    listVm.nowpage = pageIndex;
    listVm.keyWords = '';
    listVm.orgCodeCity = '';
    listVm.orgCodeCounty = '';
    listVm.listInformation = [];
    listVm.getPublickInfo();
    for (let wait=0; wait<120 && !listVm.listInformation?.length; wait++) {
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    const pageRows = Array.isArray(listVm.listInformation) ? listVm.listInformation : [];
    count = Number(listVm.maxpage || count || 0);
    rows.push(...pageRows.filter(x => isProxyPurchase(x.title)));
    if (!pageRows.length || pageIndex * pageSize >= count) break;
  }
  const items = [];
  const itemSeen = new Set();
  const rowSeen = new Set();
  for (const row of rows) {
    if (!row.releaseInfoId || rowSeen.has(row.releaseInfoId)) continue;
    rowSeen.add(row.releaseInfoId);
    const detailResponse = await root.$tools.apiPost(
      {releaseInfoId:String(row.releaseInfoId || '')}, root.$api.infoPublicNewDetails
    );
    const detail = detailResponse?.data || {};
    for (const attach of (detail.attachList || [])) {
      const suffixMatch = String(attach.attachName || '').match(/(\.[a-zA-Z0-9]{2,5})$/);
      const suffix = suffixMatch ? suffixMatch[1].toLowerCase() : '';
      const attachId = String(attach.attachId || attach.attachUuid || '');
      if (!attachId) continue;
      const title = (detail.title || row.title || '') + ((detail.attachList || []).length > 1 ? ' - ' + (attach.attachName || attachId) : '');
      const item = {
        electricpriceId:String(row.releaseInfoId || ''),
        electricpriceTitle:String(title),
        electricpriceInfo:'/omg-static/' + attachId + suffix,
        updateAt:String(row.releaseTime || detail.releaseTime || ''),
        attachName:String(attach.attachName || ''),
        attachId,
        releaseInfoId:String(row.releaseInfoId || ''),
        orgCodeProvince:orgCode,
        source:'P2019'
      };
      const key = item.electricpriceInfo;
      if (!itemSeen.has(key)) { itemSeen.add(key); items.push(item); }
    }
  }
  const category = {classifyId:'P2019-03', classifyName:'执行的电价和收费标准 / 代理购电', orgCodeProvince:orgCode};
  return {categories:[category], category, matchedCategories:[category], items, source:'P2019', totalAnnouncements:count};
}
"""


def edge_path() -> Path:
    for path in EDGE_CANDIDATES:
        if path.exists():
            return path
    raise FileNotFoundError("未找到 Microsoft Edge")


def safe_name(value: str) -> str:
    return re.sub(r'[\\/:*?"<>|\r\n]+', "_", value).strip(" ._") or "未命名"


def absolute_url(value: str) -> str:
    if value.startswith("http://") or value.startswith("https://"):
        url = value
    elif value.startswith("//"):
        url = "https:" + value
    else:
        url = "https://95598.cn/" + value.lstrip("/")
    return url.replace("/omg-static//omg-static/", "/omg-static/")


def is_pdf_url(value: str) -> bool:
    return bool(re.search(r"\.pdf(?:$|\?)", value, re.I))


def region_candidates(session: requests.Session) -> list[dict]:
    provinces = session.get(f"{REGION_BASE}/-1.json", timeout=30).json()
    output = []
    for province in provinces:
        province_code = str(province.get("codeValue", ""))
        cities = session.get(f"{REGION_BASE}/{province_code}.json", timeout=30).json()
        # The current page is province-scoped; keep a province-only request as
        # the first candidate.  Older releases may still require a city/county,
        # so the historical candidates remain as fallbacks.
        candidates = [{
            "provinceCode": province_code,
            "orgCode": ORG_CODE_BY_PROVINCE.get(province_code, ""),
            "cityCode": "",
            "countyCode": "",
            "city": "",
            "county": "",
        }]
        for city in cities[:4]:
            city_code = str(city.get("codeValue", province_code))
            try:
                counties = session.get(f"{REGION_BASE}/{city_code}.json", timeout=30).json()
            except Exception:
                counties = []
            county = next((x for x in counties if x.get("codeValue")), None)
            if county:
                candidates.append({
                    "provinceCode": province_code,
                    "orgCode": ORG_CODE_BY_PROVINCE.get(province_code, ""),
                    "cityCode": city_code,
                    "countyCode": str(county["codeValue"]),
                    "city": city.get("codeName", ""),
                    "county": county.get("codeName", ""),
                })
        output.append({"province": province.get("codeName", ""), "provinceCode": province_code, "candidates": candidates})
    return output


def normalize_item(raw: dict) -> dict:
    title = raw.get("knoweleTitle") or raw.get("electricpriceTitle") or raw.get("classifyName") or raw.get("className") or raw.get("labelName") or ""
    url = raw.get("pdfAddress") or raw.get("electricpriceInfo") or raw.get("classContentUrl") or raw.get("classContent") or ""
    extension = Path(urlparse(url).path).suffix.lower()
    return {"title": title, "url": absolute_url(url) if url else "", "is_pdf": is_pdf_url(url), "type": "pdf" if extension == ".pdf" else "image" if extension in {".png", ".jpg", ".jpeg", ".gif", ".webp"} else "other", "raw": raw}


def newest_item_key(item: dict) -> tuple[int, int, str]:
    match = re.search(r"(20\d{2})年\s*(\d{1,2})月", str(item.get("title") or ""))
    return (int(match.group(1)), int(match.group(2)), str(item.get("title") or "")) if match else (0, 0, str(item.get("title") or ""))


def select_page_province(page, province_code: str) -> None:
    """Use the site's own region switch so its encrypted address context matches the API payload."""
    page.wait_for_function(
        """() => {
          let found=false, seen=new Set();
          function walk(vm, depth=0) {
            if (!vm || seen.has(vm) || depth > 40) return;
            seen.add(vm);
            if (vm.$options?.name === 'Header' && Array.isArray(vm.cityList) && vm.cityList.length) found=true;
            (vm.$children || []).forEach(x => walk(x, depth + 1));
          }
          walk(document.querySelector('#app')?.__vue__); return found;
        }""",
        timeout=60000,
    )
    try:
        page.evaluate(
            """provinceCode => {
              let header = null, seen = new Set();
              function walk(vm, depth=0) {
                if (!vm || seen.has(vm) || depth > 40) return;
                seen.add(vm);
                if (vm.$options?.name === 'Header' && Array.isArray(vm.cityList)) header = vm;
                (vm.$children || []).forEach(x => walk(x, depth + 1));
              }
              walk(document.querySelector('#app')?.__vue__);
              const item = (header?.cityList || []).find(x => String(x.codeValue) === String(provinceCode));
              if (!item) throw new Error('province switch item not found');
              header.changeCity(item);
            }""",
            province_code,
        )
    except Exception:
        # changeCity normally destroys the execution context while navigating.
        pass
    page.wait_for_timeout(7000)
    page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(9000)


def scan(province_filter: str = "") -> dict:
    session = requests.Session()
    session.headers.update(HTTP_HEADERS)
    regions = region_candidates(session)
    filters = [x.strip() for x in re.split(r"[,，]", province_filter) if x.strip()]
    previous_results = []
    if filters and CATALOG_FILE.exists():
        try:
            previous_results = json.loads(CATALOG_FILE.read_text(encoding="utf-8")).get("results", [])
        except (OSError, json.JSONDecodeError):
            previous_results = []
    if filters:
        regions = [region for region in regions if any(name in region["province"] for name in filters)]
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            executable_path=str(edge_path()),
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = browser.new_page(viewport={"width": 1600, "height": 1200})
        page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)
        for _ in range(100):
            try:
                ready = page.evaluate("""()=>{const r=document.querySelector('#app')?.__vue__;return !!(r?.$tools?.apiPost&&r?.$api?.infoPublicNew&&r?.$api?.infoPublicNewDetails)}""")
            except Exception:
                page.wait_for_timeout(500)
                continue
            if ready:
                break
            page.wait_for_timeout(400)
        for index, region in enumerate(regions, 1):
            select_page_province(page, region["provinceCode"])
            chosen = None
            collected = None
            errors = []
            for candidate in region["candidates"]:
                try:
                    value = page.evaluate(COLLECT_JS, candidate)
                    if value.get("error"):
                        errors.append(value["error"])
                    if collected is None or (value.get("category") and len(value.get("items") or []) > len(collected.get("items") or [])):
                        chosen, collected = candidate, value
                    if value.get("source") == "P2019" and value.get("category"):
                        break
                except Exception as exc:
                    errors.append(repr(exc))
            collected = collected or {"categories": [], "category": None, "items": []}
            items = [normalize_item(x) for x in collected.get("items", [])]
            items.sort(key=newest_item_key, reverse=True)
            result = {
                "province": region["province"],
                "provinceCode": region["provinceCode"],
                "region": chosen,
                "category": normalize_item(collected["category"]) if collected.get("category") else None,
                "items": items,
                "pdf_count": sum(x["is_pdf"] for x in items),
                "errors": errors,
            }
            results.append(result)
            print(f"{index}/{len(regions)} {region['province']}: 目录 {len(items)}，PDF {result['pdf_count']}", flush=True)
        browser.close()
    if filters and previous_results:
        updates = {row.get("provinceCode"): row for row in results}
        results = [updates.pop(row.get("provinceCode"), row) for row in previous_results]
        results.extend(updates.values())
    catalog = {"scanned_at": datetime.now().isoformat(timespec="seconds"), "source": PAGE_URL, "results": results}
    CATALOG_FILE.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    return catalog


def unique_target(province_dir: Path, title: str, url: str, force_suffix: bool = False) -> Path:
    base = safe_name(title)
    stem = Path(urlparse(url).path).stem[-10:]
    extension = Path(urlparse(url).path).suffix.lower() or ".bin"
    if force_suffix:
        return province_dir / f"{base}_{stem}{extension}"
    target = province_dir / f"{base}{extension}"
    if target.exists():
        try:
            if target.stat().st_size > 0:
                return target
        except OSError:
            pass
    if target.exists():
        target = province_dir / f"{base}_{stem}{extension}"
    return target


def download_one(province: str, item: dict) -> dict:
    province_dir = DOWNLOAD_ROOT / safe_name(province)
    province_dir.mkdir(parents=True, exist_ok=True)
    recorded = item.get("local_file", "")
    if recorded and str((ROOT / recorded).resolve()).lower().startswith(str(DOWNLOAD_ROOT.resolve()).lower()):
        recorded_path = ROOT / recorded
        if recorded_path.exists():
            try:
                data = recorded_path.read_bytes()
                if data:
                    return {**item, "province": province, "status": "existing", "file": recorded, "bytes": len(data)}
            except OSError:
                pass
    target = unique_target(province_dir, item["title"], item["url"], bool(item.get("force_suffix")))
    if target.exists():
        try:
            if target.stat().st_size > 0:
                return {**item, "province": province, "status": "existing", "file": str(target.relative_to(ROOT)), "bytes": target.stat().st_size}
        except OSError:
            pass
    last_error = ""
    for _ in range(3):
        try:
            response = requests.get(item["url"], headers=HTTP_HEADERS, timeout=90)
            data = response.content
            expected = Path(urlparse(item["url"]).path).suffix.lower()
            valid = data.startswith(b"%PDF") if expected == ".pdf" else data.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"RIFF"))
            if response.status_code == 200 and valid:
                target.write_bytes(data)
                return {**item, "province": province, "status": "downloaded", "file": str(target.relative_to(ROOT)), "bytes": len(data)}
            last_error = f"HTTP {response.status_code}, 附件格式校验失败"
        except Exception as exc:
            last_error = repr(exc)
    return {**item, "province": province, "status": "failed", "error": last_error}


def download(catalog: dict, province_filter: str = "", workers: int = 10) -> list[dict]:
    jobs = []
    filters = [x.strip() for x in re.split(r"[,，]", province_filter) if x.strip()]
    previous = {}
    if REPORT_FILE.exists():
        try:
            previous = {x.get("url"): x for x in json.loads(REPORT_FILE.read_text(encoding="utf-8")).get("items", [])}
        except (OSError, json.JSONDecodeError):
            previous = {}
    for result in catalog.get("results", []):
        province = result.get("province", "未知地区")
        if filters and not any(name in province for name in filters):
            continue
        pdf_items = [item for item in result.get("items", []) if item.get("url") and item.get("type") in {"pdf", "image"}]
        title_counts = {}
        for item in pdf_items:
            title_counts[item.get("title", "")] = title_counts.get(item.get("title", ""), 0) + 1
        for item in pdf_items:
            if item.get("url"):
                item = dict(item)
                old = previous.get(item.get("url"), {})
                if old.get("file") and (ROOT / old["file"]).exists():
                    item["local_file"] = old["file"]
                item["force_suffix"] = title_counts.get(item.get("title", ""), 0) > 1
                jobs.append((province, item))
    reports = []
    lock = threading.Lock()
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = [executor.submit(download_one, province, item) for province, item in jobs]
        for future in concurrent.futures.as_completed(futures):
            record = future.result()
            reports.append(record)
            with lock:
                done += 1
                print(f"{done}/{len(jobs)} {record['status']} {record['province']} / {record['title']}", flush=True)
    reports.sort(key=lambda x: (x.get("province", ""), x.get("title", ""), x.get("url", "")))
    REPORT_FILE.write_text(json.dumps({"finished_at": datetime.now().isoformat(timespec="seconds"), "items": reports}, ensure_ascii=False, indent=2), encoding="utf-8")
    return reports


def load_catalog() -> dict:
    """读取标准扫描结果；兼容本次已生成的扁平全省清单。"""
    if CATALOG_FILE.exists():
        return json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
    flat_file = ROOT / "all_pdf_catalog.json"
    if flat_file.exists():
        flat = json.loads(flat_file.read_text(encoding="utf-8"))
        grouped = {}
        for item in flat:
            if not item.get("url", "").lower().endswith(".pdf"):
                continue
            province = item.get("province_name") or "未标注省份"
            grouped.setdefault(province, []).append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "is_pdf": True,
                "raw": item,
            })
        return {"scanned_at": "", "source": PAGE_URL, "results": [
            {"province": province, "items": items, "pdf_count": len(items)}
            for province, items in grouped.items()
        ]}
    raise FileNotFoundError("没有找到代理购电扫描清单，请先运行 --scan")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", action="store_true", help="重新扫描各省代理购电目录")
    parser.add_argument("--download", action="store_true", help="下载目录中的 PDF")
    parser.add_argument("--all", action="store_true", help="扫描后立即下载")
    parser.add_argument("--province", default="", help="仅下载名称包含此文本的省份")
    parser.add_argument("--workers", type=int, default=10, help="并行下载数量")
    args = parser.parse_args()
    if not (args.scan or args.download or args.all):
        args.all = True
    catalog = scan(args.province) if (args.scan or args.all) else load_catalog()
    if args.download or args.all:
        report = download(catalog, args.province, args.workers)
        success = sum(x["status"] in {"downloaded", "existing"} for x in report)
        print(f"完成：{success}/{len(report)} 个 PDF 已保存在 {DOWNLOAD_ROOT}")


if __name__ == "__main__":
    main()
