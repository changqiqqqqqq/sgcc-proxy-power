from __future__ import annotations

"""按表头文字映射列的通用代理购电价格表解析器。

各省公告价格主表结构高度相似（用电分类/电压等级/各价格分项/分时电价/容需量电价），
但"其中"子列顺序、分时时段数、是否有公式行/单位行、缺失值符号各不相同。
本模块通过表头文字识别每一列的含义，避免写死列索引。
"""

import logging
import re
from pathlib import Path

import pdfplumber

from .prices import (
    PriceRow,
    ParseResult,
    normalize_usage_type1,
    parse_number,
)
from .price_extract import (
    BaseExtractor,
    canonical_voltage,
    clean_cell,
    cross_check,
    detect_month,
    find_info_table,
    info_lookup,
)

LOGGER = logging.getLogger(__name__)

# 表头关键词 → 内部字段。顺序即优先级，命中第一个即停。
# 注意：更具体的关键词要放前面（如"电度输配电价"先于"输配电价"）。
HEADER_MAP = [
    ("proxy_price", ("代理购电价格", "代理工商业购电价格", "代理购电用户电价", "购电价格", "电量上网电价", "上网电价")),
    ("loss_fee", ("上网环节线损", "线损费用", "线损电价", "线损折价")),
    ("transmission_price", ("电度输配电价", "电量输配电价", "输配电价")),
    ("system_run_fee", ("系统运行费",)),
    ("gov_fund", ("政府性基金", "基金及附加", "基金附加")),
    ("total_sharp", ("尖峰",)),
    ("total_deep", ("深谷",)),
    ("total_high", ("高峰", "峰时段")),
    ("total_flat", ("平段", "平时段", "平段电价")),
    ("total_low", ("低谷", "谷时段")),
    ("demand_price", ("最大需量", "需量电价", "需量")),
    ("capacity_price", ("变压器容量", "容量电价", "容量")),
]

# 这些列标题容易误匹配，需要排除（分组/合计/公式/单位行）
GROUP_HEADER_WORDS = ("非分时", "电度用电价格", "电度电价", "电量电价", "用电价格", "其中", "分时", "容（需）量", "容需量")


def _match_header_cell(cell: str) -> str | None:
    """单个表头单元格 → 字段名；非价格字段返回 None。"""
    if not cell:
        return None
    for field, keywords in HEADER_MAP:
        for kw in keywords:
            if kw in cell:
                return field
    return None


def build_column_map(header_rows: list[list[str]]) -> dict[int, str]:
    """根据表头多行合并，推断每个数据列对应的字段。

    策略：取所有表头行，逐列向下合并文本；跳过明显是分组/合计/单位的列。
    返回 {列索引: 字段名}。分时/分项列靠子表头行（含具体关键词的行）定位。
    """
    if not header_rows:
        return {}
    width = max(len(r) for r in header_rows)
    # 合并每列的表头文字（多行表头纵向拼接）
    col_text: list[str] = []
    for ci in range(width):
        parts = []
        for row in header_rows:
            if ci < len(row):
                parts.append(row[ci])
        col_text.append("".join(parts))

    col_map: dict[int, str] = {}
    for ci, text in enumerate(col_text):
        if not text:
            continue
        field = _match_header_cell(text)
        if field and field not in col_map.values():
            col_map[ci] = field
    return col_map


def is_formula_row(cells: list[str]) -> bool:
    joined = "".join(cells)
    first = cells[0] if cells else ""
    if first in ("公式", "计算关系", "序号"):
        return True
    # 含 "1=2+3+4" 这类公式
    if re.search(r"\d+\s*=\s*\d+", joined):
        return True
    return False


