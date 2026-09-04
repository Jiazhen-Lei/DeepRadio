"""User-selected presentation settings for MainAgent replies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

Level = str
Language = str

LEVELS: Tuple[Level, ...] = ("beginner", "practitioner", "expert")
LANGUAGES: Tuple[Language, ...] = ("en", "cn")

STYLE_GUIDE: Dict[Level, str] = {
    "beginner": (
        "Use short sentences and minimal jargon. Briefly explain any necessary "
        "radio term and state the next action clearly."
    ),
    "practitioner": (
        "Use standard GNU Radio, DSP, and RF terminology. Concisely explain "
        "important parameters, decisions, and evidence."
    ),
    "expert": (
        "Use compact technical language. Focus on parameters, constraints, "
        "tradeoffs, failure modes, and verification results; omit tutorials."
    ),
}

LANGUAGE_GUIDE: Dict[Language, str] = {
    "en": "Write every user-facing response in English.",
    "cn": "所有面向用户的回复必须使用简体中文。",
}


@dataclass
class UserProfile:
    """Fixed UI-selected style and language; neither changes task behavior."""

    level: Level = "practitioner"
    language: Language = "en"

    def __post_init__(self) -> None:
        if self.level not in LEVELS:
            self.level = "practitioner"
        if self.language not in LANGUAGES:
            self.language = "en"

    def configure(self, level: Level, language: Language) -> "UserProfile":
        if level in LEVELS:
            self.level = level
        if language in LANGUAGES:
            self.language = language
        return self

    def style_prompt(self) -> str:
        return (
            f"{STYLE_GUIDE[self.level]} {LANGUAGE_GUIDE[self.language]} "
            "These settings affect wording only. Do not change Workflow, tools, "
            "evidence requirements, confirmation points, or safety behavior."
        )

    def text(self, english: str, chinese: str) -> str:
        return chinese if self.language == "cn" else english
