"""Браузерная автоматизация hh.ru через Playwright.

Соискательский API hh.ru закрыт с 15.12.2025, поэтому действуем как живой
пользователь: persistent context (профиль на диске), один раз ручной логин,
дальше поиск/отклики через UI.
"""
from __future__ import annotations

import logging
import random
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.parse import urlencode

from playwright.sync_api import (
    BrowserContext,
    Locator,
    Page,
    TimeoutError as PWTimeout,
    sync_playwright,
)


log = logging.getLogger("hh_auto.browser")

HH_BASE = "https://hh.ru"
SEARCH_URL = f"{HH_BASE}/search/vacancy"


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
        try:
            yield ctx
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
    """Открывает hh.ru и проверяет, залогинены ли мы."""
    try:
        page.goto(HH_BASE, wait_until="domcontentloaded")
    except PWTimeout:
        return False
    return _check_login_indicators(page)


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
    page.goto(f"{HH_BASE}/account/login", wait_until="domcontentloaded")
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
            except Exception:
                pass
        if _check_login_indicators(page):
            log.info("Логин успешен. Сессия сохранена в %s.", cfg.user_data_dir)
            return
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
    page.goto(vacancy_url, wait_until="domcontentloaded")
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
        ".magritte-button-view___53Slm_7-2-1:has-text('сопроводительное')",
        "[class*=magritte-button-view]:has-text('сопроводительное')",
        "button:has-text('Сопроводительное')",
        "a:has-text('Сопроводительное письмо')",
    ]
    _click_first_visible(page, toggles, timeout_ms=1500)


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


def apply_to_vacancy(
    page: Page,
    cfg: BrowserConfig,
    *,
    vacancy_url: str,
    message: str,
    dry_run: bool = False,
) -> tuple[str, str]:
    """Возвращает (результат, описание). Результаты см. ApplyResult."""
    page.goto(vacancy_url, wait_until="domcontentloaded")

    if already_applied(page):
        return ApplyResult.ALREADY, "уже откликались"

    try:
        if page.get_by_text("Тестовое задание", exact=False).count() > 0:
            return ApplyResult.SKIPPED_TEST, "вакансия с тестовым заданием"
    except Exception:
        pass

    if dry_run:
        return ApplyResult.SENT, "DRY RUN: до клика 'Откликнуться' не дошли"

    _natural_scroll(page)
    log.info("Готовлю сопроводительное письмо...")
    time.sleep(random.uniform(2.0, 5.0))

    respond_clicked = _click_first_visible(
        page,
        [
            "[data-qa=vacancy-response-link-top]",
            "[data-qa=vacancy-response-link-view-top]",
            "a[data-qa^=vacancy-response-link]",
            "button:has-text('Откликнуться')",
            "a:has-text('Откликнуться')",
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
    else:
        time.sleep(random.uniform(0.5, 1.0))

    # Если перекинуло на отдельную страницу/попап с тестовым — пропускаем
    try:
        body_text = page.locator("body").inner_text(timeout=3000).lower()
    except Exception:
        body_text = ""
    if "тестовое задание" in body_text and "обязательно" in body_text:
        return ApplyResult.SKIPPED_TEST, "после клика попросили тестовое задание"

    # Если есть выбор резюме — подтвердить первое доступное (или кнопка "Откликнуться" в модалке)
    _click_first_visible(
        page,
        [
            "[data-qa=vacancy-response-resume-selector] >> nth=0",
            "[data-qa=vacancy-response-submit-popup]",
            "button:has-text('Откликнуться')",
            "button:has-text('Продолжить')",
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
