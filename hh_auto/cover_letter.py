"""Генерация сопроводительного письма через NIM с локальным fallback."""
from __future__ import annotations

import logging
import re
from textwrap import dedent

from .filters import StackMatch
from .profile import CandidateProfile, _role_domain_for_experience
from .nim_client import LLMError, get_nim_client


log = logging.getLogger("hh_auto.cover_letter")


STATIC_SYSTEM_PROMPT = dedent(
    """
    Ты — кандидат на вакансию. Пишешь сопроводительное письмо для отклика на hh.ru.

    КРИТИЧЕСКИ ВАЖНО:
    - Пиши ОТ ПЕРВОГО ЛИЦА кандидата: "я работаю с", "мой опыт включает", "я использую".
    - НЕ ПИШИ от лица HR, рекрутера, работодателя или третьего лица.
    - НЕ ПИШИ "Ваше резюме", "Мы рады", "Приглашаем" — это не кандидат.
    - НЕ ПИШИ "я учу/изучаю Python" — это создаёт впечатление, что ты не умеешь. Вместо этого: "я работаю с Python", "мой стек включает Python", "у меня есть опыт с FastAPI и Django".

    Правила:
    - Пиши по-русски, живо и конкретно.
    - Используй только подтверждённые навыки и факты из профиля кандидата и текста вакансии.
    - Не выдумывай компании, должности, коммерческий опыт или достижения.
    - Подстраивай письмо под требования страницы: знания, обязанности, soft skills, стек, формат работы.
    - Город кандидата пиши в правильном падеже: "из Москвы", "из Питера", "из Казани", а не "из Москва".
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
    profile: CandidateProfile | None = None,
    telegram: str,
    github: str,
    pretend_experience: bool = False,
) -> str:
    profile_name = profile.full_name if profile else ""
    if profile_name:
        header = f"Здравствуйте! Меня зовут {profile_name}."
    else:
        header = _opening(stack)
    parts: list[str] = []
    parts.append(header)
    if profile and profile.city:
        parts.append(f"Я из {profile.city} и ищу подходящую роль по профилю вакансии.")
    parts.append("")
    parts.append("Что делал сам — pet-проекты:")
    parts.extend(_select_projects(stack))
    parts.append("")
    parts.append(HACKATHONS_BLOCK)
    if pretend_experience and profile:
        domain = _role_domain_for_experience(profile.desired_role, profile.hard_skills)
        parts.append("")
        parts.append(
            f"По опыту коммерческой разработки — порядка 1–2 лет {domain} "
            "(подробности готов обсудить при звонке)."
        )
    contact_lines = profile.contact_lines() if profile else []
    if not contact_lines:
        contact_lines = [line for line in [f"Telegram: {telegram}" if telegram else "", f"GitHub: {github}" if github else ""] if line]
    if contact_lines:
        parts.append("")
        parts.append("Связь: " + "; ".join(contact_lines))
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
    profile: CandidateProfile,
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
    lines.append("Профиль кандидата:")
    lines.append(profile.prompt_summary())
    lines.append("")
    if profile.contact_lines():
        lines.append("Контакты для связи: " + "; ".join(profile.contact_lines()))
        lines.append("")
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
        domain = _role_domain_for_experience(profile.desired_role, profile.hard_skills)
        lines.append(
            f"Можно один раз аккуратно упомянуть нейтральную фразу: 'порядка 1–2 лет {domain}'."
        )
    else:
        lines.append("Коммерческий опыт не упоминай.")
    contact_lines = profile.contact_lines()
    if contact_lines:
        lines.append("В конце обязательно добавь контакты: " + "; ".join(contact_lines) + ".")
    else:
        lines.append(f"В конце обязательно добавь контакты: Telegram — {telegram}; GitHub — {github}.")
    lines.append("")
    lines.append(
        "ВАЖНО: В конце письма коротко попроси дать оплачиваемое тестовое задание. "
        "Например: 'Буду рад показать навыки на практике — готов к оплачиваемому тестовому заданию'. "
        "Не навязывайся, но покажи готовность доказать себя делом."
    )
    return "\n".join(lines).strip()


def _build_system_prompt(profile: CandidateProfile) -> str:
    return dedent(
        f"""
        {STATIC_SYSTEM_PROMPT}

        Профиль кандидата:
        {profile.prompt_summary()}

        Дополнительные правила:
        - Если вакансия просит конкретные знания, подчёркивай только те из них, что есть в профиле.
        - Если вакансия подходит по формату работы или зарплате, это можно аккуратно отразить.
        - В письме должно ощущаться, что кандидат читает именно эту вакансию, а не пишет шаблон.
        """
    ).strip()


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
    profile: CandidateProfile | None = None,
    telegram: str,
    github: str,
    pretend_experience: bool = False,
    vacancy_title: str = "",
    employer: str = "",
    vacancy_url: str = "",
    vacancy_text: str = "",
) -> str:
    active_profile = profile or CandidateProfile(telegram=telegram, github=github)
    nim_client = get_nim_client()
    if nim_client is None:
        return _build_local_cover_letter(
            stack,
            profile=active_profile,
            telegram=telegram,
            github=github,
            pretend_experience=pretend_experience,
        )

    user_prompt = _build_user_prompt(
        stack,
        profile=active_profile,
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
            system_prompt=_build_system_prompt(active_profile),
            user_prompt=user_prompt,
        )
    except LLMError as exc:
        log.warning("NIM недоступен, использую локальный шаблон: %s", exc)
        return _build_local_cover_letter(
            stack,
            profile=active_profile,
            telegram=telegram,
            github=github,
            pretend_experience=pretend_experience,
        )

    cleaned = _cleanup_generated_text(generated)
    if not cleaned:
        log.warning("NIM вернул пустой текст, использую локальный шаблон.")
        return _build_local_cover_letter(
            stack,
            profile=active_profile,
            telegram=telegram,
            github=github,
            pretend_experience=pretend_experience,
        )
    return cleaned
