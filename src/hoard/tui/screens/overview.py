"""Overview of real daemon status."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Static

from ..state import collector_label, telegram_label
from ..widgets import EventsView, StatisticsView
from .base import WorkspaceScreen


class OverviewScreen(WorkspaceScreen):
    section = "overview"
    screen_title = "Обзор"
    screen_subtitle = "Состояние локального архива"

    def compose_workspace(self) -> ComposeResult:
        with Horizontal(id="overview-grid"):
            yield Static(id="system-summary")
            yield StatisticsView()
        yield Static("ПОСЛЕДНИЕ СОБЫТИЯ", classes="section-heading")
        yield EventsView(limit=8)

    def render_data(self, status: dict) -> None:
        summary = Text()
        summary.append("СИСТЕМА\n", style="bold #F79646")
        summary.append("Telegram   ", style="#888888")
        summary.append(telegram_label(status) + "\n", style="#FFFFFF")
        summary.append("Сборщик    ", style="#888888")
        summary.append(collector_label(status), style="#FFFFFF")
        self.query_one("#system-summary", Static).update(summary)
        self.query_one(StatisticsView).show_status(status)
        self.query_one(EventsView).show_status(status)
