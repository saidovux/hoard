"""Small declarative registry for Hoard and tool-local navigation.

The outer level remains the four Hoard destinations.  A tool may replace that
menu with its own workspace navigation; Telegram is the first and currently
the only workspace.  Every consumer (sidebar, digits, section cycling,
breadcrumbs and palette) reads the same registry.
"""

from __future__ import annotations

from dataclasses import dataclass

CORE = "core"
TOOLS = "tools"

GROUP_TITLES = {CORE: "", TOOLS: "ИНСТРУМЕНТЫ"}


@dataclass(frozen=True, slots=True)
class Section:
    key: str
    label: str
    group: str = CORE
    #: Workspace key for a nested page.
    parent: str | None = None
    #: Screen opened by a workspace entry in the outer menu.
    target: str | None = None
    #: Extra words matched by the command palette.
    keywords: tuple[str, ...] = ()


# The outer menu stays deliberately small.  ``telegram`` is an entry into the
# Telegram workspace rather than a screen of its own.
SECTIONS = (
    Section(
        "home",
        "Главная",
        CORE,
        keywords=("обзор", "активность", "очередь", "прогресс", "события", "overview"),
    ),
    Section("archive", "Архив", CORE, keywords=("кружки", "профили", "задания", "каталог")),
    Section(
        "telegram",
        "Telegram",
        TOOLS,
        target="telegram_status",
        keywords=("сборщик", "аккаунт", "подключить", "источники", "посты", "sources"),
    ),
    Section("ai", "AI", TOOLS, keywords=("deepseek", "чат", "промпт", "ключ")),
)

TELEGRAM_SECTIONS = (
    Section(
        "telegram_status",
        "Статус",
        TOOLS,
        parent="telegram",
        keywords=("сборщик", "очередь", "активность", "ошибка"),
    ),
    Section(
        "telegram_sources",
        "Источники",
        TOOLS,
        parent="telegram",
        keywords=("публикации", "посты", "проверить", "sources"),
    ),
    Section(
        "telegram_web",
        "Web Viewer",
        TOOLS,
        parent="telegram",
        keywords=("web", "браузер", "каталог"),
    ),
    Section(
        "telegram_login",
        "Вход",
        TOOLS,
        parent="telegram",
        keywords=("аккаунт", "авторизация", "сессия", "подключить"),
    ),
)

WORKSPACES: dict[str, tuple[Section, ...]] = {"telegram": TELEGRAM_SECTIONS}
ALL_SECTIONS = (*SECTIONS, *TELEGRAM_SECTIONS)

# Retired public route keys.  The outer ``telegram`` entry is handled by its
# target so callers using ``open_section('telegram')`` keep working too.
ALIASES = {
    "overview": "home",
    "activity": "home",
    "sources": "telegram_sources",
}


def _find(key: str) -> Section | None:
    return next((item for item in ALL_SECTIONS if item.key == key), None)


def resolve(key: str) -> str:
    """Resolve aliases and workspace entries to a concrete screen key."""
    key = ALIASES.get(key, key)
    section = _find(key)
    return section.target if section is not None and section.target else key


def get_section(key: str) -> Section | None:
    return _find(resolve(key))


def workspace(key: str) -> str | None:
    """Return the owning workspace for a concrete screen, if any."""
    section = get_section(key)
    return section.parent if section is not None else None


def sections_for(key: str) -> tuple[Section, ...]:
    """Sections visible at the navigation level containing ``key``."""
    owner = workspace(key)
    return WORKSPACES[owner] if owner is not None else SECTIONS


def section_index(key: str) -> int:
    resolved = resolve(key)
    entries = sections_for(resolved)
    return next(
        (index for index, item in enumerate(entries) if resolve(item.key) == resolved),
        0,
    )


def breadcrumb(key: str) -> tuple[Section, ...]:
    """The concrete section preceded by its workspace, outermost first."""
    section = get_section(key)
    if section is None:
        return ()
    parent = _find(section.parent) if section.parent else None
    return (parent, section) if parent else (section,)


def palette_sections() -> tuple[Section, ...]:
    """Concrete destinations shown in the command palette."""
    entries: list[Section] = []
    for section in SECTIONS:
        if section.target is None:
            entries.append(section)
        else:
            entries.extend(WORKSPACES.get(section.key, ()))
    return tuple(entries)


__all__ = [
    "ALIASES",
    "ALL_SECTIONS",
    "CORE",
    "GROUP_TITLES",
    "SECTIONS",
    "TELEGRAM_SECTIONS",
    "TOOLS",
    "WORKSPACES",
    "Section",
    "breadcrumb",
    "get_section",
    "palette_sections",
    "resolve",
    "section_index",
    "sections_for",
    "workspace",
]
