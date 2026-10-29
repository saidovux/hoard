"""Complete local AI workspace backed only by daemon IPC."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Input, Select, Static, TextArea

from ..ai_dialogs import AIRecordDialog
from ..dialogs import ConfirmDialog
from ..widgets import Panel
from .base import WorkspaceScreen

AI_COLUMNS = {
    "chats": ("Название", "Провайдер", "Модель", "Промпт", "Сообщения"),
    "prompts": (
        "Название", "Область", "Версия", "ID версии", "Модель", "Формат", "Активен",
    ),
    "memory": ("Название", "Область", "Версия", "Состояние", "Содержимое"),
    "prices": ("Провайдер", "Модель", "Валюта", "Cache hit", "Cache miss", "Output"),
    "tasks": ("Состояние", "Область", "Модель", "Стоимость", "Ошибка", "Создана"),
}

AI_TABLE_LAYOUTS = {
    "chats": {
        "full": (tuple(range(5)), (30, 12, 18, 10, 10)),
        "medium": ((0, 2, 3, 4), (30, 18, 10, 10)),
        "narrow": ((0, 2, 3, 4), (28, 18, 10, 10)),
        "compact": ((0, 2, 4), (18, 12, 8)),
    },
    "prompts": {
        "full": (tuple(range(7)), (22, 9, 8, 15, 16, 10, 8)),
        "medium": ((0, 1, 2, 4, 6), (18, 8, 8, 14, 7)),
        "narrow": ((0, 1, 2, 4, 6), (20, 9, 9, 16, 8)),
        "compact": ((0, 1, 6), (18, 8, 7)),
    },
    "memory": {
        "full": (tuple(range(5)), (22, 9, 8, 12, 49)),
        "medium": ((0, 1, 3, 4), (18, 8, 10, 24)),
        "narrow": ((0, 1, 3), (24, 9, 14)),
        "compact": ((0, 1, 3), (18, 8, 9)),
    },
    "prices": {
        "full": (tuple(range(6)), (10, 14, 8, 10, 11, 8)),
        "medium": ((0, 1, 3, 4, 5), (10, 14, 10, 11, 8)),
        "narrow": ((0, 1, 3, 4, 5), (9, 14, 10, 11, 8)),
        "compact": ((0, 1, 5), (10, 14, 8)),
    },
    "tasks": {
        "full": (tuple(range(6)), (14, 9, 18, 11, 20, 12)),
        "medium": ((0, 1, 2, 3, 4), (14, 9, 16, 11, 18)),
        "narrow": ((0, 1, 2, 3, 4), (14, 9, 16, 11, 18)),
        "compact": ((0, 2, 3), (14, 16, 12)),
    },
}


def _fit_widths(weights: tuple[int, ...], table_width: int) -> tuple[int, ...]:
    available = max(len(weights), table_width - len(weights) * 2 - 1)
    total = sum(weights)
    widths = [max(1, available * weight // total) for weight in weights]
    widest = max(range(len(widths)), key=widths.__getitem__)
    widths[widest] += available - sum(widths)
    return tuple(widths)


class AIWorkspaceScreen(WorkspaceScreen):
    section = "ai"

    MODES = ("chats", "prompts", "memory", "prices", "tasks")

    def __init__(self) -> None:
        super().__init__()
        self.mode = "chats"
        self.items: list[dict[str, Any]] = []
        self.prompts: list[dict[str, Any]] = []
        self.statistics: dict[str, dict[str, Any]] = {}
        self.config: dict[str, Any] = {}
        self.selected_chat: dict[str, Any] | None = None
        self.history_offset = 0
        self._loading = False
        self._table_mode: str | None = None
        self._table_layout: tuple[str, tuple[int, ...], tuple[int, ...]] | None = None

    def compose_workspace(self) -> ComposeResult:
        with Panel(title="AI", id="ai-main"):
            yield Static(id="ai-summary")
            with Vertical(id="ai-onboarding"):
                yield Static(
                    "DeepSeek не подключён\nДобавь один API-ключ, чтобы открыть AI-функции.",
                    id="ai-onboarding-copy",
                )
                yield Button("Добавить API-ключ", id="ai-add-key", variant="primary")
            yield Static(id="ai-statistics")
            with Horizontal(classes="mode-row ai-mode-row"):
                yield Button("Диалоги", id="ai-mode-chats", variant="primary")
                yield Button("Промпты", id="ai-mode-prompts")
                yield Button("Память", id="ai-mode-memory")
                yield Button("Тарифы", id="ai-mode-prices")
                yield Button("Попытки", id="ai-mode-tasks")
            with Vertical(id="ai-list-pane"):
                with Horizontal(classes="action-row", id="ai-actions"):
                    yield Button("Создать", id="ai-create", variant="primary")
                    yield Button("Открыть", id="ai-open")
                    yield Button("Изменить", id="ai-edit")
                    yield Button("Архивировать", id="ai-archive")
                    yield Button("Активировать", id="ai-activate")
                    yield Button("Повторить", id="ai-retry")
                    yield Button("Telegram AI-группы", id="ai-batches")
                    yield Button("Настроить ключ", id="ai-config")
                    yield Button("Открыть веб-интерфейс", id="ai-web")
                    yield Button("Обновить", id="ai-refresh")
                yield DataTable(id="ai-table", cursor_type="row")
        with Panel(title="Диалог", id="ai-chat-pane"):
            yield Static(id="ai-chat-title", classes="section-heading")
            yield Static(id="ai-history")
            yield Button("Показать более ранние", id="ai-history-older")
            yield Static(
                "Вложения: до 4 локальных JPEG/PNG/GIF/WebP, общий размер до 24 MiB.",
                classes="table-help",
            )
            yield Input(placeholder="Пути к изображениям через запятую", id="ai-attachments")
            yield Input(placeholder="ID памяти через запятую (необязательно)", id="ai-memory-ids")
            yield Select(
                [("Промпт диалога · текущая версия", "")],
                value="",
                allow_blank=False,
                id="ai-prompt-version-id",
            )
            yield TextArea(id="ai-message")
            with Horizontal(classes="dialog-actions"):
                yield Button("Отправить", id="ai-send", variant="primary")
                yield Button("Назад к диалогам", id="ai-chat-back")

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one("#ai-chat-pane").display = False
        self.query_one("#ai-onboarding").display = False
        self._load()

    def on_screen_resume(self) -> None:
        super().on_screen_resume()
        self._load()

    def render_data(self, status: dict) -> None:
        integration = status.get("integrations") or {}
        configured = bool(self.config.get("configured", integration.get("ai")))
        key_status = str(self.config.get("key_status") or "")
        state_label = {
            "not_configured": "не настроен",
            "key_saved": "ключ сохранён · ещё не проверен",
            "verified": "подключён",
            "authorization_error": "ошибка авторизации",
        }.get(key_status, "настроен" if configured else "не настроен")
        text = Text()
        text.append("DeepSeek  ", style="#888888")
        state_style = "#F79646" if key_status == "authorization_error" else "#FFFFFF"
        text.append(state_label, style=state_style)
        source_label = {"env": "окружение", "file": "файл", "none": "нет"}.get(
            str(self.config.get("key_source") or "none"), "нет"
        )
        text.append(f"  ·  ключ: {source_label}", style="#888888")
        text.append("  ·  ", style="#888888")
        text.append("платные запросы только после подтверждения", style="#F79646")
        self.query_one("#ai-summary", Static).update(text)

    def _load(self) -> None:
        if self._loading or not self.is_mounted:
            return
        self._loading = True
        self.run_worker(self._load_data(), name="ai-workspace-load", exit_on_error=False)

    async def _load_data(self) -> None:
        try:
            self.config = await self.hoard_app.api.ai_call("ai_config_get")
            available = bool(self.config.get("configured")) or "key_status" not in self.config
            if self.is_mounted:
                self.query_one("#ai-onboarding").display = not available
                self.query_one("#ai-statistics").display = available
                self.query_one(".ai-mode-row").display = available
                self.query_one("#ai-list-pane").display = available
                self.render_data(self.hoard_app.status or {})
            if not available:
                return
            prompt_page, general_stats, telegram_stats = await asyncio.gather(
                self.hoard_app.api.ai_call(
                    "ai_prompt_list", {"limit": 100, "offset": 0}
                ),
                self.hoard_app.api.ai_call("ai_statistics", {"scope": "general"}),
                self.hoard_app.api.ai_call("ai_statistics", {"scope": "telegram"}),
            )
            self.prompts = list(prompt_page.get("items") or [])
            self.statistics = {"general": general_stats, "telegram": telegram_stats}
            if self.is_mounted:
                self._update_prompt_versions()
            method, args = {
                "chats": ("ai_chat_list", {"archived": False, "limit": 100}),
                "prompts": ("ai_prompt_list", {"limit": 100}),
                "memory": ("ai_memory_list", {"limit": 100}),
                "prices": ("ai_price_list", {"limit": 100}),
                "tasks": ("ai_attempt_list", {"limit": 100}),
            }[self.mode]
            page = await self.hoard_app.api.ai_call(method, args)
            self.items = list(page.get("items") or [])
            if self.is_mounted:
                self._render_statistics()
                self._render_table()
                self._focus_default_control()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.hoard_app.set_notice(str(exc) or type(exc).__name__, error=True)
        finally:
            self._loading = False

    def _update_prompt_versions(self) -> None:
        options = [("Промпт диалога · текущая версия", "")]
        options.extend(
            (
                f"{item.get('name') or 'Промпт'} · v{item.get('version') or '—'}",
                str(item["version_id"]),
            )
            for item in self.prompts
            if item.get("scope") == "general" and item.get("version_id")
        )
        selector = self.query_one("#ai-prompt-version-id", Select)
        selector.set_options(options)
        selector.value = ""

    def _render_statistics(self) -> None:
        parts = []
        for scope in ("general", "telegram"):
            stats = self.statistics.get(scope) or {}
            costs = stats.get("estimated_costs") or {}
            cost_text = ", ".join(f"{amount} {currency}" for currency, amount in costs.items())
            unknown = int(stats.get("unknown_cost") or 0)
            if not cost_text:
                cost_text = "стоимость неизвестна" if unknown else "затрат нет"
            elif unknown:
                cost_text += f" + {unknown} без оценки"
            parts.append(f"{scope}: {int(stats.get('attempts') or 0)} попыток · {cost_text}")
        self.query_one("#ai-statistics", Static).update("\n".join(parts))

    def _render_table(self) -> None:
        table = self.query_one("#ai-table", DataTable)
        old_row = table.cursor_row if self._table_mode == self.mode else 0
        columns = AI_COLUMNS[self.mode]
        cells_rows: list[tuple[str, ...]] = []
        if self.mode == "chats":
            for item in self.items:
                cells_rows.append(
                    (
                        str(item.get("title") or "—"),
                        str(item.get("provider") or "—"),
                        str(item.get("model") or "—"),
                        str(item.get("prompt_id") or "—")[:10],
                        str(item.get("message_count") or 0),
                    )
                )
        elif self.mode == "prompts":
            for item in self.items:
                cells_rows.append(
                    (
                        str(item.get("name") or "—"),
                        str(item.get("scope") or "—"),
                        str(item.get("version") or "—"),
                        str(item.get("version_id") or "—"),
                        str(item.get("model") or "—"),
                        str(item.get("response_format") or "—"),
                        "да" if item.get("active") else "нет",
                    )
                )
        elif self.mode == "memory":
            for item in self.items:
                cells_rows.append(
                    (
                        str(item.get("title") or "—"),
                        str(item.get("scope") or "—"),
                        str(item.get("version") or "—"),
                        "включена" if item.get("enabled") else "выключена",
                        str(item.get("content") or "")[:80],
                    )
                )
        elif self.mode == "prices":
            for item in self.items:
                cells_rows.append(
                    (
                        str(item.get("provider") or "—"),
                        str(item.get("model") or "—"),
                        str(item.get("currency") or "—"),
                        str(item.get("input_cache_hit_per_million") or "неизвестно"),
                        str(item.get("input_cache_miss_per_million") or "неизвестно"),
                        str(item.get("output_per_million") or "неизвестно"),
                    )
                )
        else:
            for item in self.items:
                state = str(item.get("state") or "—")
                if state == "outcome_unknown":
                    state = "outcome_unknown ⚠ ручной повтор"
                cost = item.get("estimated_cost")
                cost_text = (
                    f"{cost} {item.get('cost_currency')}"
                    if cost is not None
                    else "неизвестно"
                )
                cells_rows.append(
                    (
                        state,
                        str(item.get("scope") or "—"),
                        str(item.get("actual_model") or item.get("requested_model") or "—"),
                        cost_text,
                        str(item.get("error_code") or "—"),
                        str(item.get("created_at") or "—"),
                    )
                )
        if not cells_rows:
            cells_rows.append(("Пока пусто", *(["—"] * (len(columns) - 1))))
        compact = self.hoard_app.has_class("compact")
        narrow = self.hoard_app.has_class("narrow")
        medium = self.hoard_app.has_class("medium")
        size_key = (
            "compact" if compact
            else "narrow" if narrow
            else "medium" if medium
            else "full"
        )
        keep, weights = AI_TABLE_LAYOUTS[self.mode][size_key]
        table_width = max(
            20,
            self.hoard_app.size.width
            - (6 if self.hoard_app.has_class("sidebar-hidden") else 28),
        )
        widths = _fit_widths(tuple(weights), table_width)
        layout = (self.mode, keep, widths)
        if layout != self._table_layout:
            table.clear(columns=True)
            for index, width in zip(keep, widths, strict=True):
                table.add_column(columns[index], width=width)
            self._table_layout = layout
        table.clear()
        for cells in cells_rows:
            table.add_row(*(cells[index] for index in keep))
        if table.row_count:
            table.move_cursor(row=min(max(0, old_row), table.row_count - 1), scroll=False)
        self._table_mode = self.mode
        self._show_actions()

    def _focus_default_control(self) -> None:
        """Put the keyboard on the page's working surface, not an invisible container."""
        if self.hoard_app.screen is not self:
            return
        focused = self.hoard_app.focused
        if focused is not None and getattr(focused, "id", None) != "navigation":
            return
        if self.query_one("#ai-list-pane").display:
            self.focus_working_surface(self.query_one("#ai-table", DataTable))
        elif self.query_one("#ai-onboarding").display:
            self.query_one("#ai-add-key", Button).focus()

    def _show_actions(self) -> None:
        create = self.query_one("#ai-create", Button)
        create.display = self.mode in {"chats", "prompts", "memory", "prices"}
        create.label = "Добавить тариф" if self.mode == "prices" else "Создать"
        self.query_one("#ai-open", Button).display = self.mode == "chats"
        self.query_one("#ai-edit", Button).display = self.mode in {"chats", "prompts", "memory"}
        self.query_one("#ai-archive", Button).display = self.mode == "chats"
        self.query_one("#ai-activate", Button).display = self.mode == "prompts"
        self.query_one("#ai-retry", Button).display = self.mode == "tasks"

    def _selected(self) -> dict[str, Any] | None:
        row = self.query_one("#ai-table", DataTable).cursor_row
        if 0 <= row < len(self.items):
            return self.items[row]
        self.hoard_app.set_notice("Выбери строку.", error=True)
        return None

    def on_button_pressed(self, event: Button.Pressed) -> None:
        identifier = str(event.button.id)
        if identifier.startswith("ai-mode-"):
            self.mode = identifier.removeprefix("ai-mode-")
            for mode in self.MODES:
                self.query_one(f"#ai-mode-{mode}", Button).variant = (
                    "primary" if mode == self.mode else "default"
                )
            self._load()
        elif identifier == "ai-refresh":
            self._load()
        elif identifier == "ai-config":
            self._open_config()
        elif identifier == "ai-add-key":
            self.hoard_app.ensure_ai(self._load)
        elif identifier == "ai-web":
            self.hoard_app.open_web_interface()
        elif identifier == "ai-create":
            self._open_editor(None)
        elif identifier == "ai-edit":
            selected = self._selected()
            if selected:
                self.run_worker(self._fetch_for_edit(selected), exit_on_error=False)
        elif identifier == "ai-open":
            selected = self._selected()
            if selected:
                self._open_chat(selected)
        elif identifier == "ai-archive":
            selected = self._selected()
            if selected:
                self._confirm_archive(selected)
        elif identifier == "ai-activate":
            selected = self._selected()
            if selected:
                self._activate_prompt(selected)
        elif identifier == "ai-retry":
            selected = self._selected()
            if selected:
                self._confirm_retry(selected)
        elif identifier == "ai-chat-back":
            self.selected_chat = None
            self.query_one("#ai-chat-pane").display = False
            self.query_one("#ai-main").display = True
            self.query_one("#ai-list-pane").display = True
            self.query_one("#workspace", VerticalScroll).scroll_home(animate=False)
            self._load()
            self.query_one("#ai-table", DataTable).focus()
        elif identifier == "ai-history-older":
            if self.selected_chat:
                self.history_offset += 30
                self.run_worker(self._load_history(), exit_on_error=False)
        elif identifier == "ai-batches":
            self.hoard_app.open_telegram_batch_history()
        elif identifier == "ai-send":
            self._confirm_send()

    def on_data_table_row_selected(self, _event: DataTable.RowSelected) -> None:
        if self.mode == "chats":
            selected = self._selected()
            if selected:
                self._open_chat(selected)

    def _open_editor(self, current: dict[str, Any] | None) -> None:
        kind = {"chats": "chat", "prompts": "prompt", "memory": "memory", "prices": "price"}.get(
            self.mode
        )
        if kind:
            self.app.push_screen(
                AIRecordDialog(kind, current=current, prompts=self.prompts),
                lambda values: self._save_record(kind, current, values),
            )

    async def _fetch_for_edit(self, selected: dict[str, Any]) -> None:
        method = {
            "chats": "ai_chat_get",
            "prompts": "ai_prompt_get",
            "memory": "ai_memory_get",
        }[self.mode]
        key = {"chats": "chat", "prompts": "prompt", "memory": "memory"}[self.mode]
        try:
            result = await self.hoard_app.api.ai_call(method, {"id": selected["id"]})
        except Exception as exc:
            self.hoard_app.set_notice(str(exc), error=True)
            return
        self._open_editor(result[key])

    def _save_record(
        self, kind: str, current: dict[str, Any] | None, values: dict[str, Any] | None
    ) -> None:
        if values is None:
            return
        method = f"ai_{kind}_{'update' if current else 'create'}"
        if kind == "price":
            method = "ai_price_create"
        if current:
            values = {**values, "id": current["id"]}
        self.run_worker(self._save(method, values), exit_on_error=False)

    async def _save(self, method: str, values: dict[str, Any]) -> None:
        try:
            await self.hoard_app.api.ai_call(method, values)
        except Exception as exc:
            self.hoard_app.set_notice(str(exc), error=True)
            return
        self.hoard_app.set_notice("Сохранено.")
        self._load()

    def _open_config(self) -> None:
        self.run_worker(self._load_config(), exit_on_error=False)

    async def _load_config(self) -> None:
        try:
            current = await self.hoard_app.api.ai_call("ai_config_get")
        except Exception as exc:
            self.hoard_app.set_notice(str(exc), error=True)
            return
        self.app.push_screen(
            AIRecordDialog("config", current=current),
            lambda values: self.run_worker(self._save("ai_config_set", values), exit_on_error=False)
            if values
            else None,
        )

    def _open_chat(self, selected: dict[str, Any]) -> None:
        self.selected_chat = selected
        self.history_offset = 0
        self.query_one("#ai-main").display = False
        self.query_one("#ai-chat-pane").display = True
        self.query_one("#ai-chat-title", Static).update(str(selected.get("title") or "Диалог"))
        # The list table is now hidden.  Without an explicit transfer, keyboard focus would
        # disappear and the newly opened conversation would be unreachable without Tab.
        self.query_one("#ai-message", TextArea).focus()
        self.query_one("#workspace", VerticalScroll).scroll_home(animate=False)
        self.run_worker(self._load_history(), exit_on_error=False)

    async def _load_history(self) -> None:
        if not self.selected_chat:
            return
        try:
            page = await self.hoard_app.api.ai_call(
                "ai_chat_history",
                {"id": self.selected_chat["id"], "limit": 30, "offset": self.history_offset},
            )
        except Exception as exc:
            self.hoard_app.set_notice(str(exc), error=True)
            return
        lines = []
        for item in page.get("items") or []:
            role = "Вы" if item.get("role") == "user" else "AI"
            state = str(item.get("state") or "")
            lines.append(f"{role} [{state}]\n{item.get('content') or ''}")
        self.query_one("#ai-history", Static).update("\n\n".join(lines) or "Сообщений пока нет")
        self.query_one("#ai-history-older", Button).display = bool(page.get("has_more"))

    def _confirm_send(self) -> None:
        if not self.selected_chat:
            return
        message = self.query_one("#ai-message", TextArea).text.strip()
        if not message:
            self.hoard_app.set_notice("Введи сообщение.", error=True)
            return
        self.app.push_screen(
            ConfirmDialog(
                "Отправить внешний AI-запрос?",
                "Текст, выбранная память и не более четырёх самых новых изображений из всего "
                "контекста будут отправлены DeepSeek; более старые вложения могут быть опущены. "
                "Запрос может быть платным; стоимость заранее может быть неизвестна. "
                "Автоматического платного повтора не будет.",
                confirm_label="Подтверждаю отправку",
            ),
            lambda confirmed: self.run_worker(self._send(message), exit_on_error=False)
            if confirmed
            else None,
        )

    async def _send(self, message: str) -> None:
        assert self.selected_chat is not None
        paths = [
            part.strip()
            for part in self.query_one("#ai-attachments", Input).value.split(",")
            if part.strip()
        ]
        if len(paths) > 4:
            self.hoard_app.set_notice("Можно выбрать не более 4 изображений.", error=True)
            return
        attachment_ids = []
        try:
            for path in paths:
                result = await self.hoard_app.api.ai_call("ai_attachment_import", {"path": path})
                attachment_ids.append(result["attachment"]["id"])
            memory_ids = [
                item.strip()
                for item in self.query_one("#ai-memory-ids", Input).value.split(",")
                if item.strip()
            ]
            result = await self.hoard_app.api.ai_call(
                "ai_chat_send",
                {
                    "chat_id": self.selected_chat["id"],
                    "content": message,
                    "operation_id": uuid.uuid4().hex,
                    "memory_ids": memory_ids,
                    "attachment_ids": attachment_ids,
                    "prompt_version_id": (
                        None
                        if self.query_one("#ai-prompt-version-id", Select).value
                        is Select.BLANK
                        else self.query_one("#ai-prompt-version-id", Select).value or None
                    ),
                    "external_confirmed": True,
                },
            )
        except Exception as exc:
            self.hoard_app.set_notice(str(exc), error=True)
            return
        attempt = result["attempt"]
        self.query_one("#ai-message", TextArea).text = ""
        self.hoard_app.set_notice("Запрос поставлен в очередь; ждём подтверждённый результат.")
        await self._load_history()
        await self._poll_attempt(str(attempt["id"]))

    async def _poll_attempt(self, attempt_id: str) -> None:
        for _ in range(60):
            await asyncio.sleep(1)
            try:
                result = await self.hoard_app.api.ai_call("ai_attempt_get", {"id": attempt_id})
            except Exception as exc:
                self.hoard_app.set_notice(str(exc), error=True)
                return
            attempt = result["attempt"]
            state = str(attempt.get("state") or "")
            if state in {"completed", "failed", "outcome_unknown"}:
                if state == "outcome_unknown":
                    self.hoard_app.set_notice(
                        "Исход запроса неизвестен. Автоповтор отключён; проверь и повтори вручную.",
                        error=True,
                    )
                elif state == "failed":
                    self.hoard_app.set_notice(
                        f"AI-запрос завершился ошибкой: {attempt.get('error_code') or 'unknown'}",
                        error=True,
                    )
                else:
                    self.hoard_app.set_notice("AI-ответ сохранён.")
                await self._load_history()
                return
        self.hoard_app.set_notice("Ожидание остановлено; состояние доступно во вкладке Попытки.")

    def _confirm_archive(self, chat: dict[str, Any]) -> None:
        self.app.push_screen(
            ConfirmDialog("Архивировать диалог?", "История сохранится локально."),
            lambda confirmed: self.run_worker(
                self._save("ai_chat_update", {"id": chat["id"], "archived": True}),
                exit_on_error=False,
            )
            if confirmed
            else None,
        )

    def _activate_prompt(self, prompt: dict[str, Any]) -> None:
        self.run_worker(
            self._save(
                "ai_prompt_activate", {"id": prompt["id"], "scope": prompt["scope"]}
            ),
            exit_on_error=False,
        )

    def _confirm_retry(self, attempt: dict[str, Any]) -> None:
        if attempt.get("state") not in {"failed", "outcome_unknown"}:
            self.hoard_app.set_notice(
                "Повтор доступен только после ошибки или неизвестного исхода.", error=True
            )
            return
        self.app.push_screen(
            ConfirmDialog(
                "Создать новую платную попытку?",
                "Это новый запрос провайдеру и новая стоимость. "
                "Исходная попытка останется в аудите.",
                confirm_label="Создать попытку",
            ),
            lambda confirmed: self.run_worker(self._retry(attempt), exit_on_error=False)
            if confirmed
            else None,
        )

    async def _retry(self, attempt: dict[str, Any]) -> None:
        try:
            result = await self.hoard_app.api.ai_call(
                "ai_attempt_retry",
                {"id": attempt["id"], "operation_id": uuid.uuid4().hex},
            )
        except Exception as exc:
            self.hoard_app.set_notice(str(exc), error=True)
            return
        self.hoard_app.set_notice("Новая AI-попытка поставлена в очередь.")
        self._load()
        await self._poll_attempt(str(result["attempt"]["id"]))

    def open_local_photo(self, path: str) -> None:
        """Open one daemon-validated local path in the user's configured viewer."""
        import webbrowser

        try:
            opened = webbrowser.open(Path(path).resolve(strict=True).as_uri(), new=2)
        except (OSError, ValueError):
            opened = False
        self.hoard_app.set_notice(
            (
                "Фото открыто во внешнем просмотрщике."
                if opened
                else "Не удалось открыть просмотрщик."
            ),
            error=not opened,
        )
