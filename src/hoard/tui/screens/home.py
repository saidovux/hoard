"""Compact global Home: current Telegram work plus small Hoard-wide figures."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.events import Resize
from textual.timer import Timer
from textual.widgets import Static

from ..colors import ACCENT, MUTED, TEXT
from ..widgets import Hero, Panel, TelegramMonitor
from .base import WorkspaceScreen


def _count(group: dict, key: str) -> str:
    raw = group.get(key)
    if isinstance(raw, bool):
        return "—"
    try:
        value = max(0, int(raw))
    except (TypeError, ValueError, OverflowError):
        return "—"
    return f"{value:,}".replace(",", " ")


class HoardSummary(Static):
    """Queue and archive totals in one quiet line (two lines on narrow terminals)."""

    def __init__(self) -> None:
        super().__init__(id="home-summary")
        self._status: dict | None = None
        self._render_key: object = None

    def show_status(self, status: dict) -> None:
        self._status = status
        self._paint()

    def on_resize(self, event: Resize) -> None:
        self._paint()

    def _paint(self) -> None:
        status = self._status or {}
        queue = status.get("queue") or {}
        stats = status.get("stats") or {}
        compact = bool(self.content_size.width and self.content_size.width < 68)
        values = (
            _count(queue, "queued"),
            _count(queue, "running"),
            _count(queue, "failed"),
            _count(stats, "sources"),
            _count(stats, "authors"),
            _count(stats, "downloads"),
        )
        render_key = (compact, self.content_size.width, values)
        if render_key == self._render_key:
            return
        self._render_key = render_key
        queued, running, failed, sources, authors, downloads = values
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append("Очередь  ", style=f"bold {MUTED}")
        if compact:
            text.append(f"{queued} ждут · ", style=TEXT)
            text.append(f"{running} в работе", style=ACCENT if running not in {"—", "0"} else TEXT)
            text.append(f" · {failed} ошибки", style=ACCENT if failed not in {"—", "0"} else TEXT)
            text.append("\nАрхив   ", style=f"bold {MUTED}")
            text.append(f"{sources} ист. · {authors} проф. · {downloads} круж.", style=TEXT)
        else:
            text.append(f"{queued} ожидают · ", style=TEXT)
            text.append(f"{running} в работе", style=ACCENT if running not in {"—", "0"} else TEXT)
            text.append(f" · {failed} ошибки", style=ACCENT if failed not in {"—", "0"} else TEXT)
            text.append("    Архив  ", style=f"bold {MUTED}")
            text.append(
                f"{sources} источников · {authors} профилей · {downloads} кружков",
                style=TEXT,
            )
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
        yield Panel(TelegramMonitor(), title="Telegram ○", id="telegram-monitor-panel")
        yield HoardSummary()

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
        for selector in ("#telegram-monitor-panel", "#home-summary"):
            self.query_one(selector).display = status is not None
        if status is None:
            for hero in self.query(Hero):
                hero.show_status(None)

    def render_data(self, status: dict) -> None:
        for hero in self.query(Hero):
            hero.show_status(status)
        self.query_one(TelegramMonitor).show_status(
            status,
            available=self.hoard_app.status_error is None,
        )
        self.query_one(HoardSummary).show_status(status)


__all__ = ["HoardSummary", "HomeScreen"]
