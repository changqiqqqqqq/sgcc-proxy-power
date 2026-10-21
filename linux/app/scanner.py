from __future__ import annotations

import logging
import tempfile
import time
import re
from datetime import datetime
from typing import Any

import httpx
from playwright.sync_api import Page, sync_playwright

from .catalog import PAGE_URL, absolute_url, read_json
from .config import Settings


LOGGER = logging.getLogger(__name__)
ORG_CODE_BY_PROVINCE = {
    "110000": "11102", "120000": "12101", "130000": "13102", "140000": "14101",
    "150000": "15101", "210000": "21102", "220000": "22101", "230000": "23101",
    "310000": "31102", "320000": "32101", "330000": "33101", "340000": "34101",
    "350000": "35101", "360000": "36101", "370000": "37101", "410000": "41101",
    "420000": "42102", "430000": "43101", "500000": "50101", "510000": "51101",
    "520000": "52101", "540000": "54101", "610000": "61102", "620000": "62101",
    "630000": "63101", "640000": "64101", "650000": "65101",
}
REGION_BASE = "https://95598.cn/pubcode/app/divisionGb"
REGION_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/132 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": PAGE_URL,
}


READY_JS = r"""
() => {
  const root = document.querySelector('#app')?.__vue__;
  let ready = false; const seen = new Set();
  function walk(v, depth=0) {
    if (!v || seen.has(v) || depth > 45) return;
    seen.add(v);
    if (v.$tools?.apiPost && v.$api?.infoPublicNew && v.$api?.infoPublicNewDetails) ready = true;
    (v.$children || []).forEach(x => walk(x, depth + 1));
  }
  walk(root); return ready;
}
"""


EXTRACT_JS = r"""
async (input) => {
  const region = input.region || input;
  const timeoutMs = Number(input.timeoutMs || 90000);
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const root = document.querySelector('#app')?.__vue__;
  if (!root) return {error:'Vue root not found', items:[]};
  const seen = new Set(); const candidates = [];
  function walk(vm, depth=0) {
    if (!vm || seen.has(vm) || depth > 50) return;
    seen.add(vm);
    if (typeof vm.getElectrovalenceType === 'function' && typeof vm.loadChild === 'function') candidates.push(vm);
    (vm.$children || []).forEach(child => walk(child, depth + 1));
  }
  walk(root);
  const vm = candidates[0];
  if (!vm) return {error:'electrovalence component not found', items:[]};
  const codes = {provinceCode:String(region.provinceCode || ''), cityCode:String(region.cityCode || ''), countyCode:String(region.countyCode || '')};
  if (!codes.provinceCode) return {error:'province code missing', items:[]};
  for (const key of Object.keys(codes)) vm[key] = codes[key];
  vm.listInformation = [];
  try { vm.getElectrovalenceType(); } catch (e) { return {error:'getElectrovalenceType: '+String(e), items:[]}; }
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline && !(Array.isArray(vm.listInformation) && vm.listInformation.length)) await sleep(250);
  const top = Array.isArray(vm.listInformation) ? vm.listInformation : [];
  const category = top.find(x => /代理购电|代理/.test(String(x.classifyName || x.knoweleTitle || x.labelName || '')) && x.classifyId != null);
  if (!category) return {error:'代理购电目录未找到', categories:top, items:[]};
  let child = [];
  try {
    const returned = vm.loadChild(category, null, x => { child = Array.isArray(x) ? x : []; });
    if (returned && typeof returned.then === 'function') { const value = await returned; if (Array.isArray(value)) child = value; }
  } catch (e) { return {error:'loadChild: '+String(e), category, items:[]}; }
  while (Date.now() < deadline && !child.length) await sleep(250);
  const items = child.map(x => ({
    electricpriceTitle:String(x.electricpriceTitle || x.knoweleTitle || x.className || x.labelName || ''),
    electricpriceInfo:String(x.electricpriceInfo || x.pdfAddress || x.classContentUrl || x.classContent || ''),
    updateAt:String(x.updateAt || x.releaseTime || ''),
    electricpriceId:String(x.electricpriceId || x.id || ''), raw:x
  })).filter(x => x.electricpriceTitle && x.electricpriceInfo);
  return {category, categories:[category], items, source:'electrovalenceStandard'};
}
"""


