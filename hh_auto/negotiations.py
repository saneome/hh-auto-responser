"""Scrape hh.ru /applicant/negotiations and save conversation threads."""
from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, Error as PWError, Page, TimeoutError as PWTimeout

from .browser import BrowserClosedError, HH_BASE, screenshot

log = logging.getLogger("hh_auto.negotiations")

NEGOTIATIONS_URL = f"{HH_BASE}/applicant/negotiations"
SAVE_PATH = Path("negotiations.json")
LOCK_PATH = SAVE_PATH.with_suffix(".lock")


@contextmanager
def negotiate_lock():
    if os.name == "posix":
        import fcntl
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOCK_PATH.open("a") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    else:
        yield


@dataclass
class Message:
    sender: str  # "employer" | "candidate"
    text: str
    ts: int = 0


@dataclass
class Thread:
    thread_id: str
    employer: str
    vacancy: str
    vacancy_id: str = ""
    status: str = "new"  # new | viewed | answered | test_task | interview | rejected | declined
    last_ts: int = 0
    messages: list[Message] = field(default_factory=list)
    unanswered: bool = False


def _norm(text: str | None) -> str:
    return (text or "").strip()


def _parse_timestamp(text: str) -> int:
    """Best-effort: return current time if parsing fails."""
    try:
        # hh.ru shows relative timestamps; we just use current time as approximation
        return int(time.time())
    except Exception:
        return int(time.time())


def _extract_vacancy_id_from_url(url: str) -> str:
    """Pull vacancy id from a URL like /vacancy/12345?..."""
    import re
    m = re.search(r"/vacancy/(\d+)", url)
    return m.group(1) if m else ""


def _load_threads(path: Path = SAVE_PATH) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_threads(data: dict[str, dict[str, Any]], path: Path = SAVE_PATH) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


# ── Playwright scraping ──────────────────────────────────────────

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


def list_threads(page: Page, timeout_ms: int = 30_000) -> list[Thread]:
    """Parse the negotiations list page and return thread stubs."""
    threads: list[Thread] = []
    page.goto(NEGOTIATIONS_URL, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PWError:
        log.debug("networkidle timeout on negotiations list, continuing")

    # hh.ru renders negotiations as a list of <a> or div items
    # Try multiple selectors because layout changes periodically
    selectors = [
        ".applicant-negotiations-content a[href*='/vacancy/']",
        ".negotiations-content a[href*='/vacancy/']",
        ".negotiations-list-item",
        ".bloko-link[href*='/vacancy/']",
        "[data-qa='negotiations-item']",
    ]

    items: list[Any] = []
    for sel in selectors:
        try:
            items = page.locator(sel).all()
            if items:
                log.debug("Found %d negotiation items via %s", len(items), sel)
                break
        except PWError:
            continue

    if not items:
        log.warning("No negotiation items found on page")
        return threads

    for item in items:
        try:
            link = item if item.evaluate("el => el.tagName") == "A" else item.locator("a").first
            href = link.get_attribute("href") or ""
            vid = _extract_vacancy_id_from_url(href)
            if not vid:
                continue

            # Employer name
            employer = ""
            for emp_sel in (".title-text--QG3lsfSrjUsWGi2m", "[data-qa='negotiations-item-employer']", ".bloko-text_strong", "h3", "span"):
                try:
                    emp_el = item.locator(emp_sel).first
                    if emp_el.is_visible():
                        employer = _norm(emp_el.inner_text())
                        if employer:
                            break
                except PWError:
                    continue

            # Vacancy name
            vacancy = _norm(link.inner_text())

            # Last message snippet / status indicator
            status = "new"
            snippet = ""
            for snippet_sel in ("[data-qa='negotiations-item-last-message']", ".negotiation-snippet", ".bloko-text"):
                try:
                    snip = item.locator(snippet_sel).first
                    if snip.is_visible():
                        snippet = _norm(snip.inner_text())
                        break
                except PWError:
                    continue

            # Detect status from snippet or badge classes
            snippet_lower = snippet.lower()
            if "тестовое" in snippet_lower or "задание" in snippet_lower:
                status = "test_task"
            elif "собеседован" in snippet_lower or "интервью" in snippet_lower:
                status = "interview"
            elif "отказ" in snippet_lower or "не подходит" in snippet_lower:
                status = "rejected"

            threads.append(Thread(
                thread_id=vid,
                employer=employer or "?",
                vacancy=vacancy,
                vacancy_id=vid,
                status=status,
                last_ts=int(time.time()),
            ))
        except PWError:
            continue

    log.info("Listed %d negotiation threads", len(threads))
    return threads


def open_thread(page: Page, thread: Thread, timeout_ms: int = 30_000) -> Thread:
    """Open a single negotiation thread and parse messages."""
    messages: list[Message] = []
    unanswered = False

    # Find the row for this vacancy and click open_chat
    try:
        item = page.locator(f"[data-qa='negotiations-item']:has(a[href*='vacancy/{thread.vacancy_id}'])").first
        if item.count() == 0:
            item = page.locator(f"[data-qa='negotiations-item']:has([href*='vacancy/{thread.vacancy_id}'])").first
        if item.count() == 0:
            # Fallback: search all items and match by href
            for candidate in page.locator("[data-qa='negotiations-item']").all():
                href = ""
                try:
                    href = candidate.locator("a").first.get_attribute("href") or ""
                except PWError:
                    pass
                if thread.vacancy_id in href:
                    item = candidate
                    break
        if item.count() == 0:
            log.warning("Negotiation item not found for vacancy %s", thread.vacancy_id)
            thread.messages = messages
            return thread

        open_btn = item.locator("[data-qa='open_chat']").first
        if open_btn.count() == 0:
            log.warning("open_chat button not found for vacancy %s", thread.vacancy_id)
            thread.messages = messages
            return thread

        open_btn.click()
        page.wait_for_timeout(4000)

        # Switch to chat iframe
        iframe_elem = page.locator("iframe.chatik-integration-iframe").first
        if iframe_elem.count() == 0:
            log.warning("Chat iframe not found for vacancy %s", thread.vacancy_id)
            thread.messages = messages
            return thread

        handle = iframe_elem.element_handle()
        chat_frame = handle.content_frame()
        if chat_frame is None:
            log.warning("Could not access chat iframe for vacancy %s", thread.vacancy_id)
            thread.messages = messages
            return thread

        # Try to update employer name from chat header inside the iframe
        for emp_sel in (
            "[class*='chat-header'] [class*='title']",
            "[class*='chat-header'] h1",
            "[class*='chat-header'] h2",
            "[data-qa*='employer']",
            "[data-qa*='company']",
            "[class*='employer-name']",
            "[class*='title-text']",
        ):
            try:
                emp_el = chat_frame.locator(emp_sel).first
                if emp_el.count() > 0:
                    text = _norm(emp_el.text_content())
                    if text and text != "?":
                        thread.employer = text
                        break
            except PWError:
                continue

        # Find messages inside the iframe — filter out containers like messages-- / chat-messages--
        raw_els = chat_frame.locator("[class*=message]").all()
        log.debug("Found %d raw message-like elements in chat iframe for %s", len(raw_els), thread.vacancy_id)

        for msg_el in raw_els:
            try:
                cls = msg_el.get_attribute("class") or ""
                # Skip containers and title elements
                if "messages--" in cls or "chat-messages--" in cls or "chat-bubble-message-title" in cls:
                    continue
                # Must be an actual message element (message-- but not messages--)
                if "message--" not in cls:
                    continue
                sender = "candidate" if "message_my" in cls else "employer"
                text = _norm(msg_el.inner_text())
                if len(text) < 3:
                    continue
                messages.append(Message(sender=sender, text=text, ts=int(time.time())))
            except PWError:
                continue

        # Close chat by pressing Escape or clicking outside
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)

    except PWError as exc:
        log.warning("Failed to open thread %s: %s", thread.vacancy_id, exc)

    # Detect unanswered: last message is from employer AND it's not a rejection
    if messages and messages[-1].sender == "employer":
        last_text_lower = messages[-1].text.lower()
        if "отказ" in last_text_lower or "не подходит" in last_text_lower or "отклик не подходит" in last_text_lower:
            unanswered = False
        else:
            unanswered = True

    # Re-detect status from full conversation text
    full_text = "\n".join(m.text for m in messages).lower()
    status = thread.status
    if "отказ" in full_text or "не подходит" in full_text:
        status = "rejected"
    elif "тестовое" in full_text or "задание" in full_text:
        status = "test_task"
    elif "собеседован" in full_text or "интервью" in full_text:
        status = "interview"
    elif not unanswered:
        status = "answered"
    else:
        status = "new"

    thread.messages = messages
    thread.unanswered = unanswered
    thread.status = status
    thread.last_ts = int(time.time())
    return thread


