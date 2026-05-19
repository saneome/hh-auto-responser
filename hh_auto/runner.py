"""Главный цикл: ищем, фильтруем, откликаемся через Playwright."""
from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Iterator

from playwright.sync_api import BrowserContext, Error as PWError, Page

from .browser import (
    ApplyResult,
    BrowserClosedError,
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
from .profile import CandidateProfile, build_search_queries, match_vacancy, search_params_for_work_format
from .responder import run_responder
from .storage import AppliedLog, SearchProgress


log = logging.getLogger("hh_auto.runner")


def _is_disconnect(exc: Exception) -> bool:
    if isinstance(exc, BrowserClosedError):
        return True
    if isinstance(exc, PWError):
        msg = str(exc).lower()
        return any(
            t in msg
            for t in (
                "browser has been closed",
                "disconnected",
                "target closed",
                "context destroyed",
                "page closed",
                "browser context has been",
                "err_aborted",
                "aborted",
            )
        )
    return False


@dataclass
class RunnerConfig:
    profile: CandidateProfile
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
    pretend_experience: bool
    max_per_run: int
    dry_run: bool
    responder_chance: float
    responder_auto_reply: bool
    no_post_search_responder: bool = False


def make_runner_config(cfg: dict) -> RunnerConfig:
    profile = CandidateProfile.from_config(cfg)
    s = cfg.get("search", {})
    rl = cfg.get("rate_limit", {})
    cl = cfg.get("cover_letter", {})
    queries = list(s.get("queries", []))
    if not queries:
        queries = build_search_queries(profile)
    schedule, remote_work_format = search_params_for_work_format(profile.work_format)
    experience = list(s.get("experience") or [profile.experience])
    area = s.get("area")
    if area is None:
        area = profile.area()
    return RunnerConfig(
        profile=profile,
        queries=queries,
        area=area,
        experience=experience,
        schedule=s.get("schedule", schedule),
        remote_work_format=bool(s.get("remote_work_format", remote_work_format)),
        max_pages=int(s.get("max_pages", 5)),
        min_seconds=int(rl.get("min_seconds", 15)),
        max_seconds=int(rl.get("max_seconds", 60)),
        long_break_chance=float(rl.get("long_break_chance", 0.0)),
        long_break_min_seconds=int(rl.get("long_break_min_seconds", 0)),
        long_break_max_seconds=int(rl.get("long_break_max_seconds", 0)),
        pretend_experience=bool(cl.get("pretend_experience", False)),
        max_per_run=int(cfg.get("max_per_run", 0)),
        dry_run=bool(cfg.get("dry_run", False)),
        responder_chance=float(cfg.get("responder", {}).get("chance", 0.15)),
        responder_auto_reply=bool(cfg.get("responder", {}).get("auto_reply", False)),
    )


# Технологии, которые мы ищем — поисковые запросы без служебных слов.
STOP_WORDS_RE = re.compile(
    r"\b(?:разработчик|developer|программист|инженер|engineer|специалист|specialist|"
    r"стаж[ёе]р|junior|middle|senior|lead|team lead|tech lead|архитектор|architect)\b",
    re.IGNORECASE,
)


def _extract_tech_keywords(query: str) -> set[str]:
    """Из поискового запроса извлекает ключевые технологии.

    Убираем служебные слова (разработчик, инженер, junior ...).
    Возвращаем set строчных токенов.
    """
    clean = STOP_WORDS_RE.sub("", query)
    words = {w.strip("-+/.,") for w in clean.lower().split() if len(w.strip("-+/.,")) > 1}
    return words


def _vacancy_matches_query(name: str, query: str) -> bool:
    """True если название вакансии содержит хотя бы одну технологию из запроса.

    Из поискового запроса убираются служебные слова (разработчик,
    инженер, junior, senior ...).  Если после чистки остались
    ключевые слова — в названии должно быть хотя бы одно из них.
    Если ничего не осталось (широкий запрос) — пропускаем.
    """
    keywords = _extract_tech_keywords(query)
    if not keywords:
        return True
    n = name.lower()
    return any(k in n for k in keywords)


def _gather_vacancies(page: Page, rc: RunnerConfig) -> list[dict]:
    """Прогоняем все queries, схлопываем дубликаты по id. Запоминаем query."""
    seen: set[str] = set()
    all_items: list[dict] = []
    for query in rc.queries:
        log.info("Поисковый запрос: %r", query)
        try:
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
                item["_query"] = query
                all_items.append(item)
        except Exception as e:
            if _is_disconnect(e):
                raise BrowserClosedError("Браузер закрыт во время поиска") from e
            raise
        time.sleep(random.uniform(1.0, 3.0))
    return all_items


def _is_target_title(name: str, profile: CandidateProfile) -> bool:
    """True if vacancy title matches the user's hard_skills or desired_role."""
    n = name.lower()
    # Blacklist — non-dev roles we explicitly skip regardless of profession
    blacklist = [
        "data engineer", "data scientist", "data analyst", "аналитик",
        "qa", "тестировщик", "автоматизатор", "devops", "sre",
        "менеджер", "manager", "директор", "teamlead",
        "архитектор", "scrum", "product owner", "project manager",
        "администратор", "support", "поддержка", "helpdesk",
        "преподаватель", "учитель", "teacher", "ментор", "mentor",
        "преподавание", "образование", "educator", "tutor",
    ]
    for b in blacklist:
        if b in n:
            return False
    # Check hard_skills
    for skill in profile.hard_skills:
        if skill.lower() in n:
            return True
    # Check desired_role
    if profile.desired_role and profile.desired_role.lower() in n:
        return True
    return False


def _has_target_language(name: str, text: str, hard_skills: list[str]) -> bool:
    """True если в названии или описании есть хотя бы один из наших языков/скиллов."""
    combined = (name + " " + text).lower()
    keywords = (
        "python", "питон", "пайтон",
        "java", "ява",
        "rust", "раст",
        "react", "реакт",
        "vue", "вью",
        "golang", "go lang", "go разработчик", "go developer",
    )
    if any(k in combined for k in keywords):
        return True
    for skill in hard_skills:
        if skill.lower() in combined:
            return True
    return False


def _is_relevant(text: str, hard_skills: list[str]) -> tuple[bool, StackMatch, str]:
    if is_negative(text):
        return False, StackMatch(), "стоп-слова в описании"
    from .filters import build_stack_patterns, detect_stack
    stack = detect_stack(text, build_stack_patterns(hard_skills))
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
    notifier=None,
) -> dict:
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    sent = 0
    skipped = 0
    errors = 0
    first_apply = True

    items = _gather_vacancies(page, rc)
    log.info("Найдено уникальных вакансий: %d", len(items))

    progress = SearchProgress("search_progress.json")
    total_new = 0
    for item in items:
        vid = str(item.get("id") or "")
        url = item.get("url") or ""
        if not url:
            continue
        if applied.has(vid, url=url):
            continue
        if progress.has(vid):
            continue
        total_new += 1

    log.info("Новых вакансий для проверки: %d (из них пропущено по progress: %d)", total_new, len(items) - total_new - (len(applied) if applied else 0))

    try:
        for item in items:
            if rc.max_per_run and sent >= rc.max_per_run:
                log.info("Достигнут лимит откликов на запуск (%d).", rc.max_per_run)
                break

            vid = str(item.get("id") or "")
            url = item.get("url") or ""
            name = item.get("name", "")
            employer = item.get("employer", "")
            query = item.get("_query", "")
            if not url:
                continue
            if applied.has(vid, url=url):
                skipped += 1
                continue
            if progress.has(vid):
                skipped += 1
                continue

            # --- фильтр названия ---
            if not _is_target_title(name, rc.profile):
                log.debug("skip %s — не подходит под профиль — %s", vid or url, name)
                if vid:
                    progress.skip(vid, name=name, employer=employer, query=query, reason="not_target_title")
                skipped += 1
                continue

            if query and not _vacancy_matches_query(name, query):
                log.info("skip %s — не совпадает с запросом %r — %s", vid or url, query, name)
                if vid:
                    progress.skip(vid, name=name, employer=employer, query=query, reason="query_mismatch")
                skipped += 1
                continue

            # --- чтение страницы ---
            try:
                text = get_vacancy_page_text(page, url)
            except Exception as e:
                if _is_disconnect(e):
                    raise BrowserClosedError("Браузер закрыт во время чтения вакансии")
                log.warning("Не удалось открыть %s: %s", url, e)
                errors += 1
                continue

            # --- match vacancy ---
            vacancy_match = match_vacancy(rc.profile, name + " " + text)
            if not vacancy_match.ok:
                log.info(
                    "skip %s — %s — %s",
                    vid or url,
                    ", ".join(vacancy_match.reasons),
                    name,
                )
                if vid:
                    progress.skip(vid, name=name, employer=employer, query=query, reason="match_vacancy:" + ",".join(vacancy_match.reasons))
                skipped += 1
                continue

            # --- relevance check ---
            ok, stack, reason = _is_relevant(text, rc.profile.hard_skills)
            if not ok:
                log.debug("skip %s — %s — %s", vid or url, reason, name)
                if vid:
                    progress.skip(vid, name=name, employer=employer, query=query, reason=reason)
                skipped += 1
                continue

            # --- generate letter ---
            message = build_cover_letter(
                stack,
                profile=rc.profile,
                pretend_experience=rc.pretend_experience,
                telegram=rc.profile.telegram,
                github=rc.profile.github,
                vacancy_title=name,
                employer=employer,
                vacancy_url=url,
                vacancy_text=text,
            )

            if not first_apply:
                _sleep_random(rc)
            first_apply = False

            log.info(
                "[%s] %s — %s | стек=%s | score=%d | hard=%s | soft=%s",
                vid or "?",
                employer,
                name,
                sorted(stack.detected),
                vacancy_match.score,
                ", ".join(vacancy_match.hard_hits) or "-",
                ", ".join(vacancy_match.soft_hits) or "-",
            )

            if rc.dry_run:
                log.info("DRY RUN — письмо:\n%s\n---", message)
                if vid:
                    applied.add(vid, url=url, name=name, employer=employer, status="dry_run")
                sent += 1
                continue

            # --- apply ---
            try:
                result, info = apply_to_vacancy(
                    page, bcfg, vacancy_url=url, message=message, vacancy_stack=stack, vacancy_name=name, dry_run=False
                )
            except Exception as e:
                if _is_disconnect(e):
                    raise BrowserClosedError("Браузер закрыт во время отклика")
                log.warning("Исключение при отклике %s: %s", url, e)
                screenshot(page, bcfg, f"exception_{vid or 'x'}")
                if vid:
                    applied.add(vid, url=url, name=name, employer=employer, status="exception")
                errors += 1
                continue

            if result == ApplyResult.SENT:
                sent += 1
                log.info("✓ Отклик отправлен (%s) — %s", vid or "?", info)
                if vid:
                    applied.add(vid, url=url, name=name, employer=employer, status="applied")
                # Периодически проверяем переписку после отклика
                if random.random() < rc.responder_chance:
                    try:
                        log.info("Проверка переписки после отклика…")
                        run_responder(ctx, rc.profile, dry_run=rc.dry_run, auto_reply=rc.responder_auto_reply)
                    except BrowserClosedError:
                        raise
                    except Exception as e:
                        log.warning("Responder не удался после отклика: %s", e)
            elif result == ApplyResult.ALREADY:
                log.info("• Уже откликались (%s)", vid)
                if vid:
                    applied.add(vid, url=url, name=name, employer=employer, status="already")
                # ALREADY попадает в applied, в progress не нужно
                skipped += 1
            elif result in (ApplyResult.SKIPPED_TEST, ApplyResult.SKIPPED_OTHER):
                log.info("→ Пропуск (%s): %s", vid or "?", info)
                if vid:
                    applied.add(vid, url=url, name=name, employer=employer, status=result)
                    progress.skip(vid, name=name, employer=employer, query=query, reason=result)
                skipped += 1
            else:
                errors += 1
                log.warning("Ошибка отклика (%s): %s", vid or "?", info)
                if vid:
                    applied.add(vid, url=url, name=name, employer=employer, status="error")
                    progress.skip(vid, name=name, employer=employer, query=query, reason="apply_error")
    except BrowserClosedError:
        raise
    except Exception as e:
        if _is_disconnect(e):
            raise BrowserClosedError("Браузер закрыт") from e
        raise

    # После откликов — проверяем переписку, если не отключено
    if not rc.no_post_search_responder:
        try:
            log.info("Проверка переписки в конце батча…")
            run_responder(ctx, rc.profile, dry_run=rc.dry_run, auto_reply=rc.responder_auto_reply)
        except BrowserClosedError:
            raise
        except Exception as e:
            log.warning("Responder не удался в конце батча: %s", e)

    return {"sent": sent, "skipped": skipped, "errors": errors}
