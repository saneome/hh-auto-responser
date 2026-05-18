"""Профиль кандидата, генерация поисковых запросов и фильтрация вакансий."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .filters import detect_stack, is_negative


CITY_AREA_IDS: dict[str, int] = {
    "москва": 1,
    "санкт-петербург": 2,
    "петербург": 2,
    "спб": 2,
    "россия": 113,
    "рф": 113,
}

FORMAT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "remote": ("удален", "удалён", "remote", "дистанц", "работа из дома"),
    "hybrid": ("гибрид", "hybrid", "частично удал", "смешан"),
    "office": ("офис", "on-site", "onsite", "в офис"),
}

FORMAT_LABELS: dict[str, str] = {
    "remote": "удаленно",
    "hybrid": "гибрид",
    "office": "офис",
}

EXPERIENCE_LABELS: dict[str, str] = {
    "noExperience": "без опыта",
    "between1And3": "1-3 года",
    "between3And6": "3-6 лет",
    "moreThan6": "6+ лет",
}


def _normalize(text: str) -> str:
    return " ".join((text or "").lower().replace("ё", "е").split())


def _unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = value.strip()
        if not item:
            continue
        norm = _normalize(item)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        result.append(item)
    return result


def split_items(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw_items = [str(item) for item in value]
    else:
        raw_items = re.split(r"[\n,;]+", str(value))
    return _unique(raw_items)


def join_items(items: Iterable[str]) -> str:
    return "\n".join(_unique(items))


def _normalize_contact_entry(value: object) -> dict[str, str] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        label = str(
            value.get("label")
            or value.get("name")
            or value.get("platform")
            or value.get("service")
            or ""
        ).strip()
        entry_value = str(
            value.get("value")
            or value.get("url")
            or value.get("link")
            or value.get("handle")
            or ""
        ).strip()
        if not label and not entry_value:
            return None
        return {"label": label or "Контакт", "value": entry_value}
    text = str(value).strip()
    if not text:
        return None
    if ":" in text:
        label, entry_value = text.split(":", 1)
        label = label.strip() or "Контакт"
        entry_value = entry_value.strip()
        if not entry_value:
            return None
        return {"label": label, "value": entry_value}
    return {"label": "Контакт", "value": text}


def parse_extra_contacts(value: object) -> list[dict[str, str]]:
    if not value:
        return []

    items: list[object]
    if isinstance(value, dict):
        items = [{"label": k, "value": v} for k, v in value.items()]
    elif isinstance(value, list):
        items = list(value)
    else:
        items = [value]

    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        entry = _normalize_contact_entry(item)
        if not entry:
            continue
        key = _normalize(entry["label"]) + "|" + _normalize(entry["value"])
        if key in seen:
            continue
        seen.add(key)
        normalized.append(entry)
    return normalized


def _parse_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = int(value)
        return number if number > 0 else None
    text = re.sub(r"[^\d]", "", str(value))
    if not text:
        return None
    number = int(text)
    return number if number > 0 else None


def _city_to_area(city: str) -> int | str | None:
    city_norm = _normalize(city)
    if not city_norm:
        return None
    for key, area in CITY_AREA_IDS.items():
        if key in city_norm:
            return area
    return None


def _extract_section(text: str, label: str) -> str:
    if not text:
        return ""
    pattern = re.compile(
        rf"^{re.escape(label)}:\s*$\n(?P<body>.*?)(?:\n\n[A-ZА-ЯЁ][^\n]*:\s*$|\Z)",
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text)
    if not match:
        return ""
    return match.group("body").strip()


def _contains_any(text: str, keywords: Iterable[str]) -> bool:
    haystack = _normalize(text)
    return any(keyword in haystack for keyword in keywords)


def _salary_numbers(text: str) -> list[int]:
    numbers: list[int] = []
    for raw in re.findall(r"\d[\d\s]*", text or ""):
        digits = re.sub(r"\D", "", raw)
        if digits:
            try:
                numbers.append(int(digits))
            except ValueError:
                continue
    return numbers


def _work_format_terms(work_format: str) -> tuple[str, ...]:
    return FORMAT_KEYWORDS.get(work_format, ())


def _role_from_skills(skills: list[str]) -> str:
    haystack = _normalize(" ".join(skills))
    if any(token in haystack for token in ("python", "django", "fastapi", "flask", "aiohttp")):
        return "Python backend"
    if any(token in haystack for token in ("java", "spring", "kotlin")):
        return "Java backend"
    if any(token in haystack for token in ("rust", "actix", "axum", "tokio")):
        return "Rust backend"
    if any(token in haystack for token in ("react", "next", "redux")):
        return "Frontend developer"
    if any(token in haystack for token in ("vue", "nuxt")):
        return "Frontend developer"
    if any(token in haystack for token in ("go", "golang")):
        return "Go backend"
    return "разработчик"



def _role_domain_for_experience(role: str, _skills: list[str]) -> str:
    """Return a natural phrase for pretend_experience mention based on desired_role."""
    role = (role or "").strip()
    if not role:
        return "на аналогичной позиции"
    return f"как {role}"


def build_search_queries(profile: "CandidateProfile", *, max_queries: int = 12) -> list[str]:
    role = profile.desired_role.strip() or _role_from_skills(profile.hard_skills)
    format_term = FORMAT_LABELS.get(profile.work_format, "")
    queries: list[str] = []

    if profile.city and role:
        queries.append(f"{profile.city} {role}")
    if role:
        queries.append(role)
    if profile.city and profile.hard_skills:
        queries.append(f"{profile.city} {profile.hard_skills[0]}")

    for skill in profile.hard_skills:
        queries.extend(
            [
                f"{skill} {role}".strip(),
                f"{skill} разработчик".strip(),
                f"{skill} developer".strip(),
            ]
        )
        if profile.city:
            queries.append(f"{profile.city} {skill}".strip())

    if format_term and role:
        queries.append(f"{role} {format_term}".strip())
        if profile.hard_skills:
            queries.append(f"{profile.hard_skills[0]} {format_term}".strip())

    if not queries:
        queries.append("разработчик")

    return _unique(queries)[:max_queries]


def search_params_for_work_format(work_format: str) -> tuple[str | None, bool]:
    if work_format == "remote":
        return "remote", True
    return None, False


@dataclass
class VacancyMatch:
    ok: bool
    hard_hits: list[str] = field(default_factory=list)
    soft_hits: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    score: int = 0


@dataclass
class CandidateProfile:
    full_name: str = "Дидоренко Александр Святославович"
    city: str = ""
    hard_skills: list[str] = field(default_factory=list)
    soft_skills: list[str] = field(default_factory=list)
    work_format: str = "remote"
    experience: str = "between1And3"
    desired_salary: int | None = None
    desired_role: str = ""
    telegram: str = ""
    github: str = ""
    extra_contacts: list[dict[str, str]] = field(default_factory=list)
    # Форматы работы: remote / hybrid / office — что требовать и что избегать
    require_work_formats: list[str] = field(default_factory=lambda: ["remote"])
    avoid_work_formats: list[str] = field(default_factory=list)
    # Типы договора: ТК РФ / ГПХ / самозанятость / ИП / ипотека / и т.д.
    require_contract_types: list[str] = field(default_factory=list)
    avoid_contract_types: list[str] = field(default_factory=lambda: ["ТК РФ"])

    @classmethod
    def from_config(cls, cfg: dict | None) -> "CandidateProfile":
        cfg = cfg or {}
        profile_cfg = cfg.get("profile", {}) or {}
        cover_letter_cfg = cfg.get("cover_letter", {}) or {}
        search_cfg = cfg.get("search", {}) or {}

        if not profile_cfg:
            base = default_candidate_profile()
            area = search_cfg.get("area")
            city = {
                1: "Москва",
                2: "Санкт-Петербург",
                113: "Россия",
            }.get(area, "") if isinstance(area, int) else ""
            if city:
                base.city = city
            base.work_format = "remote" if search_cfg.get("remote_work_format", True) or search_cfg.get("schedule") == "remote" else "office"
            if isinstance(search_cfg.get("experience"), list) and search_cfg.get("experience"):
                base.experience = str(search_cfg.get("experience")[0])
            base.telegram = str(cover_letter_cfg.get("telegram", base.telegram))
            base.github = str(cover_letter_cfg.get("github", base.github))
            base.extra_contacts = parse_extra_contacts(profile_cfg.get("extra_contacts"))
            return base

        work_format = str(profile_cfg.get("work_format", "remote")).strip().lower()
        if work_format not in FORMAT_KEYWORDS:
            work_format = "remote"

        experience = str(profile_cfg.get("experience") or "between1And3").strip()
        if experience not in EXPERIENCE_LABELS:
            experience = "between1And3"

        desired_salary = _parse_int(profile_cfg.get("desired_salary"))

        return cls(
            full_name=str(profile_cfg.get("full_name") or "Дидоренко Александр Святославович"),
            city=str(profile_cfg.get("city") or "").strip(),
            hard_skills=split_items(profile_cfg.get("hard_skills")),
            soft_skills=split_items(profile_cfg.get("soft_skills")),
            work_format=work_format,
            experience=experience,
            desired_salary=desired_salary,
            desired_role=str(profile_cfg.get("desired_role") or profile_cfg.get("target_role") or "").strip(),
            telegram=str(profile_cfg.get("telegram") or cover_letter_cfg.get("telegram", "")),
            github=str(profile_cfg.get("github") or cover_letter_cfg.get("github", "")),
            extra_contacts=parse_extra_contacts(profile_cfg.get("extra_contacts")),
            require_work_formats=profile_cfg.get("require_work_formats", ["remote"]),
            avoid_work_formats=profile_cfg.get("avoid_work_formats", []),
            require_contract_types=profile_cfg.get("require_contract_types", []),
            avoid_contract_types=profile_cfg.get("avoid_contract_types", ["ТК РФ"]),
        )

    def to_config(self) -> dict:
        return {
            "full_name": self.full_name,
            "city": self.city,
            "hard_skills": list(self.hard_skills),
            "soft_skills": list(self.soft_skills),
            "work_format": self.work_format,
            "experience": self.experience,
            "desired_salary": self.desired_salary,
            "desired_role": self.desired_role,
            "telegram": self.telegram,
            "github": self.github,
            "extra_contacts": [dict(item) for item in self.extra_contacts],
            "require_work_formats": list(self.require_work_formats),
            "avoid_work_formats": list(self.avoid_work_formats),
            "require_contract_types": list(self.require_contract_types),
            "avoid_contract_types": list(self.avoid_contract_types),
        }

    def area(self) -> int | str | None:
        return _city_to_area(self.city)

    def search_queries(self, *, max_queries: int = 12) -> list[str]:
        return build_search_queries(self, max_queries=max_queries)

    def prompt_summary(self) -> str:
        lines = [
            f"ФИО: {self.full_name}" if self.full_name else "ФИО: не указано",
            f"Город: {self.city}" if self.city else "Город: не указан",
            f"Формат работы: {FORMAT_LABELS.get(self.work_format, self.work_format)}",
            f"Опыт: {EXPERIENCE_LABELS.get(self.experience, self.experience)}",
        ]
        if self.desired_role:
            lines.append(f"Желаемая роль: {self.desired_role}")
        if self.desired_salary:
            lines.append(f"Желаемая ЗП: {self.desired_salary}")
        if self.hard_skills:
            lines.append("Харды: " + ", ".join(self.hard_skills))
        if self.soft_skills:
            lines.append("Софт-скиллы: " + ", ".join(self.soft_skills))
        if self.telegram:
            lines.append(f"Telegram: {self.telegram}")
        if self.github:
            lines.append(f"GitHub: {self.github}")
        if self.extra_contacts:
            lines.append(
                "Доп. контакты: "
                + "; ".join(
                    f"{item.get('label', 'Контакт')}: {item.get('value', '')}"
                    for item in self.extra_contacts
                    if item.get("value")
                )
            )
        return "\n".join(lines)

    def contact_lines(self) -> list[str]:
        lines: list[str] = []
        if self.telegram:
            lines.append(f"Telegram: {self.telegram}")
        if self.github:
            lines.append(f"GitHub: {self.github}")
        for item in self.extra_contacts:
            label = str(item.get("label", "Контакт")).strip() or "Контакт"
            value = str(item.get("value", "")).strip()
            if value:
                lines.append(f"{label}: {value}")
        return lines


def default_candidate_profile() -> CandidateProfile:
    return CandidateProfile(
        full_name="Дидоренко Александр Святославович",
        city="Москва",
        hard_skills=[
            "Python",
            "Django",
            "FastAPI",
            "Flask",
            "Java",
            "Spring",
            "Kotlin",
            "Rust",
            "Actix",
            "Axum",
            "Tokio",
            "React",
            "Vue",
            "HTML",
            "CSS",
            "JavaScript",
            "SCSS",
            "WebSocket",
            "WebRTC",
            "RTMP",
            "HLS",
            "TURN",
        ],
        soft_skills=["командная работа", "ответственность", "коммуникация"],
        work_format="remote",
        experience="between1And3",
        desired_salary=250000,
        desired_role="Python backend разработчик",
        telegram="t.me/alexaneodev",
        github="github.com/saneome",
    )


def match_vacancy(profile: CandidateProfile, vacancy_text: str) -> VacancyMatch:
    haystack = _normalize(vacancy_text)
    hard_hits = [skill for skill in profile.hard_skills if _normalize(skill) in haystack]
    soft_hits = [skill for skill in profile.soft_skills if _normalize(skill) in haystack]
    reasons: list[str] = []

    if profile.hard_skills and not hard_hits:
        # Если detect_stack нашел один из наших стеков - принимаем
        stack = detect_stack(vacancy_text)
        if not any(x in stack for x in ["python", "java", "rust", "react", "vue", "go", "backend", "frontend", "fullstack"]):
            reasons.append("нет совпадений по хард-скиллам")

    salary_ok = True
    if profile.desired_salary:
        salary_section = _extract_section(vacancy_text, "Зарплата")
        if salary_section:
            salary_ok = max(_salary_numbers(salary_section) or [0]) >= profile.desired_salary
    if not salary_ok:
        reasons.append("зарплата ниже ожиданий")

    if profile.city and _normalize(profile.city) in haystack:
        city_bonus = 1
    else:
        city_bonus = 0

    score = len(hard_hits) * 3 + len(soft_hits) + city_bonus
    return VacancyMatch(
        ok=not reasons,
        hard_hits=hard_hits,
        soft_hits=soft_hits,
        reasons=reasons,
        score=score,
    )