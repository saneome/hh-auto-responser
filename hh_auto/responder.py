"""Auto-reply to employer messages in hh.ru negotiations via LLM."""
from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, Error as PWError, Page

from .browser import BrowserClosedError, HH_BASE
from .negotiations import SAVE_PATH, _load_threads, _save_threads, negotiate_lock
from .nim_client import LLMError, get_nim_client
from .profile import CandidateProfile

log = logging.getLogger("hh_auto.responder")

SYSTEM_PROMPT = (
    "Ты кандидат, отвечающий работодателю на hh.ru. "
    "Пиши по-русски, от первого лица, живо и конкретно. "
    "Не выдумывай опыт, компании или должности. "
    "Не делай списки, заголовки или псевдоструктуру. "
    "Верни только готовый текст ответа, 1-3 коротких абзаца."
)

PROMPT_TEMPLATE = """Ты кандидат, отвечающий работодателю на hh.ru. Проанализируй переписку и напиши естественный ответ.

ПРОФИЛЬ КАНДИДАТА:
{profile}

ЧТО КАНДИДАТ ИЩЕТ:
- Работа удалённо (не офис, не гибрид)
- Оформление: договор ГПХ или самозанятый/ИП (не ТК РФ, не штат)
- Оплата: оплачиваемое тестовое задание или paid test task, оплачиваемый испытательный срок
- НЕ соглашайся на неоплачиваемые стажировки, волонтёрство, бесплатное тестовое
- Цель: получить оплачиваемую задачу → показать навыки → получить оффер

ВАКАНСИЯ: {vacancy}
РАБОТОДАТЕЛЬ: {employer}

ПЕРЕПИСКА:
{history}

ПОСЛЕДНЕЕ СООБЩЕНИЕ РАБОТОДАТЕЛЯ:
{last_message}

КОНТЕКСТ:
{context}

КРИТИЧЕСКИЕ ПРАВИЛА:
1. Внимательно прочитай переписку — пойми ситуацию самостоятельно.
2. НЕ ПИШИ "Здравствуйте" или приветствия, если в переписке уже есть твои сообщения — это выглядит тупо, будто ты забыл, что уже общался.
3. Если работодатель задаёт УТОЧНЯЮЩИЙ ТЕХНИЧЕСКИЙ ВОПРОС (как работать с X, опыт с Y, почему Z) — отвечай ПРЯМО и КОРОТКО, без вводных и без напоминания про тестовое. Просто ответь на вопрос в 1-2 предложения.
4. Если работодатель отказывает или предлагает плохие условия — мягко, но настойчиво предложи альтернативу: оплачиваемое тестовое, paid task, испытательный срок с оплатой.
5. Если работодатель заинтересован — дави на следующий шаг: тестовое, собеседование, уточнение деталей.
6. Пиши как живой человек, по делу. Не шаблонно.

Верни только текст ответа."""


@dataclass
class ReplyTask:
    thread_id: str
    vacancy_id: str
    employer: str
    vacancy: str
    history: list[dict[str, Any]]
    last_message: str
    generated_text: str = ""
    sent: bool = False
    test_task: bool = False


# Keywords that indicate employer offered a test task
_TEST_TASK_KEYWORDS = [
    "тестовое задание", "тестовое", "test task",
    "тз", "тестовик", "тест", "испытательное",
    "практическое задание", "техническое задание",
    "домашнее задание", "домашка", "кейс",
    "практика", "задание на проверку",
]


def _is_test_task_offer(text: str) -> bool:
    """True if employer message offers a test task."""
    if not text:
        return False
    low = text.lower()
    return any(kw in low for kw in _TEST_TASK_KEYWORDS)


def _build_history(messages: list[dict[str, Any]]) -> str:
    lines = []
    for m in messages:
        who = "Работодатель" if m["sender"] == "employer" else "Кандидат"
        lines.append(f"{who}: {m['text']}")
    return "\n".join(lines)


def _fallback_reply(employer: str, vacancy: str) -> str:
    return (
        "Здравствуйте! Спасибо за интерес к моей кандидатуре. "
        "Рассматриваю удалённую работу по договору ГПХ или как самозанятый. "
        "Готов выполнить оплачиваемое тестовое задание — это лучший способ оценить мои навыки. "
        "Когда можем обсудить детали?"
    )


