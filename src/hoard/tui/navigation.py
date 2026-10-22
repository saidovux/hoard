"""Navigation metadata shared by screens and keyboard actions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Section:
    key: str
    label: str


SECTIONS = (
    Section("overview", "Обзор"),
    Section("telegram", "Telegram"),
    Section("sources", "Источники"),
    Section("activity", "Активность"),
    Section("archive", "Архив"),
    Section("ai", "AI"),
)


def section_index(key: str) -> int:
    return next((index for index, item in enumerate(SECTIONS) if item.key == key), 0)
