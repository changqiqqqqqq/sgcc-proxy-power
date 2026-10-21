from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

DIMENSION_FIELDS = ["region", "month", "usage_type1", "usage_type2", "voltage_level"]

# 与《各省代理购电价格_合并.xlsx》保持一致的 26 个数值字段（内部名为英文）。
VALUE_FIELDS = [
    "proxy_price",
    "proxy_sharp",
    "proxy_high",
    "proxy_flat",
    "proxy_low",
    "proxy_deep",
    "proxy_month_avg",
    "proxy_capacity_comp",
    "proxy_history_dev",
    "loss_fee",
    "loss_sharp",
    "loss_high",
    "loss_flat",
    "loss_low",
    "loss_deep",
    "transmission_price",
    "system_run_fee",
    "gov_fund",
    "other_fee",
    "total_sharp",
    "total_high",
    "total_flat",
    "total_low",
    "total_deep",
    "demand_price",
    "capacity_price",
]

ROW_FIELDS = DIMENSION_FIELDS + VALUE_FIELDS

# 导出/展示用的中文表头，顺序与合并表一致。
EXPORT_HEADERS = [
    ("region", "地区"),
    ("month", "月份"),
    ("usage_type1", "用电类型一"),
    ("usage_type2", "用电类型二"),
    ("voltage_level", "电压等级"),
    ("proxy_price", "代理购电价格"),
    ("proxy_sharp", "代理购电价格-尖峰时段"),
    ("proxy_high", "代理购电价格-高峰时段"),
    ("proxy_flat", "代理购电价格-平时段"),
    ("proxy_low", "代理购电价格-低谷时段"),
    ("proxy_deep", "代理购电价格-深谷时段"),
    ("proxy_month_avg", "代理购电价格-当月平均购电价格"),
    ("proxy_capacity_comp", "代理购电价格-容量补偿电价"),
    ("proxy_history_dev", "代理购电价格-历史偏差电费折价"),
    ("loss_fee", "上网环节线损费用"),
    ("loss_sharp", "上网环节线损电价-尖峰时段"),
    ("loss_high", "上网环节线损电价-高峰时段"),
    ("loss_flat", "上网环节线损电价-平时段"),
    ("loss_low", "上网环节线损电价-低谷时段"),
    ("loss_deep", "上网环节线损电价-深谷时段"),
    ("transmission_price", "电度输配电价"),
    ("system_run_fee", "系统运行费用"),
    ("gov_fund", "政府性基金及附加"),
    ("other_fee", "其它"),
    ("total_sharp", "尖峰时段"),
    ("total_high", "高峰时段"),
    ("total_flat", "平时段"),
    ("total_low", "低谷时段"),
    ("total_deep", "深谷时段"),
    ("demand_price", "需量电价"),
    ("capacity_price", "容量电价"),
]

# 电压等级写法归一化（保留原文展示，归一值用于筛选）。
VOLTAGE_ALIASES = {
    "不满1千伏": "不满1千伏",
    "不满1kv": "不满1千伏",
    "1～10千伏": "1-10千伏",
    "1-10千伏": "1-10千伏",
    "1～10（20）千伏": "1-10（20）千伏",
    "1-10(20)千伏": "1-10（20）千伏",
    "1-10（20）千伏": "1-10（20）千伏",
    "1~10(20)千伏": "1-10（20）千伏",
    "1~10（20）千伏": "1-10（20）千伏",
    "10千伏": "1-10千伏",
    "35千伏": "35千伏",
    "35～110千伏": "35-110千伏",
    "35-110千伏": "35-110千伏",
    "35千伏及以上": "35千伏及以上",
    "110千伏": "110千伏",
    "110（66）千伏": "110（66）千伏",
    "110～220千伏": "110-220千伏",
    "110千伏及以上": "110千伏及以上",
    "220千伏": "220千伏",
    "220千伏及以上": "220千伏及以上",
    "220（330）千伏": "220（330）千伏",
    "330千伏": "330千伏",
    "66千伏": "66千伏",
}


