"""Application chrome: breadcrumb header, framed navigation and the key-chip shortcut bar."""

from __future__ import annotations

from collections.abc import Iterable

from rich.text import Text
from textual.app import ComposeResult
from textual.events import Resize
from textual.message import Message
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option, OptionDoesNotExist

from ..colors import ACCENT, MUTED, TEXT
from ..navigation import GROUP_TITLES, SECTIONS, Section, breadcrumb, section_index
from ..state import collector_label

Hint = tuple[str, str]

DEFAULT_HINTS: tuple[Hint, ...] = (("j k", "выбор"), ("⏎", "открыть"), ("[ ]", "раздел"))
GLOBAL_HINTS: tuple[Hint, ...] = ((":", "команды"), ("?", "помощь"), ("q", "выход"))


class HoardHeader(Static):
    """One quiet line: the product mark, where you are and whether the collector is alive."""

    def __init__(self, section: str = "home") -> None:
        super().__init__(id="hoard-header")
        self.section = section
        self._status: dict | None = None
        self._error: str | None = None
        self._shown = False

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
        label = collector_label(self._status)
        if label == "работает":
            return Text.assemble(("● ", ACCENT), (label, TEXT))
        return Text.assemble(("○ ", MUTED), (label, MUTED))

    def _paint(self) -> None:
        left = Text(no_wrap=True, overflow="ellipsis")
        left.append("hoard", style=f"bold {ACCENT}")
        crumbs = breadcrumb(self.section)
        for position, crumb in enumerate(crumbs):
            last = position == len(crumbs) - 1
            left.append("  ›  ", style=MUTED)
            left.append(crumb.label, style=f"bold {TEXT}" if last else MUTED)
        right = self._indicator()
        gap = max(2, self.content_size.width - left.cell_len - right.cell_len)
        left.append(" " * gap)
        left.append_text(right)
        self.update(left)


class Sidebar(Static):
    """Grouped navigation in a frame; only implemented sections appear."""

    class Navigate(Message):
        def __init__(self, section: str) -> None:
            self.section = section
            super().__init__()

    def __init__(self, active: str) -> None:
        super().__init__(id="sidebar", classes="panel")
        self.active = active
        self.border_title = "Разделы"

    def compose(self) -> ComposeResult:
        yield OptionList(*self._options(), id="navigation")

    def on_mount(self) -> None:
        navigation = self.query_one(OptionList)
        navigation.highlighted = self._index_of(self.active)

    def _index_of(self, key: str) -> int:
        navigation = self.query_one(OptionList)
        try:
            return navigation.get_option_index(key)
        except OptionDoesNotExist:  # unknown section keys fall back to the first entry
            return navigation.get_option_index(SECTIONS[section_index(key)].key)

    def _options(self) -> list[Option]:
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

    def _label(self, section: Section, number: int) -> Text:
        """``▎1  Главная``: the digit is the shortcut, the bar marks the open page."""
        active = section.key == self.active
        label = Text(no_wrap=True, overflow="ellipsis")
        label.append("▎" if active else " ", style=f"bold {ACCENT}")
        label.append("  " if section.parent else "")
        label.append(
            str(number) if number < 10 else " ", style=f"bold {ACCENT}" if active else MUTED
        )
        label.append("  ")
        label.append(section.label, style="bold" if active else "")
        return label

    def set_active(self, active: str, *, reset_highlight: bool = False) -> None:
        # Status polling repeatedly renders the current screen.  Rebuilding an unchanged
        # sidebar would reset the user's navigation highlight every poll (Home refreshes
        # every 1.5 seconds).  Only page activation or an explicit reset may move it.
        navigation = self.query_one(OptionList)
        changed = active != self.active
        if changed:
            self.active = active
            for number, section in enumerate(SECTIONS, start=1):
                index = navigation.get_option_index(section.key)
                navigation.replace_option_prompt_at_index(index, self._label(section, number))
        if changed or reset_highlight:
            navigation.highlighted = self._index_of(active)

    def move(self, direction: int) -> None:
        navigation = self.query_one(OptionList)
        navigation.focus()
        if direction > 0:
            navigation.action_cursor_down()
        else:
            navigation.action_cursor_up()

    def choose(self) -> None:
        navigation = self.query_one(OptionList)
        index = navigation.highlighted
        if index is None:
            return
        key = str(navigation.get_option_at_index(index).id)
        if any(section.key == key for section in SECTIONS):
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
