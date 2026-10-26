"""Home: the program's name and everything the daemon is doing right now, on one page.

What used to be an Overview and an Activity page is one live dashboard: the masthead, what is
running, the queue, the archive figures, one line per tool and the latest events. It is the only
page that polls the daemon, and only while it is on screen.
"""

from __future__ import annotations

from collections.abc import Callable

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Grid
from textual.events import Resize
from textual.timer import Timer
from textual.widgets import Static

from ..colors import ACCENT, MUTED, TEXT
from ..navigation import SECTIONS, TOOLS
from ..state import collector_label, telegram_label
from ..widgets import (
    QUEUE_TILES,
    EventsView,
    Hero,
    Panel,
    ProgressView,
    StatisticsView,
)
from ..widgets.status import current_action
from .base import WorkspaceScreen


def _telegram_summary(status: dict) -> tuple[bool, str]:
    authorized = str(status.get("telegram") or "") == "authorized"
    return authorized, f"{telegram_label(status)} · сборщик {collector_label(status)}"


def _ai_summary(status: dict) -> tuple[bool, str]:
    configured = bool((status.get("integrations") or {}).get("ai"))
    return configured, "настроен" if configured else "не настроен"


#: One line per tool: whether it is ready, and a short description of its state.
#: A new tool shows up on Home by adding its summary here.
MODULE_SUMMARIES: dict[str, Callable[[dict], tuple[bool, str]]] = {
    "telegram": _telegram_summary,
    "ai": _ai_summary,
}


def idle_hint(status: dict) -> str:
    """What to do next when nothing is running."""
    if str(status.get("telegram") or "") != "authorized":
        return "Подключите аккаунт на странице Telegram."
    queued = int((status.get("queue") or {}).get("queued") or 0)
    if collector_label(status) == "работает":
        return "Сборщик ждёт следующей проверки."
    if queued:
        return f"В очереди заданий: {queued}. Запустите сборщик на странице Telegram."
    return "Сборщик остановлен. Запустите его на странице Telegram."


class ToolsView(Static):
    """A status line for every tool that has one."""

    def __init__(self) -> None:
        super().__init__(id="tools")
        self._status: dict | None = None
        self._width = -1

    def show_status(self, status: dict) -> None:
        self._status = status
        self._paint()

    def on_resize(self, event: Resize) -> None:
        if self._status is not None and self.content_size.width != self._width:
            self._paint()

    def _paint(self) -> None:
        status = self._status or {}
        width = self._width = self.content_size.width
        text = Text(no_wrap=False)
        for section in SECTIONS:
            summary = MODULE_SUMMARIES.get(section.key)
            if section.group != TOOLS or summary is None:
                continue
            ready, description = summary(status)
            if text:
                text.append("\n")
            text.append("● " if ready else "○ ", style=ACCENT if ready else MUTED)
            if width and width < 40:
                # Too narrow for a label column: the description goes under its tool.
                text.append(section.label + "\n  ", style=f"bold {TEXT}")
            else:
                text.append(section.label.ljust(10), style=f"bold {TEXT}")
            text.append(description, style=TEXT if ready else MUTED)
        self.update(text)


class HomeScreen(WorkspaceScreen):
    section = "home"
    POLL_INTERVAL = 1.5

    def __init__(self) -> None:
        super().__init__()
        self.poll_timer: Timer | None = None

    def compose_workspace(self) -> ComposeResult:
        yield Hero("big")
        yield Hero("small")
        with Grid(id="home-grid"):
            yield Panel(Static(id="current-action"), ProgressView(), title="Сейчас", id="now-panel")
            yield Panel(
                StatisticsView(QUEUE_TILES, id="queue-stats"), title="Очередь", id="queue-panel"
            )
            yield Panel(StatisticsView(), title="Архив", id="archive-panel")
            yield Panel(ToolsView(), title="Инструменты", id="tools-panel")
        yield Panel(EventsView(limit=12), title="События", id="events-panel")

    def on_screen_resume(self) -> None:
        super().on_screen_resume()
        if self.poll_timer is None:
            self.poll_timer = self.set_interval(self.POLL_INTERVAL, self._poll)

    def on_screen_suspend(self) -> None:
        self.stop_polling()

    def on_unmount(self) -> None:
        self.stop_polling()

    async def _poll(self) -> None:
        await self.hoard_app.refresh_status(brief=True)

    def stop_polling(self) -> None:
        if self.poll_timer is not None:
            self.poll_timer.stop()
            self.poll_timer = None

    def render_status(self, status: dict | None, error: str | None = None) -> None:
        super().render_status(status, error)
        # Until the daemon has answered there is nothing to put in the panels: show just the logo.
        for selector in ("#home-grid", "#events-panel"):
            self.query_one(selector).display = status is not None
        if status is None:
            for hero in self.query(Hero):
                hero.show_status(None)

    def render_data(self, status: dict) -> None:
        for hero in self.query(Hero):
            hero.show_status(status)
        action = current_action(status)
        idle = action == "Нет активной работы"
        now = Text(
            action, style=MUTED if idle else f"bold {TEXT}", no_wrap=True, overflow="ellipsis"
        )
        if idle:
            now.append("\n\n" + idle_hint(status), style=MUTED)
        self.query_one("#current-action", Static).update(now)
        self.query_one(ProgressView).show_status(status)
        self.query_one("#queue-stats", StatisticsView).show_status(status)
        self.query_one("#statistics", StatisticsView).show_status(status)
        self.query_one(ToolsView).show_status(status)
        self.query_one(EventsView).show_status(status)
