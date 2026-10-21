from __future__ import annotations

"""OCR 表格识别：把扫描 PDF / 图片中的价格表识别成与文字型 PDF 相同的行结构。

流程：
1. 图片（或 PDF 渲染成的图片）分块做高分辨率 OCR，得到带坐标的文本框；
2. 按 y 坐标聚类成行、按 x 坐标分配到列，重组为二维表格（list[list[str]]）；
3. 输出与 pdfplumber extract_tables 兼容的结构，复用 GenericTableExtractor 解析。
"""

import logging
import os
import re
from dataclasses import dataclass

import numpy as np

LOGGER = logging.getLogger(__name__)

_ocr_instance = None


def get_ocr():
    """延迟初始化 PaddleOCR（首次加载模型较慢）。"""
    global _ocr_instance
    if _ocr_instance is None:
        os.environ.setdefault("FLAGS_use_mkldnn", "0")
        from paddleocr import PaddleOCR

        _ocr_instance = PaddleOCR(
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            lang="ch",
            enable_mkldnn=False,
        )
    return _ocr_instance


@dataclass
class OcrBox:
    x1: float
    y1: float
    x2: float
    y2: float
    text: str

    @property
    def cx(self) -> float:
        return (self.x1 + self.x2) / 2

    @property
    def cy(self) -> float:
        return (self.y1 + self.y2) / 2

    @property
    def height(self) -> float:
        return self.y2 - self.y1


def ocr_image(image: np.ndarray) -> list[OcrBox]:
    """对一块图片做 OCR，返回文本框列表。"""
    ocr = get_ocr()
    result = ocr.predict(image)
    boxes: list[OcrBox] = []
    if not result:
        return boxes
    r = result[0]
    texts = r["rec_texts"]
    rec_boxes = r["rec_boxes"]
    for i in range(len(texts)):
        x1, y1, x2, y2 = rec_boxes[i]
        boxes.append(OcrBox(float(x1), float(y1), float(x2), float(y2), str(texts[i]).strip()))
    return boxes


def cluster_rows(boxes: list[OcrBox], y_tol: float | None = None) -> list[list[OcrBox]]:
    """按 y 中心聚类成行。y_tol 取框中位高度的 0.6。"""
    if not boxes:
        return []
    heights = sorted(b.height for b in boxes)
    median_h = heights[len(heights) // 2] if heights else 20
    tol = y_tol if y_tol is not None else max(12.0, median_h * 0.6)
    ordered = sorted(boxes, key=lambda b: b.cy)
    rows: list[list[OcrBox]] = []
    row_centers: list[float] = []
    for box in ordered:
        placed = False
        for idx, center in enumerate(row_centers):
            if abs(center - box.cy) <= tol:
                rows[idx].append(box)
                row_centers[idx] = (center * (len(rows[idx]) - 1) + box.cy) / len(rows[idx])
                placed = True
                break
        if not placed:
            rows.append([box])
            row_centers.append(box.cy)
    # 按行中心排序，行内按 x 排序
    paired = sorted(zip(row_centers, rows), key=lambda p: p[0])
    return [sorted(r, key=lambda b: b.x1) for _c, r in paired]


def assign_columns(rows: list[list[OcrBox]]) -> list[list[str]]:
    """把多行 OCR 框分配到统一的列网格，输出二维文本表。

    列边界由所有框的 x 中心聚类得到；相邻列中心间距过近（< 列宽阈值）的合并。
    """
    all_boxes = [b for row in rows for b in row]
    if not all_boxes:
        return []
    # 收集数值列的 x 中心（价格表数据列框多为数值，用于确定列网格）
    centers = sorted(b.cx for b in all_boxes)
    # 简单聚类：间距 > 40 视为新列
    col_centers: list[float] = []
    for cx in centers:
        if not col_centers or cx - col_centers[-1] > 40:
            col_centers.append(cx)
        else:
            col_centers[-1] = (col_centers[-1] + cx) / 2
    n_cols = len(col_centers)

    def col_of(cx: float) -> int:
        best, best_d = 0, float("inf")
        for i, cc in enumerate(col_centers):
            d = abs(cc - cx)
            if d < best_d:
                best, best_d = i, d
        return best

    table: list[list[str]] = []
    for row in rows:
        cells = [""] * n_cols
        for box in sorted(row, key=lambda b: b.x1):
            ci = col_of(box.cx)
            cells[ci] = (cells[ci] + box.text) if cells[ci] else box.text
        table.append(cells)
    return table


def boxes_to_table(boxes: list[OcrBox]) -> list[list[str]]:
    """OCR 框 → 二维表格文本。"""
    rows = cluster_rows(boxes)
    return assign_columns(rows)


def load_image(path: str) -> np.ndarray:
    from PIL import Image

    with Image.open(path) as im:
        return np.array(im.convert("RGB"))


def pdf_page_to_image(path: str, page_no: int, zoom: float = 2.0) -> np.ndarray:
    """把 PDF 某页渲染成图片（用于扫描件）。"""
    import fitz  # PyMuPDF

    doc = fitz.open(path)
    page = doc[page_no]
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:
        arr = arr[:, :, :3]
    doc.close()
    return arr


def crop_regions(image: np.ndarray, max_side: int = 3800) -> list[np.ndarray]:
    """把超高/超宽图片切成不超过 max_side 的块（PaddleOCR 单边限制 4000）。"""
    h, w = image.shape[:2]
    if max(h, w) <= max_side:
        return [image]
    blocks = []
    # 按高度切块（价格表通常是长图）
    step = max_side
    y = 0
    while y < h:
        blocks.append(image[y : y + step, :])
        y += step
    return blocks
