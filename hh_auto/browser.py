"""Браузерная автоматизация hh.ru через Playwright.

Соискательский API hh.ru закрыт с 15.12.2025, поэтому действуем как живой
пользователь: persistent context (профиль на диске), один раз ручной логин,
дальше поиск/отклики через UI.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.parse import urlencode

from playwright.sync_api import (
    BrowserContext,
    Error as PWError,
    Locator,
    Page,
    TimeoutError as PWTimeout,
    sync_playwright,
)

from .filters import StackMatch, detect_stack
from .nim_client import LLMError, get_nim_client


log = logging.getLogger("hh_auto.browser")

HH_BASE = "https://hh.ru"
SEARCH_URL = f"{HH_BASE}/search/vacancy"


class BrowserClosedError(RuntimeError):
    """Raised when the user closes the browser window during automation."""


def _is_browser_disconnected(exc: PWError) -> bool:
    msg = str(exc).lower()
    return any(
        token in msg
        for token in (
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


@dataclass
class BrowserConfig:
    user_data_dir: str = "user-data"
    headless: bool = False
    default_timeout_ms: int = 20_000
    slow_mo_ms: int = 50
    locale: str = "ru-RU"
    timezone: str = "Europe/Moscow"
    screenshots_dir: str = "screenshots"


class ApplyResult:
    SENT = "sent"
    ALREADY = "already"
    SKIPPED_TEST = "skipped_test"
    SKIPPED_OTHER = "skipped_other"
    ERROR = "error"


# ---------------------------- Контекст ----------------------------


@contextmanager
def open_browser(cfg: BrowserConfig) -> Iterator[BrowserContext]:
    Path(cfg.user_data_dir).mkdir(parents=True, exist_ok=True)
    Path(cfg.screenshots_dir).mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser_type = pw.chromium
        ctx = browser_type.launch_persistent_context(
            user_data_dir=cfg.user_data_dir,
            headless=cfg.headless,
            slow_mo=cfg.slow_mo_ms,
            locale=cfg.locale,
            timezone_id=cfg.timezone,
            viewport={"width": 1366, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx.set_default_timeout(cfg.default_timeout_ms)
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        # Блокируем баннеры и метрики — они не нужны боту и иногда падают с BadRequest
        def _block_route(route, _):
            route.abort()
        ctx.route("**/*banner*", _block_route)
        ctx.route("**/*metric*", _block_route)
        ctx.route("**/*ads*", _block_route)
        try:
            yield ctx
        except PWError as e:
            if _is_browser_disconnected(e):
                raise BrowserClosedError(str(e)) from e
            raise
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def screenshot(page: Page, cfg: BrowserConfig, name: str) -> str:
    p = Path(cfg.screenshots_dir) / f"{int(time.time())}_{name}.png"
    try:
        page.screenshot(path=str(p), full_page=True)
        log.info("Скриншот: %s", p)
    except Exception as e:
        log.warning("Не удалось сохранить скриншот: %s", e)
    return str(p)


# ---------------------------- Логин ----------------------------


def _check_login_indicators(page: Page) -> bool:
    """Проверяет текущую страницу на признаки авторизации (без навигации).
    Использует evaluate для широкого поиска по DOM — надежнее цепочки локаторов.
    """
    js = """
    () => {
        const url = location.href;
        const bodyText = document.body ? document.body.innerText.toLowerCase() : '';
        // 1) URL попал в зону личного кабинета
        if (url.includes('/applicant') || url.includes('/negotiations')) return 'url_applicant';
        // 2) Есть ссылка на /applicant/resumes ("Мои резюме")
        const resumeLink = document.querySelector('a[href*="/applicant/resumes"], a[href*="/applicant/negotiations"], a[href*="/me"]');
        if (resumeLink) return 'link_applicant';
        // 3) Кнопка / текст "Выйти" в шапке или меню
        const logout = document.querySelector('[data-qa="mainmenu_logout"], a[href*="logout"], a[href*="exit"]');
        if (logout) return 'logout_btn';
        // 4) Текстовый fallback: на странице есть "Выйти" и нет "Войти"
        if (bodyText.includes('выйти') && !bodyText.includes('войти с паролем')) return 'text_logout';
        // 5) Классический data-qa меню
        const menu = document.querySelector('[data-qa="mainmenu_applicantProfile"], [data-qa="mainmenu_myResumes"], [data-qa="mainmenu_applicantNegotiations"]');
        if (menu) return 'dataqa_menu';
        return '';
    }
    """
    try:
        reason = page.evaluate(js)
    except Exception:
        reason = ""
    if reason:
        log.debug("Login detected by: %s", reason)
        return True
    return False


def is_logged_in(page: Page) -> bool:
    """Открывает /applicant/negotiations и проверяет, залогинены ли мы."""
    try:
        page.goto(f"{HH_BASE}/applicant/negotiations", wait_until="domcontentloaded")
    except PWError as e:
        if _is_browser_disconnected(e):
            raise BrowserClosedError("Браузер закрыт при проверке авторизации") from e
        return False
    except PWTimeout:
        return False
    # Если нас редиректнуло на страницу логина — точно не залогинены
    url = page.url.lower()
    if "/account/login" in url or "/login" in url:
        return False

    # Ищем явные признаки авторизации (кнопка "Выйти" или меню соискателя).
    # Просто проверять URL недостаточно — hh.ru может показать страницу
    # переписок и незалогиненному пользователю с предложением войти.
    js = """
    () => {
        const logout = document.querySelector('[data-qa="mainmenu_logout"], a[href*="logout"], a[href*="exit"]');
        if (logout) return true;
        const menu = document.querySelector('[data-qa="mainmenu_applicantProfile"], [data-qa="mainmenu_myResumes"], [data-qa="mainmenu_applicantNegotiations"]');
        if (menu) return true;
        const bodyText = document.body ? document.body.innerText.toLowerCase() : '';
        if (bodyText.includes('выйти') && !bodyText.includes('войти с паролем')) return true;
        return false;
    }
    """
    try:
        return page.evaluate(js)
    except Exception:
        return False


def ensure_logged_in(ctx: BrowserContext, cfg: BrowserConfig, login_timeout_seconds: int) -> None:
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    if is_logged_in(page):
        log.info("Уже авторизован.")
        return
    if cfg.headless:
        raise RuntimeError(
            "Не авторизованы, а браузер запущен в headless. Поставьте browser.headless: false "
            "в config.yaml, залогиньтесь руками, потом можно вернуть headless: true."
        )
    log.warning(
        "Нужна ручная авторизация. Войдите в свой аккаунт hh.ru в открытом браузере "
        "(включая капчу/SMS, если попросит). Жду до %d секунд.",
        login_timeout_seconds,
    )
    try:
        page.goto(f"{HH_BASE}/account/login", wait_until="domcontentloaded")
    except PWError as e:
        if _is_browser_disconnected(e):
            raise BrowserClosedError("Браузер закрыт на странице логина") from e
        raise
    deadline = time.time() + login_timeout_seconds
    checks = 0
    while time.time() < deadline:
        time.sleep(3)
        checks += 1
        # Диагностика: раз в ~15 сек показываем текущий URL
        if checks % 5 == 0:
            try:
                current_url = page.url
                log.info("Ожидание логина... Текущий URL: %s", current_url)
            except PWError as e:
                if _is_browser_disconnected(e):
                    raise BrowserClosedError("Браузер закрыт во время ожидания логина") from e
                pass
            except Exception:
                pass
        try:
            if _check_login_indicators(page):
                log.info("Логин успешен. Сессия сохранена в %s.", cfg.user_data_dir)
                return
        except PWError as e:
            if _is_browser_disconnected(e):
                raise BrowserClosedError("Браузер закрыт во время ожидания логина") from e
            pass
    # Таймаут — сделаем скриншот чтобы понять что видел скрипт
    screenshot(page, cfg, "login_timeout")
    raise RuntimeError(
        "Время ожидания логина истекло. Скрипт не нашел признаков авторизации. "
        "Проверьте скриншот в screenshots/ и убедитесь, что вы залогинены."
    )


# ---------------------------- Поиск ----------------------------


def build_search_url(
    text: str,
    *,
    area,
    experience,
    schedule,
    remote_work_format: bool,
    page_idx: int,
) -> str:
    params: list[tuple[str, str]] = [
        ("text", text),
        ("page", str(page_idx)),
        ("order_by", "publication_time"),
    ]
    if area is not None:
        params.append(("area", str(area)))
    if schedule:
        params.append(("schedule", schedule))
    for e in experience or []:
        params.append(("experience", e))
    if remote_work_format:
        params.append(("work_format", "REMOTE"))
    return f"{SEARCH_URL}?{urlencode(params)}"


def iter_search_results(
    page: Page,
    *,
    text: str,
    area,
    experience,
    schedule,
    remote_work_format: bool,
    max_pages: int,
) -> Iterator[dict]:
    for page_idx in range(max_pages):
        url = build_search_url(
            text,
            area=area,
            experience=experience,
            schedule=schedule,
            remote_work_format=remote_work_format,
            page_idx=page_idx,
        )
        log.info("Поиск: %s", url)
        try:
            page.goto(url, wait_until="domcontentloaded")
        except PWError as e:
            if _is_browser_disconnected(e):
                raise BrowserClosedError("Браузер закрыт во время поиска") from e
            log.warning("Ошибка открытия страницы поиска: %s", e)
            break
        except PWTimeout:
            log.warning("Таймаут открытия страницы поиска")
            break
        try:
            page.wait_for_selector(
                "a[data-qa=serp-item__title], [data-qa=vacancy-serp__results]",
                timeout=10_000,
            )
        except PWTimeout:
            log.info("Не дождались выдачи на странице %d", page_idx)
            break
        items = _extract_search_items(page)
        if not items:
            log.info("Пусто на странице %d, выходим.", page_idx)
            break
        for item in items:
            yield item
        time.sleep(random.uniform(1.0, 2.5))


_EXTRACT_JS = r"""
() => {
  const out = [];
  const links = document.querySelectorAll('a[data-qa=serp-item__title]');
  const seen = new Set();
  links.forEach(a => {
    const href = a.href || '';
    if (!href || seen.has(href)) return;
    seen.add(href);
    let id = '';
    try {
      const u = new URL(href);
      const m = u.pathname.match(/\/vacancy\/(\d+)/);
      if (m) id = m[1];
    } catch (e) {}
    const name = (a.innerText || '').trim();
    let card = a.closest('[data-qa=vacancy-serp__vacancy], [data-qa=serp-item], article') || a.parentElement;
    let employer = '';
    if (card) {
      const emp = card.querySelector('[data-qa=vacancy-serp__vacancy-employer], [data-qa=vacancy-serp__vacancy-employer-text]');
      if (emp) employer = (emp.innerText || '').trim();
    }
    out.push({id: id, url: href.split('?')[0], name: name, employer: employer});
  });
  return out;
}
"""


def _extract_search_items(page: Page) -> list[dict]:
    try:
        return page.evaluate(_EXTRACT_JS) or []
    except Exception as e:
        log.warning("Ошибка парсинга выдачи: %s", e)
        return []


# ---------------------------- Страница вакансии ----------------------------


def get_vacancy_page_text(page: Page, vacancy_url: str) -> str:
    try:
        page.goto(vacancy_url, wait_until="domcontentloaded")
    except PWError as e:
        if _is_browser_disconnected(e):
            raise BrowserClosedError("Браузер закрыт при переходе на вакансию") from e
        raise
    try:
        page.wait_for_selector(
            "[data-qa=vacancy-description], [data-qa=vacancy-title]",
            timeout=10_000,
        )
    except PWTimeout:
        pass
    parts: list[str] = []

    def add_section(label: str, selectors: list[str]) -> None:
        collected: list[str] = []
        for sel in selectors:
            try:
                for loc in page.locator(sel).all():
                    txt = (loc.inner_text(timeout=2000) or "").strip()
                    if txt:
                        collected.append(txt)
            except Exception:
                continue
        if not collected:
            return
        unique: list[str] = []
        seen: set[str] = set()
        for txt in collected:
            norm = " ".join(txt.split())
            if not norm or norm in seen:
                continue
            seen.add(norm)
            unique.append(txt)
        if unique:
            parts.append(f"{label}:\n" + "\n".join(unique))

    add_section("Название вакансии", ["[data-qa=vacancy-title]"])
    add_section("Компания", ["[data-qa=common-employer-view-title]", "[data-qa=vacancy-company-name]"])
    add_section("Зарплата", ["[data-qa=vacancy-salary]"])
    add_section("Опыт", ["[data-qa=vacancy-experience]"])
    add_section("Формат работы", ["[data-qa=vacancy-view-employment-mode]"])
    add_section("Ключевые навыки", ["[data-qa=skills-element]"])
    add_section("Описание вакансии", ["[data-qa=vacancy-description]"])

    try:
        main_text = page.locator("main").inner_text(timeout=5000).strip()
    except Exception:
        main_text = ""
    if main_text:
        main_text = main_text[:12_000].strip()
        parts.append("Полный текст страницы:\n" + main_text)

    return "\n\n".join(parts)


def already_applied(page: Page) -> bool:
    try:
        if page.get_by_text("Вы откликнулись", exact=False).count() > 0:
            return True
    except Exception:
        pass
    return False


# ---------------------------- Отклик ----------------------------


def _click_first_visible(page: Page, selectors: list[str], *, timeout_ms: int = 5000) -> bool:
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if loc.count() == 0:
                continue
            loc.wait_for(state="visible", timeout=timeout_ms)
            loc.click()
            return True
        except PWTimeout:
            continue
        except Exception as e:
            log.debug("click %s failed: %s", sel, e)
            continue
    return False


def _find_letter_textarea(page: Page):
    selectors = [
        "[data-qa=vacancy-response-popup-form-letter-input]",
        "textarea[data-qa*=letter]",
        "textarea[name=letter]",
        ".magritte-textarea-position-container___cEpbv_3-3-18 textarea",
        ".magritte-textarea-position-container___cEpbv_3-3-14 textarea",
        "[class*=magritte-textarea-position-container] textarea",
        "div[role=dialog] textarea",
        "form textarea",
    ]
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:
            continue
    return None


def _try_show_letter_field(page: Page) -> None:
    toggles = [
        "[data-qa=vacancy-response-letter-toggle]",
        "[class*=horizontal-actions-container] button:has-text('сопроводительное')",
        "[class*=horizontal-actions-container] button:has-text('Сопроводительное')",
        "button:has-text('Добавить сопроводительное')",
        "button:has-text('добавить сопроводительное')",
        "[class*=button-view]:has-text('сопроводительное')",
        "[class*=button-view]:has-text('Сопроводительное')",
        "button:has-text('Сопроводительное')",
        "button:has-text('сопроводительное')",
        "a:has-text('Сопроводительное письмо')",
    ]
    _click_first_visible(page, toggles, timeout_ms=5000)


def _human_type(loc: Locator, text: str) -> None:
    """Печатаем текст с человекоподобной скоростью."""
    try:
        loc.click()
    except Exception:
        pass
    try:
        loc.fill("")
    except Exception:
        pass
    # Не делаем прямо посимвольно с реальной скоростью — слишком долго.
    # Делим текст на куски и вставляем с микропаузами.
    chunks = [text[i:i + 40] for i in range(0, len(text), 40)]
    for ch in chunks:
        try:
            loc.type(ch, delay=random.randint(15, 45))
        except Exception:
            loc.fill(text)
            return
        time.sleep(random.uniform(0.05, 0.2))


def _natural_scroll(page: Page, *, steps: int = 4) -> None:
    """Немного прокручиваем страницу, чтобы имитировать чтение вакансии."""
    try:
        page.locator("body").hover(timeout=1000)
    except Exception:
        pass

    for _ in range(steps):
        delta = random.randint(240, 840)
        if random.random() < 0.25:
            delta = -random.randint(100, 220)
        try:
            page.mouse.wheel(0, delta)
        except Exception:
            try:
                page.evaluate(
                    "(dy) => window.scrollBy({ top: dy, left: 0, behavior: 'auto' })",
                    delta,
                )
            except Exception:
                pass
        time.sleep(random.uniform(0.25, 0.8))


def _wait_for_modal(page: Page, timeout_ms: int = 10_000) -> bool:
    """Ждём появления модалки/диалога отклика."""
    modal_selectors = [
        "[data-qa=vacancy-response-popup]",
        "[data-qa=bloko-modal]",
        "div[role=dialog]",
        "[class*=modal]:visible",
        "[class*=popup]:visible",
    ]
    for sel in modal_selectors:
        try:
            page.locator(sel).first.wait_for(state="visible", timeout=timeout_ms)
            log.debug("Модалка найдена: %s", sel)
            return True
        except PWTimeout:
            continue
        except Exception:
            continue
    return False


def _wait_for_element(page: Page, selectors: list[str], timeout_ms: int = 8_000):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=timeout_ms)
            return loc
        except PWTimeout:
            continue
        except Exception:
            continue
    return None


def _llm_pick_resume_index(resume_items: list[dict], vacancy_name: str) -> int | None:
    """Спрашиваем LLM какое резюме подходит под вакансию."""
    client = get_nim_client()
    if client is None:
        return None

    prompt = (
        "Вакансия: " + (vacancy_name or "неизвестная") + "\n"
        "Доступные резюме (номер — название):\n"
    )
    for item in resume_items:
        prompt += f"{item['idx']}: {item['text']}\n"
    prompt += (
        "\nКакое резюме лучше всего подходит к этой вакансии? "
        "Ответь ТОЛЬКО номером одного резюме, без текста, без пояснений."
    )
    try:
        reply = client.chat_completion(
            system_prompt="Ты рекрутер. Выбираешь наиболее подходящее резюме для вакансии по названию.",
            user_prompt=prompt,
        )
        for token in re.findall(r"\b(\d+)\b", reply):
            idx = int(token)
            if 0 <= idx < len(resume_items):
                log.debug("LLM выбрал резюме #%d (ответ: %r)", idx, reply[:100])
                return idx
        log.debug("LLM ответил непонятно: %r", reply[:200])
    except LLMError as exc:
        log.debug("LLM не ответил на выбор резюме: %s", exc)
    except Exception as exc:
        log.debug("LLM unexpected error: %s", exc)
    return None


def _select_best_resume(
    page: Page,
    vacancy_stack: StackMatch | None,
    vacancy_name: str = "",
) -> str | None:
    """Открывает выпадающий список резюме, парсит названия,
    выбирает резюме с наибольшим пересечением стека с vacancy_stack
    или через LLM если keyword-match не уверен.
    Возвращает data-qa или None.
    """
    log.debug("Ищем селектор резюме...")
    modal = page.locator('[data-qa=vacancy-response-popup]').first
    if modal.count() == 0:
        modal = page.locator('div[role=dialog]').first

    opened = False
    if modal.count() > 0:
        sel = modal.locator(
            '[data-qa=vacancy-response-resume-selector],'
            ' [class*=magritte-card][class*=press-enabled]'
        ).first
        if sel.count() > 0 and sel.is_visible():
            log.debug("Селектор резюме найден в модалке, кликаем.")
            try:
                sel.click()
                opened = True
            except Exception as exc:
                log.debug("Клик по селектору в модалке не удался: %s", exc)

    if not opened:
        opened = _click_first_visible(
            page,
            [
                "[data-qa=vacancy-response-resume-selector]",
                "[class*=magritte-card][class*=press-enabled]",
            ],
            timeout_ms=3000,
        )

    if not opened:
        log.debug("Селектор резюме не нашли — на странице, вероятно, нет выбора резюме.")
        return None

    time.sleep(random.uniform(1.5, 2.5))
    log.debug("Парсим выпадающий список резюме...")

    js_items = """
    () => {
        const out = [];
        const items = document.querySelectorAll(
            '[class*="magritte-select-drop-container"] [class*="magritte-content-border-container"]'
        );
        items.forEach((el, idx) => {
            const text = (el.innerText || el.textContent || '').trim().substring(0,200);
            if (text) out.push({idx: idx, text: text});
        });
        if (!out.length) {
            const drop = document.querySelector('[class*="magritte-select-drop-container"]');
            if (drop) {
                const children = drop.querySelectorAll('li, [role="option"], [class*="content"]');
                children.forEach((el, idx) => {
                    const text = (el.innerText || el.textContent || '').trim().substring(0,200);
                    if (text) out.push({idx: idx, text: text});
                });
            }
        }
        return out;
    }
    """
    try:
        items = page.evaluate(js_items)
    except Exception as exc:
        log.debug("Ошибка evaluate при парсинге резюме: %s", exc)
        items = []

    if not items:
        log.debug("Список резюме не распарсился, оставляем дефолт.")
        return None

    log.debug("Доступные резюме: %s", json.dumps(items, ensure_ascii=False))

    # ---- keyword-based scoring ----
    best_idx = None
    best_score = -1
    for item in items:
        stack = detect_stack(item["text"])
        overlap = len(stack.detected & vacancy_stack.detected) if (vacancy_stack and vacancy_stack.detected) else 0
        score = overlap * 2 + (1 if stack.has_primary else 0)
        if score > best_score:
            best_score = score
            best_idx = item["idx"]
        log.debug("Резюме '%s' → стек %s, overlap=%d, score=%d", item["text"], stack.detected, overlap, score)

    if best_idx is None:
        best_idx = 0

    # ---- LLM fallback when keyword score is weak ----
    # Одинаковый низкий score или max_overlap==0 -> спрашиваем LLM
    if best_score <= 1:
        log.info("Keyword match слабый (score=%d), спрашиваем LLM...", best_score)
        llm_idx = _llm_pick_resume_index(items, vacancy_name)
        if llm_idx is not None:
            best_idx = llm_idx
            best_score = -1  # флаг что выбор был LLM
            log.info("LLM выбрал резюме #%d.", best_idx)
        else:
            log.info("LLM не ответил, оставляем keyword-выбор #%d.", best_idx)
    else:
        log.info("Keyword match уверенный (score=%d), выбираем #%d.", best_score, best_idx)

    # Клик по выбранному элементу
    click_js = """
    (idx) => {
        let items = document.querySelectorAll(
            '[class*="magritte-select-drop-container"] [class*="magritte-content-border-container"]'
        );
        if (!items.length) {
            const drop = document.querySelector('[class*="magritte-select-drop-container"]');
            if (drop) items = drop.querySelectorAll('li, [role="option"], [class*="content"]');
        }
        if (items[idx]) {
            items[idx].click();
            return true;
        }
        return false;
    }
    """
    try:
        clicked = page.evaluate(click_js, best_idx)
    except Exception as exc:
        log.warning("Ошибка evaluate при клике по резюме #%d: %s", best_idx, exc)
        clicked = False

    if clicked:
        log.info("Выбрано резюме #%d (score=%d).", best_idx, best_score)
        time.sleep(0.3)
        try:
            page.keyboard.press("Escape")
            log.debug("Нажали Escape для закрытия выпадашки резюме.")
        except Exception:
            pass
    else:
        log.warning("Не удалось кликнуть по резюме #%d.", best_idx)
    return "ok" if clicked else None


def apply_to_vacancy(
    page: Page,
    cfg: BrowserConfig,
    *,
    vacancy_url: str,
    message: str,
    vacancy_stack: StackMatch | None = None,
    vacancy_name: str = "",
    dry_run: bool = False,
) -> tuple[str, str]:
    """Возвращает (результат, описание). Результаты см. ApplyResult."""
    try:
        page.goto(vacancy_url, wait_until="domcontentloaded")
    except PWError as e:
        if _is_browser_disconnected(e):
            raise BrowserClosedError("Браузер закрыт при открытии вакансии") from e
        raise

    if already_applied(page):
        return ApplyResult.ALREADY, "уже откликались"

    if dry_run:
        return ApplyResult.SENT, "DRY RUN: до клика 'Откликнуться' не дошли"

    _natural_scroll(page)
    log.info("Готовлю сопроводительное письмо...")
    time.sleep(random.uniform(2.0, 5.0))

    # Проверка что мы на правильной странице вакансии
    expected_vid = vacancy_url.rstrip("/").split("/")[-1].split("?")[0]
    current_url = page.url
    if expected_vid not in current_url:
        return ApplyResult.SKIPPED_OTHER, "редирект на другую страницу"

    respond_clicked = _click_first_visible(
        page,
        [
            "[data-qa=vacancy-response-link-top]",
            "[data-qa=vacancy-response-link-view-top]",
            "a[data-qa^=vacancy-response-link]",
        ],
        timeout_ms=5000,
    )
    if not respond_clicked:
        screenshot(page, cfg, "no_respond_button")
        return ApplyResult.SKIPPED_OTHER, "кнопка 'Откликнуться' не найдена"

    # Ждём появления модалки — это AJAX/popup, не навигация
    modal_appeared = _wait_for_modal(page, timeout_ms=10_000)
    if not modal_appeared:
        # Может быть редирект на отдельную страницу — проверим
        time.sleep(random.uniform(1.0, 2.0))
        screenshot(page, cfg, "after_respond_click")
        # Если модалка не появилась и на странице нет диалога — скорее всего
        # открылась полная форма отклика с множеством полей. Пропускаем.
        try:
            has_dialog = page.locator("div[role=dialog]").count() > 0
        except Exception:
            has_dialog = False
        if not has_dialog:
            return ApplyResult.SKIPPED_OTHER, "полная форма отклика (не модалка)"
    else:
        time.sleep(random.uniform(0.5, 1.0))

    # NOTE: раньше здесь была проверка "тестовое задание" по тексту модалки,
    # но она ложно срабатывала на описание вакансии внутри модалки.
    # Если модалка содержит тестовое задание — textarea не появится
    # и агент просто не отправит отклик (что видно в логе).

    # Выбираем резюме под вакансию (если есть несколько)
    if vacancy_stack:
        _select_best_resume(page, vacancy_stack, vacancy_name=vacancy_name)
    else:
        # Fallback: клик по первому резюме без анализа
        _click_first_visible(
            page,
            [
                "[data-qa=vacancy-response-resume-selector]",
            ],
            timeout_ms=3000,
        )
    time.sleep(random.uniform(0.5, 1.5))

    # Сначала ищем textarea — оно может быть уже открыто
    textarea = _find_letter_textarea(page)
    if textarea is None:
        # Раскрыть поле сопроводительного, если оно свёрнуто
        _try_show_letter_field(page)
        time.sleep(random.uniform(0.4, 1.2))
        textarea = _find_letter_textarea(page)

    if textarea is None:
        # Fallback через _wait_for_element с таймаутом
        textarea = _wait_for_element(
            page,
            [
                "[data-qa=vacancy-response-popup-form-letter-input]",
                "textarea[data-qa*=letter]",
                "textarea[placeholder*='сопроводительное' i]",
                "textarea[placeholder*='письмо' i]",
                "textarea[name=letter]",
                ".magritte-textarea-position-container___cEpbv_3-3-14 textarea",
                "[class*=magritte-textarea-position-container] textarea",
                "div[role=dialog] textarea",
                "[class*=modal] textarea",
                "form textarea",
            ],
            timeout_ms=8_000,
        )

    if textarea is not None:
        try:
            _human_type(textarea, message)
            log.info("Сопроводительное введено.")
        except Exception as e:
            log.warning("Не получилось ввести сопроводительное: %s", e)
    else:
        log.warning("Поле сопроводительного не найдено — возможно, модалка без письма.")

    # Финальная отправка
    sent = _click_first_visible(
        page,
        [
            "[data-qa=vacancy-response-submit-popup]",
            "[data-qa=vacancy-response-letter-submit]",
            ".magritte-button-view___53Slm_7-2-1:has-text('Откликнуться')",
            "[class*=magritte-button-view]:has-text('Откликнуться')",
            "button[data-qa*=submit]:has-text('Отправить')",
            "button:has-text('Отправить')",
            "button:has-text('Откликнуться')",
            "button:has-text('Сохранить и продолжить')",
        ],
        timeout_ms=5_000,
    )
    if not sent:
        screenshot(page, cfg, "no_submit_button")
        return ApplyResult.SKIPPED_OTHER, "кнопка отправки не найдена"

    # Ждём подтверждение
    time.sleep(random.uniform(1.5, 3.0))
    page.wait_for_load_state("domcontentloaded")
    if already_applied(page):
        return ApplyResult.SENT, "ok"
    try:
        page.reload(wait_until="domcontentloaded")
    except Exception:
        pass
    if already_applied(page):
        return ApplyResult.SENT, "ok"
    return ApplyResult.SENT, "submitted (без явного подтверждения)"
