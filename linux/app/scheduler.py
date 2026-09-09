from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .catalog import atomic_json_write, read_json


LOGGER = logging.getLogger(__name__)


def _field_values(field: str, minimum: int, maximum: int) -> set[int]:
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        stepped = False
        if "/" in part:
            part, raw_step = part.split("/", 1)
            step = max(1, int(raw_step))
            stepped = True
        if part == "*":
            start, end = minimum, maximum
        elif "-" in part:
            raw_start, raw_end = part.split("-", 1)
            start, end = int(raw_start), int(raw_end)
        else:
            start = int(part)
            end = maximum if stepped else start
        if start < minimum or end > maximum or start > end:
            raise ValueError(f"cron field out of range: {field}")
        values.update(range(start, end + 1, step))
    if not values:
        raise ValueError(f"empty cron field: {field}")
    return values


class CronSpec:
    def __init__(self, expression: str) -> None:
        fields = expression.split()
        if len(fields) != 5:
            raise ValueError("SGCC_SCAN_CRON must contain five fields")
        minute, hour, day, month, weekday = fields
        self.minutes = _field_values(minute, 0, 59)
        self.hours = _field_values(hour, 0, 23)
        self.days = _field_values(day, 1, 31)
        self.months = _field_values(month, 1, 12)
        self.weekdays = _field_values(weekday, 0, 7)
        self.day_wildcard = day == "*"
        self.weekday_wildcard = weekday == "*"

    def matches(self, value: datetime) -> bool:
        # cron: Sunday=0/7; Python: Monday=0.
        cron_weekday = (value.weekday() + 1) % 7
        day_matches = value.day in self.days
        weekday_matches = cron_weekday in self.weekdays or (cron_weekday == 0 and 7 in self.weekdays)
        calendar_matches = day_matches and weekday_matches
        if not self.day_wildcard and not self.weekday_wildcard:
            calendar_matches = day_matches or weekday_matches
        return (
            value.minute in self.minutes
            and value.hour in self.hours
            and value.month in self.months
            and calendar_matches
        )

    def seconds_until_next(self, timezone: ZoneInfo) -> float:
        now = datetime.now(timezone)
        candidate = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(60 * 24 * 370):
            if self.matches(candidate):
                return max(0.1, (candidate - now).total_seconds())
            candidate += timedelta(minutes=1)
        raise RuntimeError("could not find the next cron occurrence")


class ScanScheduler:
    """Single-process scheduler that never overlaps callback executions."""

    def __init__(
        self,
        callback: Callable,
        interval_seconds: float | None = None,
        *,
        cron: str | None = None,
        timezone: str = "Asia/Shanghai",
    ) -> None:
        if interval_seconds is None and not cron:
            raise ValueError("interval_seconds or cron is required")
        self.callback = callback
        self.interval_seconds = interval_seconds
        self.cron = CronSpec(cron) if cron else None
        self.timezone = ZoneInfo(timezone)
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.callback_lock = threading.Lock()

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, name="sgcc-scheduler", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.wake_event.set()
        if self.thread and self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)

    def trigger(self) -> bool:
        if self.callback_lock.locked():
            return False
        threading.Thread(target=self._run_callback, name="sgcc-scheduler-trigger", daemon=True).start()
        return True

    def _delay(self) -> float:
        if self.cron is not None:
            return self.cron.seconds_until_next(self.timezone)
        return max(0.01, float(self.interval_seconds or 1))

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            self.wake_event.clear()
            interrupted = self.wake_event.wait(self._delay())
            if self.stop_event.is_set():
                break
            if not interrupted:
                self.trigger()

    def _run_callback(self) -> None:
        if not self.callback_lock.acquire(blocking=False):
            return
        try:
            result = self.callback()
            if inspect.isawaitable(result):
                asyncio.run(result)
        except Exception:
            LOGGER.exception("scheduled scan callback failed")
        finally:
            self.callback_lock.release()


