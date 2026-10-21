from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Callable

import pdfplumber

from .prices import (
    PriceRow,
    ParseResult,
    normalize_usage_type1,
    normalize_voltage,
    parse_number,
)

LOGGER = logging.getLogger(__name__)

MONTH_PATTERN = re.compile(r"(20\d\d)\s*年\s*(\d{1,2})\s*月")

# 各省 PDF 中电压等级写法 → 合并表标准写法
VOLTAGE_CANONICAL = {
    "不满1千伏": "不满1千伏",
    "1-10千伏": "1～10千伏",
    "1-10（20）千伏": "1～10（20）千伏",
    "10千伏": "1～10千伏",
    "35千伏": "35千伏",
    "35-110千伏": "35～110千伏",
    "35千伏及以上": "35千伏及以上",
    "110千伏": "110千伏",
    "110千伏及以上": "110千伏及以上",
    "220千伏": "220千伏",
    "220千伏及以上": "220千伏及以上",
}


def canonical_voltage(value: str) -> str:
    norm = normalize_voltage(value)
    return VOLTAGE_CANONICAL.get(norm, str(value or "").strip())


def detect_month(text: str, fallback: str = "") -> str:
    match = MONTH_PATTERN.search(text or "")
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}"
    match = MONTH_PATTERN.search(fallback)
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}"
    return ""


