"""Focused modal workflows for Telegram authorization and source editing."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static
from textual.worker import Worker

from .api import AuthResult, HoardAPI, SourceValidation


class ConfirmDialog(ModalScreen[bool]):
    """Small explicit confirmation used for interrupting or mass operations."""

    BINDINGS = [Binding("escape", "cancel", "Отмена")]

    def __init__(self, title: str, message: str, *, confirm_label: str = "Продолжить") -> None:
        super().__init__()
        self.dialog_title = title
        self.message = message
        self.confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog", id="confirm-dialog"):
            yield Static(self.dialog_title, classes="dialog-title")
            yield Static(self.message, classes="dialog-message")
            with Horizontal(classes="dialog-actions"):
                yield Button(self.confirm_label, id="confirm", variant="primary")
                yield Button("Отмена", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)


class AuthDialog(ModalScreen[bool]):
    """A view over the daemon-owned Telegram authorization state machine."""

    BINDINGS = [Binding("escape", "cancel", "Отмена")]

    KNOWN_STATES = {
        "api_required",
        "phone_required",
        "code_required",
        "password_required",
        "authorized",
    }

    def __init__(self, api: HoardAPI) -> None:
        super().__init__()
        self.api = api
        self.state = "checking"
        self._busy = False
        self._worker: Worker[Any] | None = None
        self._phone = ""

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog auth-dialog", id="auth-dialog"):
            yield Static("Подключение Telegram", classes="dialog-title")
            yield Static(
                "Проверяем сохранённую сессию. Сетевой запрос может занять время; "
                "интерфейс останется доступен.",
                id="auth-message",
                classes="dialog-message",
            )
            yield Label("API ID", id="api-id-label", classes="field-label")
            yield Input(placeholder="123456", id="api-id")
            yield Label("API Hash", id="api-hash-label", classes="field-label")
            yield Input(placeholder="API Hash с my.telegram.org/apps", password=True, id="api-hash")
            yield Label("Телефон", id="phone-label", classes="field-label")
            yield Input(placeholder="+79990000000", id="phone")
            yield Label("Код из Telegram", id="code-label", classes="field-label")
            yield Input(placeholder="12345", id="code")
            yield Label("Пароль 2FA", id="password-label", classes="field-label")
            yield Input(password=True, id="password")
            yield Static(id="auth-error", classes="dialog-error")
            with Horizontal(classes="dialog-actions"):
                yield Button("Подождите…", id="auth-primary", variant="primary", disabled=True)
                yield Button("Запросить новый код", id="auth-resend")
                yield Button("Отмена", id="auth-cancel")

    def on_mount(self) -> None:
        self._show_state()
        self._run(self.api.auth_begin, pending="Проверяем сохранённую сессию Telegram…")

    def _field(self, name: str, visible: bool) -> None:
        self.query_one(f"#{name}").display = visible
        self.query_one(f"#{name}-label").display = visible

    def _show_state(self, result: AuthResult | None = None) -> None:
        for field in ("api-id", "api-hash", "phone", "code", "password"):
            self._field(field, False)
        resend = self.query_one("#auth-resend", Button)
        resend.display = False
        primary = self.query_one("#auth-primary", Button)
        primary.disabled = self._busy

        labels = {
            "checking": "Подождите…",
            "api_required": "Сохранить ключи",
            "phone_required": "Отправить код",
            "code_required": "Подтвердить код",
            "password_required": "Подтвердить пароль",
            "unknown": "Проверить снова",
        }
        primary.label = labels.get(self.state, "Продолжить")
        if self.state == "api_required":
            self._field("api-id", True)
            self._field("api-hash", True)
        elif self.state == "phone_required":
            self._field("phone", True)
            if self._phone:
                self.query_one("#phone", Input).value = self._phone
        elif self.state == "code_required":
            self._field("code", True)
            resend.display = bool(self._phone)
        elif self.state == "password_required":
            self._field("password", True)

        if result is not None:
            message = str(result.get("message") or "")
            error = str(result.get("error") or "")
            error_code = str(result.get("error_code") or "")
            if error:
                suffix = ""
                if error_code == "flood_wait":
                    suffix = " Автоматический повтор отключён."
                self.query_one("#auth-error", Static).update(Text(error + suffix, style="#F79646"))
            else:
                self.query_one("#auth-error", Static).update("")
            if message:
                self.query_one("#auth-message", Static).update(message)

    def _run(
        self,
        operation: Callable[[], Awaitable[AuthResult | dict[str, Any]]],
        *,
        pending: str,
        follow_up: bool = False,
    ) -> None:
        if self._busy:
            return
        self._busy = True
        self._show_state()
        self._worker = self.run_worker(
            self._execute(operation, pending=pending, follow_up=follow_up),
            name="telegram-auth-operation",
            exit_on_error=False,
        )

    async def _execute(
        self,
        operation: Callable[[], Awaitable[AuthResult | dict[str, Any]]],
        *,
        pending: str,
        follow_up: bool,
    ) -> None:
        self.query_one("#auth-message", Static).update(pending)
        self.query_one("#auth-error", Static).update("")
        self._show_state()
        try:
            result = await operation()
            if follow_up:
                result = await self.api.auth_begin()
        except asyncio.CancelledError:
            self._busy = False
            raise
        except Exception as exc:
            self._busy = False
            if self.is_mounted:
                self.query_one("#auth-error", Static).update(
                    Text(str(exc) or type(exc).__name__, style="#F79646")
                )
                self._show_state()
            return
        if not self.is_mounted:
            self._busy = False
            return
        auth = AuthResult(**result)
        state = str(auth.get("state") or "unknown")
        self.state = state if state in self.KNOWN_STATES else "unknown"
        if self.state == "authorized" or auth.get("authorized"):
            await self.app.refresh_status()  # type: ignore[attr-defined]
            self._busy = False
            self._clear_secrets()
            self.dismiss(True)
            return
        if self.state == "unknown":
            auth["error"] = auth.get("error") or (f"Неизвестное состояние авторизации: {state}")
        self._busy = False
        self._show_state(auth)
        self._focus_current()

    def _focus_current(self) -> None:
        field = {
            "api_required": "#api-id",
            "phone_required": "#phone",
            "code_required": "#code",
            "password_required": "#password",
        }.get(self.state)
        if field:
            self.query_one(field, Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "auth-cancel":
            self.action_cancel()
        elif event.button.id == "auth-resend":
            self._send_code(force=True)
        elif event.button.id == "auth-primary":
            self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.display:
            self._submit()

    def _submit(self) -> None:
        if self.state == "api_required":
            raw_id = self.query_one("#api-id", Input).value.strip()
            api_hash = self.query_one("#api-hash", Input).value.strip()
            try:
                api_id = int(raw_id)
            except ValueError:
                self.query_one("#auth-error", Static).update(
                    Text("API ID должен быть числом.", style="#F79646")
                )
                return
            self._run(
                lambda: self.api.save_credentials(api_id, api_hash),
                pending="Сохраняем API credentials и проверяем Telegram…",
                follow_up=True,
            )
            self.query_one("#api-hash", Input).value = ""
        elif self.state == "phone_required":
            self._send_code(force=False)
        elif self.state == "code_required":
            code = self.query_one("#code", Input).value
            self._run(
                lambda: self.api.auth_submit_code(code),
                pending="Проверяем код Telegram…",
            )
        elif self.state == "password_required":
            password = self.query_one("#password", Input).value
            self._run(
                lambda: self.api.auth_submit_password(password),
                pending="Проверяем пароль 2FA…",
            )
        else:
            self._run(self.api.auth_begin, pending="Проверяем Telegram…")

    def _send_code(self, *, force: bool) -> None:
        phone = self.query_one("#phone", Input).value.strip() or self._phone
        self._phone = phone
        self._run(
            lambda: self.api.auth_send_code(phone, force=force),
            pending="Запрашиваем код у Telegram. При недоступной сети это может занять время…",
        )

    def action_cancel(self) -> None:
        if self._worker is not None and self._worker.is_running:
            self._worker.cancel()
        self._clear_secrets()
        self._busy = False
        self._worker = self.run_worker(
            self._cancel_flow(), name="telegram-auth-cancel", exit_on_error=False
        )

    async def _cancel_flow(self) -> None:
        self.query_one("#auth-message", Static).update(
            "Отменяем незавершённое ожидание и очищаем challenge авторизации…"
        )
        try:
            await self.api.auth_cancel()
        except Exception:
            # Closing the UI workflow must remain possible even if IPC disappeared.
            pass
        if self.is_mounted:
            self.dismiss(False)

    def on_unmount(self) -> None:
        if self._worker is not None and self._worker.is_running:
            self._worker.cancel()

    def _clear_secrets(self) -> None:
        if not self.is_mounted:
            return
        self.query_one("#code", Input).value = ""
        self.query_one("#password", Input).value = ""
        self.query_one("#api-hash", Input).value = ""


class SourceDialog(ModalScreen[bool]):
    """Validate, configure and upsert one Telegram source."""

    BINDINGS = [Binding("escape", "cancel", "Отмена")]

    def __init__(
        self,
        api: HoardAPI,
        *,
        source: dict[str, Any] | None = None,
        default_output: str = "~/Videos/hoard",
    ) -> None:
        super().__init__()
        self.api = api
        self.source = source
        self.default_output = default_output
        self.step = "link"
        self.validation: SourceValidation | None = None
        self._busy = False
        self._worker: Worker[Any] | None = None

    def compose(self) -> ComposeResult:
        source = self.source or {}
        with VerticalScroll(classes="dialog source-dialog", id="source-dialog"):
            yield Static(
                "Настроить источник" if source else "Добавить источник",
                classes="dialog-title",
            )
            yield Static(
                "Ссылка → проверка доступа → параметры → сохранение",
                id="source-message",
                classes="dialog-message",
            )
            yield Label("Ссылка на пост", id="source-url-label", classes="field-label")
            yield Input(
                value=str(source.get("url") or ""),
                placeholder="https://t.me/channel/123",
                id="source-url",
            )
            yield Label("Название", id="source-title-label", classes="field-label")
            yield Input(value=str(source.get("title") or ""), id="source-title")
            yield Label("Каталог архива", id="source-output-label", classes="field-label")
            yield Input(value=str(source.get("output") or self.default_output), id="source-output")
            yield Label("Интервал, секунд", id="source-interval-label", classes="field-label")
            yield Input(value=str(source.get("interval") or 120), id="source-interval")
            yield Static(id="source-error", classes="dialog-error")
            with Horizontal(classes="dialog-actions"):
                yield Button("Проверить доступ", id="source-primary", variant="primary")
                yield Button("Запустить сборщик", id="source-start")
                yield Button("Назад", id="source-back")
                yield Button("Отмена", id="source-cancel")

    def on_mount(self) -> None:
        self._show_step()
        self.query_one("#source-url", Input).focus()

    def _show_step(self) -> None:
        parameters = self.step in {"parameters", "done"}
        for field in ("source-title", "source-output", "source-interval"):
            self.query_one(f"#{field}").display = parameters
            self.query_one(f"#{field}-label").display = parameters
        self.query_one("#source-url", Input).disabled = self.step == "done"
        primary = self.query_one("#source-primary", Button)
        primary.disabled = self._busy
        primary.label = {
            "link": "Проверить доступ",
            "parameters": "Сохранить",
            "done": "Закрыть",
        }[self.step]
        self.query_one("#source-back", Button).display = self.step == "parameters"
        self.query_one("#source-start", Button).display = self.step == "done"
        self.query_one("#source-cancel", Button).label = (
            "Закрыть" if self.step == "done" else "Отмена"
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "source-primary":
            self._submit()
        elif event.button.id == "source-back":
            self.step = "link"
            self.validation = None
            self._show_step()
        elif event.button.id == "source-start":
            self._start(self._start_collector(), "Запускаем сборщик…")
        elif event.button.id == "source-cancel":
            self.action_cancel()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.display:
            self._submit()

    def _submit(self) -> None:
        if self._busy:
            return
        if self.step == "link":
            url = self.query_one("#source-url", Input).value.strip()
            self._start(self._validate(url), "Проверяем доступ к посту через Telegram…")
        elif self.step == "parameters":
            self._start(self._save(), "Сохраняем параметры источника…")
        else:
            self.dismiss(True)

    def _start(self, operation: Awaitable[None], pending: str) -> None:
        if self._busy:
            return
        self._busy = True
        self._show_step()
        self._worker = self.run_worker(
            self._execute(operation, pending), name="source-dialog-operation", exit_on_error=False
        )

    async def _execute(self, operation: Awaitable[None], pending: str) -> None:
        self.query_one("#source-message", Static).update(pending)
        self.query_one("#source-error", Static).update("")
        self._show_step()
        try:
            await operation
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.is_mounted:
                self.query_one("#source-error", Static).update(
                    Text(str(exc) or type(exc).__name__, style="#F79646")
                )
        finally:
            self._busy = False
            if self.is_mounted:
                self._show_step()

    async def _validate(self, url: str) -> None:
        self.validation = await self.api.validate_source(url)
        if not self.is_mounted:
            return
        self.query_one("#source-url", Input).value = self.validation["url"]
        title = self.query_one("#source-title", Input)
        if not title.value.strip():
            title.value = self.validation["title"]
        self.step = "parameters"
        self.query_one("#source-message", Static).update(
            f"Доступ подтверждён: @{self.validation['username']} / "
            f"пост {self.validation['post_id']}. Проверь параметры."
        )
        self._show_step()
        self.query_one("#source-output", Input).focus()

    async def _save(self) -> None:
        url = self.query_one("#source-url", Input).value.strip()
        output = self.query_one("#source-output", Input).value.strip()
        title = self.query_one("#source-title", Input).value.strip()
        raw_interval = self.query_one("#source-interval", Input).value.strip()
        try:
            interval = int(raw_interval)
        except ValueError as exc:
            raise ValueError("Интервал должен быть целым числом секунд.") from exc
        result = await self.api.add_source(url, output, interval, title=title)
        if not self.is_mounted:
            return
        self.step = "done"
        message = str(result.get("message") or "Источник сохранён")
        self.query_one("#source-message", Static).update(
            message + "\nИсточник уже назначен на ближайшую проверку; отдельный дубль не создан."
        )
        self._show_step()

    async def _start_collector(self) -> None:
        result = await self.api.start_collector()
        if not self.is_mounted:
            return
        await self.app.refresh_status()  # type: ignore[attr-defined]
        self.query_one("#source-message", Static).update(
            str(result.get("message") or "Сборщик запущен")
            + " Источник уже стоит на ближайшую проверку."
        )

    def action_cancel(self) -> None:
        if self._worker is not None and self._worker.is_running:
            self._worker.cancel()
        self.dismiss(self.step == "done")

    def on_unmount(self) -> None:
        if self._worker is not None and self._worker.is_running:
            self._worker.cancel()
