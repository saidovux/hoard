"""Telegram connection and collector lifecycle controls."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, Static

from ..dialogs import ConfirmDialog
from ..state import collector_label, telegram_label
from .base import WorkspaceScreen


class TelegramScreen(WorkspaceScreen):
    section = "telegram"
    screen_title = "Telegram"
    screen_subtitle = "Подключение аккаунта и управление сборщиком"

    def compose_workspace(self) -> ComposeResult:
        yield Static(id="telegram-summary")
        with Horizontal(classes="action-row", id="telegram-actions"):
            yield Button("Подключить", id="telegram-connect", variant="primary")
            yield Button("Запустить", id="collector-start")
            yield Button("Пауза", id="collector-pause")
            yield Button("Остановить", id="collector-stop")
        yield Static("ЧТО ПРОИЗОЙДЁТ", classes="section-heading")
        yield Static(
            "Подключение использует сохранённую сессию daemon. Пауза и остановка могут "
            "прервать активное задание, но не завершают службу и не удаляют очередь.",
            id="collector-help",
        )

    def render_data(self, status: dict) -> None:
        summary = Text()
        summary.append("Telegram   ", style="#888888")
        summary.append(telegram_label(status) + "\n", style="#FFFFFF")
        summary.append("Сборщик    ", style="#888888")
        summary.append(collector_label(status) + "\n", style="#FFFFFF")
        queue = status.get("queue") or {}
        summary.append("Очередь     ", style="#888888")
        summary.append(
            f"{queue.get('queued', 0)} ожидают · {queue.get('running', 0)} выполняются",
            style="#FFFFFF",
        )
        self.query_one("#telegram-summary", Static).update(summary)

        running = bool(status.get("running"))
        collector_state = str(status.get("collector_state") or "stopped")
        authorized = str(status.get("telegram") or "") == "authorized"
        connect = self.query_one("#telegram-connect", Button)
        connect.label = "Проверить сессию" if authorized else "Подключить"
        self.query_one("#collector-start", Button).label = (
            "Возобновить" if collector_state == "paused" else "Запустить"
        )
        self.query_one("#collector-start", Button).disabled = running
        self.query_one("#collector-pause", Button).disabled = not running
        self.query_one("#collector-stop", Button).disabled = (
            not running and collector_state == "stopped"
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "telegram-connect": self._connect,
            "collector-start": self._start,
            "collector-pause": self._pause,
            "collector-stop": self._stop,
        }
        action = actions.get(str(event.button.id))
        if action:
            action()

    def _connect(self) -> None:
        self.hoard_app.open_auth()

    def _start(self) -> None:
        self.hoard_app.ensure_telegram(self._start_authorized)

    def _start_authorized(self) -> None:
        self._control(
            "collector-start",
            self.hoard_app.api.start_collector,
            "Запускаем сборщик…",
        )

    def _pause(self) -> None:
        self._confirm_interrupt("pause")

    def _stop(self) -> None:
        self._confirm_interrupt("stop")

    def _confirm_interrupt(self, operation: str) -> None:
        status = self.hoard_app.status or {}
        active = bool(status.get("progress")) or bool((status.get("queue") or {}).get("running"))
        if not active:
            self._run_interrupt(operation)
            return
        verb = "Поставить на паузу" if operation == "pause" else "Остановить"
        self.app.push_screen(
            ConfirmDialog(
                verb + " сборщик?",
                "Активные Telegram-задания будут прерваны и возвращены в очередь. "
                "Операция может завершиться не мгновенно. Daemon продолжит работу.",
                confirm_label=verb,
            ),
            lambda confirmed: self._run_interrupt(operation) if confirmed else None,
        )

    def _run_interrupt(self, operation: str) -> None:
        if operation == "pause":
            self._control(
                "collector-pause",
                self.hoard_app.api.pause_collector,
                "Приостанавливаем сборщик и возвращаем незавершённые задания в очередь…",
            )
        else:
            self._control(
                "collector-stop",
                self.hoard_app.api.stop_collector,
                "Останавливаем сборщик и возвращаем незавершённые задания в очередь…",
            )

    def _control(
        self,
        key: str,
        operation: Callable[[], Awaitable[dict[str, Any]]],
        pending: str,
    ) -> None:
        self.run_worker(
            self.hoard_app.execute(key, operation, pending=pending),
            name=key,
            exit_on_error=False,
        )