def _generate_reply(task: ReplyTask, profile: CandidateProfile) -> str:
    """Generate reply via LLM with full conversation context."""
    history_text = _build_history(task.history)
    profile_text = profile.prompt_summary()

    candidate_msgs = [m for m in task.history if m.get("sender") == "candidate"]
    if candidate_msgs:
        context = (
            f"Это продолжение переписки. Ты уже написал {len(candidate_msgs)} сообщений. "
            "НЕ здоровайся снова — просто ответь на вопрос или продолжи диалог естественно."
        )
    else:
        context = "Это ПЕРВОЕ твоё сообщение в переписке — можно поздороваться."

    prompt = PROMPT_TEMPLATE.format(
        profile=profile_text,
        vacancy=task.vacancy,
        employer=task.employer,
        history=history_text,
        last_message=task.last_message,
        context=context,
    )

    client = get_nim_client()
    for attempt in range(1, 4):
        try:
            text = client.chat_completion(
                system_prompt=SYSTEM_PROMPT,
                user_prompt=prompt,
            )
            cleaned = text.strip()
            if cleaned:
                return cleaned
            log.warning("LLM returned empty text for %s (attempt %d)", task.thread_id, attempt)
        except LLMError as exc:
            err_str = str(exc).lower()
            log.warning("LLM error for %s (attempt %d): %s", task.thread_id, attempt, exc)
            if "429" in err_str or "too many requests" in err_str:
                wait = 15 * attempt
                log.info("Rate limited, waiting %d seconds...", wait)
                time.sleep(wait)
                continue
        if attempt < 3:
            time.sleep(4 * attempt)

    log.warning("All LLM attempts failed for %s — using fallback", task.thread_id)
    return _fallback_reply(task.employer, task.vacancy)


def _send_reply(page: Page, thread_id: str, text: str, timeout_ms: int = 30_000) -> bool:
    """Navigate to negotiations list, open chat overlay, and submit reply inside iframe."""
    from .negotiations import NEGOTIATIONS_URL

    # 1. Open negotiations list page
    try:
        page.goto(NEGOTIATIONS_URL, wait_until="domcontentloaded", timeout=120_000)
    except PWError as exc:
        log.warning("Failed to open negotiations list for %s: %s", thread_id, exc)
        return False
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PWError:
        pass  # networkidle timeout is common, continue

    # 2. Find the negotiation row for this vacancy
    item = None
    selectors = [
        f"[data-qa='negotiations-item']:has(a[href*='vacancy/{thread_id}'])",
        f"[data-qa='negotiations-item']:has([href*='vacancy/{thread_id}'])",
    ]
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if el.count() > 0:
                item = el
                break
        except PWError:
            continue
    if item is None:
        # Fallback: scan all items
        for candidate in page.locator("[data-qa='negotiations-item']").all():
            try:
                href = candidate.locator("a").first.get_attribute("href") or ""
                if thread_id in href:
                    item = candidate
                    break
            except PWError:
                continue
    if item is None:
        log.warning("Negotiation item not found for vacancy %s", thread_id)
        return False

    # 3. Click open_chat button
    open_btn = item.locator("[data-qa='open_chat']").first
    if open_btn.count() == 0:
        log.warning("open_chat button not found for vacancy %s", thread_id)
        return False
    try:
        open_btn.click()
        page.wait_for_timeout(4000)
    except PWError as exc:
        log.warning("Failed to click open_chat for %s: %s", thread_id, exc)
        return False

    # 4. Switch to chat iframe
    iframe_elem = page.locator("iframe.chatik-integration-iframe").first
    if iframe_elem.count() == 0:
        log.warning("Chat iframe not found for vacancy %s", thread_id)
        return False
    try:
        handle = iframe_elem.element_handle()
        chat_frame = handle.content_frame()
        if chat_frame is None:
            log.warning("Could not access chat iframe for vacancy %s", thread_id)
            return False
    except PWError as exc:
        log.warning("Failed to access chat iframe for %s: %s", thread_id, exc)
        return False

    # 5. Find input inside iframe
    input_selectors = [
        "textarea[placeholder]",
        "textarea",
        "[contenteditable='true']",
        "input[type='text']",
    ]
    inp = None
    for sel in input_selectors:
        try:
            el = chat_frame.locator(sel).first
            el.wait_for(state="visible", timeout=5000)
            inp = el
            break
        except PWError:
            continue
    if inp is None:
        log.warning("No input field found in chat iframe for thread %s", thread_id)
        return False

    # 6. Type and send (type triggers oninput/change events, unlike fill)
    try:
        inp.fill("")
        inp.type(text, delay=random.randint(15, 35))
        page.wait_for_timeout(800)  # allow JS to enable button after typing
        btn_selectors = [
            "[data-qa='chatik-do-send-message']",
            "[data-qa^='chatik-do-send']",
            "button[type='submit']",
            "[data-qa='send-message-button']",
            "button:has-text('Отправить')",
            "button:has-text('Send')",
        ]
        btn = None
        for sel in btn_selectors:
            try:
                b = chat_frame.locator(sel).first
                b.wait_for(state="visible", timeout=3000)
                btn = b
                break
            except PWError:
                continue

        if btn is None:
            log.warning("No send button found in chat iframe for thread %s", thread_id)
            return False

        btn.click()
        page.wait_for_timeout(1500)
        return True
    except PWError as exc:
        log.warning("Failed to send reply to %s: %s", thread_id, exc)
        return False
    finally:
        # Close chat overlay
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
        except PWError:
            pass