class ScheduleController:
    """Persist schedule settings and replace the in-process scheduler atomically."""

    def __init__(
        self,
        callback: Callable,
        config_file: Path,
        *,
        enabled: bool,
        cron: str,
        timezone: str,
        download_after_scan: bool,
        on_change: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.callback = callback
        self.config_file = config_file
        self.timezone = timezone
        self.on_change = on_change
        self.lock = threading.RLock()
        self.scheduler: ScanScheduler | None = None
        self.started = False
        defaults = self._defaults(enabled, cron, download_after_scan)
        saved = read_json(config_file, {})
        try:
            self.state = self._normalize(saved if isinstance(saved, dict) else {}, defaults)
        except (TypeError, ValueError):
            self.state = defaults
        self._notify()

    def _defaults(self, enabled: bool, cron: str, download_after_scan: bool) -> dict[str, Any]:
        parts = cron.split()
        daily = len(parts) == 5 and parts[2:] == ["*", "*", "*"] and all(x.isdigit() for x in parts[:2])
        daily_time = f"{int(parts[1]):02d}:{int(parts[0]):02d}" if daily else "03:00"
        CronSpec(cron)
        return {
            "enabled": enabled,
            "mode": "daily" if daily else "cron",
            "daily_time": daily_time,
            "interval_minutes": 1440,
            "cron": cron,
            "download_after_scan": download_after_scan,
            "timezone": self.timezone,
        }

    def _normalize(self, raw: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
        state = dict(base)
        state.update({key: raw[key] for key in state if key in raw and key != "timezone"})
        if not isinstance(state["enabled"], bool) or not isinstance(state["download_after_scan"], bool):
            raise ValueError("定时开关必须是布尔值")
        mode = str(state["mode"])
        if mode not in {"daily", "interval", "cron"}:
            raise ValueError("定时模式无效")
        state["mode"] = mode
        if mode == "daily":
            try:
                hour_text, minute_text = str(state["daily_time"]).split(":", 1)
                hour, minute = int(hour_text), int(minute_text)
            except (TypeError, ValueError) as exc:
                raise ValueError("每日执行时间格式无效") from exc
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError("每日执行时间超出范围")
            state["daily_time"] = f"{hour:02d}:{minute:02d}"
            state["cron"] = f"{minute} {hour} * * *"
        elif mode == "interval":
            try:
                interval = int(state["interval_minutes"])
            except (TypeError, ValueError) as exc:
                raise ValueError("扫描间隔必须是整数") from exc
            if not 5 <= interval <= 10080:
                raise ValueError("扫描间隔应为 5 到 10080 分钟")
            state["interval_minutes"] = interval
        else:
            state["cron"] = str(state["cron"]).strip()
            CronSpec(state["cron"])
        state["timezone"] = self.timezone
        return state

    def _notify(self) -> None:
        if self.on_change:
            self.on_change(dict(self.state))

    def _replace_scheduler(self) -> None:
        if self.scheduler:
            self.scheduler.stop()
            self.scheduler = None
        if not self.started or not self.state["enabled"]:
            return
        if self.state["mode"] == "interval":
            self.scheduler = ScanScheduler(
                self.callback,
                interval_seconds=self.state["interval_minutes"] * 60,
                timezone=self.timezone,
            )
        else:
            self.scheduler = ScanScheduler(
                self.callback,
                cron=self.state["cron"],
                timezone=self.timezone,
            )
        self.scheduler.start()

    def start(self) -> None:
        with self.lock:
            self.started = True
            self._replace_scheduler()

    def stop(self) -> None:
        with self.lock:
            self.started = False
            self._replace_scheduler()

    def update(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.state = self._normalize(payload, self.state)
            atomic_json_write(self.config_file, self.state)
            self._notify()
            self._replace_scheduler()
            return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            value = dict(self.state)
            value["running"] = bool(self.scheduler and self.scheduler.thread and self.scheduler.thread.is_alive())
            return value
