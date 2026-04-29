"""Локальное хранилище ID вакансий, на которые уже откликнулись."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class AppliedLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                self._data = raw
            elif isinstance(raw, list):  # на случай старого формата
                self._data = {str(x): {"ts": 0} for x in raw}
        except (json.JSONDecodeError, OSError):
            self._data = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)

    def has(self, vacancy_id: str) -> bool:
        return str(vacancy_id) in self._data

    def add(self, vacancy_id: str, *, name: str = "", employer: str = "", status: str = "applied") -> None:
        self._data[str(vacancy_id)] = {
            "ts": int(time.time()),
            "name": name,
            "employer": employer,
            "status": status,
        }
        self._save()

    def __len__(self) -> int:
        return len(self._data)