def prepare_replies(profile: CandidateProfile) -> list[ReplyTask]:
    """Read negotiations.json, generate replies for all unanswered threads via LLM."""
    with negotiate_lock():
        threads = _load_threads()
    log.info("Loaded %d threads from negotiations.json", len(threads))
    unanswered = [tid for tid, t in threads.items() if t.get("unanswered")]
    log.info("Found %d unanswered threads: %s", len(unanswered), unanswered)

    tasks: list[ReplyTask] = []
    for tid in unanswered:
        t = threads[tid]
        messages = t.get("messages", [])
        last_employer_msg = ""
        for m in reversed(messages):
            if m.get("sender") == "employer":
                last_employer_msg = m.get("text", "")
                break

        task = ReplyTask(
            thread_id=tid,
            vacancy_id=t.get("vacancy_id", tid),
            employer=t.get("employer", "?"),
            vacancy=t.get("vacancy", "?"),
            history=messages,
            last_message=last_employer_msg,
            test_task=_is_test_task_offer(last_employer_msg),
        )
        log.info("Generating reply for %s (%s)...", tid, task.employer)
        reply_text = _generate_reply(task, profile)
        if reply_text:
            task.generated_text = reply_text
            tasks.append(task)
            log.info("Generated reply for %s (%s): %d chars", tid, task.employer, len(reply_text))
        else:
            log.warning("Empty reply for %s", tid)
        time.sleep(5)  # rate-limit between LLM calls
    log.info("Prepared %d replies total", len(tasks))
    return tasks


def send_prepared_replies(tasks: list[ReplyTask], context: BrowserContext,
                          *, dry_run: bool = False) -> list[ReplyTask]:
    """Use Playwright to submit generated replies."""
    if not tasks:
        log.info("No tasks to send")
        return tasks

    log.info("Sending %d replies via Playwright...", len(tasks))
    page = context.new_page()
    try:
        for task in tasks:
            log.info("Processing thread %s (%s)...", task.thread_id, task.employer)
            if dry_run:
                log.info("[DRY-RUN] Would reply to %s:\n%s", task.thread_id, task.generated_text)
                task.sent = True
                continue

            try:
                ok = _send_reply(page, task.thread_id, task.generated_text)
                task.sent = ok
                if ok:
                    log.info("Sent reply to %s", task.thread_id)
                else:
                    log.warning("Failed to send reply to %s", task.thread_id)
            except PWError as exc:
                if _is_disconnected(exc):
                    raise BrowserClosedError("browser closed while sending replies")
                log.warning("Error sending reply to %s: %s", task.thread_id, exc)
                task.sent = False
    finally:
        try:
            page.close()
        except PWError:
            pass

    with negotiate_lock():
        threads = _load_threads()
        for task in tasks:
            if task.sent and task.thread_id in threads:
                threads[task.thread_id]["unanswered"] = False
                threads[task.thread_id]["status"] = "answered"
                threads[task.thread_id]["messages"].append({
                    "sender": "candidate",
                    "text": task.generated_text,
                    "ts": int(time.time()),
                })
        _save_threads(threads)
    log.info("Saved replies to negotiations.json")
    return tasks


