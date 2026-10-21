"""Textual application lifecycle, navigation, and bounded status refresh."""

from __future__ import annotations

import asyncio
from typing import ClassVar

from textual.app import App
from textual.binding import Binding
from textual.events import Resize
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Input, OptionList, Static, TextArea

from .api import IPCStatusAPI, StatusAPI
from .screens import ActivityScreen, OverviewScreen
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
            "?             помощь\n"
            "q             выход",
            id="help-card",
        )


class HoardApp(App[None]):
    """Read-only round-two UI backed entirely by the established daemon IPC."""

    CSS_PATH = "theme.tcss"
    TITLE = "Hoard"
    ENABLE_COMMAND_PALETTE = False
    SCREENS: ClassVar[dict[str, type[WorkspaceScreen]]] = {
        "overview": OverviewScreen,
        "activity": ActivityScreen,
    }
    BINDINGS = [
        Binding("q", "quit", "Выход", priority=True),
        Binding("?", "help", "Помощь", priority=True),
        Binding("j", "nav_down", "Вниз", priority=True),
        Binding("down", "arrow_down", "Вниз", priority=True),
        Binding("k", "nav_up", "Вверх", priority=True),
        Binding("up", "arrow_up", "Вверх", priority=True),
        Binding("l,right,enter", "open", "Открыть", priority=True),
        Binding("h,left,escape", "back", "Назад", priority=True),
        Binding("b", "sidebar", "Sidebar", priority=True),
    ]

    def __init__(self, api: StatusAPI | None = None) -> None:
        super().__init__()
        self.api = api or IPCStatusAPI()
        self.status: dict | None = None
        self.status_error: str | None = None
        self._refresh_lock = asyncio.Lock()
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

    def open_section(self, section: str) -> None:
        if isinstance(self.screen, HelpScreen) or section not in self.SCREENS:
            return
        current = getattr(self.screen, "section", None)
        if current != section:
            self.switch_screen(section)

    def _accept_letter_navigation(self) -> bool:
        return not isinstance(self.focused, (Input, TextArea))

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if isinstance(self.focused, (Input, TextArea)) and action in {
            "quit",
            "help",
            "nav_down",
            "arrow_down",
            "nav_up",
            "arrow_up",
            "open",
            "back",
            "sidebar",
        }:
            return False
        return super().check_action(action, parameters)

    def _sidebar(self) -> Sidebar | None:
        if isinstance(self.screen, WorkspaceScreen):
            return self.screen.query_one(Sidebar)
        return None

    def action_nav_down(self) -> None:
        if self._accept_letter_navigation() and (sidebar := self._sidebar()):
            sidebar.move(1)

    def action_nav_up(self) -> None:
        if self._accept_letter_navigation() and (sidebar := self._sidebar()):
            sidebar.move(-1)

    def action_arrow_down(self) -> None:
        self.action_nav_down()

    def action_arrow_up(self) -> None:
        self.action_nav_up()

    def action_open(self) -> None:
        if self._accept_letter_navigation() and (sidebar := self._sidebar()):
            sidebar.choose()

    def action_back(self) -> None:
        if not self._accept_letter_navigation():
            return
        if isinstance(self.screen, HelpScreen):
            self.pop_screen()
        elif getattr(self.screen, "section", None) != "overview":
            self.open_section("overview")

    def action_help(self) -> None:
        if isinstance(self.screen, HelpScreen):
            self.pop_screen()
        else:
            self.push_screen(HelpScreen())

    def action_sidebar(self) -> None:
        if self._accept_letter_navigation():
            self.toggle_class("sidebar-hidden")

    def on_resize(self, event: Resize) -> None:
        narrow = event.size.width < 60
        if narrow != self._last_narrow:
            self.set_class(narrow, "sidebar-hidden")
            self._last_narrow = narrow
        self.set_class(event.size.width < 76, "compact")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        # Sidebar handles this event; stopping it avoids accidental future global actions.
        event.stop()

    def on_unmount(self) -> None:
        for screen in self.screen_stack:
            if isinstance(screen, ActivityScreen):
                screen.stop_polling()
