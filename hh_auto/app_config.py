"""Общие helpers для загрузки, сохранения и дефолтной конфигурации приложения."""
from __future__ import annotations

from pathlib import Path

import yaml

from .browser import BrowserConfig
from .profile import CandidateProfile, build_search_queries, default_candidate_profile


def load_config(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)
    with p.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config {path} must contain a mapping")
    return data


def save_config(path: str | Path, cfg: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)


def save_env_file(path: str | Path, values: dict[str, object]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for key, value in values.items():
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        lines.append(f"{key}={text}")
    p.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def make_browser_config(cfg: dict, user_data_dir_override: str | None = None) -> BrowserConfig:
    b = cfg.get("browser", {}) or {}
    return BrowserConfig(
        user_data_dir=user_data_dir_override or b.get("user_data_dir", "user-data"),
        headless=bool(b.get("headless", False)),
        default_timeout_ms=int(b.get("default_timeout_ms", 20_000)),
        slow_mo_ms=int(b.get("slow_mo_ms", 50)),
        locale=b.get("locale", "ru-RU"),
        timezone=b.get("timezone", "Europe/Moscow"),
        screenshots_dir=cfg.get("screenshots_dir", "screenshots"),
    )


def build_default_config() -> dict:
    profile = default_candidate_profile()
    queries = build_search_queries(profile)
    search_schedule = "remote" if profile.work_format == "remote" else None
    search_cfg: dict = {
        "area": profile.area() or 113,
        "experience": [profile.experience],
        "remote_work_format": profile.work_format == "remote",
        "queries": queries,
        "max_pages": 5,
    }
    if search_schedule:
        search_cfg["schedule"] = search_schedule

    return {
        "browser": {
            "user_data_dir": "user-data",
            "headless": False,
            "default_timeout_ms": 90_000,
            "slow_mo_ms": 50,
            "locale": "ru-RU",
            "timezone": "Europe/Moscow",
        },
        "login_timeout_seconds": 600,
        "profile": profile.to_config(),
        "search": search_cfg,
        "cover_letter": {
            "pretend_experience": False,
            "telegram": profile.telegram,
            "github": profile.github,
        },
        "rate_limit": {
            "min_seconds": 15,
            "max_seconds": 60,
            "long_break_chance": 0.0,
            "long_break_min_seconds": 0,
            "long_break_max_seconds": 0,
        },
        "applied_log": "applied.json",
        "max_per_run": 30,
        "screenshots_dir": "screenshots",
        "dry_run": False,
        "notifications": {
            "telegram": {
                "token": "",
                "chat_id": "",
            }
        },
    }