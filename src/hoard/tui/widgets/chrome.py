"""Application chrome: header, navigation, and shortcut footer."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.message import Message
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from hoard import __version__

from ..navigation import SECTIONS, section_index
from ..state import collector_label


class HoardHeader(Static):
    def __init__(self) -> None:
        super().__init__(id="hoard-header")

    def show_status(self, status: dict | None, error: str | None = None) -> None:
        text = Text()
        text.append("hoard.", style="bold #F79646")
        text.append(f"  v{__version__}", style="#888888")
        if error:
            text.append("  ● ", style="#F79646")
            text.append("служба недоступна", style="#FFFFFF")
        elif status is None:
            text.append("  · подключение…", style="#888888")
        else:
            text.append("  ● ", style="#F79646")
            text.append(collector_label(status), style="#FFFFFF")
        self.update(text)


class Sidebar(Static):
    """The visible navigation contains only currently implemented sections."""

    class Navigate(Message):
        def __init__(self, section: str) -> None:
            self.section = section
            super().__init__()

    def __init__(self, active: str) -> None:
        super().__init__(id="sidebar")
        self.active = active

    def compose(self) -> ComposeResult:
        yield Static("РАЗДЕЛЫ", classes="eyebrow")
        yield OptionList(
            *(Option(self._label(item.key, item.label), id=item.key) for item in SECTIONS),
            id="navigation",
        )

    def on_mount(self) -> None:
        navigation = self.query_one(OptionList)
        navigation.highlighted = section_index(self.active)

    def set_active(self, active: str) -> None:
        self.active = active
        navigation = self.query_one(OptionList)
        for index, item in enumerate(SECTIONS):
            navigation.replace_option_prompt_at_index(index, self._label(item.key, item.label))
        navigation.highlighted = section_index(active)

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
        if index is not None and 0 <= index < len(SECTIONS):
            self.post_message(self.Navigate(SECTIONS[index].key))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.post_message(self.Navigate(str(event.option.id)))

    def _label(self, key: str, label: str) -> Text:
        marker = "▸" if key == self.active else " "
        style = "bold #F79646" if key == self.active else "#FFFFFF"
        return Text(f"{marker} {label}", style=style)


class HoardStatusBar(Static):
    def __init__(self) -> None:
        super().__init__(id="statusbar")
        self.update(
            Text.from_markup(
                "[bold #F79646]j/k[/] выбор  [bold #F79646]l/Enter[/] открыть  "
                "[bold #F79646]скобки[/] раздел  [bold #F79646]?[/] помощь  "
                "[bold #F79646]q[/] выход"
            )
        )