def is_unit_row(cells: list[str]) -> bool:
    """单位行：整行几乎都是"（元/…）"这类单位标注，无具体字段名和数值。

    区别于表头行：表头行除单位外还含"代理购电价格/线损/输配电价"等字段名。
    """
    joined = "".join(cells)
    if "元/千瓦" not in joined and "元/千伏安" not in joined:
        return False
    # 含价格字段名 → 是表头行不是单位行
    for _f, kws in HEADER_MAP:
        if any(k in joined for k in kws):
            return False
    if any(w in joined for w in ("用电分类", "电压等级", "时段", "其中")):
        return False
    # 含 3 位以上小数的数值 → 数据行
    if re.search(r"\d+\.\d{3,}", joined):
        return False
    return True


class GenericTableExtractor(BaseExtractor):
    """通用价格表解析器。子类只需设置 region/network 及少量钩子。

    可覆写属性：
    - USAGE1_COL: 单一制/两部制所在列（默认 1）
    - VOLTAGE_COL: 电压等级所在列（默认 2，但优先用表头定位）
    - INFO_AVG_KEYWORDS / INFO_DEV_KEYWORDS: 信息公开表取数关键词
    - SKIP_PAGE_KEYWORDS: 命中即跳过该页（如 1.5 倍表）
    - REGIONS_FROM_PAGE: 是否按页标题拆分多地区（陕西榆林）
    """

    USAGE1_COL = 1
    VOLTAGE_COL = 2
    INFO_AVG_KEYWORDS = ("平均购电价格", "平均上网电价", "代理购电价格", "当月代理购电价格", "平价购电价格")
    INFO_DEV_KEYWORDS = ("偏差电费折价", "偏差",)
    SKIP_PAGE_KEYWORDS = ("执行1.5倍", "执行 1.5 倍", "1.5倍代理购电", "1.5 倍代理购电价格工商业")
    # 有些省把单一/两部制和用电类型二写在同一列（上海、陕西、海南）
    USAGE1_IN_CLASSIFY_COL = False
    # 是否对空值列向下填充（陕西尖峰列为真实 0，需关闭）
    FILL_DOWN = True
    # 即使 FILL_DOWN 开启也不填充的字段（分时电价/容需量/输配电价按行独立）
    NO_FILL_FIELDS: tuple[str, ...] = ()

    def extract(self, pdf: pdfplumber.PDF, path: Path, result: ParseResult) -> None:
        first_text = pdf.pages[0].extract_text() or ""
        result.month = detect_month(first_text, path.name)
        info = find_info_table(pdf)
        month_avg = self._lookup_info(info, self.INFO_AVG_KEYWORDS)
        history_dev = self._lookup_info(info, self.INFO_DEV_KEYWORDS)

        for page in pdf.pages:
            page_text = (page.extract_text() or "")
            if any(k in page_text.replace(" ", "") for k in self.SKIP_PAGE_KEYWORDS):
                continue
            self._parse_page(page, result, month_avg, history_dev)

    def _lookup_info(self, info: dict[str, float], keywords: tuple[str, ...]) -> float | None:
        for kw in keywords:
            value = info_lookup(info, kw)
            if value is not None:
                return value
        return None

    def _parse_page(self, page, result: ParseResult, month_avg, history_dev) -> None:
        for table in page.extract_tables():
            rows = [[clean_cell(c) for c in row] for row in table if row]
            if len(rows) < 3:
                continue
            head_blob = "".join("".join(r) for r in rows[:3])
            # 信息公开表/明细表（名称|序号|明细|计算关系|数值）不是价格主表，跳过
            if "计算关系" in head_blob and "明细" in head_blob and "序号" in head_blob:
                continue
            # 表级 1.5 倍识别：表头出现"1.5倍代理购电"则跳过（同页多表的省份）
            if "1.5倍" in head_blob.replace(" ", "") or "1.5 倍" in head_blob:
                continue
            # 定位表头结束位置（公式行/单位行/首个数据行）
            header_end = self._find_header_end(rows)
            header_rows = rows[:header_end]
            body = rows[header_end:]
            col_map = build_column_map(header_rows)
            # 价格主表必须同时识别到 代理购电价格 和 电压等级列可解析
            if "proxy_price" not in col_map.values():
                continue
            self._parse_body(body, col_map, result, month_avg, history_dev)

    def _find_header_end(self, rows: list[list[str]]) -> int:
        for i, row in enumerate(rows):
            if is_formula_row(row) or is_unit_row(row):
                continue
            joined = "".join(row)
            if "千伏" in joined and parse_number(row[3] if len(row) > 3 else "") is not None:
                return i
        return 2

    def _classify_usage(self, cells: list[str], current: str) -> str:
        # 标准：USAGE1_COL 列出现"单一制/两部制"
        if self.USAGE1_COL < len(cells):
            v = cells[self.USAGE1_COL]
            if "两部" in v:
                return "两部制"
            if "单一" in v:
                return "单一制"
        # 备用：分类列内嵌（如"单一制（一般工商业用电）"）
        if self.USAGE1_IN_CLASSIFY_COL and cells:
            v = cells[0]
            if "两部" in v:
                return "两部制"
            if "单一" in v:
                return "单一制"
        return current

    def _parse_body(self, body, col_map, result, month_avg, history_dev, region=None) -> None:
        region = region or self.region
        usage1 = ""
        merged: dict[str, float | None] = {}
        for cells in body:
            if is_formula_row(cells) or is_unit_row(cells):
                continue
            usage1 = self._classify_usage(cells, usage1)
            if not usage1:
                continue
            # 电压等级列
            vcol = self.VOLTAGE_COL if self.VOLTAGE_COL < len(cells) else None
            if vcol is None:
                continue
            voltage = canonical_voltage(cells[vcol])
            if not voltage or "千伏" not in voltage:
                continue
            values: dict[str, float | None] = {
                "proxy_month_avg": month_avg,
                "proxy_history_dev": history_dev,
            }
            for ci, field in col_map.items():
                if ci >= len(cells):
                    continue
                num = parse_number(cells[ci])
                if num is None and self.FILL_DOWN and field not in self.NO_FILL_FIELDS and field in merged:
                    num = merged[field]
                if num is not None and field not in merged:
                    merged[field] = num
                values[field] = num
            row = PriceRow(
                region=region,
                month=result.month,
                usage_type1=usage1,
                usage_type2=self.usage_type2,
                voltage_level=voltage,
                values=values,
            )
            result.warnings.extend(cross_check(row))
            result.rows.append(row)


