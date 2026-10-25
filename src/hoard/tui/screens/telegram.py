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

    def __init__(self) -> None:
        super().__init__()
        self._web_state: dict[str, Any] = {"state": "stopped"}
        self._web_loading = False

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
        yield Static("WEB-ИНТЕРФЕЙС", classes="section-heading")
        yield Static(id="web-summary")
        with Horizontal(classes="action-row", id="web-actions"):
            yield Button("Включить Web", id="web-start", variant="primary")
            yield Button("Открыть Web", id="web-open")
            yield Button("Выключить Web", id="web-stop")

    def on_mount(self) -> None:
        super().on_mount()
        self._refresh_web()

    def on_screen_resume(self) -> None:
        super().on_screen_resume()
        self._refresh_web()

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
        self._render_web()

    def _render_web(self) -> None:
        state = str(self._web_state.get("state") or "stopped")
        labels = {
            "stopped": "выключен",
            "starting": "запускается",
            "ready": "работает",
            "stopping": "останавливается",
            "error": "ошибка",
        }
        text = Text()
        text.append("Web        ", style="#888888")
        text.append(labels.get(state, state), style="#FFFFFF")
        if state == "ready" and self._web_state.get("port"):
            text.append(f" · 127.0.0.1:{self._web_state['port']}", style="#888888")
        if state == "error" and self._web_state.get("error"):
            text.append(f"\n{self._web_state['error']}", style="#F79646")
        if state == "ready" and not self._web_state.get("controllable", True):
            text.append(
                "\nWeb запущен без управляющего канала; перезапусти его вручную",
                style="#F79646",
            )
        self.query_one("#web-summary", Static).update(text)

        busy = self._web_loading
        controllable = bool(self._web_state.get("controllable", True))
        start = self.query_one("#web-start", Button)
        open_button = self.query_one("#web-open", Button)
        stop = self.query_one("#web-stop", Button)
        start.disabled = busy or state in {"starting", "ready", "stopping"}
        open_button.disabled = busy or state != "ready" or not controllable
        stop.disabled = busy or state != "ready" or not controllable

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "telegram-connect": self._connect,
            "collector-start": self._start,
            "collector-pause": self._pause,
            "collector-stop": self._stop,
            "web-start": self._web_start,
            "web-open": self._web_open,
            "web-stop": self._web_stop,
        }
        action = actions.get(str(event.button.id))
        if action:
            action()

    def _refresh_web(self) -> None:
        if self._web_loading:
            return
        self._web_loading = True
        self._render_web()
        self.run_worker(
            self._load_web_state(), name="web-status", exit_on_error=False
        )

    async def _load_web_state(self) -> None:
        try:
            self._web_state = await self.hoard_app.api.web_status()
        except Exception as exc:
            self._web_state = {"state": "error", "error": str(exc) or type(exc).__name__}
        finally:
            self._web_loading = False
            if self.is_mounted:
                self._render_web()

    def _web_start(self) -> None:
        if self._web_loading:
            return
        self._web_state = {"state": "starting"}
        self._run_web_action(
            "web-start",
            self.hoard_app.api.start_web,
            "Включаем Web…",
        )

    def _web_open(self) -> None:
        self._run_web_action(
            "web-open",
            self.hoard_app.api.open_web,
            "Открываем Web…",
        )

    def _web_stop(self) -> None:
        if self._web_loading:
            return
        self._web_state = {"state": "stopping"}
        self._run_web_action(
            "web-stop",
            self.hoard_app.api.stop_web,
            "Останавливаем Web…",
        )

    def _run_web_action(
        self,
        key: str,
        operation: Callable[[], Awaitable[dict[str, Any]]],
        pending: str,
    ) -> None:
        if self._web_loading:
            return
        self._web_loading = True
        self.hoard_app.set_notice(pending)
        self._render_web()
        self.run_worker(
            self._perform_web_action(key, operation), name=key, exit_on_error=False
        )

    async def _perform_web_action(
        self,
        key: str,
        operation: Callable[[], Awaitable[dict[str, Any]]],
    ) -> None:
        del key
        try:
            result = await operation()
        except Exception as exc:
            self.hoard_app.set_notice(str(exc) or type(exc).__name__, error=True)
        else:
            self.hoard_app.set_notice(str(result.get("message") or "Готово"))
        finally:
            await self._load_web_state()

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