def clean_cell(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def is_data_row(cells: list[str]) -> bool:
    """数据行判定：包含电压等级（千伏，容忍截断为"千"），且其后至少 2 个数值。"""
    joined = "".join(cells)
    if not re.search(r"千伏|千$|千[^\d]", joined):
        return False
    numeric = sum(1 for c in cells if parse_number(c) is not None)
    return numeric >= 2


def find_price_table(
    pdf: pdfplumber.PDF,
    header_keywords: tuple[str, ...] = ("代理购电价格", "分时"),
    exclude_keywords: tuple[str, ...] = ("1.5倍", "1.5 倍", "信息公开", "依据", "退补"),
) -> tuple[int, list[list[str]]] | tuple[None, None]:
    """返回（页码，清洗后的表格行）。跳过 1.5 倍表与信息公开表。"""
    for page_no, page in enumerate(pdf.pages):
        page_text = page.extract_text() or ""
        head_text = page_text[:400]
        if any(k in head_text for k in exclude_keywords):
            continue
        for table in page.extract_tables():
            rows = [[clean_cell(c) for c in row] for row in table if row]
            header = "".join("".join(r) for r in rows[:3])
            if all(k in header for k in header_keywords):
                return page_no, rows
    return None, None


def find_info_table(
    pdf: pdfplumber.PDF,
    marker_keywords: tuple[str, ...] = ("计算关系", "明细"),
) -> dict[str, float]:
    """信息公开表：返回 {明细关键词: 数值}。"""
    values: dict[str, float] = {}
    for page in pdf.pages:
        for table in page.extract_tables():
            rows = [[clean_cell(c) for c in row] for row in table if row]
            if not rows:
                continue
            header = "".join(rows[0])
            if not all(k in header for k in marker_keywords):
                continue
            for row in rows[1:]:
                if len(row) < 2:
                    continue
                number = parse_number(row[-1])
                if number is None:
                    continue
                label = "".join(row[:-2])
                values[label] = number
    return values


def info_lookup(info: dict[str, float], *keywords: str) -> float | None:
    for label, number in info.items():
        if all(k in label for k in keywords):
            return number
    return None


def cross_check(row: PriceRow, tolerance: float = 0.01) -> list[str]:
    """到户平段价 ≈ 代理价 + 线损 + 输配 + 系统运行费 + 基金（单位需一致）。"""
    v = row.values
    parts = [v.get(k) for k in ("proxy_price", "loss_fee", "transmission_price", "system_run_fee", "gov_fund")]
    total = v.get("total_flat")
    if total is None or any(p is None for p in parts):
        return []
    expected = sum(p for p in parts if p is not None)
    if abs(expected - total) > tolerance:
        return [f"勾稽校验偏差 {expected - total:+.4f}（{row.voltage_level}）"]
    return []


class BaseExtractor:
    region = ""
    network = "sgcc"
    usage_type2 = "工商业"

    def parse(self, path: Path) -> ParseResult:
        result = ParseResult(source_file=str(path), region=self.region, month="", status="failed")
        try:
            with pdfplumber.open(str(path)) as pdf:
                self.extract(pdf, path, result)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("parse failed: %s", path)
            result.errors.append(f"解析异常：{exc}")
            return result
        if not result.month:
            result.month = detect_month("", path.name)
        if result.rows:
            result.status = "partial" if result.warnings or result.errors else "success"
        elif not result.errors:
            result.errors.append("未提取到任何数据行")
        return result

    def extract(self, pdf: pdfplumber.PDF, path: Path, result: ParseResult) -> None:
        raise NotImplementedError


class GuangdongExtractor(BaseExtractor):
    """广东电网公告：前 4 页为珠三角/东西两翼/粤北/江门四张同构价格表（分/千瓦时），末页为信息公开表。"""

    region = "广东省"
    network = "csg"
    PAGE_REGIONS = ["广东省珠三角五市", "广东省东西两翼地区", "广东省粤北山区", "广东省江门市", "广东省惠州市"]

    def extract(self, pdf: pdfplumber.PDF, path: Path, result: ParseResult) -> None:
        first_text = pdf.pages[0].extract_text() or ""
        result.month = detect_month(first_text, path.name)
        info = find_info_table(pdf)
        month_avg_raw = info_lookup(info, "平均上网电价")
        # 广东公告以分/千瓦时为单位，入库统一为元/千瓦时
        month_avg = round(month_avg_raw / 100, 8) if month_avg_raw is not None else None

        # 页标题中的区域名 → 参考表区域名；早期公告含惠州市页
        region_aliases = {
            "珠三角": "广东省珠三角五市",
            "东西两翼": "广东省东西两翼地区",
            "粤北": "广东省粤北山区",
            "江门": "广东省江门市",
            "惠州": "广东省惠州市",
        }
        seen: set[str] = set()
        for page in pdf.pages:
            page_text_full = page.extract_text() or ""
            page_text = page_text_full[:400]
            if "1.5倍" in page_text.replace(" ", ""):
                continue
            tables = page.extract_tables()
            if not tables:
                continue
            rows = [[clean_cell(c) for c in row] for row in tables[0] if row]
            header = "".join("".join(r) for r in rows[:3])
            if "代理购电价格" not in header or "分时" not in header:
                continue
            region = next((v for k, v in region_aliases.items() if k in page_text_full[:200]), "")
            if not region:
                # 无标题时按出现顺序套用默认区域序列
                region = next((r for r in self.PAGE_REGIONS if r not in seen), "")
            if not region:
                result.warnings.append("价格表页数超出预期，多余页已跳过")
                break
            if region in seen:
                continue
            seen.add(region)
            self.parse_price_rows(rows, region, result, month_avg)
        if len(seen) < 4:
            result.warnings.append(f"仅识别到 {len(seen)} 个区域价格表")

    def parse_price_rows(
        self,
        rows: list[list[str]],
        region: str,
        result: ParseResult,
        month_avg: float | None,
    ) -> None:
        usage1 = ""
        for cells in rows:
            if len(cells) < 15:
                continue
            if cells[1] in ("单一制", "两部制"):
                usage1 = cells[1]
            if not is_data_row(cells):
                continue
            voltage = canonical_voltage(cells[2])
            if not voltage or voltage in {"电压等级"}:
                continue
            to_yuan = lambda i: (lambda n: round(n / 100, 8) if n is not None else None)(parse_number(cells[i]))
            demand = parse_number(cells[13]) if usage1 == "两部制" else None
            capacity = parse_number(cells[14]) if usage1 == "两部制" else None
            row = PriceRow(
                region=region,
                month=result.month,
                usage_type1=usage1 or normalize_usage_type1(cells[1]),
                usage_type2=self.usage_type2,
                voltage_level=voltage,
                values={
                    "proxy_price": to_yuan(4),
                    "proxy_month_avg": month_avg,
                    "loss_fee": to_yuan(5),
                    "transmission_price": to_yuan(6),
                    "system_run_fee": to_yuan(7),
                    "gov_fund": to_yuan(8),
                    "total_sharp": to_yuan(9),
                    "total_high": to_yuan(10),
                    "total_flat": to_yuan(11),
                    "total_low": to_yuan(12),
                    "demand_price": demand,
                    "capacity_price": capacity,
                },
            )
            result.warnings.extend(cross_check(row))
            result.rows.append(row)


class GuizhouExtractor(BaseExtractor):
    """贵州电网公告：p3 标准价格表（元/千瓦时），p4 为 1.5 倍表需跳过，p5-6 信息公开表。"""

    region = "贵州省"
    network = "csg"

    def extract(self, pdf: pdfplumber.PDF, path: Path, result: ParseResult) -> None:
        first_text = pdf.pages[0].extract_text() or ""
        result.month = detect_month(first_text, path.name)
        info = find_info_table(pdf)
        month_avg = info_lookup(info, "平均上网电价")
        history_dev = info_lookup(info, "偏差")

        page_no, rows = find_price_table(
            pdf,
            header_keywords=("代理购电价格", "分时"),
            exclude_keywords=("1.5倍",),
        )
        if rows is None:
            result.errors.append(f"未找到标准价格表（page={page_no}）")
            return
        usage1 = ""
        merged: dict[str, float | None] = {}
        for cells in rows:
            if len(cells) < 11:
                continue
            if cells[0] in ("单一制", "两部制"):
                usage1 = cells[0]
            if not is_data_row(cells):
                continue
            voltage = canonical_voltage(cells[1])
            if not voltage or voltage == "电压等级":
                continue

            def num(i: int) -> float | None:
                value = parse_number(cells[i]) if i < len(cells) else None
                if value is None and i in merged and usage1 == "两部制":
                    # 表内合并单元格：两部制沿用单一制首行的公共列数值
                    return merged[i]
                if value is not None and i not in merged:
                    merged[i] = value
                return value

            is_two = usage1 == "两部制"
            row = PriceRow(
                region=self.region,
                month=result.month,
                usage_type1=usage1,
                usage_type2=self.usage_type2,
                voltage_level=voltage,
                values={
                    "proxy_price": num(3),
                    "proxy_month_avg": month_avg,
                    "proxy_history_dev": history_dev,
                    "transmission_price": num(4),
                    "loss_fee": num(5),
                    "system_run_fee": num(6),
                    "gov_fund": num(7),
                    "total_high": num(8) if is_two else None,
                    "total_flat": num(9) if is_two else None,
                    "total_low": num(10) if is_two else None,
                    "demand_price": num(11) if is_two else None,
                    "capacity_price": num(12) if is_two else None,
                },
            )
            if is_two:
                result.warnings.extend(cross_check(row))
            result.rows.append(row)


class JiangsuExtractor(BaseExtractor):
    """国网江苏公告：p3 信息公开表，p4 价格表（元/千瓦时）。参考表仅覆盖两部制。"""

    region = "江苏省"
    network = "sgcc"

    def extract(self, pdf: pdfplumber.PDF, path: Path, result: ParseResult) -> None:
        first_text = pdf.pages[0].extract_text() or ""
        result.month = detect_month(first_text, path.name)
        info = find_info_table(pdf)
        month_avg = info_lookup(info, "当月平均购电价格")
        history_dev = info_lookup(info, "历史偏差")

        page_no, rows = find_price_table(
            pdf,
            header_keywords=("代理工商业购电价", "分时"),
            exclude_keywords=("1.5倍",),
        )
        if rows is None:
            result.errors.append("未找到价格表")
            return
        # 江苏分时列版式不固定：1/4-12 月含尖峰列（尖/高/平/谷），2-3 月无尖峰列（高/平/谷）。
        # 按表头文字定位各数据列，避免写死索引错位。
        from .price_generic import build_column_map, is_formula_row as cells_is_formula

        header_end = 0
        for i, row in enumerate(rows):
            joined = "".join(row)
            if cells_is_formula(row):
                continue  # 公式行属于表头区，继续找首个数据行
            # 分组行/列名行（含"电压等级/用电分类/代理购电"等字段名）属于表头
            if any(w in joined for w in ("电压等级", "用电分类", "代理购电", "代理工商业购电")):
                header_end = i + 1
                continue
            if re.search(r"千伏|千$|（20）千|20）千", joined):
                header_end = i
                break
            header_end = i + 1
        col_map = build_column_map([list(r) for r in rows[:header_end]])
        usage1 = ""
        merged: dict[str, float | None] = {}
        for cells in rows[header_end:]:
            cells = cells + [""] * (16 - len(cells))
            if cells[1] in ("单一制", "两部制"):
                usage1 = cells[1]
            if usage1 != "两部制":
                continue  # 参考表暂不包含单一制，避免产出无法核对的数据
            raw_v = cells[3]
            # 江苏部分月份电压等级被竖排截断（"1-10（20）千"→补"伏"，"220千伏及"→"220千伏及以上"）
            if raw_v.endswith("千") and ("20" in raw_v or "10" in raw_v):
                raw_v += "伏"
            if raw_v in ("220千伏及", "220千伏及以"):
                raw_v = "220千伏及以上"
            voltage = canonical_voltage(raw_v)
            if not voltage or voltage == "电压等级" or "千" not in voltage:
                continue

            def num_field(field: str) -> float | None:
                for ci, f in col_map.items():
                    if f == field and ci < len(cells):
                        return parse_number(cells[ci])
                return None

            values = {
                "proxy_price": num_field("proxy_price"),
                "proxy_month_avg": month_avg,
                "proxy_history_dev": history_dev,
                "loss_fee": num_field("loss_fee"),
                "transmission_price": num_field("transmission_price"),
                "gov_fund": num_field("gov_fund"),
                "system_run_fee": num_field("system_run_fee"),
                "total_sharp": num_field("total_sharp"),
                "total_high": num_field("total_high"),
                "total_flat": num_field("total_flat"),
                "total_low": num_field("total_low"),
                "demand_price": num_field("demand_price"),
                "capacity_price": num_field("capacity_price"),
            }
            for key in ("proxy_price", "loss_fee", "system_run_fee", "gov_fund"):
                if values[key] is None:
                    values[key] = merged.get(key)
                else:
                    merged[key] = values[key]
            row = PriceRow(
                region=self.region,
                month=result.month,
                usage_type1=usage1,
                usage_type2=self.usage_type2,
                voltage_level=voltage,
                values=values,
            )
            result.warnings.extend(cross_check(row))
            result.rows.append(row)


class ZhejiangExtractor(BaseExtractor):
    """国网浙江公告：p4 表1 价格表（元/千瓦时，含深谷列），p5 为 1.5 倍表，p6 信息公开表。"""

    region = "浙江省"
    network = "sgcc"

    def extract(self, pdf: pdfplumber.PDF, path: Path, result: ParseResult) -> None:
        first_text = pdf.pages[0].extract_text() or ""
        result.month = detect_month(first_text, path.name)
        info = find_info_table(pdf)
        month_avg = info_lookup(info, "平价购电价格") or info_lookup(info, "平均购电价格")
        history_dev = info_lookup(info, "历史偏差")

        page_no, rows = find_price_table(
            pdf,
            header_keywords=("代理购电价格", "分时"),
            exclude_keywords=("1.5倍", "1.5 倍"),
        )
        if rows is None:
            result.errors.append("未找到价格表")
            return
        # 1-6 月表含"用电类型二"列（大工业/一般工商业），电压等级在 cells[2]；
        # 7 月起表头无该列，电压等级在 cells[1]。通过探测确定列偏移。
        offset = 0
        for probe in rows:
            probe = probe + [""] * 4
            if "两部" in probe[0] or "单一" in probe[0]:
                if "千伏" in probe[2]:
                    offset = 1
                break
        usage1 = ""
        merged: dict[str, float | None] = {}
        for cells in rows:
            cells = cells + [""] * (16 - len(cells))
            label = cells[0]
            if "两部" in label:
                usage1 = "两部制"
            elif "单一" in label:
                usage1 = "单一制"
            if not usage1:
                continue
            body = cells[offset:]
            if not is_data_row(body):
                continue
            voltage = canonical_voltage(body[1])
            if not voltage or voltage == "电压等级":
                continue

            def num(i: int) -> float | None:
                value = parse_number(body[i]) if i < len(body) else None
                if value is None and i in merged:
                    return merged[i]
                if value is not None and i not in merged:
                    merged[i] = value
                return value

            is_two = usage1 == "两部制"
            row = PriceRow(
                region=self.region,
                month=result.month,
                usage_type1=usage1,
                usage_type2=self.usage_type2,
                voltage_level=voltage,
                values={
                    "proxy_price": num(3),
                    "proxy_month_avg": month_avg,
                    "proxy_history_dev": history_dev,
                    "loss_fee": num(4),
                    "system_run_fee": num(5),
                    "transmission_price": num(6),
                    "gov_fund": num(7),
                    "total_sharp": num(8),
                    "total_high": num(9),
                    "total_flat": num(10),
                    "total_low": num(11),
                    "total_deep": num(12),
                    "demand_price": num(13) if is_two else None,
                    "capacity_price": num(14) if is_two else None,
                },
            )
            result.warnings.extend(cross_check(row))
            result.rows.append(row)


EXTRACTORS: dict[str, type[BaseExtractor]] = {
    "广东省": GuangdongExtractor,
    "贵州省": GuizhouExtractor,
    "江苏省": JiangsuExtractor,
    "浙江省": ZhejiangExtractor,
}

_generic_registered = False


def _register_generic() -> None:
    """把通用表头映射解析器补充到 EXTRACTORS（延迟导入避免循环依赖）。"""
    global _generic_registered
    if _generic_registered:
        return
    from .price_generic import GENERIC_EXTRACTORS

    for region, cls in GENERIC_EXTRACTORS.items():
        EXTRACTORS.setdefault(region, cls)
    _generic_registered = True


_ocr_registered = False


def _register_ocr() -> None:
    """把 OCR 解析器补充到 EXTRACTORS（图片/扫描件省份）。"""
    global _ocr_registered
    if _ocr_registered:
        return
    try:
        from .price_ocr_extract import OCR_EXTRACTORS
    except Exception:  # noqa: BLE001 - OCR 依赖（paddleocr）不可用时跳过
        _ocr_registered = True
        return
    for region, cls in OCR_EXTRACTORS.items():
        EXTRACTORS.setdefault(region, cls)
    _ocr_registered = True


def extractor_for_region(region: str) -> BaseExtractor | None:
    _register_generic()
    _register_ocr()
    cls = EXTRACTORS.get(region)
    return cls() if cls else None


def supported_regions() -> list[str]:
    _register_generic()
    return list(EXTRACTORS)


def parse_file(path: Path, region: str) -> ParseResult:
    extractor = extractor_for_region(region)
    if extractor is None:
        result = ParseResult(source_file=str(path), region=region, month="", status="failed")
        result.errors.append(f"暂不支持该地区解析：{region}")
        return result
    return extractor.parse(path)
