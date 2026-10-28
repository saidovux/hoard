"""Application chrome: breadcrumb header, framed navigation and the key-chip shortcut bar."""

from __future__ import annotations

from collections.abc import Iterable

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.events import Resize
from textual.message import Message
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option, OptionDoesNotExist

from ..colors import ACCENT, DANGER, GOOD, MUTED, TEXT, WARNING
from ..navigation import (
    GROUP_TITLES,
    SECTIONS,
    TELEGRAM_SECTIONS,
    Section,
    breadcrumb,
    navigation_parent,
    resolve,
    sections_for,
    workspace,
)

Hint = tuple[str, str]

DEFAULT_HINTS: tuple[Hint, ...] = (("j k", "выбор"), ("⏎", "открыть"), ("[ ]", "раздел"))
GLOBAL_HINTS: tuple[Hint, ...] = ((":", "команды"), ("?", "помощь"), ("q", "выход"))


class HoardHeader(Static):
    """One quiet line: the product mark, location and daemon availability."""

    def __init__(self, section: str = "home") -> None:
        super().__init__(id="hoard-header")
        self.section = section
        self._status: dict | None = None
        self._error: str | None = None
        self._shown = False
        self._detail = ""

    def set_detail(self, value: str) -> None:
        self._detail = str(value or "")
        if self._shown:
            self._paint()

    def show_status(self, status: dict | None, error: str | None = None) -> None:
        self._status, self._error, self._shown = status, error, True
        self._paint()

    def on_resize(self, event: Resize) -> None:
        if self._shown:
            self._paint()

    def _indicator(self) -> Text:
        if self._error:
            return Text.assemble(("● ", f"bold {ACCENT}"), ("служба недоступна", ACCENT))
        if self._status is None:
            return Text("○ подключение…", style=MUTED)
        return Text.assemble(("● ", GOOD), ("служба доступна", TEXT))

    def _paint(self) -> None:
        left = Text(no_wrap=True, overflow="ellipsis")
        left.append("hoard", style=f"bold {ACCENT}")
        crumbs = breadcrumb(self.section)
        for position, crumb in enumerate(crumbs):
            last = position == len(crumbs) - 1 and not self._detail
            left.append("  ›  ", style=MUTED)
            left.append(crumb.label, style=f"bold {TEXT}" if last else MUTED)
        if self._detail:
            left.append("  ›  ", style=MUTED)
            left.append(self._detail, style=f"bold {TEXT}")
        right = self._indicator()
        gap = max(2, self.content_size.width - left.cell_len - right.cell_len)
        left.append(" " * gap)
        left.append_text(right)
        self.update(left)


