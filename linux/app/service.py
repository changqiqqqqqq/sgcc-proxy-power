from __future__ import annotations

import concurrent.futures
import logging
import threading
from copy import deepcopy
from datetime import datetime
from typing import Any

from .catalog import CatalogStore, merge_scan_results
from .csg import CSGScanner
from .config import Settings
from .downloader import Downloader
from .scanner import Scanner


LOGGER = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class AppService:
    def __init__(
        self,
        settings: Settings,
        store: CatalogStore | None = None,
        scanner: Scanner | None = None,
        csg_scanner: CSGScanner | None = None,
        downloader: Downloader | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or CatalogStore(settings.catalog_file, settings.download_dir, settings.report_file)
        self.scanner = scanner or Scanner(settings)
        self.csg_scanner = csg_scanner or CSGScanner(settings)
        self.downloader = downloader or Downloader(settings, self.store)
        self.download_after_scan = settings.download_after_scan
        self.lock = threading.RLock()
        self.scan_thread: threading.Thread | None = None
        self.download_thread: threading.Thread | None = None
        initial = self.store.snapshot()
        self.runtime: dict[str, Any] = {
            "phase": "idle",
            "message": "服务已启动。",
            "scan_running": False,
            "download_running": False,
            "scan_completed": 0,
            "scan_total": 32,
            "download_completed": 0,
            "download_total": 0,
            "current_province": "",
            "current_item": "",
            "last_error": "",
            "counts": initial.get("summary", {}),
            "last_scan": initial.get("scanned_at", ""),
            "updated_at": now_iso(),
        }

    def _update(self, **values: Any) -> None:
        with self.lock:
            self.runtime.update(values)
            self.runtime["updated_at"] = now_iso()

    def status(self) -> dict[str, Any]:
        with self.lock:
            runtime = deepcopy(self.runtime)
        runtime.update({
            "ok": True,
        })
        return runtime

    def scan(self) -> dict[str, Any]:
        with self.lock:
            if self.scan_thread and self.scan_thread.is_alive():
                return {"ok": False, "message": "扫描任务正在运行。"}
            self.runtime.update({
                "phase": "scanning",
                "message": "正在从 95598 获取各省代理购电目录…",
                "scan_running": True,
                "scan_completed": 0,
                "scan_total": 32,
                "current_province": "",
                "last_error": "",
                "updated_at": now_iso(),
            })
            self.scan_thread = threading.Thread(target=self._scan_worker, name="sgcc-scan", daemon=True)
            self.scan_thread.start()
        return {"ok": True, "message": "扫描任务已启动。"}

    def _scan_progress(self, completed: int, total: int, result: dict[str, Any]) -> None:
        province = str(result.get("province") or "")
        count = len(result.get("items") or [])
        self._update(
            scan_completed=completed,
            scan_total=32,
            current_province=province,
            message=f"正在扫描 {province}：读取 {count} 条资料（{completed}/32）",
        )

    def _csg_scan_progress(self, completed: int, total: int, result: dict[str, Any]) -> None:
        self._scan_progress(27 + completed, 27 + total, result)

    def _scan_worker(self) -> None:
        try:
            scan_errors: list[str] = []
            try:
                sgcc = self.scanner.scan(self._scan_progress)
            except Exception as exc:
                sgcc = {"schema_version": "1.0", "source": "95598.cn", "results": []}
                scan_errors.append(f"国家电网扫描失败：{exc}")
                LOGGER.exception("sgcc catalog scan failed")
            try:
                csg = self.csg_scanner.scan(self._csg_scan_progress)
                fresh = {
                    **sgcc,
                    "source": "国家电网 + 南方电网",
                    "results": list(sgcc.get("results") or []) + list(csg.get("results") or []),
                    "csg_scanned_at": csg.get("scanned_at", ""),
                }
            except Exception as exc:
                scan_errors.append(f"南方电网扫描失败：{exc}")
                LOGGER.exception("csg catalog scan failed")
                fresh = {**sgcc, "source": "国家电网 + 南方电网", "results": list(sgcc.get("results") or [])}
            if scan_errors:
                fresh["errors"] = scan_errors
            fresh_item_total = sum(len(x.get("items") or []) for x in fresh.get("results", []))
            if fresh_item_total == 0:
                raise RuntimeError("本次扫描没有返回任何资料，已保留上一次有效目录")
            previous = self.store.load()
            merged = merge_scan_results(previous, fresh)
            item_total = sum(len(x.get("items") or []) for x in merged.get("results", []))
            stale_count = sum(bool(x.get("stale")) for x in merged.get("results", []))
            snapshot = self.store.save(merged)
            message = f"目录扫描完成：共 {item_total} 条资料。"
            if stale_count or scan_errors:
                details = []
                if stale_count:
                    details.append(f"{stale_count} 个省份沿用上次目录")
                details.extend(scan_errors)
                message = f"目录扫描部分完成：共 {item_total} 条资料；" + "；".join(details)
            self._update(
                phase="warning" if stale_count or scan_errors else "complete",
                message=message,
                scan_running=False,
                scan_completed=len(merged.get("results", [])),
                scan_total=len(merged.get("results", [])),
                current_province="",
                counts=snapshot.get("summary", {}),
                last_scan=snapshot.get("scanned_at", ""),
            )
            LOGGER.info("catalog scan completed: %s items", item_total)
            if self.download_after_scan:
                self.download_missing()
        except Exception as exc:
            LOGGER.exception("catalog scan failed")
            self._update(
                phase="error",
                message=f"目录扫描失败：{exc}",
                scan_running=False,
                current_province="",
                last_error=str(exc),
            )

    def download_item(self, item_id: str) -> dict[str, Any]:
        if self.download_thread and self.download_thread.is_alive():
            return {"ok": False, "message": "下载任务正在运行。"}
        item = self.store.get_item(item_id)
        if item is None:
            return {"ok": False, "message": "没有找到该资料。"}
        if item.get("local_status") == "acquired":
            return {"ok": False, "message": "该资料已在本地。"}
        return self._start_download([item])

    def download_selected(self, item_ids: list[str]) -> dict[str, Any]:
        if self.download_thread and self.download_thread.is_alive():
            return {"ok": False, "message": "已有下载任务运行中"}
        items = []
        seen: set[str] = set()
        for item_id in item_ids:
            if item_id in seen:
                continue
            seen.add(item_id)
            item = self.store.get_item(item_id)
            if item and item.get("downloadable") and item.get("local_status") != "acquired":
                items.append(item)
        if not items:
            return {"ok": True, "message": "所选文件均已获取", "count": 0}
        return self._start_download(items)

    def download_missing(self, province: str | None = None, network: str | None = None) -> dict[str, Any]:
        if self.download_thread and self.download_thread.is_alive():
            return {"ok": False, "message": "下载任务正在运行。"}
        snapshot = self.store.snapshot()
        items = [
            item
            for group in snapshot.get("provinces", [])
            if not province or province in str(group.get("province", ""))
            if not network or str(group.get("network") or "sgcc").lower() == network.lower()
            for item in group.get("items", [])
            if item.get("downloadable") and item.get("local_status") != "acquired"
        ]
        if not items:
            return {"ok": False, "message": "没有待下载资料。"}
        return self._start_download(items)

    def _start_download(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        with self.lock:
            if self.download_thread and self.download_thread.is_alive():
                return {"ok": False, "message": "下载任务正在运行。"}
            self.runtime.update({
                "phase": "downloading",
                "message": f"准备下载 {len(items)} 条资料…",
                "download_running": True,
                "download_completed": 0,
                "download_total": len(items),
                "current_item": "",
                "last_error": "",
                "updated_at": now_iso(),
            })
            self.download_thread = threading.Thread(target=self._download_worker, args=(items,), name="sgcc-download", daemon=True)
            self.download_thread.start()
        return {"ok": True, "message": f"已开始下载 {len(items)} 条资料。"}

    def _download_worker(self, items: list[dict[str, Any]]) -> None:
        success = failed = completed = 0
        try:
            csg_urls = {
                str(item.get("url") or "")
                for item in items
                if str(item.get("network") or "").lower() == "csg"
                and str(item.get("format") or item.get("type") or "").lower() in {"html", "htm"}
            }
            csg_items = [item for item in items if str(item.get("url") or "") in csg_urls]
            remaining_items = [item for item in items if str(item.get("url") or "") not in csg_urls]
            if csg_items:
                from .csg import download_html_documents

                def csg_progress(index: int, total: int, record: dict[str, Any]) -> None:
                    nonlocal success, failed, completed
                    completed += 1
                    if record.get("status") == "acquired":
                        success += 1
                    else:
                        failed += 1
                    self.store.update_download(record)
                    self._update(
                        download_completed=completed,
                        current_item=str(record.get("title") or ""),
                        message=f"保存南网公告 {index}/{total}：成功 {success}，失败 {failed}",
                    )

                download_html_documents(csg_items, self.settings, csg_progress)
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.settings.download_workers) as executor:
                futures = {executor.submit(self.downloader.download_item, item): item for item in remaining_items}
                for future in concurrent.futures.as_completed(futures):
                    completed += 1
                    item = futures[future]
                    try:
                        record = future.result()
                    except Exception as exc:
                        record = {"status": "failed", "error": str(exc)}
                    if record.get("status") == "acquired":
                        success += 1
                    else:
                        failed += 1
                    self._update(
                        download_completed=completed,
                        current_item=str(item.get("title") or ""),
                        message=f"下载进度 {completed}/{len(items)}：成功 {success}，失败 {failed}",
                    )
            self._update(
                phase="complete" if not failed else "download_complete",
                message=f"下载完成：成功 {success}，失败 {failed}。",
                download_running=False,
                current_item="",
                counts=self.store.snapshot().get("summary", {}),
            )
        except Exception as exc:
            LOGGER.exception("download job failed")
            self._update(
                phase="error",
                message=f"下载任务失败：{exc}",
                download_running=False,
                current_item="",
                last_error=str(exc),
            )
