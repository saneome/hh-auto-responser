"""Генерация сопроводительного письма через NIM с локальным fallback."""
from __future__ import annotations

import logging
import re
from textwrap import dedent

from .filters import StackMatch
from .nim_client import NimClientError, get_nim_client


log = logging.getLogger("hh_auto.cover_letter")


SYSTEM_PROMPT = dedent(
    """
    Ты пишешь сопроводительные письма для отклика на hh.ru от лица Дидоренко Александра Святославовича.

    Подтверждённый профиль по проекту:
    - Python backend: Django, FastAPI, Flask
    - Java backend: Spring, Kotlin
    - Rust backend: Actix, Axum, Tokio
    - Frontend: React, Vue, HTML, CSS, JavaScript, SCSS
    - Real-time и media: WebSocket, WebRTC, RTMP, HLS, TURN/coTURN
    - Проекты: мессенджер на Django + WebSocket + WebRTC; стриминговая платформа на FastAPI + WebSocket + RTMP + HLS; платформа для хакатонов на Rust + WebSocket + Vue
    - Соревновательный опыт: 10 место из 138 команд на Wink AI Challenge; 26 место из 400 команд на IT Purple Hack

    Правила:
    - Пиши по-русски, от первого лица, живо и конкретно.
    - Используй только подтверждённые навыки и факты из этого профиля и текста вакансии.
    - Не выдумывай компании, должности, коммерческий опыт или достижения.
    - Подстраивай письмо под требования страницы: знания, обязанности, soft skills, стек, формат работы.
    - Не делай списки, заголовки, псевдоструктуру или пояснения к ответу.
    - Верни только готовый текст письма, 3-5 коротких абзацев.
    """
).strip()


# Описание трёх pet-проектов. Подбираются те, что попадают в стек вакансии.
PROJECTS = {
    "messenger": {
        "tags": {"python", "websocket", "webrtc"},
        "text": (
            "— Мессенджер на Django с WebSockets и WebRTC (coTURN — локальный open-source TURN-сервер для обеспечения звонков через мобильную связь). "
            "Фронтенд на ванильном HTML/CSS/JS."
        ),
    },
    "streaming": {
        "tags": {"python", "websocket", "streaming", "react"},
        "text": (
            "— Стриминговая платформа на FastAPI с WebSocket, RTMP и HLS. "
            "Фронтенд на React."
        ),
    },
    "hackathon_platform": {
        "tags": {"rust", "websocket", "vue"},
        "text": (
            "— Платформа для организации хакатонов: бэкенд на Rust с WebSocket, фронт на Vue + SCSS."
        ),
    },
}


HACKATHONS_BLOCK = (
    "Из соревновательного: 10 место из 138 команд на Wink AI Challenge "
    "(делал бэкенд сервиса, который из большого сценария собирает таблицу "
    "сущностей/экспонатов, и интеграцию с NER-моделью); 26 место из 400 команд "
    "на IT Purple Hack (кейс Ингосстраха — бэкенд + помощь фронтендеру)."
)


def _select_projects(stack: StackMatch) -> list[str]:
    """Выбирает релевантные проекты, всегда оставляя минимум один."""
    detected = stack.detected
    chosen: list[str] = []
    for key, proj in PROJECTS.items():
        if proj["tags"] & detected:
            chosen.append(proj["text"])
    if not chosen:
        # На всякий случай — если детект не сработал, шлём все три коротко
        chosen = [p["text"] for p in PROJECTS.values()]
    return chosen


def _opening(stack: StackMatch) -> str:
    primary = stack.detected & {"python", "java", "rust", "react", "vue", "go"}
    if not primary:
        return "Здравствуйте! Заинтересовала ваша вакансия — хотел бы предложить кандидатуру."
    pretty = {
        "python": "Python",
        "java": "Java",
        "rust": "Rust",
        "react": "React",
        "vue": "Vue",
        "go": "Go",
    }
    techs = ", ".join(pretty[t] for t in primary if t in pretty)
    return (
        f"Здравствуйте! Увидел вакансию — стек ({techs}) совпадает с тем, "
        f"что я использую в своих проектах, поэтому хочу откликнуться."
    )


