"""Shared screen layout and status presentation."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import OptionList, Static

from ..colors import ACCENT, TEXT
from ..widgets import HoardHeader, HoardStatusBar, Sidebar
from ..widgets.chrome import DEFAULT_HINTS, Hint

if TYPE_CHECKING:
    from ..app import HoardApp


class WorkspaceScreen(Screen):
    """Header, framed navigation, one workspace of panels, a notice line and the shortcut bar."""

    section = ""
    #: Shortcuts shown on the left of the footer while this screen is active.
    HINTS: ClassVar[tuple[Hint, ...]] = DEFAULT_HINTS

    @property
    def hoard_app(self) -> HoardApp:
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        yield HoardHeader(self.section)
        with Horizontal(id="screen-body"):
            yield Sidebar(self.section)
            with Vertical(id="content"):
                with VerticalScroll(id="workspace"):
                    yield Static(id="status-error")
                    yield from self.compose_workspace()
                yield Static(id="action-feedback")
        yield HoardStatusBar()

    def compose_workspace(self) -> ComposeResult:
        yield Static()

    def on_mount(self) -> None:
        # A scroll container is not a meaningful Tab stop: it has no visible focus outline,
        # and j/k are handled by the application as section navigation.  Keep wheel scrolling,
        # but leave the keyboard focus chain to tables and buttons.
        self.query_one("#workspace").can_focus = False
        self.query_one(Sidebar).set_active(self.section, reset_highlight=True)
        self.render_status(self.hoard_app.status, self.hoard_app.status_error)

    def on_screen_resume(self) -> None:
        self.query_one(Sidebar).set_active(self.section, reset_highlight=True)
        self.render_status(self.hoard_app.status, self.hoard_app.status_error)

    def render_status(self, status: dict | None, error: str | None = None) -> None:
        self.query_one(HoardHeader).show_status(status, error)
        self.query_one(HoardStatusBar).show_hints(self.HINTS)
        sidebar = self.query_one(Sidebar)
        account = (status or {}).get("telegram_account") or {}
        sidebar.set_indicators(
            web=str(self.hoard_app.web_state.get("state") or "unknown"),
            account=str(account.get("state") or "unknown"),
        )
        error_view = self.query_one("#status-error", Static)
        if error:
            error_view.update(
                Text(
                    "Не удалось связаться со службой Hoard.\n"
                    f"{error}\n\nНажмите q, чтобы закрыть интерфейс.",
                    style=ACCENT,
                )
            )
            error_view.display = True
        else:
            error_view.display = False
        feedback = self.query_one("#action-feedback", Static)
        notice = self.hoard_app.notice
        if notice:
            message, is_error = notice
            line = Text()
            line.append("!  " if is_error else "›  ", style=f"bold {ACCENT}")
            line.append(message, style=ACCENT if is_error else TEXT)
            feedback.update(line)
            feedback.display = True
        else:
            feedback.display = False
        if status is not None:
            self.render_data(status)

    def render_data(self, status: dict) -> None:
        del status

    def on_app_resize(self) -> None:
        """Called after the terminal changed size; repaint anything that depends on density."""
        status = self.hoard_app.status
        if status is not None and self.is_mounted:
            self.render_data(status)

    def focus_working_surface(self, table: Widget) -> None:
        """Focus a table only when it is visible; otherwise keep the top of the page in view."""
        workspace = self.query_one("#workspace", VerticalScroll)
        if (
            table.display
            and table.region.width > 0
            and table.region.y >= workspace.region.y
            and table.region.bottom <= workspace.region.bottom
        ):
            table.focus()
            return
        sidebar = self.query_one(Sidebar)
        if sidebar.display:
            sidebar.query_one(OptionList).focus()
        else:
            self.set_focus(None)

    def on_sidebar_navigate(self, message: Sidebar.Navigate) -> None:
        if message.section == Sidebar.BACK:
            self.hoard_app.leave_workspace()
        else:
            self.hoard_app.open_section(message.section)


__all__ = ["WorkspaceScreen"]
