"""Navigation registry shared by the sidebar, palette, header and keyboard actions.

Hoard is a small-tool core, not a Telegram client: Telegram and AI are the first *tools*, and
more (downloaders, browser integration) will follow. Adding a tool to the TUI is therefore one
declarative step: append a :class:`Section` here and register its screen in
``HoardApp.SCREENS``. The sidebar, ``[`` / ``]``, the digit shortcuts, the command palette and
the header breadcrumb all read this registry, so nothing else needs to know about the tool.

One page per concern: everything about the daemon's present (what it is doing, the queue, the
latest events) lives on Home, and everything about a tool (its account, its sources) lives on
that tool's page. Pages that used to be separate are kept as :data:`ALIASES` so old keys,
scripts and muscle memory still land in the right place.
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
    #: Key of the tool this page belongs to; rendered indented and as a breadcrumb prefix.
    parent: str | None = None
    #: Extra words the command palette matches besides the label.
    keywords: tuple[str, ...] = ()


# Order is the order of the sidebar, of ``[`` / ``]`` and of the 1-9 shortcuts. Only implemented
# integrations may appear here: unfinished tools stay hidden from the interface.
SECTIONS = (
    Section(
        "home",
        "Главная",
        CORE,
        keywords=("обзор", "активность", "очередь", "прогресс", "события", "статус", "overview"),
    ),
    Section("archive", "Архив", CORE, keywords=("кружки", "профили", "задания", "каталог")),
    Section(
        "telegram",
        "Telegram",
        TOOLS,
        keywords=("сборщик", "аккаунт", "подключить", "источники", "посты", "sources"),
    ),
    Section("ai", "AI", TOOLS, keywords=("deepseek", "чат", "промпт", "ключ")),
)

#: Former pages that were folded into another one.
ALIASES = {"overview": "home", "activity": "home", "sources": "telegram"}


def resolve(key: str) -> str:
    """Map a retired page key to the page that replaced it."""
    return ALIASES.get(key, key)


def section_index(key: str) -> int:
    key = resolve(key)
    return next((index for index, item in enumerate(SECTIONS) if item.key == key), 0)


def get_section(key: str) -> Section | None:
    key = resolve(key)
    return next((item for item in SECTIONS if item.key == key), None)


def breadcrumb(key: str) -> tuple[Section, ...]:
    """The section preceded by its parent tool, outermost first."""
    section = get_section(key)
    if section is None:
        return ()
    parent = get_section(section.parent) if section.parent else None
    return (parent, section) if parent else (section,)