def normalize_voltage(value: str) -> str:
    text = re.sub(r"\s+", "", str(value or ""))
    text = text.replace("～", "-").replace("~", "-").replace("—", "-").replace("（", "(").replace("）", ")")
    for src, dst in VOLTAGE_ALIASES.items():
        if text == src.replace("～", "-").replace("（", "(").replace("）", ")"):
            return dst
    # 兜底：截断的"220千伏及"之类按前缀匹配
    for suffix in ("及以上", "以下"):
        for src, dst in VOLTAGE_ALIASES.items():
            base = src.replace("～", "-").replace("（", "(").replace("）", ")")
            if base.endswith(suffix) and text == base[: -len(suffix)] + suffix[: len(text) - (len(base) - len(suffix))]:
                return dst
    if text in {"220千伏及", "220千伏及以"}:
        return "220千伏及以上"
    return str(value or "").strip()


def normalize_usage_type1(value: str) -> str:
    text = str(value or "").strip()
    if "两部" in text:
        return "两部制"
    if "单一" in text:
        return "单一制"
    return text


def parse_number(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace("，", "")
    if text in {"", "-", "—", "/", "\\", "--", "无"}:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


@dataclass
class PriceRow:
    region: str
    month: str
    usage_type1: str
    usage_type2: str
    voltage_level: str
    values: dict[str, float | None] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        row = {
            "region": self.region,
            "month": self.month,
            "usage_type1": self.usage_type1,
            "usage_type2": self.usage_type2,
            "voltage_level": self.voltage_level,
            "voltage_norm": normalize_voltage(self.voltage_level),
        }
        for name in VALUE_FIELDS:
            row[name] = self.values.get(name)
        return row


@dataclass
class ParseResult:
    source_file: str
    region: str
    month: str
    status: str  # success / partial / failed
    rows: list[PriceRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS price_rows (
    region TEXT NOT NULL,
    month TEXT NOT NULL,
    usage_type1 TEXT NOT NULL DEFAULT '',
    usage_type2 TEXT NOT NULL DEFAULT '',
    voltage_level TEXT NOT NULL DEFAULT '',
    voltage_norm TEXT NOT NULL DEFAULT '',
    {", ".join(f"{name} REAL" for name in VALUE_FIELDS)},
    source_file TEXT NOT NULL DEFAULT '',
    network TEXT NOT NULL DEFAULT '',
    parse_status TEXT NOT NULL DEFAULT 'success',
    parse_confidence REAL NOT NULL DEFAULT 1,
    parsed_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (region, month, usage_type1, usage_type2, voltage_level)
);
CREATE TABLE IF NOT EXISTS parse_files (
    source_file TEXT PRIMARY KEY,
    region TEXT NOT NULL DEFAULT '',
    month TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    row_count INTEGER NOT NULL DEFAULT 0,
    errors TEXT NOT NULL DEFAULT '',
    warnings TEXT NOT NULL DEFAULT '',
    parsed_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_price_rows_region ON price_rows(region, month);
"""


class PriceStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self.lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self.lock:
            self._conn.close()

    def save_result(self, result: ParseResult, network: str = "") -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.lock:
            cur = self._conn.cursor()
            if result.rows:
                cur.execute(
                    "DELETE FROM price_rows WHERE source_file = ?",
                    (result.source_file,),
                )
            for row in result.rows:
                data = row.as_dict()
                cur.execute(
                    f"""
                    INSERT OR REPLACE INTO price_rows (
                        region, month, usage_type1, usage_type2, voltage_level, voltage_norm,
                        {", ".join(VALUE_FIELDS)},
                        source_file, network, parse_status, parse_confidence, parsed_at
                    ) VALUES ({", ".join(["?"] * (6 + len(VALUE_FIELDS) + 5))})
                    """,
                    (
                        data["region"], data["month"], data["usage_type1"], data["usage_type2"],
                        data["voltage_level"], data["voltage_norm"],
                        *[data[name] for name in VALUE_FIELDS],
                        result.source_file, network, result.status,
                        1.0 if result.status == "success" else 0.5, now,
                    ),
                )
            cur.execute(
                """
                INSERT OR REPLACE INTO parse_files (source_file, region, month, status, row_count, errors, warnings, parsed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.source_file, result.region, result.month, result.status,
                    len(result.rows), " | ".join(result.errors), " | ".join(result.warnings), now,
                ),
            )
            self._conn.commit()

    def delete_source(self, source_file: str) -> None:
        with self.lock:
            self._conn.execute("DELETE FROM price_rows WHERE source_file = ?", (source_file,))
            self._conn.execute("DELETE FROM parse_files WHERE source_file = ?", (source_file,))
            self._conn.commit()

    def file_status(self, source_file: str) -> dict[str, Any] | None:
        with self.lock:
            row = self._conn.execute(
                "SELECT * FROM parse_files WHERE source_file = ?", (source_file,)
            ).fetchone()
        return dict(row) if row else None

    def all_file_status(self) -> dict[str, dict[str, Any]]:
        with self.lock:
            rows = self._conn.execute("SELECT * FROM parse_files").fetchall()
        return {row["source_file"]: dict(row) for row in rows}

    def refresh_voltage_norms(self) -> int:
        with self.lock:
            rows = self._conn.execute("SELECT rowid, voltage_level FROM price_rows").fetchall()
            updated = 0
            for row in rows:
                norm = normalize_voltage(row["voltage_level"])
                self._conn.execute("UPDATE price_rows SET voltage_norm = ? WHERE rowid = ?", (norm, row["rowid"]))
                updated += 1
            self._conn.commit()
        return updated

    def query(
        self,
        region: str = "",
        month: str = "",
        usage_type1: str = "",
        voltage: str = "",
        limit: int = 500,
        offset: int = 0,
    ) -> tuple[int, list[dict[str, Any]]]:
        clauses: list[str] = []
        params: list[Any] = []
        if region:
            clauses.append("region = ?")
            params.append(region)
        if month:
            clauses.append("month = ?")
            params.append(month)
        if usage_type1:
            clauses.append("usage_type1 = ?")
            params.append(usage_type1)
        if voltage:
            clauses.append("(voltage_level = ? OR voltage_norm = ?)")
            params.extend([voltage, voltage])
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self.lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) AS c FROM price_rows {where}", params
            ).fetchone()["c"]
            rows = self._conn.execute(
                f"""SELECT * FROM price_rows {where}
                    ORDER BY region, month, usage_type1 DESC, voltage_level
                    LIMIT ? OFFSET ?""",
                params + [limit, offset],
            ).fetchall()
        return total, [dict(row) for row in rows]

    def iter_all(self) -> Iterable[dict[str, Any]]:
        with self.lock:
            rows = self._conn.execute(
                "SELECT * FROM price_rows ORDER BY region, month, usage_type1 DESC, voltage_level"
            ).fetchall()
        for row in rows:
            yield dict(row)

    def summary(self) -> dict[str, Any]:
        with self.lock:
            regions = [r[0] for r in self._conn.execute(
                "SELECT DISTINCT region FROM price_rows ORDER BY region"
            ).fetchall()]
            months = [r[0] for r in self._conn.execute(
                "SELECT DISTINCT month FROM price_rows ORDER BY month"
            ).fetchall()]
            usage = [r[0] for r in self._conn.execute(
                "SELECT DISTINCT usage_type1 FROM price_rows WHERE usage_type1 != '' ORDER BY usage_type1"
            ).fetchall()]
            voltages = [r[0] for r in self._conn.execute(
                "SELECT DISTINCT voltage_norm FROM price_rows WHERE voltage_norm != '' ORDER BY voltage_norm"
            ).fetchall()]
            row_count = self._conn.execute("SELECT COUNT(*) AS c FROM price_rows").fetchone()["c"]
            files = self._conn.execute(
                "SELECT status, COUNT(*) AS c FROM parse_files GROUP BY status"
            ).fetchall()
        return {
            "regions": regions,
            "months": months,
            "usage_types": usage,
            "voltages": voltages,
            "row_count": row_count,
            "files": {row["status"]: row["c"] for row in files},
        }