def fetch_all_negotiations(page: Page, *, dry_run: bool = False) -> dict[str, dict[str, Any]]:
    """List + open every thread and return serializable data."""
    with negotiate_lock():
        existing = _load_threads()
        threads = list_threads(page)
        data: dict[str, dict[str, Any]] = {}

        for t in threads:
            if not dry_run:
                try:
                    t = open_thread(page, t)
                except PWError as exc:
                    if _is_disconnected(exc):
                        raise BrowserClosedError("browser closed during thread open")
                    log.warning("Failed to open thread %s: %s", t.vacancy_id, exc)
                    # keep stub from list

            # Merge with existing messages if we failed to open this time
            existing_thread = existing.get(t.thread_id)
            if existing_thread and not t.messages:
                t.messages = [Message(**m) for m in existing_thread.get("messages", [])]
                t.unanswered = existing_thread.get("unanswered", False)
                t.status = existing_thread.get("status", t.status)

            data[t.thread_id] = {
                "employer": t.employer,
                "vacancy": t.vacancy,
                "vacancy_id": t.vacancy_id,
                "status": t.status,
                "last_ts": t.last_ts,
                "messages": [{"sender": m.sender, "text": m.text, "ts": m.ts} for m in t.messages],
                "unanswered": t.unanswered,
            }

        if not dry_run:
            _save_threads(data)
        return data


def run_negotiations(context: BrowserContext, cfg, login_timeout: int = 600, *, dry_run: bool = False, screenshots_dir: str | None = None) -> dict[str, dict[str, Any]]:
    """High-level entry: open page, log in if needed, scrape all threads."""
    from .browser import ensure_logged_in

    try:
        page = context.new_page()
    except PWError as exc:
        log.warning("run_negotiations: could not create page: %s", exc)
        if _is_disconnected(exc):
            raise BrowserClosedError("browser disconnected before new_page")
        raise
    try:
        ensure_logged_in(context, cfg, login_timeout)
        result = fetch_all_negotiations(page, dry_run=dry_run)
        return result
    except BrowserClosedError:
        raise
    except PWError as exc:
        if _is_disconnected(exc):
            raise BrowserClosedError("browser disconnected")
        log.error("Error scraping negotiations: %s", exc)
        raise
    finally:
        try:
            page.close()
        except PWError:
            pass
