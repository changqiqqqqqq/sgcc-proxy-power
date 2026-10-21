from __future__ import annotations

"""OCR 省份解析器：扫描件/图片 → 价格数据行。

与文字型 PDF（按表头文字映射列）不同，OCR 表头识别不可靠，
改为"数据行锚点（电压等级）+ 数值列 x 坐标区间 → 字段"的配置化解析。
每个 OCR 省份一个 ColumnLayout 配置。
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .price_extract import BaseExtractor, canonical_voltage
from .price_ocr import crop_regions, load_image, ocr_image, cluster_rows, pdf_page_to_image
from .prices import PriceRow, ParseResult, parse_number

LOGGER = logging.getLogger(__name__)


@dataclass
class ColumnLayout:
    """一省价格表的列布局（按 x 坐标区间映射字段）。

    columns: list of (字段名, x_min, x_max)。x 为框中心坐标。
    usage_from_text: 数据行文本中识别"两部制/单一制"的关键词。
    proxy_price_merged: 代理购电价格是否为合并单元格（需从信息表或块首行补）。
    sum_fields: 某些字段由多列求和得到（如山东系统运行费=多分项之和）。
    """

    columns: list[tuple[str, float, float]] = field(default_factory=list)
    voltage_x: tuple[float, float] = (0, 0)
    usage_keywords: tuple[str, str] = ("两部", "单一")
    proxy_price_merged: bool = True
    # 信息公开表取数关键词
    info_avg: tuple[str, ...] = ("平均购电价", "平均上网电价", "代理购电价格")
    info_dev: tuple[str, ...] = ("偏差",)


class OcrTableExtractor(BaseExtractor):
    """OCR 省份基类。子类提供 LAYOUT 与文件加载方式（图片/扫描PDF）。"""

    LAYOUT: ColumnLayout = ColumnLayout()
    IS_IMAGE = True  # True=图片文件，False=扫描PDF

    def extract(self, pdf, path: Path, result: ParseResult) -> None:
        raise NotImplementedError("OCR 解析器走 parse_image")

    def parse(self, path: Path) -> ParseResult:
        result = ParseResult(source_file=str(path), region=self.region, month="", status="failed")
        try:
            boxes = self._load_boxes(path)
            result.month = self._detect_month(path, boxes)
            rows = cluster_rows(boxes)
            self._parse_rows(rows, result)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("ocr parse failed: %s", path)
            result.errors.append(f"OCR 解析异常：{exc}")
            return result
        if result.rows:
            result.status = "partial" if (result.warnings or result.errors) else "success"
        elif not result.errors:
            result.errors.append("OCR 未提取到数据行")
        return result

    def _load_boxes(self, path: Path):
        if self.IS_IMAGE:
            arr = load_image(str(path))
            blocks = crop_regions(arr, max_side=2500)
        else:
            # 扫描 PDF：每页渲染成图（可能多块）
            import fitz
            doc = fitz.open(str(path))
            n = doc.page_count
            doc.close()
            blocks = [pdf_page_to_image(str(path), i) for i in range(n)]
        boxes = []
        yoff = 0
        for block in blocks:
            for box in ocr_image(block):
                box.y1 += yoff
                box.y2 += yoff
                boxes.append(box)
            yoff += block.shape[0]
        return boxes

    def _detect_month(self, path: Path, boxes) -> str:
        text = path.name + " " + " ".join(b.text for b in boxes[:40])
        m = re.search(r"(20\d\d)\s*年\s*(\d{1,2})\s*月", text)
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}"
        return ""

    def _col_field(self, cx: float) -> str | None:
        for name, x1, x2 in self.LAYOUT.columns:
            if x1 <= cx <= x2:
                return name
        return None

    def _parse_rows(self, rows, result: ParseResult) -> None:
        vx1, vx2 = self.LAYOUT.voltage_x
        # 第一遍：找代理购电价格的合并单元格值（整表一个值，常出现在表中间单独成行）
        proxy_merged_value: float | None = None
        px1 = px2 = 0
        for name, x1, x2 in self.LAYOUT.columns:
            if name == "proxy_price":
                px1, px2 = x1, x2
        for row in rows:
            nums = [b for b in row if px1 <= b.cx <= px2 and parse_number(b.text) is not None]
            # 合并单元格行：整行只有这一个数值框
            all_nums = [b for b in row if parse_number(b.text) is not None]
            if len(nums) == 1 and len(all_nums) == 1:
                proxy_merged_value = parse_number(nums[0].text)
                break

        usage1 = ""
        merged: dict[str, float | None] = {}
        in_main_table = False
        for row in rows:
            texts = [b.text for b in row]
            line = "".join(texts)
            # 识别主表开始/结束（跳过 1.5 倍表、信息表、注释）
            if "代理购电" in line and ("电价表" in line or "价格表" in line):
                in_main_table = "1.5" not in line and "1.5倍" not in line.replace(" ", "")
                continue
            if "1.5" in line and "代理购电" in line:
                in_main_table = False
                continue
            if "信息" in line and ("公开" in line or "价格表" in line):
                in_main_table = False
                continue
            if not in_main_table:
                continue
            # 用电分类
            if self.LAYOUT.usage_keywords[0] in line:
                usage1 = "两部制"
            elif self.LAYOUT.usage_keywords[1] in line:
                usage1 = "单一制"
            # 找电压等级框
            vbox = next((b for b in row if vx1 <= b.cx <= vx2 and "千伏" in b.text), None)
            if vbox is None:
                continue
            voltage = canonical_voltage(vbox.text)
            if not voltage or "千" not in voltage:
                continue
            values: dict[str, float | None] = {}
            for b in row:
                field = self._col_field(b.cx)
                if not field:
                    continue
                num = parse_number(b.text)
                if num is None:
                    continue
                if field not in values:
                    values[field] = num
            # 代理购电价格：整表合并值填充
            if values.get("proxy_price") is None:
                values["proxy_price"] = proxy_merged_value
            # 其余合并单元格补全
            for f in ("loss_fee", "system_run_fee", "gov_fund"):
                if values.get(f) is None and f in merged:
                    values[f] = merged[f]
                if values.get(f) is not None:
                    merged[f] = values[f]
            row_obj = PriceRow(
                region=self.region,
                month=result.month,
                usage_type1=usage1,
                usage_type2=self.usage_type2,
                voltage_level=voltage,
                values=values,
            )
            result.rows.append(row_obj)


# ---------------- 云南 ----------------

class YunnanOcrExtractor(OcrTableExtractor):
    """云南电网：长图 PNG，主表 10 列（其中4分项+分时4+容需量2），代理购电价合并。"""

    region = "云南省"
    network = "csg"
    IS_IMAGE = True
    LAYOUT = ColumnLayout(
        voltage_x=(300, 440),
        columns=[
            ("proxy_price", 585, 735),       # 合并单元格（cx≈672，整表一个值）
            ("loss_fee", 750, 850),          # 0.008381 (cx≈802)
            ("transmission_price", 880, 1000),  # 输配电价 (cx≈930)
            ("gov_fund", 1000, 1140),        # 政府性基金（其中：政府性基金及附加）
            ("system_run_fee", 1140, 1280),  # 系统运行费 (cx≈1188)
            ("total_high", 1400, 1500),      # 高峰 (cx≈1436)
            ("total_flat", 1520, 1620),      # 平段 (cx≈1560)
            ("total_low", 1640, 1750),       # 低谷 (cx≈1684)
            ("demand_price", 1780, 1870),    # 最大需量 (cx≈1807)
            ("capacity_price", 1900, 1990),  # 变压器容量 (cx≈1935)
        ],
        proxy_price_merged=True,
    )


OCR_EXTRACTORS: dict[str, type[OcrTableExtractor]] = {
    "云南省": YunnanOcrExtractor,
}
