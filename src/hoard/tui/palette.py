"""Command palette: type a few letters to jump to a section or run an app-level command."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.events import Resize
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from .colors import MUTED


@dataclass(frozen=True, slots=True)
class Command:
    """One palette entry. ``key`` is handed back to the app when the entry is chosen."""

    key: str
    label: str
    hint: str = ""
    keywords: tuple[str, ...] = ()
    #: Shown in muted text before the label, e.g. the tool a page belongs to.
    context: str = ""


def matches(commands: Sequence[Command], query: str) -> list[Command]:
    """Every word of ``query`` must occur in the label, context or keywords."""
    terms = query.lower().split()
    found = [
        command
        for command in commands
        if all(
            term in " ".join((command.context, command.label, *command.keywords)).lower()
            for term in terms
        )
    ]
    if terms:
        found.sort(key=lambda command: not command.label.lower().startswith(terms[0]))
    return found


class PaletteScreen(ModalScreen[str | None]):
    BINDINGS = [
        Binding("escape", "close", "Закрыть"),
        Binding("down,ctrl+n", "move(1)", "Вниз", show=False),
        Binding("up,ctrl+p", "move(-1)", "Вверх", show=False),
    ]

    def __init__(self, commands: Sequence[Command]) -> None:
        super().__init__()
        self.commands = tuple(commands)

    def compose(self) -> ComposeResult:
        with Vertical(id="palette"):
            yield Input(placeholder="Куда перейти или что сделать…", id="palette-input")
            yield Static(id="palette-rule")
            yield OptionList(id="palette-list")

    def on_mount(self) -> None:
        self.query_one(OptionList).can_focus = False
        self._paint_rule()
        self._filter("")
        self.query_one(Input).focus()

    def on_resize(self, event: Resize) -> None:
        self._paint_rule()

    def _paint_rule(self) -> None:
        width = self.query_one("#palette-rule", Static).content_size.width
        self.query_one("#palette-rule", Static).update(Text("─" * max(width, 1), style="#262626"))

    @staticmethod
    def _prompt(command: Command) -> Table:
        label = Text(no_wrap=True, overflow="ellipsis")
        if command.context:
            label.append(command.context + "  ›  ", style=MUTED)
        label.append(command.label)
        row = Table.grid(expand=True)
        row.add_column(ratio=1, no_wrap=True)
        row.add_column(justify="right", no_wrap=True)
        row.add_row(label, Text(command.hint, style=MUTED))
        return row

    def _filter(self, query: str) -> None:
        options = self.query_one(OptionList)
        options.clear_options()
        found = matches(self.commands, query)
        if found:
            options.add_options(Option(self._prompt(item), id=item.key) for item in found)
            options.highlighted = 0
        else:
            options.add_option(Option(Text("Ничего не найдено", style=MUTED), disabled=True))

    def on_input_changed(self, event: Input.Changed) -> None:
        self._filter(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        options = self.query_one(OptionList)
        index = options.highlighted
        if index is None:
            return
        option = options.get_option_at_index(index)
        if not option.disabled and option.id:
            self.dismiss(str(option.id))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id:
            self.dismiss(str(event.option.id))

    def action_move(self, delta: int) -> None:
        options = self.query_one(OptionList)
        if delta > 0:
            options.action_cursor_down()
        else:
            options.action_cursor_up()

    def action_close(self) -> None:
        self.dismiss(None)
