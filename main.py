"""Точка входа: `python main.py`."""
from __future__ import annotations

import argparse
import datetime
import logging
import sys
import time
from pathlib import Path

from hh_auto.app_config import load_config, make_browser_config
from hh_auto.browser import BrowserClosedError, ensure_logged_in, open_browser
from hh_auto.negotiations import run_negotiations
from hh_auto.notifications import load_notifier
from hh_auto.profile import default_candidate_profile
from hh_auto.reports import daily_report, print_report
from hh_auto.responder import run_chat_manager, run_responder
from hh_auto.runner import make_runner_config, run
from hh_auto.storage import AppliedLog


def main() -> int:
    parser = argparse.ArgumentParser(description="Авто-отклик на вакансии hh.ru (Playwright)")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Не нажимать 'Откликнуться'")
    parser.add_argument("--login-only", action="store_true", help="Только пройти логин и выйти")
    parser.add_argument("--interactive", action="store_true", help="Открыть браузер и оставить его открытым для ручной работы")
    parser.add_argument("--gui", action="store_true", help="Открыть Qt GUI вместо CLI")
    parser.add_argument("--check-negotiations", action="store_true", help="Проверить сообщения от работодателей")
    parser.add_argument("--auto-reply", action="store_true", help="Автоответ на сообщения работодателей (требует --check-negotiations)")
    parser.add_argument("--report", action="store_true", help="Ежедневный отчет")
    parser.add_argument("--user-data-dir", type=str, default=None, help="Override browser.user_data_dir из config.yaml")
    parser.add_argument("--applied-log", type=str, default=None, help="Override пути к applied_log")
    parser.add_argument("--loop", action="store_true", help="Бесконечный цикл для responder (требует --check-negotiations)")
    parser.add_argument("--loop-interval", type=int, default=None, help="Секунды между итерациями responder loop (default: 300)")
    parser.add_argument("--no-post-search-responder", action="store_true", help="Не вызывать responder после завершения поиска")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.loop and not args.check_negotiations:
        print("--loop требует --check-negotiations", file=sys.stderr)
        return 1

    if args.report:
        print_report()
        return 0

    # logging setup — always, even in GUI mode, so logs appear in terminal
    Path("logs").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("logs/app.log", encoding="utf-8"),
        ],
    )

    if args.gui:
        try:
            from hh_auto.gui import run_gui
        except ImportError as exc:
            print(
                "GUI mode requires PySide6. Install dependencies with `pip install -r requirements.txt`."
            )
            print(str(exc))
            return 1

        return run_gui(args.config)

    try:
        cfg = load_config(args.config)
    except FileNotFoundError:
        print(f"Не найден {args.config}. Скопируйте config.example.yaml -> {args.config} и заполните.")
        return 1
    if args.dry_run:
        cfg["dry_run"] = True

    bcfg = make_browser_config(cfg, user_data_dir_override=args.user_data_dir)
    rc = make_runner_config(cfg)
    login_timeout = int(cfg.get("login_timeout_seconds", 600))
    applied_log_path = args.applied_log or cfg.get("applied_log", "applied.json")
    applied = AppliedLog(applied_log_path)
    rc.no_post_search_responder = args.no_post_search_responder

    notifier = load_notifier(cfg)

    if args.login_only:
        with open_browser(bcfg) as ctx:
            ensure_logged_in(ctx, bcfg, login_timeout)
        logging.info("Логин выполнен, выходим (как и просили).")
        return 0

    if args.interactive:
        with open_browser(bcfg) as ctx:
            ensure_logged_in(ctx, bcfg, login_timeout)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://hh.ru", wait_until="domcontentloaded")
            logging.info(
                "Браузер открыт в интерактивном режиме. "
                "Нажмите Enter в этом терминале, чтобы закрыть сессию."
            )
            input()
        return 0

    if args.check_negotiations:
        from hh_auto.profile import CandidateProfile
        profile = CandidateProfile.from_config(cfg)
        interval = (
            args.loop_interval
            if args.loop_interval is not None
            else (cfg.get("responder") or {}).get("check_interval_seconds", 300)
        )
        first = True
        report_time_str = (cfg.get("telegram") or {}).get("daily_report_time", "09:00")
        try:
            report_h, report_m = map(int, report_time_str.split(":"))
        except Exception:
            report_h, report_m = 9, 0
        _last_report_date = None
        while True:
            if not first:
                try:
                    logging.info("Responder loop: sleeping %ss", interval)
                    time.sleep(interval)
                except KeyboardInterrupt:
                    logging.info("Responder loop interrupted")
                    break
            first = False

            # Daily report check
            now = datetime.datetime.now()
            if _last_report_date != now.date():
                if now.hour > report_h or (now.hour == report_h and now.minute >= report_m):
                    logging.info("Отправляю ежедневный отчёт...")
                    if notifier:
                        try:
                            notifier.daily_report("")
                        except Exception:
                            pass
                    _last_report_date = now.date()

            # Пересоздаём браузер каждый цикл — Chrome не выдерживает idle
            try:
                with open_browser(bcfg) as ctx:
                    logging.info("Проверяю переписку с работодателями...")
                    if args.auto_reply:
                        logging.info("Запуск chat_manager (scrape + generate + send)...")
                        tasks = run_chat_manager(ctx, profile, bcfg,
                                                 dry_run=args.dry_run, auto_reply=True,
                                                 login_timeout=login_timeout)
                        logging.info("chat_manager завершён: %d задач", len(tasks))
                    else:
                        logging.info("Запуск chat_manager (scrape only)...")
                        tasks = run_chat_manager(ctx, profile, bcfg,
                                                 dry_run=args.dry_run, auto_reply=False,
                                                 login_timeout=login_timeout)
                        logging.info("chat_manager завершён: %d задач", len(tasks))
                    if notifier:
                        for t in tasks:
                            if t.test_task:
                                notifier.test_task(t.employer, t.vacancy, t.last_message[:500])
            except BrowserClosedError:
                logging.warning("Браузер закрыт (возможно, Chrome упал). Пересоздам на следующей итерации.")
            except Exception:
                logging.exception("Ошибка в цикле responder — не убиваю процесс, жду следующей итерации.")

            if not args.loop:
                break
        return 0

    logging.info("Уже отправлено ранее: %d вакансий", len(applied))
    with open_browser(bcfg) as ctx:
        stats = run(ctx, bcfg, rc, applied, notifier=notifier)
    logging.info(
        "Готово. Отправлено: %d | Пропущено: %d | Ошибок: %d",
        stats["sent"], stats["skipped"], stats["errors"],
    )
    # После поиска — проверим переписку и ответим работодателям
    if not rc.no_post_search_responder:
        from hh_auto.profile import CandidateProfile
        profile = CandidateProfile.from_config(cfg)
        try:
            with open_browser(bcfg) as ctx:
                tasks = run_chat_manager(ctx, profile, bcfg,
                                         dry_run=args.dry_run, auto_reply=True,
                                         login_timeout=login_timeout)
                if tasks:
                    logging.info("Ответили на %d сообщений", len([t for t in tasks if t.sent]))
                    if notifier:
                        for t in tasks:
                            if t.test_task:
                                notifier.test_task(t.employer, t.vacancy, t.last_message[:500])
        except BrowserClosedError:
            logging.warning("Браузер закрыт при post-search responder. Пропускаю.")
        except Exception as exc:
            logging.warning("Responder не удался после поиска: %s", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