class Scanner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _get_json(self, client: httpx.Client, url: str) -> list[dict[str, Any]]:
        response = client.get(url)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, list):
            raise RuntimeError(f"地区接口返回格式异常：{url}")
        return [x for x in value if isinstance(x, dict)]

    def _preferred_regions(self) -> dict[str, dict[str, str]]:
        raw = read_json(self.settings.catalog_file, {})
        rows = raw.get("results") or raw.get("provinces") or [] if isinstance(raw, dict) else []
        preferred: dict[str, dict[str, str]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            province_code = str(row.get("provinceCode") or row.get("province_code") or "")
            region = row.get("region") if isinstance(row.get("region"), dict) else {}
            city_code = str(region.get("cityCode") or region.get("city_code") or "")
            county_code = str(region.get("countyCode") or region.get("county_code") or "")
            if province_code and city_code and county_code:
                preferred[province_code] = {
                    "provinceCode": province_code,
                    "cityCode": city_code,
                    "countyCode": county_code,
                    "city": str(region.get("city") or "上次有效市"),
                    "county": str(region.get("county") or "上次有效区县"),
                    "provinceName": str(row.get("province") or row.get("name") or ""),
                }
        return preferred

    @staticmethod
    def _representative(values: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        if len(values) <= limit:
            return values
        indexes = {round(index * (len(values) - 1) / (limit - 1)) for index in range(limit)}
        return [values[index] for index in sorted(indexes)]

    def _discover_candidates(self, province_code: str, deadline: float) -> list[dict[str, str]]:
        timeout = httpx.Timeout(self.settings.scan_request_timeout_seconds)
        # New P2019 page accepts a province-only selection.  Try it before
        # city/county combinations used by the legacy component.
        candidates: list[dict[str, str]] = [{
            "provinceCode": province_code,
            "orgCode": ORG_CODE_BY_PROVINCE.get(province_code, ""),
            "cityCode": "",
            "countyCode": "",
            "city": "",
            "county": "",
        }]
        with httpx.Client(headers=REGION_HEADERS, timeout=timeout, follow_redirects=True) as client:
            cities = self._get_json(client, f"{REGION_BASE}/{province_code}.json")
            for city in self._representative(cities, 6):
                if time.monotonic() >= deadline:
                    break
                city_code = str(city.get("codeValue") or province_code)
                try:
                    counties = self._get_json(client, f"{REGION_BASE}/{city_code}.json")
                except Exception as exc:
                    LOGGER.warning("读取 %s 区县失败：%s", city.get("codeName"), exc)
                    continue
                for county in self._representative([x for x in counties if x.get("codeValue")], 3):
                    candidates.append({
                        "provinceCode": province_code,
                        "orgCode": ORG_CODE_BY_PROVINCE.get(province_code, ""),
                        "cityCode": city_code,
                        "countyCode": str(county.get("codeValue")),
                        "city": str(city.get("codeName") or ""),
                        "county": str(county.get("codeName") or ""),
                    })
                    if len(candidates) >= self.settings.max_region_candidates:
                        return candidates
        return candidates

    def regions(self) -> list[dict[str, Any]]:
        timeout = httpx.Timeout(self.settings.scan_request_timeout_seconds)
        preferred = self._preferred_regions()
        try:
            with httpx.Client(headers=REGION_HEADERS, timeout=timeout, follow_redirects=True) as client:
                provinces = self._get_json(client, f"{REGION_BASE}/-1.json")
        except Exception:
            if not preferred:
                raise
            LOGGER.warning("地区总表读取失败，使用上次有效省份列表", exc_info=True)
            provinces = [
                {"codeValue": code, "codeName": value.get("provinceName") or code}
                for code, value in preferred.items()
            ]
        output: list[dict[str, Any]] = []
        for province in provinces:
            province_code = str(province.get("codeValue") or "")
            candidate = preferred.get(province_code)
            province_name = str(province.get("codeName") or (candidate or {}).get("provinceName") or province_code)
            output.append({
                "province": province_name,
                "provinceCode": province_code,
                "candidates": [candidate] if candidate else [],
                "discover_fallback": True,
            })
        return output

    def _wait_ready(self, page: Page, timeout_seconds: float | None = None) -> None:
        timeout = timeout_seconds if timeout_seconds is not None else self.settings.navigation_timeout_seconds
        page.wait_for_function(READY_JS, timeout=max(1, int(timeout * 1000)))

    def _ensure_ready(self, page: Page, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("单省页面准备超时")
        try:
            self._wait_ready(page, min(5, remaining))
        except Exception:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("单省页面准备超时")
            page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=max(1, int(remaining * 1000)))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("单省页面准备超时")
            page.wait_for_timeout(1500)
            self._wait_ready(page, remaining)

    @staticmethod
    def _newest_item_key(item: dict[str, Any]) -> tuple[int, int, str]:
        match = re.search(r"(20\d{2})年\s*(\d{1,2})月", str(item.get("title") or ""))
        return (int(match.group(1)), int(match.group(2)), str(item.get("title") or "")) if match else (0, 0, str(item.get("title") or ""))

    @staticmethod
    def _normalize_result(province: dict[str, Any], region: dict[str, str] | None, value: dict[str, Any]) -> dict[str, Any]:
        category_raw = value.get("category") if isinstance(value.get("category"), dict) else None
        category = None
        if category_raw:
            category = {
                "title": str(category_raw.get("classifyName") or category_raw.get("classificationName") or category_raw.get("knoweleTitle") or ""),
                "id": str(category_raw.get("classifyId") or ""),
                "url": absolute_url(category_raw.get("knoweleContentUrl") or category_raw.get("classifyIcon")),
                "raw": category_raw,
            }
        items = []
        for raw in value.get("items") or []:
            if not isinstance(raw, dict):
                continue
            title = str(raw.get("electricpriceTitle") or raw.get("knoweleTitle") or "")
            url = absolute_url(raw.get("electricpriceInfo") or raw.get("pdfAddress") or raw.get("classContentUrl"))
            if not url:
                continue
            items.append({
                "title": title,
                "url": url,
                "publish_time": str(raw.get("updateAt") or raw.get("releaseTime") or ""),
                "category_id": str(raw.get("classifyId") or (category or {}).get("id") or ""),
                "raw": raw,
            })
        items.sort(key=Scanner._newest_item_key, reverse=True)
        return {
            "province": province["province"],
            "provinceCode": province["provinceCode"],
            "region": region or {},
            "category": category,
            "items": items,
            "errors": [str(value["error"])] if value.get("error") else [],
        }

    def _scan_province(self, page: Page, province: dict[str, Any]) -> dict[str, Any]:
        best: tuple[dict[str, str] | None, dict[str, Any]] = (None, {"categories": [], "category": None, "items": []})
        errors: list[str] = []
        deadline = time.monotonic() + self.settings.province_scan_timeout_seconds
        timed_out = False
        province_code = str(province.get("provinceCode") or "")
        candidates = [{
            "provinceCode": province_code,
            "orgCode": ORG_CODE_BY_PROVINCE.get(province_code, ""),
            "cityCode": "", "countyCode": "", "city": "", "county": "",
        }]
        candidates.extend(list(province.get("candidates") or []))
        candidate_keys = {(x.get("cityCode"), x.get("countyCode")) for x in candidates}
        fallback_loaded = False
        candidate_index = 0
        while candidate_index < len(candidates) or not fallback_loaded:
            if candidate_index >= len(candidates):
                fallback_loaded = True
                try:
                    discovered = self._discover_candidates(str(province.get("provinceCode") or ""), deadline)
                    for candidate in discovered:
                        key = (candidate.get("cityCode"), candidate.get("countyCode"))
                        if key not in candidate_keys:
                            candidate_keys.add(key)
                            candidates.append(candidate)
                except Exception as exc:
                    errors.append(f"候选地区读取失败：{exc}")
                if candidate_index >= len(candidates):
                    break
            region = candidates[candidate_index]
            candidate_index += 1
            for attempt in range(2):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    errors.append(f"单省扫描超过 {self.settings.province_scan_timeout_seconds} 秒，已继续下一省")
                    timed_out = True
                    break
                try:
                    self._ensure_ready(page, deadline)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("单省页面准备超时")
                    value = page.evaluate(
                        EXTRACT_JS,
                        {
                            "region": region,
                            "timeoutMs": min(self.settings.scan_request_timeout_seconds, max(1, int(remaining))) * 1000,
                        },
                    )
                except Exception as exc:
                    errors.append(f"{region.get('city')}/{region.get('county')} 第{attempt + 1}次：{exc}")
                    try:
                        page.wait_for_timeout(500)
                    except Exception:
                        pass
                    continue
                if not isinstance(value, dict):
                    errors.append("页面组件返回非对象数据")
                    continue
                if value.get("error"):
                    errors.append(str(value["error"]))
                current_score = (int(bool(value.get("category"))), len(value.get("items") or []))
                best_score = (int(bool(best[1].get("category"))), len(best[1].get("items") or []))
                if current_score > best_score:
                    best = (region, value)
                if value.get("category") and value.get("items"):
                    if value.get("source") == "P2019":
                        return self._normalize_result(province, region, value)
                    continue
            if timed_out:
                break
        result = self._normalize_result(province, best[0], best[1])
        result["errors"] = errors + result.get("errors", [])
        return result

    def scan(self, progress=None) -> dict[str, Any]:
        regions = self.regions()
        results: list[dict[str, Any]] = []
        with sync_playwright() as playwright:
            with tempfile.TemporaryDirectory(prefix="sgcc-playwright-") as profile_dir:
                launch_options: dict[str, Any] = {
                    "headless": self.settings.headless,
                    "chromium_sandbox": False,
                    "args": [
                        "--disable-blink-features=AutomationControlled",
                        "--disable-dev-shm-usage",
                        "--no-first-run",
                        "--no-default-browser-check",
                    ],
                    "locale": "zh-CN",
                    "viewport": {"width": 1600, "height": 1200},
                }
                if self.settings.browser_executable:
                    launch_options["executable_path"] = self.settings.browser_executable
                if self.settings.browser_proxy:
                    launch_options["proxy"] = {"server": self.settings.browser_proxy}
                context = playwright.chromium.launch_persistent_context(profile_dir, **launch_options)
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    page.set_default_timeout(self.settings.navigation_timeout_seconds * 1000)
                    page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=self.settings.navigation_timeout_seconds * 1000)
                    self._wait_ready(page)
                    for index, province in enumerate(regions, 1):
                        result = self._scan_province(page, province)
                        results.append(result)
                        if progress:
                            progress(index, len(regions), result)
                finally:
                    context.close()
        return {
            "schema_version": "1.0",
            "scanned_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "source": PAGE_URL,
            "results": results,
        }




