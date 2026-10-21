from __future__ import annotations

import hmac
import inspect
import json
import logging
import os
import tempfile
import zipfile
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import quote

import uvicorn
from fastapi import Body, Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from starlette.background import BackgroundTask

from . import __version__
from .catalog import CatalogStore, safe_name
from .config import Settings
from .scheduler import ScheduleController
from .service import AppService


PROJECT_DIR = Path(__file__).resolve().parents[1]
WEB_FILE = PROJECT_DIR / "web" / "index.html"
SEED_CATALOG = PROJECT_DIR / "seed" / "catalog.json"
SEED_CSG_CATALOG = PROJECT_DIR / "seed" / "csg_catalog.json"


def configure_logging(settings: Settings) -> None:
    settings.prepare()
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    file_handler = RotatingFileHandler(settings.log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(console)
    root.addHandler(file_handler)


def _seed_catalog(settings: Settings) -> None:
    if settings.catalog_file.exists() or not SEED_CATALOG.exists():
        return
    settings.catalog_file.parent.mkdir(parents=True, exist_ok=True)
    base = json.loads(SEED_CATALOG.read_text(encoding="utf-8"))
    if SEED_CSG_CATALOG.exists():
        try:
            csg = json.loads(SEED_CSG_CATALOG.read_text(encoding="utf-8"))
            base["results"] = list(base.get("results") or []) + list(csg.get("results") or [])
            base["source"] = "国家电网 + 南方电网"
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
    settings.catalog_file.write_text(json.dumps(base, ensure_ascii=False, indent=2), encoding="utf-8")


async def _invoke(value):
    result = value()
    if inspect.isawaitable(result):
        return await result
    return result


def _decorate_files(snapshot: dict[str, Any]) -> dict[str, Any]:
    for province in snapshot.get("provinces", []):
        for item in province.get("items", []):
            local = str(item.get("local_file") or "")
            item["local_url"] = f"/files/{quote(local, safe='/')}" if local else ""
    return snapshot


def create_app(settings: Settings | None = None, service=None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.prepare()
    configure_logging(settings)
    injected_service = service is not None
    if service is None:
        _seed_catalog(settings)
        store = CatalogStore(settings.catalog_file, settings.download_dir, settings.report_file)
        service = AppService(settings, store=store)
    def schedule_changed(state: dict[str, Any]) -> None:
        service.download_after_scan = bool(state["download_after_scan"])

    schedule = ScheduleController(
        service.scan,
        settings.schedule_file,
        enabled=settings.scan_enabled and not injected_service,
        cron=settings.scan_cron,
        timezone=settings.timezone,
        download_after_scan=settings.download_after_scan,
        on_change=schedule_changed,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        schedule.start()
        if settings.scan_on_start:
            service.scan()
        yield
        schedule.stop()

    app = FastAPI(title="95598 代理购电资料服务", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.service = service
    app.state.scheduler = schedule
    app.state.schedule = schedule

    def require_admin(x_admin_token: str = Header(default="")) -> None:
        if settings.admin_token and not hmac.compare_digest(x_admin_token, settings.admin_token):
            raise HTTPException(status_code=401, detail="管理令牌错误")

    @app.get("/", response_class=HTMLResponse)
    def index():
        if not WEB_FILE.is_file():
            raise HTTPException(status_code=500, detail="管理页面缺失")
        return HTMLResponse(WEB_FILE.read_text(encoding="utf-8"))

    @app.get("/api/health")
    def health():
        return {"ok": True, "service": "sgcc-proxy-power", "version": __version__}

    @app.get("/api/status")
    async def status():
        value = await _invoke(service.status)
        schedule_state = schedule.snapshot()
        value.update({
            "scheduled": schedule_state["enabled"],
            "scan_cron": schedule_state["cron"],
            "schedule": schedule_state,
        })
        return value

    @app.get("/api/schedule")
    def schedule_status():
        return {"ok": True, "schedule": schedule.snapshot()}

    @app.get("/api/catalog")
    def catalog():
        store = getattr(service, "store", None)
        if store is None:
            return {"ok": True, "provinces": [], "summary": {}}
        return _decorate_files(store.snapshot())

    @app.get("/api/provinces")
    def provinces():
        snapshot = catalog()
        summaries = []
        for province in snapshot.get("provinces", []):
            summary = {key: value for key, value in province.items() if key != "items"}
            summaries.append(summary)
        return {"ok": True, "scanned_at": snapshot.get("scanned_at", ""), "summary": snapshot.get("summary", {}), "provinces": summaries}

    @app.post("/api/scan", dependencies=[Depends(require_admin)])
    async def scan():
        return await _invoke(service.scan)

    @app.post("/api/schedule", dependencies=[Depends(require_admin)])
    def update_schedule(payload: dict[str, Any] = Body(default_factory=dict)):
        try:
            value = schedule.update(payload)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"ok": True, "message": "定时设置已保存。", "schedule": value}

    @app.post("/api/download/missing", dependencies=[Depends(require_admin)])
    async def download_missing(payload: dict[str, Any] = Body(default_factory=dict)):
        province = str(payload.get("province") or "") or None
        network = str(payload.get("network") or "") or None
        if network:
            try:
                return await _invoke(lambda: service.download_missing(province, network))
            except TypeError:
                return await _invoke(lambda: service.download_missing(province))
        return await _invoke(lambda: service.download_missing(province))

    @app.post("/api/download/{item_id}", dependencies=[Depends(require_admin)])
    async def download_item(item_id: str):
        return await _invoke(lambda: service.download_item(item_id))

    @app.post("/api/download-selected", dependencies=[Depends(require_admin)])
    async def download_selected(payload: dict[str, Any] = Body(default_factory=dict)):
        item_ids = [str(value) for value in payload.get("item_ids", []) if str(value)] if isinstance(payload.get("item_ids"), list) else []
        if not item_ids:
            raise HTTPException(status_code=400, detail="请至少选择一个文件")
        method = getattr(service, "download_selected", None)
        if method is None:
            raise HTTPException(status_code=501, detail="当前服务未启用批量下载")
        return await _invoke(lambda: method(item_ids))

    @app.post("/api/export", dependencies=[Depends(require_admin)])
    def export_selected(payload: dict[str, Any] = Body(default_factory=dict)):
        item_ids = {str(value) for value in payload.get("item_ids", []) if str(value)} if isinstance(payload.get("item_ids"), list) else set()
        if not item_ids:
            raise HTTPException(status_code=400, detail="请至少选择一个文件")
        snapshot = catalog()
        selected = [
            {
                **item,
                "province": item.get("province") or province.get("province") or "未知省份",
                "network": item.get("network") or province.get("network") or "sgcc",
            }
            for province in snapshot.get("provinces", [])
            for item in province.get("items", [])
            if str(item.get("item_id") or "") in item_ids
        ]
        if len(selected) != len(item_ids):
            raise HTTPException(status_code=400, detail="部分所选文件已不在当前目录，请刷新页面后重试")
        missing = [item for item in selected if item.get("local_status") != "acquired" or not item.get("local_file")]
        if missing:
            raise HTTPException(status_code=409, detail=f"所选文件中有 {len(missing)} 个尚未获取，请先下载后再导出")
        export_dir = settings.data_dir / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix="95598-selected-", suffix=".zip", dir=export_dir)
        os.close(handle)
        used: set[str] = set()
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for item in selected:
                    source = (settings.download_dir / str(item["local_file"])).resolve()
                    try:
                        source.relative_to(settings.download_dir.resolve())
                    except ValueError:
                        raise HTTPException(status_code=400, detail="文件路径无效")
                    if not source.is_file():
                        raise HTTPException(status_code=409, detail=f"本地文件不存在：{item.get('title')}")
                    grid = "南方电网" if str(item.get("network") or "sgcc").lower() == "csg" else "国家电网"
                    province = safe_name(str(item.get("province") or "未知省份"))
                    filename = safe_name(source.name)
                    arcname = f"{grid}/{province}/{filename}"
                    stem, suffix, number = Path(filename).stem, Path(filename).suffix, 2
                    while arcname.lower() in used:
                        arcname = f"{grid}/{province}/{stem}_{number}{suffix}"
                        number += 1
                    used.add(arcname.lower())
                    archive.write(source, arcname)
        except Exception:
            Path(temporary).unlink(missing_ok=True)
            raise
        filename = f"95598代理购电资料_{len(selected)}个.zip"
        return FileResponse(
            temporary,
            media_type="application/zip",
            filename=filename,
            background=BackgroundTask(lambda: Path(temporary).unlink(missing_ok=True)),
        )

    @app.get("/files/{relative_path:path}")
    def file_download(relative_path: str):
        target = (settings.download_dir / relative_path).resolve()
        try:
            target.relative_to(settings.download_dir.resolve())
        except ValueError:
            raise HTTPException(status_code=400, detail="文件路径无效")
        if not target.is_file():
            raise HTTPException(status_code=404, detail="文件不存在")
        return FileResponse(target, filename=target.name)

    # ---------------- 代理购电价格数据 ----------------

    def _price_store():
        from .prices import PriceStore

        return PriceStore(settings.data_dir / "prices.db")

    @app.get("/api/prices/summary")
    def price_summary():
        store = _price_store()
        try:
            return {"ok": True, **store.summary()}
        finally:
            store.close()

    @app.get("/api/prices/query")
    def price_query(
        region: str = "",
        month: str = "",
        usage_type1: str = "",
        voltage: str = "",
        limit: int = 500,
        offset: int = 0,
    ):
        store = _price_store()
        try:
            total, rows = store.query(
                region=region.strip(),
                month=month.strip(),
                usage_type1=usage_type1.strip(),
                voltage=voltage.strip(),
                limit=min(max(1, limit), 5000),
                offset=max(0, offset),
            )
            return {"ok": True, "total": total, "rows": rows}
        finally:
            store.close()

    @app.get("/api/prices/export")
    def price_export(
        region: str = "",
        month: str = "",
        usage_type1: str = "",
        voltage: str = "",
    ):
        import openpyxl

        from .prices import EXPORT_HEADERS

        store = _price_store()
        try:
            _total, rows = store.query(
                region=region.strip(),
                month=month.strip(),
                usage_type1=usage_type1.strip(),
                voltage=voltage.strip(),
                limit=200000,
            )
        finally:
            store.close()
        if not rows:
            raise HTTPException(status_code=404, detail="没有符合条件的数据")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "各省数据"
        ws.append([header for _name, header in EXPORT_HEADERS])
        for row in rows:
            ws.append([row.get(name) if row.get(name) is not None else 0 for name, _h in EXPORT_HEADERS])
        export_dir = settings.data_dir / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix="代理购电价格-", suffix=".xlsx", dir=export_dir)
        os.close(handle)
        wb.save(temporary)
        return FileResponse(
            temporary,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=f"代理购电价格_{len(rows)}行.xlsx",
            background=BackgroundTask(lambda: Path(temporary).unlink(missing_ok=True)),
        )

    @app.post("/api/prices/parse", dependencies=[Depends(require_admin)])
    async def price_parse(payload: dict[str, Any] = Body(default_factory=dict)):
        """解析本地已下载目录中的价格文件入库。payload: {region?, year?}"""
        from .price_extract import EXTRACTORS
        from .parse_prices import NETWORK_OF, SOURCE_DIRS, iter_region_files

        year = int(payload.get("year") or 2026)
        region_filter = str(payload.get("region") or "").strip()
        regions = [region_filter] if region_filter else list(EXTRACTORS)
        crawl_base = payload.get("crawl_base") or os.getenv("SGCC_CRAWL_BASE", "")
        if not crawl_base:
            raise HTTPException(status_code=400, detail="未配置爬取目录（SGCC_CRAWL_BASE）")
        store = _price_store()
        results = []
        try:
            for region in regions:
                files = iter_region_files(Path(crawl_base), region, year)
                for path in files:
                    from .price_extract import parse_file

                    result = parse_file(path, region)
                    store.save_result(result, network=NETWORK_OF.get(region, "sgcc"))
                    results.append({
                        "file": path.name,
                        "region": region,
                        "month": result.month,
                        "status": result.status,
                        "rows": len(result.rows),
                        "errors": result.errors,
                    })
        finally:
            store.close()
        return {"ok": True, "parsed": len(results), "results": results}

    @app.post("/api/prices/parse-selected", dependencies=[Depends(require_admin)])
    async def price_parse_selected(payload: dict[str, Any] = Body(default_factory=dict)):
        """解析目录中所选的已下载文件入库。"""
        item_ids = [str(value) for value in payload.get("item_ids", []) if str(value)] if isinstance(payload.get("item_ids"), list) else []
        if not item_ids:
            raise HTTPException(status_code=400, detail="请至少选择一个文件")
        return await _invoke(lambda: service.parse_selected(item_ids))

    @app.get("/api/prices/files")
    def price_files():
        store = _price_store()
        try:
            return {"ok": True, "files": store.all_file_status()}
        finally:
            store.close()

    return app


app = create_app()


def main() -> None:
    settings = app.state.settings
    uvicorn.run(app, host=settings.host, port=settings.port, workers=1, access_log=True)


if __name__ == "__main__":
    main()