class Sidebar(Static):
    """One framed menu whose contents follow the active navigation level."""

    BACK = "workspace-back"
    INDICATOR_COLORS = {
        "ready": GOOD,
        "connected": GOOD,
        "saved": WARNING,
        "stopped": DANGER,
        "error": DANGER,
        "login_required": DANGER,
        "starting": MUTED,
        "stopping": MUTED,
        "unknown": MUTED,
    }

    class Navigate(Message):
        def __init__(self, section: str) -> None:
            self.section = section
            super().__init__()

    def __init__(self, active: str) -> None:
        super().__init__(id="sidebar", classes="panel")
        self.active = resolve(active)
        self.owner = workspace(self.active)
        self._indicators = {"telegram_web": "unknown", "telegram_login": "unknown"}
        self.border_title = "Telegram" if self.owner == "telegram" else "Разделы"

    def compose(self) -> ComposeResult:
        if self.owner == "telegram":
            with Vertical(id="workspace-navigation"):
                yield OptionList(*self._primary_options(), id="navigation")
                yield OptionList(*self._secondary_options(), id="navigation-secondary")
        else:
            yield OptionList(*self._global_options(), id="navigation")

    def on_mount(self) -> None:
        navigation, index = self._location(self.active)
        navigation.highlighted = index

    def _lists(self) -> tuple[OptionList, ...]:
        return tuple(self.query(OptionList))

    def _location(self, key: str) -> tuple[OptionList, int]:
        key = navigation_parent(key)
        for navigation in self._lists():
            try:
                return navigation, navigation.get_option_index(key)
            except OptionDoesNotExist:
                continue
        navigation = self._lists()[0]
        fallback = "telegram_status" if self.owner else "home"
        return navigation, navigation.get_option_index(fallback)

    def _global_options(self) -> list[Option]:
        options: list[Option] = []
        previous: str | None = None
        for number, section in enumerate(SECTIONS, start=1):
            if section.group != previous:
                title = GROUP_TITLES.get(section.group, "")
                if previous is not None:
                    options.append(
                        Option(
                            Text("\n" + title, style=f"bold {MUTED}"),
                            id=f"group-{section.group}",
                            disabled=True,
                        )
                    )
                previous = section.group
            options.append(Option(self._label(section, number), id=section.key))
        return options

    def _primary_options(self) -> list[Option]:
        return [
            Option(Text("←  Назад в Hoard", style=MUTED), id=self.BACK),
            *(Option(self._label(section, number), id=section.key)
              for number, section in enumerate(TELEGRAM_SECTIONS[:2], start=1)),
        ]

    def _secondary_options(self) -> list[Option]:
        return [
            Option(self._label(section, number), id=section.key)
            for number, section in enumerate(TELEGRAM_SECTIONS[2:], start=3)
        ]

    def _label(self, section: Section, number: int) -> Text:
        """``▎1  Главная``: the digit is the shortcut, the bar marks the open page."""
        active = resolve(section.key) == navigation_parent(self.active)
        label = Text(no_wrap=True, overflow="ellipsis")
        label.append("▎" if active else " ", style=f"bold {ACCENT}")
        label.append(
            str(number) if number < 10 else " ", style=f"bold {ACCENT}" if active else MUTED
        )
        label.append("  ")
        label.append(section.label, style="bold" if active else "")
        indicator = self._indicators.get(section.key)
        if indicator is not None:
            label.append(" " * max(1, 17 - label.cell_len))
            label.append("●", style=self.INDICATOR_COLORS.get(indicator, MUTED))
        return label

    def set_active(self, active: str, *, reset_highlight: bool = False) -> None:
        # Status polling repeatedly renders the current screen.  Rebuilding an unchanged
        # sidebar would reset the user's navigation highlight every poll (Home refreshes
        # every 1.5 seconds).  Only page activation or an explicit reset may move it.
        active = resolve(active)
        changed = active != self.active
        if changed:
            self.active = active
            for number, section in enumerate(sections_for(active), start=1):
                navigation, index = self._location(section.key)
                navigation.replace_option_prompt_at_index(index, self._label(section, number))
        if changed or reset_highlight:
            navigation, index = self._location(active)
            navigation.highlighted = index

    def set_indicators(self, *, web: str | None = None, account: str | None = None) -> None:
        """Repaint only the two status dots, preserving focus and highlights."""
        if self.owner != "telegram":
            return
        changes = {"telegram_web": web, "telegram_login": account}
        for key, value in changes.items():
            if value is None or self._indicators[key] == value:
                continue
            self._indicators[key] = value
            section = next(item for item in TELEGRAM_SECTIONS if item.key == key)
            number = TELEGRAM_SECTIONS.index(section) + 1
            navigation, index = self._location(key)
            navigation.replace_option_prompt_at_index(index, self._label(section, number))

    def move(self, direction: int) -> None:
        lists = self._lists()
        navigation = next(
            (
                item
                for item in lists
                if self.app.focused is item or item in getattr(self.app.focused, "ancestors", [])
            ),
            lists[0],
        )
        navigation.focus()
        index = navigation.highlighted
        if len(lists) > 1 and index is not None:
            at_edge = (
                direction > 0 and index >= navigation.option_count - 1
            ) or (direction < 0 and index <= 0)
            if at_edge:
                target = lists[
                    (lists.index(navigation) + (1 if direction > 0 else -1)) % len(lists)
                ]
                target.focus()
                target.highlighted = 0 if direction > 0 else target.option_count - 1
                return
        if direction > 0:
            navigation.action_cursor_down()
        else:
            navigation.action_cursor_up()

    def choose(self) -> None:
        lists = self._lists()
        navigation = next((item for item in lists if self.app.focused is item), lists[0])
        index = navigation.highlighted
        if index is None:
            return
        key = str(navigation.get_option_at_index(index).id)
        if key == self.BACK or any(section.key == key for section in sections_for(self.active)):
            self.post_message(self.Navigate(key))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.post_message(self.Navigate(str(event.option.id)))


def _hint_text(hints: Iterable[Hint], *, labels: bool = True) -> Text:
    """Each shortcut is a small orange chip followed by what it does."""
    text = Text(no_wrap=True)
    for position, (key, label) in enumerate(hints):
        if position:
            text.append("  ")
        text.append(f" {key} ", style=f"bold reverse {ACCENT}")
        if labels:
            text.append(" " + label, style=MUTED)
    return text


class HoardStatusBar(Static):
    """Shortcuts for the current screen on the left, the global ones on the right."""

    def __init__(self) -> None:
        super().__init__(id="statusbar")
        self._hints: tuple[Hint, ...] = DEFAULT_HINTS
        self._paint()

    def show_hints(self, hints: Iterable[Hint]) -> None:
        hints = tuple(hints)
        if hints != self._hints:
            self._hints = hints
            self._paint()

    def on_resize(self, event: Resize) -> None:
        self._paint()

    def _paint(self) -> None:
        width = self.content_size.width or 80
        tail = _hint_text(GLOBAL_HINTS)
        if tail.cell_len + 4 > width:
            tail = _hint_text(GLOBAL_HINTS, labels=False)
        line = Text(no_wrap=True, overflow="ellipsis")
        budget = width - tail.cell_len - 3
        used = 0
        for position, hint in enumerate(self._hints):
            piece = _hint_text([hint])
            cost = piece.cell_len + (2 if position else 0)
            if used + cost > budget:
                break
            if position:
                line.append("  ")
            line.append_text(piece)
            used += cost
        line.append(" " * max(2, width - line.cell_len - tail.cell_len))
        line.append_text(tail)
        self.update(line)


__all__ = [
    "DEFAULT_HINTS",
    "GLOBAL_HINTS",
    "Hint",
    "HoardHeader",
    "HoardStatusBar",
    "Sidebar",
]
