"""Daily statistics from applied.json + negotiations.json."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DAY = 86400


@dataclass
class DailyStats:
    applied: int = 0
    responses: int = 0
    test_tasks: int = 0
    interviews: int = 0
    rejections: int = 0
    no_answer: int = 0
    by_employer: dict[str, dict[str, int]] = field(default_factory=dict)

    def summary(self) -> str:
        lines = [
            f"Откликов: {self.applied}",
            f"Ответов: {self.responses}",
            f"Тестовые задания: {self.test_tasks}",
            f"Собеседования: {self.interviews}",
            f"Отказов: {self.rejections}",
            f"Без ответа: {self.no_answer}",
        ]
        if self.by_employer:
            lines.append("\nПо компаниям:")
            for emp, counts in self.by_employer.items():
                parts = [f"{k}={v}" for k, v in counts.items() if v]
                if parts:
                    lines.append(f"  {emp}: {', '.join(parts)}")
        return "\n".join(lines)


def _is_today(ts: int) -> bool:
    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).date()
    return dt == datetime.now(tz=timezone.utc).date()


def _load_json(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def daily_report(applied_path: str | Path = "applied.json",
                 negotiations_path: str | Path = "negotiations.json") -> DailyStats:
    stats = DailyStats()
    applied = _load_json(applied_path)
    for vid, entry in applied.items():
        ts = entry.get("ts", 0)
        if _is_today(ts):
            stats.applied += 1
            emp = entry.get("employer", "?")
            stats.by_employer.setdefault(emp, {"applied": 0, "responses": 0,
                                                "test_tasks": 0, "interviews": 0,
                                                "rejections": 0})
            stats.by_employer[emp]["applied"] += 1

    negotiations = _load_json(negotiations_path)
    for thread_id, thread in negotiations.items():
        last_ts = thread.get("last_ts", 0)
        if not _is_today(last_ts):
            continue
        emp = thread.get("employer", "?")
        stats.by_employer.setdefault(emp, {"applied": 0, "responses": 0,
                                            "test_tasks": 0, "interviews": 0,
                                            "rejections": 0})
        status = thread.get("status", "")
        if status in ("new", "viewed", "answered"):
            stats.responses += 1
            stats.by_employer[emp]["responses"] += 1
        if status in ("test_task",):
            stats.test_tasks += 1
            stats.by_employer[emp]["test_tasks"] += 1
        if status in ("interview",):
            stats.interviews += 1
            stats.by_employer[emp]["interviews"] += 1
        if status in ("rejected", "declined"):
            stats.rejections += 1
            stats.by_employer[emp]["rejections"] += 1

    stats.no_answer = max(0, stats.applied - stats.responses)
    return stats


def print_report(applied_path: str | Path = "applied.json",
                 negotiations_path: str | Path = "negotiations.json") -> str:
    stats = daily_report(applied_path, negotiations_path)
    text = stats.summary()
    print(text)
    return text