# ---------------- 各省配置 ----------------

class BeijingExtractor(GenericTableExtractor):
    region = "北京市"
    network = "sgcc"


class SichuanExtractor(GenericTableExtractor):
    region = "四川省"
    network = "sgcc"


class AnhuiExtractor(GenericTableExtractor):
    region = "安徽省"
    network = "sgcc"


class JiangxiExtractor(GenericTableExtractor):
    region = "江西省"
    network = "sgcc"
    INFO_AVG_KEYWORDS = ("当月平均上网电价", "平均购电价格", "平均上网电价")


class FujianExtractor(GenericTableExtractor):
    region = "福建省"
    network = "sgcc"


class ChongqingExtractor(GenericTableExtractor):
    region = "重庆市"
    network = "sgcc"


class QinghaiExtractor(GenericTableExtractor):
    region = "青海省"
    network = "sgcc"


class HeilongjiangExtractor(GenericTableExtractor):
    region = "黑龙江省"
    network = "sgcc"


class ShanghaiExtractor(GenericTableExtractor):
    region = "上海市"
    network = "sgcc"
    USAGE1_IN_CLASSIFY_COL = True
    INFO_AVG_KEYWORDS = ("代理购电平均购电价格", "平均购电价格")


class InnerMongoliaExtractor(GenericTableExtractor):
    """蒙东：主表与 1.5 倍表同页（p1 表0=主表、表1=1.5倍），不能按页跳过。

    同页出现多个价格主表时，只取第一个（1.5 倍表代理购电价 ≈ 主表 ×1.5，第二个即 1.5 倍表）。
    """

    region = "内蒙古东部地区"
    network = "sgcc"
    SKIP_PAGE_KEYWORDS = ()  # 不按页跳过，主表与 1.5 倍表同页

    def _parse_page(self, page, result, month_avg, history_dev) -> None:
        parsed_main = False
        for table in page.extract_tables():
            rows = [[clean_cell(c) for c in row] for row in table if row]
            if len(rows) < 3:
                continue
            head_blob = "".join("".join(r) for r in rows[:3])
            if "计算关系" in head_blob and "明细" in head_blob and "序号" in head_blob:
                continue
            header_end = self._find_header_end(rows)
            col_map = build_column_map(rows[:header_end])
            if "proxy_price" not in col_map.values():
                continue
            if parsed_main:
                continue  # 同页第二个价格表 = 1.5 倍表，跳过
            self._parse_body(rows[header_end:], col_map, result, month_avg, history_dev)
            parsed_main = True


