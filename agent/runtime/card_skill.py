"""The built-in skill that teaches the interactive card format, listed only where cards are shown."""

from __future__ import annotations

import os
from importlib.resources import files


CARD_SKILL_NAME = "interactive-cards"
CARD_SKILL_BODY = files("agent.runtime").joinpath("interactive_cards.md").read_text(encoding="utf-8").rstrip("\n")
CARD_SKILL_DESCRIPTION = (
    "桌面端可交互卡片：讲解概念、流程、对比或带参数的计算，而一个能动手试的小展示明显比文字清楚时，"
    "或用户要求卡片、交互演示时，先读取再写。普通回答不需要。"
)


def card_skill_listed() -> bool:
    """Only the desktop runs cards, and it names itself when it starts a backend.

    The catalog is applied per conversation, so a conversation keeps the entry it
    started with when another client continues it.
    """
    return os.getenv("ASTRA_UI_SURFACE", "").strip().lower() == "gui"


def card_skill_content() -> str:
    return f"---\nname: {CARD_SKILL_NAME}\ndescription: {CARD_SKILL_DESCRIPTION}\n---\n\n{CARD_SKILL_BODY}\n"


def card_skill_metadata() -> dict:
    return {
        "name": CARD_SKILL_NAME,
        "description": CARD_SKILL_DESCRIPTION,
        "files": 1,
        "category": "builtin",
        "origin": "builtin",
    }
