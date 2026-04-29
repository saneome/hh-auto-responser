"""Точка входа: `python main.py`."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import yaml

from hh_auto.browser import BrowserConfig, ensure_logged_in, open_browser
from hh_auto.runner import make_runner_config, run
from hh_auto.storage import AppliedLog


def load_config(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        print(f"Не найден {path}. Скопируйте config.example.yaml -> {path} и заполните.")
        sys.exit(1)
    with p.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def make_browser_config(cfg: dict) -> BrowserConfig:
    b = cfg.get("browser", {}) or {}
    return BrowserConfig(
        user_data_dir=b.get("user_data_dir", "user-data"),
        headless=bool(b.get("headless", False)),
        default_timeout_ms=int(b.get("default_timeout_ms", 20_000)),
        slow_mo_ms=int(b.get("slow_mo_ms", 50)),
        locale=b.get("locale", "ru-RU"),
        timezone=b.get("timezone", "Europe/Moscow"),
        screenshots_dir=cfg.get("screenshots_dir", "screenshots"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Авто-отклик на вакансии hh.ru (Playwright)")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Не нажимать 'Откликнуться'")
    parser.add_argument("--login-only", action="store_true", help="Только пройти логин и выйти")
    parser.add_argument("--interactive", action="store_true", help="Открыть браузер и оставить его открытым для ручной работы")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load_config(args.config)
    if args.dry_run:
        cfg["dry_run"] = True

    bcfg = make_browser_config(cfg)
    rc = make_runner_config(cfg)
    login_timeout = int(cfg.get("login_timeout_seconds", 600))
    applied = AppliedLog(cfg.get("applied_log", "applied.json"))

    with open_browser(bcfg) as ctx:
        ensure_logged_in(ctx, bcfg, login_timeout)
        if args.login_only:
            logging.info("Логин выполнен, выходим (как и просили).")
            return 0
        if args.interactive:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://hh.ru", wait_until="domcontentloaded")
            logging.info(
                "Браузер открыт в интерактивном режиме. "
                "Нажмите Enter в этом терминале, чтобы закрыть сессию."
            )
            input()
            return 0
        logging.info("Уже отправлено ранее: %d вакансий", len(applied))
        stats = run(ctx, bcfg, rc, applied)
        logging.info(
            "Готово. Отправлено: %d | Пропущено: %d | Ошибок: %d",
            stats["sent"], stats["skipped"], stats["errors"],
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