def _build_local_cover_letter(
    stack: StackMatch,
    *,
    telegram: str,
    github: str,
    pretend_experience: bool = False,
) -> str:
    parts: list[str] = []
    parts.append(_opening(stack))
    parts.append("")
    parts.append("Что делал сам — pet-проекты:")
    parts.extend(_select_projects(stack))
    parts.append("")
    parts.append(HACKATHONS_BLOCK)
    if pretend_experience:
        parts.append("")
        parts.append(
            "По опыту коммерческой разработки — порядка 1–2 лет в части backend-задач "
            "(подробности готов обсудить при звонке)."
        )
    parts.append("")
    parts.append(f"Связь: {telegram}, код — {github}.")
    parts.append("Готов созвониться и показать код подробнее. Спасибо за внимание!")
    return "\n".join(parts).strip()


def _truncate_text(text: str, limit: int = 12_000) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[Текст страницы обрезан]"


def _build_user_prompt(
    stack: StackMatch,
    *,
    vacancy_title: str,
    employer: str,
    vacancy_url: str,
    vacancy_text: str,
    telegram: str,
    github: str,
    pretend_experience: bool,
) -> str:
    lines: list[str] = []
    lines.append("Это контекст страницы вакансии hh.ru.")
    if vacancy_title:
        lines.append(f"Название: {vacancy_title}")
    if employer:
        lines.append(f"Компания: {employer}")
    if vacancy_url:
        lines.append(f"Ссылка: {vacancy_url}")
    if stack.detected:
        lines.append("Автоматически найденный стек по странице: " + ", ".join(sorted(stack.detected)))
    lines.append("")
    lines.append("Текст страницы с требованиями, обязанностями, soft skills и условиями:")
    lines.append(_truncate_text(vacancy_text or ""))
    lines.append("")
    lines.append("Задача: напиши готовое сопроводительное письмо для отклика на hh.ru.")
    lines.append("Сделай текст живым и конкретным, без заголовка, без списков и без шаблонных фраз.")
    lines.append(
        "Если в вакансии перечислены требования к знаниям или soft skills, естественно отрази, "
        "как мой опыт это закрывает."
    )
    lines.append("Не выдумывай факты, компании и коммерческий опыт.")
    if pretend_experience:
        lines.append(
            "Можно один раз аккуратно упомянуть нейтральную фразу: 'порядка 1–2 лет в backend-задачах'."
        )
    else:
        lines.append("Коммерческий опыт не упоминай.")
    lines.append(f"В конце обязательно добавь контакты: Telegram — {telegram}; GitHub — {github}.")
    return "\n".join(lines).strip()


def _cleanup_generated_text(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:text|markdown)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = re.sub(r"^(сопроводительное письмо|письмо)\s*:\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def build_cover_letter(
    stack: StackMatch,
    *,
    telegram: str,
    github: str,
    pretend_experience: bool = False,
    vacancy_title: str = "",
    employer: str = "",
    vacancy_url: str = "",
    vacancy_text: str = "",
) -> str:
    nim_client = get_nim_client()
    if nim_client is None:
        return _build_local_cover_letter(
            stack,
            telegram=telegram,
            github=github,
            pretend_experience=pretend_experience,
        )

    user_prompt = _build_user_prompt(
        stack,
        vacancy_title=vacancy_title,
        employer=employer,
        vacancy_url=vacancy_url,
        vacancy_text=vacancy_text,
        telegram=telegram,
        github=github,
        pretend_experience=pretend_experience,
    )

    try:
        generated = nim_client.chat_completion(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )
    except NimClientError as exc:
        log.warning("NIM недоступен, использую локальный шаблон: %s", exc)
        return _build_local_cover_letter(
            stack,
            telegram=telegram,
            github=github,
            pretend_experience=pretend_experience,
        )

    cleaned = _cleanup_generated_text(generated)
    if not cleaned:
        log.warning("NIM вернул пустой текст, использую локальный шаблон.")
        return _build_local_cover_letter(
            stack,
            telegram=telegram,
            github=github,
            pretend_experience=pretend_experience,
        )
    return cleaned
