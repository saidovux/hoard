"""Live activity screen with moderate, non-overlapping status polling."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.timer import Timer
from textual.widgets import Static

from ..state import value
from ..widgets import EventsView, ProgressView
from ..widgets.status import current_action
from .base import WorkspaceScreen


class ActivityScreen(WorkspaceScreen):
    section = "activity"
    screen_title = "Активность"
    screen_subtitle = "Текущая работа службы"
    POLL_INTERVAL = 1.5

    def __init__(self) -> None:
        super().__init__()
        self.poll_timer: Timer | None = None

    def compose_workspace(self) -> ComposeResult:
        yield Static("ТЕКУЩЕЕ ДЕЙСТВИЕ", classes="section-heading")
        yield Static(id="current-action")
        yield ProgressView()
        yield Static("ОЧЕРЕДЬ", classes="section-heading")
        yield Static(id="activity-queue")
        yield Static("СОБЫТИЯ И ОШИБКИ", classes="section-heading")
        yield EventsView(limit=10)

    def on_screen_resume(self) -> None:
        super().on_screen_resume()
        if self.poll_timer is None:
            self.poll_timer = self.set_interval(self.POLL_INTERVAL, self._poll)

    def on_screen_suspend(self) -> None:
        self.stop_polling()

    def on_unmount(self) -> None:
        self.stop_polling()

    async def _poll(self) -> None:
        await self.hoard_app.refresh_status()

    def stop_polling(self) -> None:
        if self.poll_timer is not None:
            self.poll_timer.stop()
            self.poll_timer = None

    def render_data(self, status: dict) -> None:
        action = current_action(status)
        action_style = "#888888" if action == "Нет активной работы" else "#FFFFFF"
        self.query_one("#current-action", Static).update(Text(action, style=action_style))
        self.query_one(ProgressView).show_status(status)
        queue = status.get("queue") or {}
        self.query_one("#activity-queue", Static).update(
            Text(
                f"Ожидают {value(queue, 'queued')}   ·   "
                f"Выполняются {value(queue, 'running')}   ·   "
                f"Ошибки {value(queue, 'failed')}",
                style="#FFFFFF",
            )
        )
        self.query_one(EventsView).show_status(status)
