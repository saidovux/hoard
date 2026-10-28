"""Small data-entry dialogs used by the AI workspace."""

from __future__ import annotations

import json
import webbrowser
from collections import Counter
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Input,
    Label,
    OptionList,
    Select,
    SelectionList,
    Static,
    TextArea,
)
from textual.widgets.option_list import Option

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
                "PSL — субъективная, ненаучная визуальная оценка. Возрастной допуск 18+ "
                "применяется автоматически. Выбранные фото будут отправлены в DeepSeek; "
                "запрос может быть платным.",
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
                yield Button(
                    "Отправить фото в DeepSeek и запустить анализ",
                    id="telegram-ai-submit",
                    variant="primary",
                )
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


class VideoFramesDialog(ModalScreen[dict[str, Any] | None]):
    """Inspect one local frame set without starting an external request."""

    BINDINGS = [Binding("escape", "cancel", "Отмена")]

    def __init__(
        self,
        details: dict[str, Any] | None,
        *,
        video_id: str = "",
        view_only: bool = False,
        open_frame: Callable[[int], None] | None = None,
    ) -> None:
        super().__init__()
        self.details = details or {}
        self.video_id = video_id or str(self.details.get("video_id") or "")
        self.view_only = bool(view_only)
        self.open_frame = open_frame

    def compose(self) -> ComposeResult:
        status = str(self.details.get("status") or "missing")
        labels = {
            "missing": "кадры ещё не извлекались",
            "pending": "ожидает обработки",
            "running": "извлекаются",
            "saved": "готовы",
            "failed": "ошибка обработки",
            "stale": "требуется повторная обработка",
        }
        with VerticalScroll(classes="dialog telegram-frames-dialog"):
            yield Static(f"Кадры видеокружка {self.video_id or '—'}", classes="dialog-title")
            yield Static(
                f"Состояние: {labels.get(status, status)}. "
                "Извлечение выполняется локально, без AI-запроса.",
                classes="dialog-message",
            )
            if self.details.get("error"):
                yield Static(str(self.details["error"]), classes="dialog-error")
            frames = self.details.get("frames") or []
            if frames:
                yield Static(
                    "\n".join(
                        f"Кадр {item.get('position')} · "
                        f"{float(item.get('timestamp_seconds') or 0):.2f} с · "
                        f"{int(item.get('size') or 0)} байт · "
                        f"{'доступен' if item.get('available') else 'недоступен'}"
                        for item in frames
                    ),
                    classes="table-help",
                )
            else:
                yield Static("Готовых кадров пока нет.", classes="table-help")
            with Horizontal(classes="dialog-actions"):
                for item in frames:
                    yield Button(
                        f"Открыть кадр {item.get('position')}",
                        id=f"frames-open-{int(item.get('position') or 0)}",
                    )
                if not self.view_only:
                    yield Button(
                        "Анализировать кадры",
                        id="frames-analyze",
                        variant="primary",
                        disabled=not bool(self.details.get("valid")),
                    )
                yield Button("Закрыть", id="frames-close")
            yield Static(id="frames-error", classes="dialog-error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        identifier = str(event.button.id)
        if identifier == "frames-close":
            self.dismiss(None)
            return
        if identifier == "frames-analyze":
            self.dismiss(
                {
                    "analyze": True,
                    "video_id": self.video_id,
                    "author_id": self.details.get("author_id"),
                    "image_snapshot": "",
                }
            )
            return
        if identifier.startswith("frames-open-"):
            try:
                position = int(identifier.rsplit("-", 1)[-1])
                if self.open_frame is not None:
                    self.open_frame(position)
                    opened = True
                else:
                    frame = next(
                        item
                        for item in self.details.get("frames") or []
                        if int(item.get("position") or 0) == position
                    )
                    path = Path(str(frame["file_path"])).resolve(strict=True)
                    opened = webbrowser.open(path.as_uri(), new=2)
            except (OSError, ValueError, StopIteration):
                opened = False
            if not opened:
                self.query_one("#frames-error", Static).update(
                    "Не удалось открыть кадр во внешнем просмотрщике"
                )

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
    """Paginated review of an immutable local-photo selection."""

    BINDINGS = [Binding("escape", "cancel", "Отмена")]

    def __init__(
        self,
        preview: dict[str, Any],
        *,
        page_loader: Callable[[int], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        super().__init__()
        self.preview = preview
        self.page_loader = page_loader
        self.single = int(preview.get("selected_count") or 0) == 1
        self.page_limit = max(1, int(preview.get("limit") or 20))
        self.page_offset = max(0, int(preview.get("offset") or 0))
        self.pages: dict[int, dict[str, Any]] = {self.page_offset: preview}
        self.image_mode = str(preview.get("image_mode") or "photos")
        self.photo_ids: dict[str, list[str]] = {}
        self.photo_snapshots: dict[str, str] = {}
        self.video_ids: dict[str, str] = {}
        self.image_snapshots: dict[str, str] = {}
        self._ingest(preview)

    def _ingest(self, page: dict[str, Any]) -> None:
        for item in page.get("items") or []:
            author_id = int(item.get("author_id") or 0)
            if author_id <= 0:
                continue
            key = str(author_id)
            selected = [str(value) for value in item.get("selected_photo_ids") or []]
            if selected:
                self.photo_ids.setdefault(key, selected)
            snapshot = str(item.get("image_snapshot") or item.get("photo_snapshot") or "")
            if selected and snapshot:
                self.photo_snapshots.setdefault(key, snapshot)
            if snapshot:
                self.image_snapshots.setdefault(key, snapshot)
            if item.get("video_id"):
                self.video_ids.setdefault(key, str(item["video_id"]))

    def compose(self) -> ComposeResult:
        source = self.preview.get("source") or {}
        title = "Анализ профиля" if self.single else "Анализ выбранных профилей"
        if source:
            title = "Профили публикации"
        with Vertical(classes="dialog telegram-analysis-dialog"):
            with VerticalScroll(id="analysis-scroll"):
                yield Static(title, classes="dialog-title")
                yield Static(
                    f"Выбрано: {self.preview.get('selected_count', 0)} · "
                    f"готово: {self.preview.get('ready_count', 0)} · "
                    f"изображений: {self.preview.get('image_count', 0)}. "
                    "Предпросмотр не обращается к Telegram и DeepSeek.",
                    classes="dialog-message",
                )
                if source:
                    channel = (
                        source.get("channel_title")
                        or source.get("title")
                        or source.get("username")
                    )
                    yield Static(
                        f"Публикация: {channel} #{source.get('post_id')}",
                        classes="table-help",
                    )
                yield Static(id="analysis-page-label", classes="table-help")
                yield Static(id="analysis-page-items")
                if (
                    self.single
                    and (self.preview.get("items") or [])
                    and self.image_mode != "photos"
                ):
                    item = self.preview["items"][0]
                    choices = item.get("videos") or []
                    if choices:
                        options = [
                            (
                                f"сообщение {choice.get('message_id')} · "
                                f"{choice.get('message_date') or '—'} · "
                                f"{choice.get('frame_count') or 3} кадра",
                                str(choice["video_id"]),
                            )
                            for choice in choices
                        ]
                        yield Label("Видеокружок (один набор кадров)", classes="field-label")
                        option_nodes = [
                            Option(label, id=str(value)) for label, value in options
                        ]
                        yield OptionList(*option_nodes, id="analysis-video")
                        yield Static(id="analysis-video-preview", classes="table-help")
                    else:
                        yield Static(
                            "Нет готовых наборов кадров; профиль будет пропущен.",
                            classes="dialog-error",
                        )
                    yield Static(
                        "В один запрос будут переданы три кадра одного видео"
                        + (" и одна фотография профиля." if self.image_mode == "combined" else "."),
                        classes="table-help",
                    )
                if (
                    self.single
                    and (self.preview.get("items") or [])
                    and self.image_mode == "photos"
                ):
                    item = self.preview["items"][0]
                    yield Label("Фотографии (до четырёх)", classes="field-label")
                    selected = set(item.get("selected_photo_ids") or [])
                    options = [
                        (
                            f"{photo['photo_id']} · {photo.get('date') or 'без даты'} · "
                            f"{photo.get('size') or 0} байт",
                            str(photo["photo_id"]),
                            str(photo["photo_id"]) in selected,
                        )
                        for photo in item.get("photos") or []
                        if photo.get("available")
                    ]
                    if options:
                        yield SelectionList(*options, id="analysis-photos")
                    else:
                        yield Static(
                            "Доступных локальных фотографий нет.", classes="dialog-error"
                        )
                yield Static(id="analysis-skipped", classes="dialog-error")
                yield Static(
                    "Возрастной допуск 18+ применяется автоматически. Будут отправлены только "
                    "указанные локальные изображения; запрос DeepSeek может быть платным, "
                    "автоматического платного повтора нет.",
                    classes="table-help",
                )
                yield Static(id="analysis-error", classes="dialog-error")
            with Horizontal(classes="analysis-pages"):
                yield Button("← Страница", id="analysis-previous")
                yield Button("Страница →", id="analysis-next")
            with Horizontal(classes="dialog-actions"):
                count = int(
                    self.preview.get("selected_image_count")
                    if self.image_mode != "photos"
                    else self.preview.get("selected_photo_count")
                ) or sum(len(value) for value in self.photo_ids.values())
                noun = "изображение" if count == 1 else "изображения"
                yield Button(
                    f"Отправить {count} {noun} в DeepSeek и запустить анализ",
                    id="analysis-submit",
                    variant="primary",
                )
                yield Button("Отмена", id="analysis-cancel")

    def on_mount(self) -> None:
        self._render_page()
        if self.single and self.query("#analysis-video"):
            item = (self.preview.get("items") or [None])[0]
            if item:
                selected = str(item.get("video_id") or "")
                chooser = self.query_one("#analysis-video", OptionList)
                if selected:
                    index = chooser.get_option_index(selected)
                    if index is not None:
                        chooser.highlighted = index
                self._render_video_preview()

    def _render_video_preview(self) -> None:
        if not self.query("#analysis-video") or not self.query("#analysis-video-preview"):
            return
        chooser = self.query_one("#analysis-video", OptionList)
        if chooser.highlighted is None:
            return
        option = chooser.get_option_at_index(chooser.highlighted)
        video_id = str(option.id or "")
        item = (self.preview.get("items") or [None])[0]
        choice = next(
            (
                candidate
                for candidate in (item or {}).get("videos") or []
                if str(candidate.get("video_id") or "") == video_id
            ),
            None,
        )
        if choice is None:
            return
        frames = " · ".join(
            f"#{frame.get('position')} {float(frame.get('timestamp_seconds') or 0):.2f}s "
            f"({int(frame.get('size') or 0)} байт)"
            for frame in choice.get("frames") or []
        )
        self.query_one("#analysis-video-preview", Static).update(
            f"Будет использовано видео {video_id}: {frames or 'три сохранённых кадра'}"
        )

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        if event.option_list.id == "analysis-video":
            self._render_video_preview()

    def _profile_line(self, item: dict[str, Any]) -> str:
        name = item.get("display_name") or item.get("username") or f"ID {item['author_id']}"
        previous = item.get("previous") or {}
        previous_status = previous.get("result_status") or previous.get("attempt_state")
        suffix = f" · предыдущий: {previous_status}" if previous_status else ""
        if self.image_mode != "photos":
            video = f" · видео {item.get('video_id')}" if item.get("video_id") else ""
            frames = int(item.get("frame_count") or 0)
            images = ", ".join(str(value) for value in item.get("selected_image_ids") or [])
            selected = f" · {images}" if images else ""
            return (
                f"{name} · ID {item['author_id']} · кадры: {frames}{video}{selected}{suffix}"
            )
        photos = ", ".join(str(value) for value in item.get("selected_photo_ids") or [])
        selected = f" · {photos}" if photos else ""
        return (
            f"{name} · ID {item['author_id']} · {item.get('photo_count', 0)} фото"
            f"{selected}{suffix}"
        )

    def _render_page(self) -> None:
        page = self.pages[self.page_offset]
        items = list(page.get("items") or [])
        start = self.page_offset + 1 if items else 0
        end = self.page_offset + len(items)
        self.query_one("#analysis-page-label", Static).update(
            f"Профили {start}–{end} из {page.get('selected_count', 0)}"
        )
        selected_key = (
            "selected_image_ids" if self.image_mode != "photos" else "selected_photo_ids"
        )
        lines = [self._profile_line(item) for item in items if item.get(selected_key)]
        self.query_one("#analysis-page-items", Static).update("\n".join(lines))
        reasons = Counter(
            str(item.get("reason") or "будет пропущен")
            for item in items
            if not item.get(selected_key)
        )
        reason_text = " · ".join(f"{reason}: {count}" for reason, count in reasons.items())
        skipped = int(page.get("skipped_count") or 0)
        self.query_one("#analysis-skipped", Static).update(
            f"Пропущено всего: {skipped}. {reason_text}" if skipped else ""
        )
        self.query_one("#analysis-previous", Button).disabled = self.page_offset <= 0
        self.query_one("#analysis-next", Button).disabled = not bool(page.get("has_more"))

    def _save_single_selection(self) -> bool:
        if self.image_mode != "photos":
            if self.single and self.query("#analysis-video"):
                chooser = self.query_one("#analysis-video", OptionList)
                item = (self.preview.get("items") or [None])[0]
                if item and chooser.highlighted is not None:
                    selected = chooser.get_option_at_index(chooser.highlighted)
                    if selected.id:
                        key = str(item["author_id"])
                        selected_video = str(selected.id)
                        self.video_ids[key] = selected_video
                        if selected_video != str(item.get("video_id") or ""):
                            self.image_snapshots.pop(key, None)
            return True
        if not self.single or not self.query("#analysis-photos"):
            return True
        item = (self.preview.get("items") or [None])[0]
        if not item:
            return True
        selected = [str(value) for value in self.query_one(
            "#analysis-photos", SelectionList
        ).selected]
        if not 1 <= len(selected) <= 4:
            self.query_one("#analysis-error", Static).update("Выбери от 1 до 4 фото.")
            return False
        key = str(item["author_id"])
        self.photo_ids[key] = selected
        if selected != [str(value) for value in item.get("selected_photo_ids") or []]:
            self.photo_snapshots.pop(key, None)
        return True

    async def _load_page(self, offset: int) -> None:
        if offset not in self.pages:
            if self.page_loader is None:
                return
            try:
                page = await self.page_loader(offset)
            except Exception as exc:
                self.query_one("#analysis-error", Static).update(
                    str(exc) or type(exc).__name__
                )
                return
            self.pages[offset] = page
            self._ingest(page)
        self.page_offset = offset
        self._render_page()

    async def _finish(self) -> None:
        if not self.single:
            self.dismiss(
                {
                    "image_mode": self.image_mode,
                    "selection_snapshot": str(
                        self.preview.get("selection_snapshot") or ""
                    ),
                }
            )
            return
        if not self.photo_ids and not self.video_ids:
            self.query_one("#analysis-error", Static).update(
                "Нет подходящих локальных профилей с изображениями."
            )
            return
        self.dismiss(
            {
                "photo_ids": self.photo_ids,
                "photo_snapshots": self.photo_snapshots,
                "image_mode": self.image_mode,
                "video_ids": self.video_ids,
                "image_snapshots": self.image_snapshots,
            }
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        identifier = str(event.button.id)
        if identifier == "analysis-cancel":
            self.dismiss(None)
        elif identifier == "analysis-previous":
            self.run_worker(self._load_page(max(0, self.page_offset - self.page_limit)))
        elif identifier == "analysis-next":
            self.run_worker(self._load_page(self.page_offset + self.page_limit))
        elif identifier == "analysis-submit" and self._save_single_selection():
            event.button.disabled = True
            self.run_worker(self._finish())

    def action_cancel(self) -> None:
        self.dismiss(None)
