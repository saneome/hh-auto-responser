"""Определение релевантности вакансии и стека по тексту."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable


# Ключи стека -> регэкспы для поиска в name/description
STACK_PATTERNS: dict[str, list[str]] = {
    "python": [r"\bpython\b", r"\bdjango\b", r"\bfastapi\b", r"\bflask\b", r"\baiohttp\b"],
    "java": [r"\bjava\b(?!\s*script)", r"\bspring\b", r"\bkotlin\b"],
    "rust": [r"\brust\b", r"\bactix\b", r"\baxum\b", r"\btokio\b"],
    "react": [r"\breact\b", r"\bnext\.?js\b", r"\bredux\b"],
    "vue": [r"\bvue\b", r"\bnuxt\b"],
    "go": [r"\bgolang\b", r"\bgo\s*lang\b", r"\bgo\s*разработчик\b", r"\bgo\s*developer\b"],
    "flutter": [r"\bflutter\b", r"\bdart\b", r"\bmobile\b"],
    "websocket": [r"\bweb\s*socket", r"\bws\b"],
    "webrtc": [r"\bwebrtc\b", r"\bcoturn\b", r"\bturn\b"],
    "streaming": [r"\brtmp\b", r"\bhls\b", r"\bстрим", r"\bstreaming\b"],
    "frontend": [r"\bfront[\s-]?end\b", r"\bфронт"],
    "backend": [r"\bback[\s-]?end\b", r"\bбэк", r"\bбэкенд", r"\bбекенд"],
    "fullstack": [r"\bfull[\s-]?stack\b", r"\bфулл[\s-]?стек\b", r"\bфуллстек\b"],
}

# Минимальный список — хотя бы один из этих тегов должен быть в вакансии
PRIMARY_TECH = {"python", "java", "rust", "react", "vue", "go", "flutter", "backend", "frontend", "fullstack", "websocket", "streaming"}

# Стоп-слова — если присутствуют, скорее всего не наш профиль
NEGATIVE_PATTERNS = [
    r"\b1c\b|\b1с\b",
    # Go убран из негатива — теперь это целевой язык
    r"\bphp\b",
    r"\bруководитель\b|\bteam\s*lead\b|\btechlead\b|\bтех\s*лид",
    r"\bsenior\b|\bведущий\b",
    r"опыт\s+от\s+(4|5|6|7|8|9|10)",
    # ML / Data Science — не наш профиль
    r"\bdata\s+scientist\b|\bml\b|\bmachine\s+learning\b|\bdeep\s+learning\b|\bdata\s+engineer\b",
    r"\bаналитик\b|\bmlops\b|\bml-инженер\b|\bdata\s+analyst\b|\bcv\b|\bкомпьютерное\s*зрение\b",
]


@dataclass
class StackMatch:
    detected: set[str] = field(default_factory=set)
    primary: set[str] = field(default_factory=set)

    @property
    def has_primary(self) -> bool:
        primary = self.primary or PRIMARY_TECH
        return bool(self.detected & primary)

    def __bool__(self) -> bool:
        return self.has_primary

    def __contains__(self, item: str) -> bool:
        return item in self.detected


def build_stack_patterns(hard_skills: list[str]) -> dict[str, list[str]]:
    """Build regex patterns from user's hard_skills for universal detection."""
    patterns: dict[str, list[str]] = {}
    for raw in hard_skills:
        skill = raw.strip()
        if not skill:
            continue
        key = skill.lower()
        escaped = re.escape(key)
        # Whole-word / phrase boundary matching that works with any language/symbols
        patterns[key] = [rf"(?:^|(?<=\W)){escaped}(?:(?=\W)|$)"]
    return patterns


def detect_stack(text: str, custom_patterns: dict[str, list[str]] | None = None) -> StackMatch:
    """Returns detected technologies. Uses custom_patterns if provided, otherwise default STACK_PATTERNS."""
    if not text:
        return StackMatch()
    low = text.lower()
    found: set[str] = set()
    patterns = custom_patterns or STACK_PATTERNS
    for key, pats in patterns.items():
        for p in pats:
            if re.search(p, low, re.IGNORECASE):
                found.add(key)
                break
    primary = set(custom_patterns.keys()) if custom_patterns else PRIMARY_TECH
    return StackMatch(detected=found, primary=primary)


def is_negative(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    for p in NEGATIVE_PATTERNS:
        if re.search(p, low, re.IGNORECASE):
            return True
    return False


def vacancy_text(vacancy: dict) -> str:
    """Склеивает name + snippet + description (если есть)."""
    parts: list[str] = []
    if vacancy.get("name"):
        parts.append(vacancy["name"])
    snippet = vacancy.get("snippet") or {}
    if snippet.get("requirement"):
        parts.append(snippet["requirement"])
    if snippet.get("responsibility"):
        parts.append(snippet["responsibility"])
    if vacancy.get("description"):
        parts.append(strip_html(vacancy["description"]))
    if vacancy.get("key_skills"):
        parts.extend(s.get("name", "") for s in vacancy["key_skills"])
    return "\n".join(parts)


_HTML_TAG = re.compile(r"<[^>]+>")


def strip_html(s: str) -> str:
    return _HTML_TAG.sub(" ", s or "")


def is_remote(vacancy: dict) -> bool:
    sched = (vacancy.get("schedule") or {}).get("id")
    if sched == "remote":
        return True
    # Новые поля hh: work_format / employment_form
    for wf in vacancy.get("work_format", []) or []:
        if (wf.get("id") or "").upper() == "REMOTE":
            return True
    return False


def matches_experience(vacancy: dict, allowed: Iterable[str]) -> bool:
    exp = (vacancy.get("experience") or {}).get("id")
    if not exp:
        return True  # не указано — оставим
    return exp in set(allowed)
