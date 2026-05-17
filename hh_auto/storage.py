"""Локальное хранилище ID вакансий, на которые уже откликнулись."""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any


def _normalize_url(url: str) -> str:
    """Убрать query-параметры и trailing slash."""
    if not url:
        return ""
    return url.split("?")[0].rstrip("/")


def _extract_id_from_url(url: str) -> str:
    """Извлечь vacancy_id из URL /vacancy/12345."""
    if not url:
        return ""
    m = re.search(r"/vacancy/(\d+)", url)
    return m.group(1) if m else ""


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

    def has(self, vacancy_id: str, url: str = "") -> bool:
        vid = str(vacancy_id)
        if vid in self._data:
            logging.getLogger(__name__).debug("applied.has: found by id %s", vid)
            return True
        if url:
            norm = _normalize_url(url)
            if norm in self._data:
                logging.getLogger(__name__).debug("applied.has: found by url %s", norm)
                return True
        return False

    def add(
        self,
        vacancy_id: str,
        *,
        name: str = "",
        employer: str = "",
        status: str = "",
        url: str = "",
    ) -> None:
        entry = {
            "ts": int(time.time()),
            "name": name,
            "employer": employer,
            "status": status,
            "url": url,
        }
        vid = str(vacancy_id)
        self._data[vid] = entry
        if url:
            # Также индексируем по нормализованному URL для быстрого поиска
            self._data[_normalize_url(url)] = entry
        self._save()

    def __len__(self) -> int:
        return len(self._data)


class SearchProgress:
    """Хранит ID вакансий, которые уже проверяли и явно пропустили.

    Не откликнулись (applied) — отдельно.
    Не дотронутые из-за max_per_run — НЕ записываем, чтобы при
    следующем запуске продолжить с них.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._seen: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict) and isinstance(raw.get("seen"), dict):
                self._seen = raw["seen"]
        except (json.JSONDecodeError, OSError):
            self._seen = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump({"seen": self._seen}, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)

    def has(self, vid: str) -> bool:
        return str(vid) in self._seen

    def skip(
        self,
        vid: str,
        *,
        name: str = "",
        employer: str = "",
        query: str = "",
        reason: str = "",
    ) -> None:
        vid = str(vid)
        self._seen[vid] = {
            "ts": int(time.time()),
            "name": name,
            "employer": employer,
            "query": query,
            "reason": reason,
        }
        self._save()
