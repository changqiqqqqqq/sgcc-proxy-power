from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.price_extract import EXTRACTORS, parse_file  # noqa: E402
from app.prices import PriceStore  # noqa: E402

LOGGER = logging.getLogger("parse_prices")

# 目录命名：国网为 provinces_verified/<省>，南网为 csg_documents/<省>
SOURCE_DIRS = {
    "sgcc": "provinces_verified",
    "csg": "csg_documents",
}
# 各省所属电网 → 目录名（provinces_verified=国网，csg_documents=南网）
NETWORK_OF = {
    "广东省": "csg", "贵州省": "csg", "广西壮族自治区": "csg", "海南省": "csg", "云南省": "csg",
}

# 爬取目录中的省文件夹名（多数与标准地区名一致，个别需映射）
FOLDER_OF = {
    "内蒙古东部地区": "内蒙古自治区",
}


def network_of(region: str) -> str:
    return NETWORK_OF.get(region, "sgcc")

MONTH_PATTERN = re.compile(r"(20\d\d)\s*年\s*(\d{1,2})\s*月")


def file_month(name: str) -> str:
    match = MONTH_PATTERN.search(name)
    if not match:
        return ""
    return f"{match.group(1)}-{int(match.group(2)):02d}"


def iter_region_files(base: Path, region: str, year: int) -> list[Path]:
    network = network_of(region)
    folder_name = FOLDER_OF.get(region, region)
    folder = base / SOURCE_DIRS[network] / folder_name
    if not folder.is_dir():
        LOGGER.warning("目录不存在：%s", folder)
        return []
    best: dict[str, Path] = {}
    for path in sorted(folder.iterdir()):
        if path.suffix.lower() != ".pdf":
            continue
        month = file_month(path.name)
        if not month or not month.startswith(str(year)):
            continue
        # 同月可能有多份（带哈希后缀的重复下载），取文件名最短的一份
        current = best.get(month)
        if current is None or len(path.name) < len(current.name):
            best[month] = path
    return [best[m] for m in sorted(best)]


def main() -> None:
    parser = argparse.ArgumentParser(description="解析代理购电价格 PDF 并入库")
    parser.add_argument("--base", required=True, help="爬取输出根目录（含 provinces_verified/csg_documents）")
    parser.add_argument("--db", default="linux/data/prices.db", help="SQLite 数据库路径")
    parser.add_argument("--regions", default=",".join(EXTRACTORS), help="逗号分隔的地区")
    parser.add_argument("--year", type=int, default=2026, help="只解析该年份文件")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    base = Path(args.base)
    store = PriceStore(Path(args.db))
    total_ok = total_partial = total_failed = 0
    for region in [r.strip() for r in args.regions.split(",") if r.strip()]:
        files = iter_region_files(base, region, args.year)
        LOGGER.info("== %s：%d 个 %d 年文件", region, len(files), args.year)
        for path in files:
            result = parse_file(path, region)
            store.save_result(result, network=NETWORK_OF.get(region, "sgcc"))
            tag = {"success": "OK", "partial": "PARTIAL", "failed": "FAILED"}[result.status]
            LOGGER.info(
                "  [%s] %s month=%s rows=%d %s",
                tag, path.name, result.month, len(result.rows),
                ("; " + "; ".join(result.errors[:2])) if result.errors else "",
            )
            total_ok += result.status == "success"
            total_partial += result.status == "partial"
            total_failed += result.status == "failed"
    LOGGER.info("完成：success=%d partial=%d failed=%d", total_ok, total_partial, total_failed)
    store.close()


if __name__ == "__main__":
    main()
