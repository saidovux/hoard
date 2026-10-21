"""Shared screen layout and status presentation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from ..widgets import HoardHeader, HoardStatusBar, Sidebar

if TYPE_CHECKING:
    from ..app import HoardApp


class WorkspaceScreen(Screen):
    section = ""
    screen_title = ""
    screen_subtitle = ""

    @property
    def hoard_app(self) -> HoardApp:
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        yield HoardHeader()
        with Horizontal(id="screen-body"):
            yield Sidebar(self.section)
            with VerticalScroll(id="workspace"):
                yield Static(self.screen_title, classes="screen-title")
                yield Static(self.screen_subtitle, classes="screen-subtitle")
                yield from self.compose_workspace()
        yield HoardStatusBar()

    def compose_workspace(self) -> ComposeResult:
        yield Static()

    def on_mount(self) -> None:
        self.render_status(self.hoard_app.status, self.hoard_app.status_error)

    def on_screen_resume(self) -> None:
        self.render_status(self.hoard_app.status, self.hoard_app.status_error)

    def render_status(self, status: dict | None, error: str | None = None) -> None:
        self.query_one(HoardHeader).show_status(status, error)
        self.query_one(Sidebar).set_active(self.section)
        error_view = self.query_one("#status-error", Static)
        if error:
            error_view.update(
                Text(
                    "Не удалось связаться со службой Hoard.\n"
                    f"{error}\n\nНажмите q, чтобы закрыть интерфейс.",
                    style="#F79646",
                )
            )
            error_view.display = True
        else:
            error_view.display = False
        if status is not None:
            self.render_data(status)

    def render_data(self, status: dict) -> None:
        del status

    def on_sidebar_navigate(self, message: Sidebar.Navigate) -> None:
        self.hoard_app.open_section(message.section)
