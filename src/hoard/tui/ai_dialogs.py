"""Small data-entry dialogs used by the AI workspace."""

from __future__ import annotations

import json
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, SelectionList, Static, TextArea

from ..ai.config import DEFAULT_DEEPSEEK_MODEL


class AIRecordDialog(ModalScreen[dict[str, Any] | None]):
    """Edit a chat, prompt, memory entry, tariff, or private provider config."""

    BINDINGS = [Binding("escape", "cancel", "Отмена")]

    def __init__(
        self,
        kind: str,
        *,
        current: dict[str, Any] | None = None,
        prompts: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__()
        self.kind = kind
        self.current = current or {}
        self.prompts = prompts or []

    def compose(self) -> ComposeResult:
        title = {
            "chat": "Диалог AI",
            "prompt": "Промпт",
            "memory": "Память",
            "price": "Тариф",
            "config": "DeepSeek",
        }[self.kind]
        with VerticalScroll(classes="dialog ai-record-dialog"):
            yield Static(title, classes="dialog-title")
            yield Static(id="ai-record-error", classes="dialog-error")
            if self.kind == "chat":
                yield from self._chat_fields()
            elif self.kind == "prompt":
                yield from self._prompt_fields()
            elif self.kind == "memory":
                yield from self._memory_fields()
            elif self.kind == "price":
                yield from self._price_fields()
            else:
                yield from self._config_fields()
            with Horizontal(classes="dialog-actions"):
                yield Button("Сохранить", id="ai-record-save", variant="primary")
                yield Button("Отмена", id="ai-record-cancel")

    def _input(self, label: str, identifier: str, value: Any = "", **kwargs: Any):
        yield Label(label, classes="field-label")
        yield Input(value=str(value if value is not None else ""), id=identifier, **kwargs)

    def _chat_fields(self):
        yield from self._input("Название", "record-name", self.current.get("title", "Новый диалог"))
        yield from self._input(
            "Провайдер", "record-provider", self.current.get("provider", "deepseek")
        )
        yield from self._input(
            "Модель", "record-model", self.current.get("model", DEFAULT_DEEPSEEK_MODEL)
        )
        options = [("Без промпта", "")]
        options.extend(
            (
                f"{item['name']} · v{item.get('version', '—')}",
                str(item["id"]),
            )
            for item in self.prompts
        )
        yield Label("Активная версия промпта", classes="field-label")
        yield Select(
            options,
            value=str(self.current.get("prompt_id") or ""),
            allow_blank=False,
            id="record-prompt",
        )

    def _prompt_fields(self):
        version = self.current.get("current_version") or self.current
        yield from self._input("Название", "record-name", self.current.get("name", ""))
        yield Label("Область", classes="field-label")
        yield Select(
            [("general", "general"), ("telegram", "telegram")],
            value=str(self.current.get("scope") or "general"),
            allow_blank=False,
            id="record-scope",
        )
        yield from self._input("Провайдер", "record-provider", version.get("provider", "deepseek"))
        yield from self._input(
            "Модель", "record-model", version.get("model", DEFAULT_DEEPSEEK_MODEL)
        )
        yield Label("Формат ответа", classes="field-label")
        yield Select(
            [("Текст", "text"), ("JSON", "json")],
            value=str(version.get("response_format") or "text"),
            allow_blank=False,
            id="record-format",
        )
        yield Label("Инструкции", classes="field-label")
        yield TextArea(str(version.get("instructions") or ""), id="record-content")
        yield Label("JSON Schema (необязательно)", classes="field-label")
        schema = version.get("json_schema")
        yield TextArea(
            json.dumps(schema, ensure_ascii=False, indent=2) if schema else "",
            id="record-schema",
        )

    def _memory_fields(self):
        yield from self._input("Название", "record-name", self.current.get("title", ""))
        yield Label("Область", classes="field-label")
        yield Select(
            [("general", "general"), ("telegram", "telegram")],
            value=str(self.current.get("scope") or "general"),
            allow_blank=False,
            id="record-scope",
        )
        yield Label("Содержимое", classes="field-label")
        yield TextArea(str(self.current.get("content") or ""), id="record-content")

    def _price_fields(self):
        yield from self._input("Провайдер", "record-provider", "deepseek")
        yield from self._input("Модель", "record-model", DEFAULT_DEEPSEEK_MODEL)
        yield from self._input("Валюта", "record-currency", "USD")
        yield from self._input("Cache hit / 1M", "record-hit")
        yield from self._input("Cache miss / 1M", "record-miss")
        yield from self._input("Output / 1M", "record-output")
        yield from self._input("Источник тарифа", "record-source")

    def _config_fields(self):
        source = {"env": "окружение", "file": "файл", "none": "не настроен"}.get(
            str(self.current.get("key_source") or "none"), "не настроен"
        )
        yield Static(f"Текущий источник ключа: {source}", classes="muted")
        yield from self._input("Новый API-ключ", "record-key", "", password=True)
        yield from self._input(
            "Модель", "record-model", self.current.get("model", DEFAULT_DEEPSEEK_MODEL)
        )
        yield from self._input("Таймаут, сек", "record-timeout", self.current.get("timeout", 120))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ai-record-cancel":
            self.dismiss(None)
            return
        try:
            self.dismiss(self._values())
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            self.query_one("#ai-record-error", Static).update(str(exc))

    def _value(self, identifier: str) -> str:
        return self.query_one(f"#{identifier}", Input).value.strip()

    def _selected(self, identifier: str) -> str:
        value = self.query_one(f"#{identifier}", Select).value
        return "" if value is Select.BLANK else str(value)

    def _values(self) -> dict[str, Any]:
        if self.kind == "chat":
            return {
                "title": self._value("record-name"),
                "scope": "general",
                "provider": self._value("record-provider"),
                "model": self._value("record-model"),
                "prompt_id": self._selected("record-prompt"),
            }
        if self.kind == "prompt":
            raw_schema = self.query_one("#record-schema", TextArea).text.strip()
            response_format = self._selected("record-format")
            schema = json.loads(raw_schema) if raw_schema else None
            if schema is not None and response_format != "json":
                raise ValueError("JSON Schema требует формат JSON")
            return {
                "name": self._value("record-name"),
                "scope": self._selected("record-scope"),
                "instructions": self.query_one("#record-content", TextArea).text,
                "provider": self._value("record-provider"),
                "model": self._value("record-model"),
                "response_format": response_format,
                "json_schema": schema,
            }
        if self.kind == "memory":
            return {
                "title": self._value("record-name"),
                "scope": self._selected("record-scope"),
                "content": self.query_one("#record-content", TextArea).text,
                "enabled": True,
            }
        if self.kind == "price":
            return {
                "provider": self._value("record-provider"),
                "model": self._value("record-model"),
                "currency": self._value("record-currency"),
                "input_cache_hit_per_million": self._value("record-hit") or None,
                "input_cache_miss_per_million": self._value("record-miss") or None,
                "output_per_million": self._value("record-output") or None,
                "source": self._value("record-source"),
            }
        return {
            "provider": "deepseek",
            "api_key": self._value("record-key"),
            "model": self._value("record-model"),
            "timeout": float(self._value("record-timeout")),
        }

    def action_cancel(self) -> None:
        self.dismiss(None)


class TelegramAIDialog(ModalScreen[dict[str, Any] | None]):
    """Select an immutable local-photo snapshot before external processing."""

    BINDINGS = [Binding("escape", "cancel", "Отмена")]

    def __init__(
        self,
        profile: dict[str, Any],
        prompts: list[dict[str, Any]],
        analyses: list[dict[str, Any]],
    ) -> None:
        super().__init__()
        self.profile = profile
        self.prompts = prompts
        self.analyses = analyses

    def compose(self) -> ComposeResult:
        name = self.profile.get("display_name") or self.profile.get("current_username") or "—"
        with VerticalScroll(classes="dialog telegram-ai-dialog"):
            yield Static(f"Telegram AI · {name}", classes="dialog-title")
            yield Static(
                "PSL — субъективная, ненаучная визуальная оценка. Не выбирай фото, если "
                "совершеннолетие человека не подтверждено вне изображения.",
                classes="dialog-message",
            )
            yield Label("Фото: введи 1–4 ID через запятую", classes="field-label")
            photo_lines = [
                f"{item['photo_id']} · {item.get('photo_date') or 'без даты'} · "
                f"{'локально' if item.get('available') else 'недоступно'}"
                for item in self.profile.get("photos", [])
            ]
            yield Static("\n".join(photo_lines) or "Сохранённых фото нет", id="telegram-ai-photos")
            yield Input(placeholder="photo_id_1, photo_id_2", id="telegram-ai-photo-ids")
            options = [
                (
                    f"{item['name']} · v{item.get('version', '—')}",
                    str(item.get("version_id") or item["id"]),
                )
                for item in self.prompts
            ]
            yield Label("Промпт", classes="field-label")
            yield Select(options, allow_blank=False, id="telegram-ai-prompt")
            yield Label("Задача", classes="field-label")
            yield TextArea(
                "Оцени выбранные изображения по PSL-контракту.",
                id="telegram-ai-content",
            )
            yield Input(value=DEFAULT_DEEPSEEK_MODEL, id="telegram-ai-model")
            previous = self.analyses[0] if self.analyses else None
            previous_text = "Предыдущих анализов нет"
            if previous:
                age = previous.get("estimated_age")
                raw_min = previous.get("raw_psl_min")
                raw_max = previous.get("raw_psl_max")
                boosted = previous.get("boosted_psl")
                status = previous.get("result_status") or previous.get("attempt_state")
                model = previous.get("actual_model") or previous.get("requested_model") or "—"
                previous_text = (
                    "Последний сохранённый результат (открытие не создаёт запрос):\n"
                    f"status={status} · "
                    f"presentation={previous.get('presentation') or '—'} · "
                    f"age≈{age if age is not None else '—'}\n"
                    f"raw={raw_min if raw_min is not None else '—'}"
                    f"–{raw_max if raw_max is not None else '—'} · "
                    f"boosted={boosted if boosted is not None else '—'} · "
                    f"model={model} · "
                    f"cost={previous.get('estimated_cost') or 'неизвестно'} "
                    f"{previous.get('cost_currency') or ''}"
                )
            yield Static(
                previous_text,
                id="telegram-ai-previous",
            )
            yield Static(id="telegram-ai-error", classes="dialog-error")
            with Horizontal(classes="dialog-actions"):
                yield Button("Продолжить", id="telegram-ai-submit", variant="primary")
                yield Button("Открыть первое фото", id="telegram-ai-open")
                yield Button("Отмена", id="telegram-ai-cancel")

    def on_mount(self) -> None:
        if not self.prompts:
            self.query_one("#telegram-ai-submit", Button).disabled = True
            self.query_one("#telegram-ai-error", Static).update(
                "Нет Telegram-промпта. Создай его в разделе AI → Промпты."
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "telegram-ai-cancel":
            self.dismiss(None)
        elif event.button.id == "telegram-ai-open":
            photo_ids = self._photo_ids()
            if photo_ids:
                self.dismiss({"open_photo_id": photo_ids[0]})
        elif event.button.id == "telegram-ai-submit":
            photo_ids = self._photo_ids()
            if not 1 <= len(photo_ids) <= 4:
                self.query_one("#telegram-ai-error", Static).update("Выбери от 1 до 4 фото.")
                return
            prompt = self.query_one("#telegram-ai-prompt", Select).value
            if prompt is Select.BLANK:
                self.query_one("#telegram-ai-error", Static).update("Выбери промпт.")
                return
            content = self.query_one("#telegram-ai-content", TextArea).text.strip()
            if not content:
                self.query_one("#telegram-ai-error", Static).update("Запрос не может быть пустым.")
                return
            self.dismiss(
                {
                    "photo_ids": photo_ids,
                    "prompt_version_id": str(prompt),
                    "content": content,
                    "model": self.query_one("#telegram-ai-model", Input).value.strip(),
                }
            )

    def _photo_ids(self) -> list[str]:
        value = self.query_one("#telegram-ai-photo-ids", Input).value
        return [part.strip() for part in value.split(",") if part.strip()]

    def action_cancel(self) -> None:
        self.dismiss(None)


class AIKeyDialog(ModalScreen[str | None]):
    """One-field first-run onboarding; advanced settings stay in the AI workspace."""

    BINDINGS = [Binding("escape", "cancel", "Отмена")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog ai-key-dialog"):
            yield Static("DeepSeek не подключён", classes="dialog-title")
            yield Static(
                "Добавь один API-ключ. Модель, тарифы, промпты и остальные параметры "
                "не требуются для начала работы.",
                classes="dialog-message",
            )
            yield Label("API-ключ DeepSeek", classes="field-label")
            yield Input(password=True, placeholder="sk-…", id="ai-key-value")
            yield Static(id="ai-key-error", classes="dialog-error")
            with Horizontal(classes="dialog-actions"):
                yield Button("Сохранить", id="ai-key-save", variant="primary")
                yield Button("Отмена", id="ai-key-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ai-key-cancel":
            self.dismiss(None)
            return
        key = self.query_one("#ai-key-value", Input).value.strip()
        if not key:
            self.query_one("#ai-key-error", Static).update("Вставь API-ключ DeepSeek.")
            return
        self.dismiss(key)

    def action_cancel(self) -> None:
        self.dismiss(None)


class TelegramAnalysisDialog(ModalScreen[dict[str, Any] | None]):
    """Review exact local photos before an explicit external request."""

    BINDINGS = [Binding("escape", "cancel", "Отмена")]

    def __init__(self, preview: dict[str, Any]) -> None:
        super().__init__()
        self.preview = preview
        self.items = list(preview.get("items") or [])
        self.single = int(preview.get("selected_count") or 0) == 1

    @staticmethod
    def _profile_label(item: dict[str, Any]) -> str:
        name = item.get("display_name") or item.get("username") or f"ID {item['author_id']}"
        username = f" · @{item['username']}" if item.get("username") else ""
        return f"{name}{username} · {item.get('photo_count', 0)} фото"

    def compose(self) -> ComposeResult:
        source = self.preview.get("source") or {}
        title = "Анализ профиля" if self.single else "Анализ выбранных профилей"
        if source:
            title = "Профили публикации"
        with VerticalScroll(classes="dialog telegram-analysis-dialog"):
            yield Static(title, classes="dialog-title")
            yield Static(
                f"Выбрано: {self.preview.get('selected_count', 0)} · "
                f"локальных фото: {self.preview.get('photo_count', 0)}. "
                "Preview не обращается к Telegram и не отправляет данные в DeepSeek.",
                classes="dialog-message",
            )
            if source:
                channel = (
                    source.get("channel_title")
                    or source.get("title")
                    or source.get("username")
                )
                yield Static(
                    f"Публикация: {channel} #{source.get('post_id')}", classes="table-help"
                )
            if self.single and self.items:
                item = self.items[0]
                yield Label("Фотографии (до четырёх)", classes="field-label")
                selected = set(item.get("selected_photo_ids") or [])
                photo_options = [
                    (
                        f"{photo['photo_id']} · {photo.get('date') or 'без даты'} · "
                        f"{photo.get('size') or 0} байт",
                        str(photo["photo_id"]),
                        str(photo["photo_id"]) in selected,
                    )
                    for photo in item.get("photos") or []
                    if photo.get("available")
                ]
                if photo_options:
                    yield SelectionList(*photo_options, id="analysis-photos")
                else:
                    yield Static("Доступных локальных фотографий нет.", classes="dialog-error")
                previous = item.get("previous") or {}
                if previous:
                    previous_status = (
                        previous.get("result_status")
                        or previous.get("attempt_state")
                        or "—"
                    )
                    raw_min = previous.get("raw_psl_min")
                    raw_max = previous.get("raw_psl_max")
                    yield Static(
                        f"Предыдущий анализ: {previous_status} · "
                        f"Raw {raw_min if raw_min is not None else '—'}"
                        f"–{raw_max if raw_max is not None else '—'}",
                        classes="table-help",
                    )
            yield Static(
                "Возрастной допуск для локальных пользовательских профилей: 18+ автоматически.",
                classes="table-help",
            )
            skipped = [item for item in self.items if not item.get("selected_photo_ids")]
            if skipped:
                lines = [
                    f"ID {item['author_id']}: {item.get('reason') or 'будет пропущен'}"
                    for item in skipped[:20]
                ]
                yield Static("\n".join(lines), classes="dialog-error")
            if self.preview.get("has_more"):
                yield Static(
                    "Список длиннее одной страницы. Непоказанные профили останутся без "
                    "подтверждения и будут пропущены.",
                    classes="table-help",
                )
            yield Static(id="analysis-error", classes="dialog-error")
            with Horizontal(classes="dialog-actions"):
                yield Button("Продолжить", id="analysis-submit", variant="primary")
                yield Button("Отмена", id="analysis-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "analysis-cancel":
            self.dismiss(None)
            return
        photo_ids = {
            str(item["author_id"]): [str(photo) for photo in item.get("selected_photo_ids") or []]
            for item in self.items
            if item.get("selected_photo_ids") and int(item["author_id"]) > 0
        }
        photo_snapshots = {
            str(item["author_id"]): str(item["photo_snapshot"])
            for item in self.items
            if item.get("photo_snapshot") and int(item["author_id"]) > 0
        }
        if self.single and self.items and self.query("#analysis-photos"):
            photos = list(self.query_one("#analysis-photos", SelectionList).selected)
            if not 1 <= len(photos) <= 4:
                self.query_one("#analysis-error", Static).update("Выбери от 1 до 4 фото.")
                return
            photo_ids[str(self.items[0]["author_id"])] = [str(item) for item in photos]
            if photo_ids[str(self.items[0]["author_id"])] != list(
                self.items[0].get("selected_photo_ids") or []
            ):
                photo_snapshots.pop(str(self.items[0]["author_id"]), None)
        if not photo_ids:
            self.query_one("#analysis-error", Static).update(
                "Нет подходящих локальных пользовательских профилей с фотографиями."
            )
            return
        self.dismiss(
            {
                "photo_ids": photo_ids,
                "photo_snapshots": photo_snapshots,
            }
        )

    def action_cancel(self) -> None:
        self.dismiss(None)