"""
Unified chat manager: scrape negotiations, generate LLM replies,
and optionally send them — all in a single browser page.
"""

def run_chat_manager(context: BrowserContext, profile: CandidateProfile, cfg,
                     *, dry_run: bool = False, auto_reply: bool = False,
                     login_timeout: int = 600) -> list[ReplyTask]:
    """One-stop: scrape → generate → send, without closing/opening pages."""
    from .negotiations import fetch_all_negotiations
    from .browser import ensure_logged_in

    page = context.pages[0] if context.pages else None
    if page is None:
        try:
            page = context.new_page()
        except PWError as exc:
            log.warning("run_chat_manager: could not create page: %s", exc)
            if _is_disconnected(exc):
                raise BrowserClosedError("browser disconnected before new_page")
            raise

    try:
        ensure_logged_in(context, cfg, login_timeout)

        # 1. Scrape
        log.info("[chat] scraping negotiations...")
        fetch_all_negotiations(page, dry_run=dry_run)

        # 2. Generate replies
        tasks = prepare_replies(profile)
        if not tasks:
            log.info("[chat] no unanswered threads")
            return []

        log.info("[chat] generated %d replies", len(tasks))
        if not auto_reply:
            for t in tasks:
                log.info("Reply for %s (%s):\n%s", t.thread_id, t.employer, t.generated_text)
            return tasks

        # 3. Send replies on the same page
        log.info("[chat] sending %d replies...", len(tasks))
        for task in tasks:
            if dry_run:
                log.info("[DRY-RUN] Would reply to %s:\n%s", task.thread_id, task.generated_text)
                task.sent = True
                continue

            try:
                ok = _send_reply(page, task.thread_id, task.generated_text)
                task.sent = ok
                if ok:
                    log.info("Sent reply to %s", task.thread_id)
                else:
                    log.warning("Failed to send reply to %s", task.thread_id)
            except PWError as exc:
                if _is_disconnected(exc):
                    raise BrowserClosedError("browser closed while sending replies")
                log.warning("Error sending reply to %s: %s", task.thread_id, exc)
                task.sent = False

        # 4. Update JSON
        with negotiate_lock():
            threads = _load_threads()
            for task in tasks:
                if task.sent and task.thread_id in threads:
                    threads[task.thread_id]["unanswered"] = False
                    threads[task.thread_id]["status"] = "answered"
                    threads[task.thread_id]["messages"].append({
                        "sender": "candidate",
                        "text": task.generated_text,
                        "ts": int(time.time()),
                    })
            _save_threads(threads)
        log.info("[chat] updated negotiations.json")
        return tasks

    except BrowserClosedError:
        raise
    except PWError as exc:
        if _is_disconnected(exc):
            raise BrowserClosedError("browser disconnected")
        log.error("Error in chat manager: %s", exc)
        raise
    finally:
        try:
            page.close()
        except PWError:
            pass


def run_responder(context: BrowserContext, profile: CandidateProfile,
                  *, dry_run: bool = False,
                  auto_reply: bool = False) -> list[ReplyTask]:
    """High-level: generate replies via LLM, optionally send them."""
    log.info("Starting responder: auto_reply=%s dry_run=%s", auto_reply, dry_run)
    tasks = prepare_replies(profile)
    if not tasks:
        log.info("No unanswered threads found")
        return tasks

    log.info("Prepared %d replies", len(tasks))
    if auto_reply:
        log.info("Auto-reply enabled — sending %d replies...", len(tasks))
        return send_prepared_replies(tasks, context, dry_run=dry_run)

    log.info("Generated %d replies (auto_reply disabled — manual review needed)", len(tasks))
    for t in tasks:
        log.info("Reply for %s (%s):\n%s", t.thread_id, t.employer, t.generated_text)
    return tasks


def _is_disconnected(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(
        t in msg
        for t in (
            "browser has been closed",
            "browser closed",
            "disconnected",
            "target closed",
            "context destroyed",
            "page closed",
            "browser context has been",
            "err_aborted",
            "aborted",
        )
    )
