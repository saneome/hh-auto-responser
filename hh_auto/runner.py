"""Главный цикл: ищем, фильтруем, откликаемся через Playwright."""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Iterator

from playwright.sync_api import BrowserContext, Page

from .browser import (
    ApplyResult,
    BrowserConfig,
    apply_to_vacancy,
    get_vacancy_page_text,
    iter_search_results,
    screenshot,
)
from .cover_letter import build_cover_letter
from .filters import (
    StackMatch,
    detect_stack,
    is_negative,
)
from .storage import AppliedLog


log = logging.getLogger("hh_auto.runner")


@dataclass
class RunnerConfig:
    queries: list[str]
    area: int | str | None
    experience: list[str]
    schedule: str | None
    remote_work_format: bool
    max_pages: int
    min_seconds: int
    max_seconds: int
    long_break_chance: float
    long_break_min_seconds: int
    long_break_max_seconds: int
    telegram: str
    github: str
    pretend_experience: bool
    max_per_run: int
    dry_run: bool


def make_runner_config(cfg: dict) -> RunnerConfig:
    s = cfg.get("search", {})
    rl = cfg.get("rate_limit", {})
    cl = cfg.get("cover_letter", {})
    return RunnerConfig(
        queries=list(s.get("queries", [])),
        area=s.get("area"),
        experience=list(s.get("experience", ["noExperience", "between1And3"])),
        schedule=s.get("schedule", "remote"),
        remote_work_format=bool(s.get("remote_work_format", True)),
        max_pages=int(s.get("max_pages", 5)),
        min_seconds=int(rl.get("min_seconds", 90)),
        max_seconds=int(rl.get("max_seconds", 540)),
        long_break_chance=float(rl.get("long_break_chance", 0.07)),
        long_break_min_seconds=int(rl.get("long_break_min_seconds", 900)),
        long_break_max_seconds=int(rl.get("long_break_max_seconds", 2700)),
        telegram=cl.get("telegram", ""),
        github=cl.get("github", ""),
        pretend_experience=bool(cl.get("pretend_experience", False)),
        max_per_run=int(cfg.get("max_per_run", 0)),
        dry_run=bool(cfg.get("dry_run", False)),
    )


def _gather_vacancies(page: Page, rc: RunnerConfig) -> list[dict]:
    """Прогоняем все queries, схлопываем дубликаты по id."""
    seen: set[str] = set()
    all_items: list[dict] = []
    for query in rc.queries:
        log.info("Поисковый запрос: %r", query)
        for item in iter_search_results(
            page,
            text=query,
            area=rc.area,
            experience=rc.experience,
            schedule=rc.schedule,
            remote_work_format=rc.remote_work_format,
            max_pages=rc.max_pages,
        ):
            vid = item.get("id") or item.get("url")
            if not vid or vid in seen:
                continue
            seen.add(vid)
            all_items.append(item)
        time.sleep(random.uniform(2.0, 4.0))
    return all_items


def _is_dev_title(name: str) -> bool:
    """Только вакансии на разработчика/программиста."""
    n = name.lower()
    dev_keywords = [
        "разработчик", "developer", "программист", "программирование",
        "backend", "frontend", "фронтенд", "бэкенд", "бекенд",
        "software", "инженер-программист", "веб-разработчик",
        "веб-мастер", "web-разработчик", "fullstack", "фулстак",
    ]
    # Явно исключаем дата-инженерию, аналитику, qa, менеджмент
    blacklist = [
        "data engineer", "data scientist", "data analyst", "аналитик",
        "qa", "тестировщик", "автоматизатор", "devops", "sre",
        "менеджер", "manager", "директор", "lead", "teamlead",
        "архитектор", "scrum", "product owner", "project manager",
        "администратор", "support", "поддержка", "helpdesk",
        "преподаватель", "учитель", "teacher", "ментор", "mentor",
        "преподавание", "образование", "educator", "tutor",
    ]
    for b in blacklist:
        if b in n:
            return False
    for kw in dev_keywords:
        if kw in n:
            return True
    return False


def _has_target_language(name: str, text: str) -> bool:
    """True если в названии или описании есть хотя бы один из наших языков."""
    combined = (name + " " + text).lower()
    keywords = (
        "python", "питон", "пайтон",
        "java", "ява",
        "rust", "раст",
        "react", "реакт",
        "vue", "вью",
        "golang", "go lang", "go разработчик", "go developer",
    )
    return any(k in combined for k in keywords)


