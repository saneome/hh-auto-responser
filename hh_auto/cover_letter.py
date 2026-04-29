"""Контекстная генерация сопроводительного письма."""
from __future__ import annotations

from .filters import StackMatch


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


def build_cover_letter(
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