class GuangxiExtractor(GenericTableExtractor):
    region = "广西壮族自治区"
    network = "csg"
    INFO_AVG_KEYWORDS = ("当月平均上网电价", "平均上网电价")
    INFO_DEV_KEYWORDS = ("偏差电费折价", "偏差")


class HainanExtractor(GenericTableExtractor):
    region = "海南省"
    network = "csg"
    USAGE1_IN_CLASSIFY_COL = True
    INFO_AVG_KEYWORDS = ("当月代理购电价格", "代理购电价格", "平均购电价格")
    INFO_DEV_KEYWORDS = ("代理购电偏差电费折价", "偏差")


class ShaanxiExtractor(GenericTableExtractor):
    """陕西：主表为非榆林（p1），p3 为榆林地区表，p2/p4 为 1.5 倍表。

    陕西尖峰列真实值为 0（当月无尖峰时段），不能用向下填充覆盖，故关闭填充。
    榆林地区表结构相同但电压等级/输配电价不同，单列到"陕西省榆林地区"。
    """

    region = "陕西省"
    network = "sgcc"
    USAGE1_IN_CLASSIFY_COL = True
    VOLTAGE_COL = 1  # 陕西无独立"单一/两部制"列，电压等级在第 2 列（索引 1）
    SKIP_PAGE_KEYWORDS = ("1.5倍代理购电", "1.5 倍代理购电")
    # 代理购电价/线损等只在块首行填数需填充，但尖峰列是真实 0 不能填充
    NO_FILL_FIELDS = ("total_sharp", "total_high", "total_flat", "total_low", "total_deep",
                      "demand_price", "capacity_price", "transmission_price")

    def _parse_page(self, page, result, month_avg, history_dev) -> None:
        page_text = page.extract_text() or ""
        # "不含榆林地区"也含"榆林"，需精确判断
        is_yulin = "榆林地区" in page_text[:200] and "不含榆林" not in page_text[:200]
        region = "陕西省榆林地区" if is_yulin else self.region
        for table in page.extract_tables():
            rows = [[clean_cell(c) for c in row] for row in table if row]
            if len(rows) < 3:
                continue
            head_blob = "".join("".join(r) for r in rows[:3])
            if "计算关系" in head_blob and "明细" in head_blob and "序号" in head_blob:
                continue
            if "1.5倍" in head_blob.replace(" ", ""):
                continue
            # 陕西价格主表固定 2 行表头（分组行 + 列名行），无公式行/单位行
            col_map = build_column_map(rows[:2])
            if "proxy_price" not in col_map.values():
                continue
            self._parse_body(rows[2:], col_map, result, month_avg, history_dev, region)


GENERIC_EXTRACTORS: dict[str, type[GenericTableExtractor]] = {
    "北京市": BeijingExtractor,
    "四川省": SichuanExtractor,
    "安徽省": AnhuiExtractor,
    "江西省": JiangxiExtractor,
    "福建省": FujianExtractor,
    "重庆市": ChongqingExtractor,
    "陕西省": ShaanxiExtractor,
    "青海省": QinghaiExtractor,
    "黑龙江省": HeilongjiangExtractor,
    "上海市": ShanghaiExtractor,
    "内蒙古东部地区": InnerMongoliaExtractor,
    "广西壮族自治区": GuangxiExtractor,
    "海南省": HainanExtractor,
}
