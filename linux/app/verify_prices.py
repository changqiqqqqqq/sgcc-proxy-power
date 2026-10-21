from __future__ import annotations

"""用《各省代理购电价格_合并.xlsx》作为标准答案，回归比对解析结果。"""

import argparse
import sys
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.prices import EXPORT_HEADERS, PriceStore  # noqa: E402

TOLERANCE = 1e-4
DIM_FIELDS = ["region", "month", "usage_type1", "usage_type2", "voltage_level"]
# 解析方提供、但参考合并表恒为 0/空的字段，比对时跳过
SKIP_FIELDS = {"proxy_month_avg", "proxy_history_dev", "proxy_capacity_comp", "other_fee",
               "loss_sharp", "loss_high", "loss_flat", "loss_low", "loss_deep",
               "proxy_sharp", "proxy_high", "proxy_flat", "proxy_low", "proxy_deep"}


def load_reference(xlsx: Path) -> dict[tuple, dict]:
    wb = openpyxl.load_workbook(str(xlsx), data_only=True, read_only=True)
    ws = wb[wb.sheetnames[0]]
    headers = None
    ref: list[dict] = []
    region = month = usage1 = ""
    for row in ws.iter_rows(values_only=True):
        if headers is None:
            headers = [str(h).strip() for h in row]
            continue
        cells = list(row)
        if all(v is None for v in cells):
            continue
        if cells[0]:
            region = str(cells[0]).strip()
        if cells[1]:
            month = str(cells[1]).strip()
        if cells[2]:
            usage1 = str(cells[2]).strip()
        record = {"region": region, "month": month, "usage_type1": usage1}
        for name, header in EXPORT_HEADERS[3:]:
            idx = headers.index(header)
            record[name] = cells[idx]
        ref.append(record)
    return ref


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--xlsx", required=True)
    parser.add_argument("--db", required=True)
    args = parser.parse_args()

    ref = load_reference(Path(args.xlsx))
    store = PriceStore(Path(args.db))
    _, parsed = store.query(limit=100000)

    # load_reference 已按原表顺序完成维度向下填充，直接按键索引
    filled: dict[tuple, dict] = {}
    for record in ref:
        key = (record["region"], record["month"], record["usage_type1"], record["usage_type2"], record["voltage_level"])
        filled[key] = record

    compared = matched = 0
    mismatches: list[str] = []
    missing: list[str] = []
    for row in parsed:
        key = (row["region"], row["month"], row["usage_type1"], row["usage_type2"], row["voltage_level"])
        target = filled.get(key)
        if target is None:
            missing.append(f"参考表无此行: {key}")
            continue
        compared += 1
        row_ok = True
        for name, _header in EXPORT_HEADERS[5:]:
            if name in SKIP_FIELDS:
                continue
            got = row.get(name)
            want = target.get(name)
            want_num = None
            if want not in (None, ""):
                try:
                    want_num = float(str(want).replace(",", ""))
                except ValueError:
                    want_num = None
            if got is None and want_num in (None, 0.0):
                continue
            if got is None or want_num is None:
                if (got or 0) != (want_num or 0):
                    mismatches.append(f"{key} {name}: 解析={got} 参考={want}")
                    row_ok = False
            elif abs(got - want_num) > TOLERANCE:
                mismatches.append(f"{key} {name}: 解析={got} 参考={want_num}")
                row_ok = False
        matched += row_ok

    print(f"比对行数: {compared}，完全一致: {matched}，字段级差异: {len(mismatches)}，参考表缺失行: {len(missing)}")
    for line in mismatches[:40]:
        print("  DIFF", line)
    for line in missing[:20]:
        print("  MISS", line)


if __name__ == "__main__":
    main()
