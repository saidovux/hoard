"""Textual application lifecycle, navigation, and bounded status refresh."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

from textual.app import App
from textual.binding import Binding
from textual.events import Resize
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widget import Widget
from textual.widgets import (
    Button,
    DataTable,
    Input,
    ListView,
    OptionList,
    Select,
    Static,
    TextArea,
)

from .api import HoardAPI, IPCStatusAPI
from .dialogs import AuthDialog
from .navigation import SECTIONS, section_index
from .screens import (
    ActivityScreen,
    ArchiveScreen,
    OverviewScreen,
    SourcesScreen,
    TelegramScreen,
)
from .screens.base import WorkspaceScreen
from .widgets import Sidebar

BLACK_THEME = Theme(
    name="hoard-black",
    primary="#F79646",
    secondary="#888888",
    warning="#F79646",
    error="#F79646",
    success="#FFFFFF",
    accent="#F79646",
    foreground="#FFFFFF",
    background="#000000",
    surface="#000000",
    panel="#000000",
    boost="#000000",
    dark=True,
    luminosity_spread=0.0,
    text_alpha=1.0,
    variables={
        "footer-background": "#000000",
        "footer-foreground": "#888888",
        "input-cursor-background": "#F79646",
        "input-selection-background": "#000000",
    },
)


class HelpScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "dismiss", "Закрыть"),
        Binding("?", "dismiss", "Закрыть"),
    ]

    def compose(self):
        yield Static(
            "КЛАВИАТУРА\n\n"
            "j / ↓        вниз\n"
            "k / ↑        вверх\n"
            "l / → / Enter  открыть\n"
            "h / ← / Esc    назад\n"
            "Tab           следующий элемент\n"
            "Shift+Tab     предыдущий\n"
            "b             скрыть sidebar\n"
            "[ / ]         сменить раздел\n"
            "?             помощь\n"
            "q             выход",
            id="help-card",
        )


class HoardApp(App[None]):
    """Full-screen client backed entirely by the established daemon IPC."""

    CSS_PATH = "theme.tcss"
    TITLE = "Hoard"
    ENABLE_COMMAND_PALETTE = False
    SCREENS: ClassVar[dict[str, type[WorkspaceScreen]]] = {
        "overview": OverviewScreen,
        "telegram": TelegramScreen,
        "sources": SourcesScreen,
        "activity": ActivityScreen,
        "archive": ArchiveScreen,
    }
    BINDINGS = [
        Binding("q", "quit", "Выход"),
        Binding("?", "help", "Помощь"),
        Binding("j", "nav_down", "Вниз"),
        Binding("down", "nav_down", "Вниз"),
        Binding("k", "nav_up", "Вверх"),
        Binding("up", "nav_up", "Вверх"),
        Binding("l,right,enter", "open", "Открыть"),
        Binding("h,left,escape", "back", "Назад"),
        Binding("b", "sidebar", "Sidebar"),
        Binding("[", "previous_section", "Предыдущий раздел"),
        Binding("]", "next_section", "Следующий раздел"),
    ]

    def __init__(self, api: HoardAPI | None = None) -> None:
        super().__init__()
        self.api = api or IPCStatusAPI()
        self.status: dict | None = None
        self.status_error: str | None = None
        self.notice: tuple[str, bool] | None = None
        self._refresh_lock = asyncio.Lock()
        self._operations: set[str] = set()
        self._startup_complete = False
        self._last_narrow: bool | None = None

    def on_mount(self) -> None:
        self.register_theme(BLACK_THEME)
        self.theme = BLACK_THEME.name
        self.push_screen("overview")
        self.run_worker(self._startup(), name="daemon-startup", exit_on_error=False)

    async def _startup(self) -> None:
        try:
            await self.api.ensure_started()
        except Exception as exc:  # UI boundary: keep an actionable screen on any IPC failure.
            self.status_error = str(exc) or type(exc).__name__
            self._startup_complete = True
            self._render_current()
            return
        self._startup_complete = True
        await self.refresh_status()

    async def refresh_status(self) -> bool:
        """Perform at most one status request at a time."""
        if self._refresh_lock.locked():
            return False
        async with self._refresh_lock:
            try:
                status = await self.api.status()
            except Exception as exc:  # UI must stay responsive when IPC/network work fails.
                self.status_error = str(exc) or type(exc).__name__
            else:
                self.status = status
                self.status_error = None
            self._render_current()
        return True

    def _render_current(self) -> None:
        screen = self.screen
        if isinstance(screen, WorkspaceScreen) and screen.is_mounted:
            screen.render_status(self.status, self.status_error)

    def set_notice(self, message: str, *, error: bool = False) -> None:
        self.notice = (message, error)
        self._render_current()

    async def execute(
        self,
        key: str,
        operation: Callable[[], Awaitable[dict[str, Any]]],
        *,
        pending: str,
        refresh: bool = True,
    ) -> dict[str, Any] | None:
        """Run one named IPC mutation at a time and present only confirmed results."""
        if key in self._operations:
            self.set_notice("Эта операция уже выполняется.", error=True)
            return None
        self._operations.add(key)
        self.set_notice(pending)
        try:
            result = await operation()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.set_notice(str(exc) or type(exc).__name__, error=True)
            return None
        finally:
            self._operations.discard(key)
        if refresh:
            await self.refresh_status()
        self.set_notice(str(result.get("message") or "Готово"))
        return result

    def open_section(self, section: str) -> None:
        if isinstance(self.screen, HelpScreen) or section not in self.SCREENS:
            return
        current = getattr(self.screen, "section", None)
        if current != section:
            self.switch_screen(section)

    def open_auth(self, on_authorized: Callable[[], None] | None = None) -> None:
        def complete(authorized: bool | None) -> None:
            if authorized and on_authorized is not None:
                on_authorized()

        self.push_screen(AuthDialog(self.api), complete)

    def ensure_telegram(self, on_authorized: Callable[[], None]) -> None:
        if str((self.status or {}).get("telegram") or "") == "authorized":
            on_authorized()
        else:
            self.open_auth(on_authorized)

    @staticmethod
    def _is_text_entry(widget: Widget | None) -> bool:
        return isinstance(widget, (Input, TextArea))

    @staticmethod
    def _is_interactive(widget: Widget | None) -> bool:
        return isinstance(
            widget, (Button, DataTable, Input, ListView, OptionList, Select, TextArea)
        )

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if isinstance(self.screen, ModalScreen) and action in {
            "quit",
            "help",
            "nav_down",
            "nav_up",
            "open",
            "back",
            "sidebar",
            "previous_section",
            "next_section",
        }:
            return False
        if self._is_text_entry(self.focused) and action in {
            "quit",
            "help",
            "nav_down",
            "nav_up",
            "open",
            "back",
            "sidebar",
            "previous_section",
            "next_section",
        }:
            return False
        return super().check_action(action, parameters)

    def _sidebar(self) -> Sidebar | None:
        if isinstance(self.screen, WorkspaceScreen):
            return self.screen.query_one(Sidebar)
        return None

    def action_nav_down(self) -> None:
        focused = self.focused
        if self._is_text_entry(focused):
            return
        if isinstance(focused, (DataTable, ListView, OptionList)):
            focused.action_cursor_down()
        elif self._is_interactive(focused):
            self.screen.focus_next()
        elif sidebar := self._sidebar():
            sidebar.move(1)

    def action_nav_up(self) -> None:
        focused = self.focused
        if self._is_text_entry(focused):
            return
        if isinstance(focused, (DataTable, ListView, OptionList)):
            focused.action_cursor_up()
        elif self._is_interactive(focused):
            self.screen.focus_previous()
        elif sidebar := self._sidebar():
            sidebar.move(-1)

    def action_open(self) -> None:
        focused = self.focused
        if self._is_text_entry(focused):
            return
        if isinstance(focused, DataTable):
            focused.action_select_cursor()
        elif isinstance(focused, ListView):
            focused.action_select_cursor()
        elif isinstance(focused, OptionList):
            focused.action_select()
        elif isinstance(focused, Button):
            focused.action_press()
        elif sidebar := self._sidebar():
            sidebar.choose()

    def action_back(self) -> None:
        if self._is_text_entry(self.focused):
            return
        if getattr(self.screen, "section", None) != "overview":
            self.open_section("overview")

    def action_help(self) -> None:
        if isinstance(self.screen, HelpScreen):
            self.pop_screen()
        else:
            self.push_screen(HelpScreen())

    def action_sidebar(self) -> None:
        if not self._is_text_entry(self.focused):
            self.toggle_class("sidebar-hidden")

    def _move_section(self, offset: int) -> None:
        current = getattr(self.screen, "section", "overview")
        index = (section_index(current) + offset) % len(SECTIONS)
        self.open_section(SECTIONS[index].key)

    def action_previous_section(self) -> None:
        if not self._is_text_entry(self.focused):
            self._move_section(-1)

    def action_next_section(self) -> None:
        if not self._is_text_entry(self.focused):
            self._move_section(1)

    def on_resize(self, event: Resize) -> None:
        narrow = event.size.width < 60
        if narrow != self._last_narrow:
            self.set_class(narrow, "sidebar-hidden")
            self._last_narrow = narrow
        self.set_class(event.size.width < 76, "compact")

    def on_unmount(self) -> None:
        for screen in self.screen_stack:
            if isinstance(screen, ActivityScreen):
                screen.stop_polling()