def _is_relevant(text: str) -> tuple[bool, StackMatch, str]:
    if is_negative(text):
        return False, StackMatch(), "стоп-слова в описании"
    stack = detect_stack(text)
    if not stack.has_primary:
        return False, stack, "нет нужного стека"
    return True, stack, ""


def _sleep_random(rc: RunnerConfig) -> None:
    if random.random() < rc.long_break_chance:
        delay = random.uniform(rc.long_break_min_seconds, rc.long_break_max_seconds)
        log.info("Длинная пауза: %.1f мин", delay / 60)
    else:
        delay = random.uniform(rc.min_seconds, rc.max_seconds)
        log.info("Пауза: %.1f сек", delay)
    end = time.time() + delay
    while time.time() < end:
        time.sleep(min(end - time.time(), 1.0))


def run(
    ctx: BrowserContext,
    bcfg: BrowserConfig,
    rc: RunnerConfig,
    applied: AppliedLog,
) -> dict:
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    sent = 0
    skipped = 0
    errors = 0
    first_apply = True

    items = _gather_vacancies(page, rc)
    log.info("Найдено уникальных вакансий: %d", len(items))

    for item in items:
        if rc.max_per_run and sent >= rc.max_per_run:
            log.info("Достигнут лимит откликов на запуск (%d).", rc.max_per_run)
            break

        vid = str(item.get("id") or "")
        url = item.get("url") or ""
        name = item.get("name", "")
        employer = item.get("employer", "")
        if not url:
            continue
        if vid and applied.has(vid):
            skipped += 1
            continue
        if not _is_dev_title(name):
            log.debug("skip %s — не вакансия разработчика — %s", vid or url, name)
            skipped += 1
            continue

        # Открываем вакансию, читаем текст для детекта стека
        try:
            text = get_vacancy_page_text(page, url)
        except Exception as e:
            log.warning("Не удалось открыть %s: %s", url, e)
            errors += 1
            continue

        if not _has_target_language(name, text):
            log.debug("skip %s — нет нашего языка в названии/описании — %s", vid or url, name)
            skipped += 1
            continue

        ok, stack, reason = _is_relevant(text)
        if not ok:
            log.debug("skip %s — %s — %s", vid or url, reason, name)
            skipped += 1
            continue

        message = build_cover_letter(
            stack,
            telegram=rc.telegram,
            github=rc.github,
            pretend_experience=rc.pretend_experience,
            vacancy_title=name,
            employer=employer,
            vacancy_url=url,
            vacancy_text=text,
        )

        if not first_apply:
            _sleep_random(rc)
        first_apply = False

        log.info("[%s] %s — %s | стек=%s", vid or "?", employer, name, sorted(stack.detected))

        if rc.dry_run:
            log.info("DRY RUN — письмо:\n%s\n---", message)
            if vid:
                applied.add(vid, name=name, employer=employer, status="dry_run")
            sent += 1
            continue

        try:
            result, info = apply_to_vacancy(
                page, bcfg, vacancy_url=url, message=message, dry_run=False
            )
        except Exception as e:
            log.warning("Исключение при отклике %s: %s", url, e)
            screenshot(page, bcfg, f"exception_{vid or 'x'}")
            if vid:
                applied.add(vid, name=name, employer=employer, status="exception")
            errors += 1
            continue

        if result == ApplyResult.SENT:
            sent += 1
            log.info("✓ Отклик отправлен (%s) — %s", vid or "?", info)
            if vid:
                applied.add(vid, name=name, employer=employer, status="applied")
        elif result == ApplyResult.ALREADY:
            log.info("• Уже откликались (%s)", vid)
            if vid:
                applied.add(vid, name=name, employer=employer, status="already")
            skipped += 1
        elif result in (ApplyResult.SKIPPED_TEST, ApplyResult.SKIPPED_OTHER):
            log.info("→ Пропуск (%s): %s", vid or "?", info)
            if vid:
                applied.add(vid, name=name, employer=employer, status=result)
            skipped += 1
        else:
            errors += 1
            log.warning("Ошибка отклика (%s): %s", vid or "?", info)
            if vid:
                applied.add(vid, name=name, employer=employer, status="error")

    return {"sent": sent, "skipped": skipped, "errors": errors}
