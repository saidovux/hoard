"""Read-only, source-scoped Telegram publication workspace."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Button, DataTable, Input, Select, Static

from ..ai_dialogs import VideoFramesDialog
from ..colors import MUTED, TEXT
from ..dialogs import ConfirmDialog
from ..state import format_bytes
from ..widgets import HoardHeader, Panel
from ..widgets.chrome import Hint
from .base import WorkspaceScreen

MODES = ("downloads", "profiles", "frames", "psl")
MODE_LABELS = {
    "downloads": "Кружки",
    "profiles": "Профили",
    "frames": "Кадры",
    "psl": "PSL",
}
FRAME_LABELS = {
    "missing": "Без кадров",
    "pending": "Ожидает",
    "waiting": "Ожидает",
    "queued": "В очереди",
    "running": "Обрабатывается",
    "saved": "Готово",
    "failed": "Ошибка",
    "stale": "Устаревший",
}


def _short_date(value: object) -> str:
    text = str(value or "").replace("T", " ")
    return text[:16] if text else "—"


def _fit_widths(weights: tuple[int, ...], width: int) -> tuple[int, ...]:
    available = max(len(weights), width - len(weights) * 2 - 1)
    total = sum(weights)
    result = [max(1, available * weight // total) for weight in weights]
    result[0] += available - sum(result)
    return tuple(result)


def _frame_label(item: dict[str, Any]) -> str:
    state = str(item.get("job_state") or item.get("frame_status") or "missing")
    return FRAME_LABELS.get(state, state)


class PublicationDetailDialog(ModalScreen[dict[str, Any] | None]):
    """Small read-only card; photo buttons return stable IDs to the screen."""

    BINDINGS = [Binding("escape", "cancel", "Закрыть")]

    def __init__(
        self,
        title: str,
        content: Text | str,
        *,
        photos: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__()
        self.title = title
        self.content = content
        self.photos = photos or []

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog publication-detail-dialog"):
            yield Static(self.title, classes="dialog-title")
            yield Static(self.content, classes="dialog-message")
            if self.photos:
                yield Static("Сохранённые фотографии", classes="table-help")
                for index, photo in enumerate(self.photos):
                    label = f"Открыть фото {photo.get('photo_id') or index + 1}"
                    yield Button(label, id=f"publication-photo-{index}")
            with Horizontal(classes="dialog-actions"):
                yield Button("Закрыть", id="publication-detail-close", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        identifier = str(event.button.id)
        if identifier == "publication-detail-close":
            self.dismiss(None)
        elif identifier.startswith("publication-photo-"):
            index = int(identifier.rsplit("-", 1)[-1])
            self.dismiss({"photo_id": str(self.photos[index]["photo_id"])})

    def action_cancel(self) -> None:
        self.dismiss(None)


class TelegramPublicationScreen(WorkspaceScreen):
    """One contextual screen with four independently paginated local views."""

    section = "telegram_publication"
    PAGE_SIZE = 50
    SEARCH_DEBOUNCE = 0.3
    HINTS: ClassVar[tuple[Hint, ...]] = (
        ("j k", "выбор"),
        ("space", "отметить"),
        ("v x", "видимые / снять"),
        ("⏎", "подробнее"),
        ("h", "к источникам"),
    )
    BINDINGS = [
        Binding("r", "refresh_publication", "Обновить"),
        Binding("space", "toggle_mark", "Выбрать"),
        Binding("v", "select_visible", "Видимые"),
        Binding("x", "clear_marks", "Снять"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.source_id = 0
        self.publication: dict[str, Any] = {}
        self.mode = "downloads"
        self._pages: dict[str, dict[str, Any]] = {
            mode: {"items": [], "total": 0, "offset": 0, "loaded": False, "error": ""}
            for mode in MODES
        }
        self._selected: dict[str, int | str | None] = {mode: None for mode in MODES}
        self._row_ids: list[int | str] = []
        self._table_layout: tuple[str, tuple[int, ...], tuple[int, ...]] | None = None
        self._entry_generation = 0
        self._request_generation = {mode: 0 for mode in MODES}
        self._loading_modes: set[str] = set()
        self._pending_reload: set[str] = set()
        self._profile_query = ""
        self._search_timer: Timer | None = None
        self._marked: dict[str, set[int]] = {"downloads": set(), "profiles": set()}
        self._work_timer: Timer | None = None
        self._work_polling = False
        self._frame_session: dict[str, Any] | None = None
        self._ai_batch: dict[str, Any] | None = None

    def compose_workspace(self) -> ComposeResult:
        with Panel(title="Публикация", id="publication-summary-panel"):
            yield Static("Загрузка локальной публикации…", id="publication-summary")
        with Horizontal(classes="mode-row", id="publication-modes"):
            for mode in MODES:
                yield Button(
                    MODE_LABELS[mode],
                    id=f"publication-mode-{mode}",
                    variant="primary" if mode == self.mode else "default",
                )
        with Panel(title="Содержимое", id="publication-content-panel"):
            yield Input(
                placeholder="Поиск профиля по ID, username или имени",
                id="publication-profile-search",
            )
            yield DataTable(id="publication-table", cursor_type="row")
            with Horizontal(id="publication-pagination"):
                yield Static("0 из 0", id="publication-range")
                yield Button("Назад", id="publication-prev")
                yield Button("Далее", id="publication-next")
                yield Button("Обновить", id="publication-refresh")
        with Panel(title="Выбранный объект", id="publication-object-panel"):
            with Horizontal(classes="action-row", id="publication-actions"):
                yield Button("← Источники", id="publication-back")
                yield Button("Подробнее", id="publication-details", variant="primary")
                yield Button("Открыть видео", id="publication-open-video")
                yield Button("Показать кадры", id="publication-show-frames")
                yield Button("Показать автора", id="publication-show-author")
            yield Static("Выберите объект в таблице.", id="publication-object")
        with Panel(title="Операции", id="publication-operations-panel"):
            with Horizontal(classes="action-row", id="publication-selection-actions"):
                yield Button("Выбрать / снять", id="publication-toggle")
                yield Button("Выбрать видимые", id="publication-select-visible")
                yield Button("Снять все", id="publication-clear-selection")
            with Horizontal(classes="action-row", id="publication-frame-actions"):
                yield Button(
                    "Извлечь выбранные", id="publication-frames-selected", variant="primary"
                )
                yield Button("Обработать публикацию", id="publication-frames-source")
                yield Button("Повторить ошибки", id="publication-frames-retry")
                yield Button("Остановить FFmpeg", id="publication-frames-stop")
            with Horizontal(id="publication-psl-controls"):
                yield Select(
                    (
                        ("Фотографии", "photos"),
                        ("Кадры", "frames"),
                        ("Кадры + фото", "combined"),
                    ),
                    value="photos",
                    allow_blank=False,
                    id="publication-psl-mode",
                )
                yield Button(
                    "Анализировать выбранных",
                    id="publication-psl-selected",
                    variant="primary",
                )
                yield Button("Анализировать публикацию", id="publication-psl-source")
                yield Button("История AI-групп", id="publication-psl-batch")
            yield Static("FFmpeg не запущен.", id="publication-frame-status")
            yield Static("AI-групп публикации пока нет.", id="publication-batch-status")

    def on_mount(self) -> None:
        super().on_mount()
        self._work_timer = self.set_interval(1.5, self._poll_work_state, pause=True)
        self._work_timer.resume()
        self._activate_context()
        self.call_after_refresh(
            lambda: self.focus_working_surface(
                self.query_one("#publication-table", DataTable)
            )
        )

    def on_screen_resume(self) -> None:
        super().on_screen_resume()
        if self._work_timer is not None:
            self._work_timer.resume()
        context = self.hoard_app.publication_source or {}
        if int(context.get("id") or 0) != self.source_id:
            self._activate_context()
        else:
            page = self._pages[self.mode]
            if not page["loaded"] or page["error"] or self.mode in self._pending_reload:
                self._request_page(self.mode, force=True)
            self._request_work_state()

    def on_screen_suspend(self) -> None:
        self._entry_generation += 1
        self._cancel_search_timer()
        if self._work_timer is not None:
            self._work_timer.pause()

    def on_unmount(self) -> None:
        self._entry_generation += 1
        self._cancel_search_timer()
        if self._work_timer is not None:
            self._work_timer.stop()

    def _activate_context(self) -> None:
        context = dict(self.hoard_app.publication_source or {})
        source_id = int(context.get("id") or 0)
        if source_id <= 0:
            self.hoard_app.set_notice("Публикация не выбрана.", error=True)
            self.hoard_app.open_section("telegram_sources")
            return
        changed = source_id != self.source_id
        self.source_id = source_id
        self.publication = context
        self._entry_generation += 1
        if changed:
            self.mode = "downloads"
            self._pending_reload.clear()
            self._pages = {
                mode: {
                    "items": [], "total": 0, "offset": 0, "loaded": False, "error": ""
                }
                for mode in MODES
            }
            self._selected = {mode: None for mode in MODES}
            self._marked = {"downloads": set(), "profiles": set()}
            self._frame_session = None
            self._ai_batch = None
            self._profile_query = ""
            if self.is_mounted:
                self.query_one("#publication-profile-search", Input).value = ""
        self._set_header_detail()
        self._render_publication()
        self._request_header()
        self._request_page(self.mode)
        self._request_work_state()

    def _set_header_detail(self) -> None:
        username = str(self.publication.get("username") or "—").lstrip("@")
        post_id = self.publication.get("post_id") or "—"
        if self.is_mounted:
            self.query_one(HoardHeader).set_detail(f"@{username}/{post_id}")

    def _request_header(self) -> None:
        source_id = self.source_id
        generation = self._entry_generation
        self.run_worker(
            self._load_header(source_id, generation),
            name="publication-header",
            exit_on_error=False,
        )

    async def _load_header(self, source_id: int, generation: int) -> None:
        try:
            result = await self.hoard_app.api.telegram_call(
                "telegram_publication_get", {"source_id": source_id}
            )
        except Exception as exc:
            if self._current(source_id, generation):
                self.hoard_app.set_notice(str(exc) or type(exc).__name__, error=True)
            return
        if self._current(source_id, generation):
            self.publication = dict(result.get("publication") or {})
            self._set_header_detail()
            self._render_publication()

    def _current(self, source_id: int, generation: int) -> bool:
        return (
            self.is_mounted
            and self.app.screen is self
            and self.source_id == source_id
            and self._entry_generation == generation
        )

    def _render_publication(self) -> None:
        if not self.is_mounted:
            return
        source = self.publication
        title = str(source.get("title") or "—")
        username = str(source.get("username") or "—").lstrip("@")
        mode = "Полный сбор" if source.get("collection_mode") == "full" else "Минимальный"
        values = (
            ("Источник", f"@{username} / {source.get('post_id') or '—'}"),
            ("Название", title),
            ("Ссылка", str(source.get("url") or "—")),
            (
                "Состояние",
                ("Включён" if source.get("enabled") else "Отключён") + f" · {mode}",
            ),
            ("Последняя", _short_date(source.get("last_checked_at"))),
        )
        text = Text()
        for index, (label, value) in enumerate(values):
            if index:
                text.append("\n")
            text.append(label.ljust(12), style=MUTED)
            text.append(value, style=TEXT)
        text.append("\n\n")
        totals = (
            ("Кружки", source.get("download_count")),
            ("Профили", source.get("profile_count")),
            ("Кадры", source.get("frame_count")),
            ("PSL", source.get("psl_count")),
        )
        for index, (label, value) in enumerate(totals):
            if index:
                text.append("   ·   ", style=MUTED)
            text.append(f"{label} ", style=MUTED)
            text.append(str(value if value is not None else "—"), style=f"bold {TEXT}")
        self.query_one("#publication-summary", Static).update(text)
        self._render_page()

    def _request_page(self, mode: str, *, force: bool = False) -> None:
        page = self._pages[mode]
        if mode in self._loading_modes:
            self._request_generation[mode] += 1
            self._pending_reload.add(mode)
            page["loaded"] = False
            self._render_pagination()
            return
        if page["loaded"] and not force:
            return
        self._request_generation[mode] += 1
        request_generation = self._request_generation[mode]
        source_id = self.source_id
        entry_generation = self._entry_generation
        offset = int(page["offset"])
        query = self._profile_query if mode == "profiles" else ""
        self._loading_modes.add(mode)
        page["error"] = ""
        self._render_pagination()
        self.run_worker(
            self._load_page(
                mode, source_id, offset, query, entry_generation, request_generation
            ),
            name=f"publication-{mode}",
            exit_on_error=False,
        )

    async def _load_page(
        self,
        mode: str,
        source_id: int,
        offset: int,
        query: str,
        entry_generation: int,
        request_generation: int,
    ) -> None:
        args = {"source_id": source_id, "limit": self.PAGE_SIZE, "offset": offset}
        try:
            if mode == "profiles":
                result = await self.hoard_app.api.ai_call(
                    "ai_telegram_profile_list", {**args, "query": query}
                )
            else:
                method = {
                    "downloads": "telegram_publication_downloads_list",
                    "frames": "telegram_publication_frames_list",
                    "psl": "telegram_publication_psl_list",
                }[mode]
                result = await self.hoard_app.api.telegram_call(method, args)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._page_current(
                mode, source_id, offset, query, entry_generation, request_generation
            ):
                self._pages[mode]["error"] = str(exc) or type(exc).__name__
                self._render_page()
            return
        finally:
            self._loading_modes.discard(mode)
            pending = mode in self._pending_reload
            if pending:
                self._pending_reload.discard(mode)
            if self.is_mounted and self.mode == mode:
                self._render_pagination()
            if (
                pending
                and self.is_mounted
                and self.app.screen is self
                and self.mode == mode
            ):
                self._request_page(mode, force=True)
        if not self._page_current(
            mode, source_id, offset, query, entry_generation, request_generation
        ):
            return
        page = self._pages[mode]
        page.update(
            items=list(result.get("items") or []),
            total=max(0, int(result.get("total") or 0)),
            loaded=True,
            error="",
        )
        selected = self._selected[mode]
        identifiers = [self._item_id(mode, item) for item in page["items"]]
        if selected not in identifiers:
            self._selected[mode] = identifiers[0] if identifiers else None
        self._render_page()

    def _page_current(
        self,
        mode: str,
        source_id: int,
        offset: int,
        query: str,
        entry_generation: int,
        request_generation: int,
    ) -> bool:
        return (
            self._current(source_id, entry_generation)
            and self.mode == mode
            and int(self._pages[mode]["offset"]) == offset
            and (mode != "profiles" or query == self._profile_query)
            and request_generation == self._request_generation[mode]
        )

    @staticmethod
    def _item_id(mode: str, item: dict[str, Any]) -> int | str:
        key = {"downloads": "message_id", "profiles": "author_id", "frames": "message_id",
               "psl": "analysis_id"}[mode]
        value = item.get(key)
        return str(value) if mode == "psl" else int(value or 0)

    def selected_item(self) -> dict[str, Any] | None:
        selected = self._selected[self.mode]
        return next(
            (
                item
                for item in self._pages[self.mode]["items"]
                if self._item_id(self.mode, item) == selected
            ),
            None,
        )

    def _selection_kind(self, mode: str | None = None) -> str | None:
        selected_mode = mode or self.mode
        if selected_mode in {"downloads", "frames"}:
            return "downloads"
        if selected_mode == "profiles":
            return "profiles"
        return None

    def _marked_ids(self, mode: str | None = None) -> set[int]:
        kind = self._selection_kind(mode)
        return self._marked[kind] if kind is not None else set()

    def _layout(self) -> tuple[tuple[str, ...], tuple[int, ...], tuple[int, ...]]:
        compact = self.hoard_app.has_class("compact")
        medium = self.hoard_app.has_class("medium")
        if self.mode == "downloads":
            columns, weights = ("ID", "Автор", "Дата", "Размер", "Кадры"), (8, 18, 13, 9, 11)
            keep = (0, 1, 4) if compact else (0, 1, 2, 4) if medium else tuple(range(5))
        elif self.mode == "profiles":
            columns = ("TG ID", "Username", "Имя", "Фото", "Последнее")
            weights = (12, 15, 18, 6, 13)
            keep = (1, 3) if compact else (0, 1, 3, 4) if medium else tuple(range(5))
        elif self.mode == "frames":
            columns = ("ID", "Автор", "Состояние", "Кадры", "Обновлено")
            weights = (8, 17, 14, 6, 13)
            keep = (0, 2) if compact else (0, 1, 2, 3) if medium else tuple(range(5))
        else:
            columns = ("Автор", "Дата", "Изображения", "AI", "PSL", "Оценка")
            weights = (16, 13, 11, 11, 14, 10)
            keep = (0, 4, 5) if compact else (0, 1, 3, 4, 5) if medium else tuple(range(6))
        return columns, weights, keep

    def _row_cells(self, item: dict[str, Any]) -> tuple[str, ...]:
        if self.mode == "downloads":
            author = item.get("author_username") or item.get("author_name") or item.get("author_id")
            message_id = int(item.get("message_id") or 0)
            marker = "✓ " if message_id in self._marked["downloads"] else "  "
            return (
                marker + str(item.get("message_id") or "—"),
                "@" + str(author).lstrip("@") if author else "—",
                _short_date(item.get("date")),
                format_bytes(item.get("size")),
                _frame_label(item),
            )
        if self.mode == "profiles":
            username = str(item.get("current_username") or "—").lstrip("@")
            author_id = int(item.get("author_id") or 0)
            marker = "✓ " if author_id in self._marked["profiles"] else "  "
            return (
                marker + str(item.get("author_id") or "—"),
                "@" + username,
                str(item.get("display_name") or "—"),
                str(item.get("photo_count") or 0),
                _short_date(item.get("last_seen")),
            )
        if self.mode == "frames":
            author = item.get("author_username") or item.get("author_name") or item.get("author_id")
            message_id = int(item.get("message_id") or 0)
            marker = "✓ " if message_id in self._marked["downloads"] else "  "
            return (
                marker + str(item.get("message_id") or "—"),
                "@" + str(author).lstrip("@") if author else "—",
                _frame_label(item),
                str(item.get("frame_count") or 0),
                _short_date(item.get("updated_at")),
            )
        author = item.get("current_username") or item.get("display_name") or item.get("author_id")
        score = "—"
        if item.get("boosted_psl") is not None:
            score = str(item["boosted_psl"])
        return (
            "@" + str(author).lstrip("@") if author else "—",
            _short_date(item.get("analyzed_at") or item.get("created_at")),
            str(item.get("image_mode") or "—"),
            str(item.get("attempt_state") or "—"),
            str(item.get("result_status") or item.get("failure_category") or "—"),
            score,
        )

    def _render_page(self) -> None:
        if not self.is_mounted:
            return
        table = self.query_one("#publication-table", DataTable)
        columns, weights, keep = self._layout()
        table_width = max(
            16,
            self.hoard_app.size.width
            - (6 if self.hoard_app.has_class("sidebar-hidden") else 28),
        )
        widths = _fit_widths(tuple(weights[index] for index in keep), table_width)
        layout = (self.mode, keep, widths)
        old_row = table.cursor_row
        old_scroll = table.scroll_offset
        if layout != self._table_layout:
            table.clear(columns=True)
            for index, width in zip(keep, widths, strict=True):
                table.add_column(columns[index], width=width)
            self._table_layout = layout
        table.clear()
        self._row_ids = []
        items = list(self._pages[self.mode]["items"])
        for item in items:
            identifier = self._item_id(self.mode, item)
            self._row_ids.append(identifier)
            cells = self._row_cells(item)
            table.add_row(*(cells[index] for index in keep), key=str(identifier))
        if not items:
            loading = self.mode in self._loading_modes
            label = "Загрузка…" if loading else "Данных в публикации нет"
            table.add_row(label, *("—" for _ in keep[1:]))
        selected = self._selected[self.mode]
        if selected in self._row_ids:
            target = self._row_ids.index(selected)
        else:
            target = min(max(old_row, 0), max(0, len(self._row_ids) - 1))
            self._selected[self.mode] = self._row_ids[target] if self._row_ids else None
        if table.row_count:
            table.move_cursor(row=target, scroll=False)
            self.call_after_refresh(
                lambda: table.scroll_to(x=old_scroll.x, y=old_scroll.y, animate=False)
            )
        self.query_one("#publication-profile-search", Input).display = self.mode == "profiles"
        for mode in MODES:
            self.query_one(f"#publication-mode-{mode}", Button).variant = (
                "primary" if mode == self.mode else "default"
            )
        self._render_pagination()
        self._render_object()
        self._render_operations()

    def _render_pagination(self) -> None:
        if not self.is_mounted:
            return
        page = self._pages[self.mode]
        items = page["items"]
        first = int(page["offset"]) + 1 if items else 0
        last = int(page["offset"]) + len(items)
        label = f"{first}–{last} из {page['total']}" if items else f"0 из {page['total']}"
        if page["error"]:
            label += " · ошибка обновления"
        elif self.mode in self._loading_modes:
            label += " · обновляется…"
        marked = self._marked_ids()
        if marked:
            label += f" · выбрано {len(marked)}"
        self.query_one("#publication-range", Static).update(label)
        self.query_one("#publication-prev", Button).disabled = (
            self.mode in self._loading_modes or int(page["offset"]) <= 0
        )
        self.query_one("#publication-next", Button).disabled = (
            self.mode in self._loading_modes or last >= int(page["total"])
        )

    def _render_operations(self) -> None:
        if not self.is_mounted:
            return
        selection_kind = self._selection_kind()
        marked = self._marked_ids()
        self.query_one("#publication-selection-actions", Horizontal).display = (
            selection_kind is not None
        )
        self.query_one("#publication-frame-actions", Horizontal).display = self.mode in {
            "downloads",
            "frames",
        }
        self.query_one("#publication-psl-controls", Horizontal).display = self.mode in {
            "profiles",
            "psl",
        }
        self.query_one("#publication-toggle", Button).disabled = (
            selection_kind is None or self.selected_item() is None
        )
        self.query_one("#publication-select-visible", Button).disabled = (
            selection_kind is None or not self._row_ids
        )
        self.query_one("#publication-clear-selection", Button).disabled = not marked
        self.query_one("#publication-frames-selected", Button).disabled = not self._marked[
            "downloads"
        ]
        self.query_one("#publication-psl-selected", Button).display = self.mode == "profiles"
        self.query_one("#publication-psl-selected", Button).disabled = not self._marked[
            "profiles"
        ]
        active = bool(
            self._frame_session
            and str(self._frame_session.get("state") or "") == "running"
            and int(self._frame_session.get("source_id") or 0) == self.source_id
        )
        self.query_one("#publication-frames-stop", Button).disabled = not active

    def _render_object(self) -> None:
        item = self.selected_item()
        for identifier in (
            "publication-details", "publication-open-video", "publication-show-frames",
            "publication-show-author",
        ):
            self.query_one(f"#{identifier}", Button).disabled = item is None
        self.query_one("#publication-open-video", Button).display = self.mode == "downloads"
        self.query_one("#publication-show-frames", Button).display = self.mode in {
            "downloads", "frames"
        }
        self.query_one("#publication-show-author", Button).display = self.mode in {
            "downloads", "frames"
        }
        if item is None:
            message = self._pages[self.mode]["error"] or "Выберите объект в таблице."
            self.query_one("#publication-object", Static).update(message)
            return
        cells = self._row_cells(item)
        labels = self._layout()[0]
        text = Text()
        for index, (label, value) in enumerate(zip(labels, cells, strict=True)):
            if index:
                text.append("\n")
            text.append(label.ljust(15), style=MUTED)
            text.append(value, style=TEXT)
        self.query_one("#publication-object", Static).update(text)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "publication-table":
            return
        row = event.data_table.cursor_row
        self._selected[self.mode] = self._row_ids[row] if 0 <= row < len(self._row_ids) else None
        self._render_object()
        self._render_operations()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "publication-table" and self.selected_item() is not None:
            self.open_selected()

    def open_selected(self) -> None:
        if self.selected_item() is not None:
            self._show_details()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        identifier = str(event.button.id)
        if identifier.startswith("publication-mode-"):
            self._switch_mode(identifier.removeprefix("publication-mode-"))
        elif identifier == "publication-back":
            self.hoard_app.open_section("telegram_sources")
        elif identifier == "publication-prev":
            self._move_page(-1)
        elif identifier == "publication-next":
            self._move_page(1)
        elif identifier == "publication-refresh":
            self.action_refresh_publication()
        elif identifier == "publication-details":
            self._show_details()
        elif identifier == "publication-open-video":
            self._open_video()
        elif identifier == "publication-show-frames":
            self._show_frames()
        elif identifier == "publication-show-author":
            self._show_author()
        elif identifier == "publication-toggle":
            self.action_toggle_mark()
        elif identifier == "publication-select-visible":
            self.action_select_visible()
        elif identifier == "publication-clear-selection":
            self.action_clear_marks()
        elif identifier == "publication-frames-selected":
            self._confirm_frames_selected()
        elif identifier == "publication-frames-source":
            self._confirm_frames_source()
        elif identifier == "publication-frames-retry":
            self._confirm_frames_retry()
        elif identifier == "publication-frames-stop":
            session = self._frame_session or {}
            self._run_frame_action(
                "telegram_frames_backfill_pause",
                {
                    "expected_session_id": str(session.get("session_id") or ""),
                    "expected_source_id": self.source_id,
                },
            )
        elif identifier == "publication-psl-selected":
            self._analyze_selected_profiles()
        elif identifier == "publication-psl-source":
            self._analyze_publication()
        elif identifier == "publication-psl-batch":
            self._open_last_batch()

    def action_toggle_mark(self) -> None:
        kind = self._selection_kind()
        item = self.selected_item()
        if kind is None or item is None:
            return
        identifier = int(item["message_id"] if kind == "downloads" else item["author_id"])
        selected = self._marked[kind]
        if identifier in selected:
            selected.remove(identifier)
        elif len(selected) >= 100:
            self.hoard_app.set_notice(
                "За одну операцию можно выбрать не более 100 объектов.", error=True
            )
            return
        else:
            selected.add(identifier)
        self._render_page()

    def action_select_visible(self) -> None:
        kind = self._selection_kind()
        if kind is None:
            return
        visible = {int(value) for value in self._row_ids if int(value) > 0}
        combined = self._marked[kind].union(visible)
        if len(combined) > 100:
            self.hoard_app.set_notice(
                "Выбор видимой страницы превысит предел 100 объектов.", error=True
            )
            return
        self._marked[kind] = combined
        self._render_page()

    def action_clear_marks(self) -> None:
        kind = self._selection_kind()
        if kind is None:
            return
        self._marked[kind].clear()
        self._render_page()

    def _confirm_frames_selected(self) -> None:
        message_ids = sorted(self._marked["downloads"])
        if not message_ids:
            self.hoard_app.set_notice("Сначала выбери видеокружки.", error=True)
            return
        source_id = self.source_id
        self.app.push_screen(
            ConfirmDialog(
                "Извлечь кадры выбранных кружков?",
                f"Будет обработано только выбранных кружков: {len(message_ids)}. "
                "FFmpeg работает последовательно и только при остановленном Telegram. "
                "Закрытие TUI не останавливает начатую daemon-сессию.",
                confirm_label="Запустить FFmpeg",
            ),
            lambda confirmed: self._run_frame_action(
                "telegram_frames_selected_start",
                {
                    "source_id": source_id,
                    "message_ids": message_ids,
                    "confirmed": True,
                },
            )
            if confirmed
            else None,
        )

    def _confirm_frames_source(self) -> None:
        source_id = self.source_id
        estimate = int(self.publication.get("download_count") or 0)
        self.app.push_screen(
            ConfirmDialog(
                "Обработать текущую публикацию?",
                f"Daemon проверит до {estimate} сохранённых кружков этой публикации и "
                "поставит не более восьми frame-jobs одновременно. Другие публикации "
                "не будут обработаны.",
                confirm_label="Запустить FFmpeg",
            ),
            lambda confirmed: self._run_frame_action(
                "telegram_frames_source_start",
                {"source_id": source_id, "confirmed": True},
            )
            if confirmed
            else None,
        )

    def _confirm_frames_retry(self) -> None:
        source_id = self.source_id
        self.app.push_screen(
            ConfirmDialog(
                "Повторить ошибки этой публикации?",
                "Будут повторно поставлены только failed/stale наборы текущей "
                "публикации, максимум 100 за один явный запуск.",
                confirm_label="Повторить через FFmpeg",
            ),
            lambda confirmed: self._run_frame_action(
                "telegram_frames_source_retry_failed",
                {"source_id": source_id, "confirmed": True},
            )
            if confirmed
            else None,
        )

    def _run_frame_action(self, method: str, args: dict[str, Any]) -> None:
        self.run_worker(
            self._perform_frame_action(method, dict(args)),
            name=f"publication-{method}",
            exit_on_error=False,
        )

    async def _perform_frame_action(self, method: str, args: dict[str, Any]) -> None:
        try:
            result = await self.hoard_app.api.telegram_call(method, args)
        except Exception as exc:
            self.hoard_app.set_notice(str(exc) or type(exc).__name__, error=True)
            return
        self.hoard_app.set_notice(str(result.get("message") or "Готово"))
        session = result.get("session")
        if isinstance(session, dict):
            self._frame_session = session
        self._pages["downloads"]["loaded"] = False
        self._pages["frames"]["loaded"] = False
        if self.mode in {"downloads", "frames"}:
            self._request_page(self.mode, force=True)
        self._request_work_state()

    def _selected_image_mode(self) -> str:
        value = self.query_one("#publication-psl-mode", Select).value
        return str(value) if value in {"photos", "frames", "combined"} else "photos"

    def _analyze_selected_profiles(self) -> None:
        author_ids = sorted(self._marked["profiles"])
        if not author_ids:
            self.hoard_app.set_notice("Сначала выбери профили.", error=True)
            return
        self.hoard_app.open_telegram_analysis(
            {
                "source_id": self.source_id,
                "author_ids": author_ids,
                "image_mode": self._selected_image_mode(),
            }
        )

    def _analyze_publication(self) -> None:
        self.hoard_app.open_telegram_analysis(
            {"source_id": self.source_id, "image_mode": self._selected_image_mode()}
        )

    def _open_last_batch(self) -> None:
        self.hoard_app.open_telegram_batch_history(
            source_id=self.source_id,
            title=f"AI-группы · {self.publication.get('title') or self.source_id}",
        )

    def _switch_mode(self, mode: str) -> None:
        if mode not in MODES or mode == self.mode:
            return
        self.mode = mode
        self._table_layout = None
        self._render_page()
        self._request_page(mode)

    def _move_page(self, direction: int) -> None:
        page = self._pages[self.mode]
        target = max(0, int(page["offset"]) + direction * self.PAGE_SIZE)
        if target == int(page["offset"]):
            return
        page.update(offset=target, loaded=False, items=[])
        self._selected[self.mode] = None
        self._render_page()
        self._request_page(self.mode)

    def action_refresh_publication(self) -> None:
        self._pages[self.mode]["loaded"] = False
        self._request_header()
        self._request_page(self.mode, force=True)
        self._request_work_state()

    def _poll_work_state(self) -> None:
        self._request_work_state()

    def _request_work_state(self) -> None:
        if (
            self._work_polling
            or self.source_id <= 0
            or not self.is_mounted
            or self.app.screen is not self
        ):
            return
        self._work_polling = True
        source_id = self.source_id
        generation = self._entry_generation
        self.run_worker(
            self._load_work_state(source_id, generation),
            name="publication-work-state",
            exit_on_error=False,
        )

    async def _load_work_state(self, source_id: int, generation: int) -> None:
        try:
            frame_result, batches_result = await asyncio.gather(
                self.hoard_app.api.telegram_call(
                    "telegram_frames_session_status", {"source_id": source_id}
                ),
                self.hoard_app.api.ai_call(
                    "ai_telegram_batch_catalog",
                    {"source_id": source_id, "limit": 1, "offset": 0},
                ),
                return_exceptions=True,
            )
        finally:
            self._work_polling = False
        if not self._current(source_id, generation):
            return
        if isinstance(frame_result, dict):
            session = frame_result.get("session")
            self._frame_session = session if isinstance(session, dict) else None
        if isinstance(batches_result, dict):
            items = list(batches_result.get("items") or [])
            self._ai_batch = dict(items[0]) if items else None
        self._render_work_status()
        self._render_operations()

    def _render_work_status(self) -> None:
        if not self.is_mounted:
            return
        session = self._frame_session
        if session:
            mode = {
                "single": "один кружок",
                "selected": "выбранные",
                "retry_source": "повтор ошибок",
                "source": "публикация",
                "bulk": "архив",
            }.get(str(session.get("session_type") or ""), "ручная сессия")
            source = session.get("source_id")
            scope = "эта публикация" if source == self.source_id else f"источник {source or '—'}"
            completed = int(session.get("completed") or 0)
            errors = int(session.get("errors") or 0)
            successful = int(session.get("successful") or max(0, completed - errors))
            frame_text = (
                f"FFmpeg · {mode} · {scope} · {session.get('state') or '—'}\n"
                f"Завершено {completed} из "
                f"{session.get('selected_count', 0)} · ожидает "
                f"{int(session.get('waiting', 0)) + int(session.get('queued', 0))} · "
                f"в работе {session.get('running', 0)} · успешно {successful} · "
                f"ошибки {errors}"
            )
            if source != self.source_id and str(session.get("state") or "") == "running":
                frame_text += "\nЭтой сессией можно управлять в Статус → Обслуживание."
            if session.get("current_message_id"):
                frame_text += f" · текущий {session['current_message_id']}"
        else:
            frame_text = "FFmpeg не запущен. Обработка начинается только после подтверждения."
        self.query_one("#publication-frame-status", Static).update(frame_text)

        batch = self._ai_batch
        if batch:
            batch_text = (
                f"AI-группа {batch.get('id')} · режим {batch.get('image_mode') or 'photos'}\n"
                f"Всего {batch.get('selected_count', 0)} · очередь {batch.get('queued', 0)} · "
                f"готово {batch.get('completed', 0)} · пропущено {batch.get('skipped', 0)} · "
                f"ошибки {batch.get('errors', 0)}"
            )
        else:
            batch_text = "AI-групп публикации пока нет. Preview и отправка запускаются явно."
        self.query_one("#publication-batch-status", Static).update(batch_text)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "publication-profile-search":
            return
        self._cancel_search_timer()
        self._search_timer = self.set_timer(self.SEARCH_DEBOUNCE, self._apply_profile_search)

    def _cancel_search_timer(self) -> None:
        if self._search_timer is not None:
            self._search_timer.stop()
            self._search_timer = None

    def _apply_profile_search(self) -> None:
        self._search_timer = None
        query = self.query_one("#publication-profile-search", Input).value.strip()
        if query == self._profile_query:
            return
        self._profile_query = query
        self._pages["profiles"].update(offset=0, loaded=False, items=[])
        self._selected["profiles"] = None
        if self.mode == "profiles":
            self._render_page()
            self._request_page("profiles")

    def _show_details(self) -> None:
        item = self.selected_item()
        if item is None:
            return
        if self.mode == "downloads":
            self.run_worker(
                self._load_download_details(int(item["message_id"])),
                name="publication-download-details",
                exit_on_error=False,
            )
        elif self.mode == "frames":
            self.app.push_screen(
                PublicationDetailDialog(
                    f"Видеокружок {item.get('message_id')}",
                    self._download_details(item),
                )
            )
        elif self.mode == "profiles":
            self.run_worker(self._load_profile_details(int(item["author_id"])))
        else:
            self.run_worker(self._load_psl_details(str(item["analysis_id"])))

    async def _load_download_details(self, message_id: int) -> None:
        source_id, generation = self.source_id, self._entry_generation
        try:
            result = await self.hoard_app.api.telegram_call(
                "telegram_publication_download_get",
                {"source_id": source_id, "message_id": message_id},
            )
        except Exception as exc:
            if self._current(source_id, generation):
                self.hoard_app.set_notice(str(exc) or type(exc).__name__, error=True)
            return
        if self._current(source_id, generation):
            self.app.push_screen(
                PublicationDetailDialog(
                    f"Видеокружок {message_id}",
                    self._download_details(dict(result["download"])),
                )
            )

    @staticmethod
    def _download_details(item: dict[str, Any]) -> Text:
        values = (
            ("Message ID", item.get("message_id")),
            ("Автор ID", item.get("author_id")),
            ("Username", item.get("author_username") or "—"),
            ("Имя", item.get("author_name") or "—"),
            ("Дата", _short_date(item.get("date"))),
            ("Размер", format_bytes(item.get("size"))),
            ("Ссылка", item.get("media_url") or "—"),
            (
                "Локальный MP4",
                "доступен" if item.get("available") else item.get("availability_error") or "—",
            ),
            ("FFmpeg", _frame_label(item)),
        )
        text = Text()
        for index, (label, value) in enumerate(values):
            if index:
                text.append("\n")
            text.append(str(label).ljust(14), style=MUTED)
            text.append(str(value if value is not None else "—"), style=TEXT)
        return text

    async def _load_profile_details(self, author_id: int) -> None:
        source_id, generation = self.source_id, self._entry_generation
        try:
            result = await self.hoard_app.api.telegram_call(
                "telegram_publication_profile_get",
                {"source_id": source_id, "author_id": author_id},
            )
        except Exception as exc:
            if self._current(source_id, generation):
                self.hoard_app.set_notice(str(exc) or type(exc).__name__, error=True)
            return
        if not self._current(source_id, generation):
            return
        profile = dict(result["profile"])
        history = ", ".join(
            "@" + str(item.get("username") or "").lstrip("@")
            for item in profile.get("username_history") or []
        ) or "—"
        psl = profile.get("psl") or {}
        values = (
            ("Telegram ID", profile.get("author_id")),
            ("Username", "@" + str(profile.get("current_username") or "—").lstrip("@")),
            ("Имя", profile.get("display_name") or "—"),
            ("История", history),
            ("Первое", _short_date(profile.get("first_seen"))),
            ("Последнее", _short_date(profile.get("last_seen"))),
            ("Кружки здесь", profile.get("source_video_count") or 0),
            ("PSL здесь", psl.get("result_status") or psl.get("failure_category") or "—"),
        )
        text = Text()
        for index, (label, value) in enumerate(values):
            if index:
                text.append("\n")
            text.append(label.ljust(15), style=MUTED)
            text.append(str(value), style=TEXT)
        self.app.push_screen(
            PublicationDetailDialog(
                "Профиль из локального архива",
                text,
                photos=list(profile.get("photos") or []),
            ),
            lambda response: self._open_photo(author_id, response),
        )

    async def _load_psl_details(self, analysis_id: str) -> None:
        source_id, generation = self.source_id, self._entry_generation
        try:
            result = await self.hoard_app.api.telegram_call(
                "telegram_publication_psl_get",
                {"source_id": source_id, "analysis_id": analysis_id},
            )
        except Exception as exc:
            if self._current(source_id, generation):
                self.hoard_app.set_notice(str(exc) or type(exc).__name__, error=True)
            return
        if not self._current(source_id, generation):
            return
        item = dict(result["analysis"])
        score = "—"
        if item.get("boosted_psl") is not None:
            score = (
                f"{item.get('raw_psl_min')}–{item.get('raw_psl_max')} · "
                f"boosted {item.get('boosted_psl')}"
            )
        values = (
            ("Анализ", item.get("analysis_id")),
            ("Автор", item.get("author_id")),
            ("Дата", _short_date(item.get("analyzed_at") or item.get("created_at"))),
            ("Изображения", item.get("image_mode") or "—"),
            ("Типы", ", ".join(item.get("image_types") or []) or "—"),
            ("Привязка", item.get("attribution") or "—"),
            ("AI", item.get("attempt_state") or "—"),
            ("PSL", item.get("result_status") or "—"),
            ("Ошибка", item.get("failure_category") or "—"),
            ("Оценка", score),
        )
        text = Text()
        for index, (label, value) in enumerate(values):
            if index:
                text.append("\n")
            text.append(label.ljust(14), style=MUTED)
            text.append(str(value), style=TEXT)
        self.app.push_screen(PublicationDetailDialog("PSL · сохранённый результат", text))

    def _open_video(self) -> None:
        item = self.selected_item()
        if item is not None:
            self._open_media("video", message_id=int(item["message_id"]))

    def _open_photo(self, author_id: int, response: dict[str, Any] | None) -> None:
        if response and response.get("photo_id"):
            self._open_media(
                "photo", author_id=author_id, photo_id=str(response["photo_id"])
            )

    def _open_media(
        self, kind: str, *, publication_id: int | None = None, **identifiers: Any
    ) -> None:
        source_id = int(publication_id or self.source_id)
        self.run_worker(
            self._perform_media_open(source_id, kind, dict(identifiers)),
            name=f"publication-open-{kind}",
            exit_on_error=False,
        )

    async def _perform_media_open(
        self, source_id: int, kind: str, identifiers: dict[str, Any]
    ) -> None:
        try:
            result = await self.hoard_app.api.telegram_call(
                "telegram_publication_media_open",
                {"source_id": source_id, "kind": kind, **identifiers},
            )
        except Exception as exc:
            self.hoard_app.set_notice(str(exc) or type(exc).__name__, error=True)
        else:
            self.hoard_app.set_notice(str(result.get("message") or "Файл открыт"))

    def _show_frames(self) -> None:
        item = self.selected_item()
        if item is None:
            return
        message_id = int(item.get("message_id") or 0)
        self.run_worker(
            self._load_frames(message_id), name="publication-frame-details", exit_on_error=False
        )

    async def _load_frames(self, message_id: int) -> None:
        source_id, generation = self.source_id, self._entry_generation
        try:
            result = await self.hoard_app.api.telegram_call(
                "telegram_frames_get",
                {"source_id": source_id, "message_id": message_id},
            )
        except Exception as exc:
            if self._current(source_id, generation):
                self.hoard_app.set_notice(str(exc) or type(exc).__name__, error=True)
            return
        if not self._current(source_id, generation):
            return
        details = result.get("frames")
        if not details:
            self.hoard_app.set_notice("Для кружка нет сохранённого набора кадров.", error=True)
            return
        self.app.push_screen(
            VideoFramesDialog(
                details,
                video_id=f"{source_id}:{message_id}",
                view_only=True,
                open_frame=lambda position: self._open_media(
                    "frame",
                    publication_id=source_id,
                    message_id=message_id,
                    position=position,
                ),
            )
        )

    def _show_author(self) -> None:
        item = self.selected_item()
        author_id = int((item or {}).get("author_id") or 0)
        if author_id <= 0:
            self.hoard_app.set_notice("Автор кружка не определён.", error=True)
            return
        search = self.query_one("#publication-profile-search", Input)
        search.value = str(author_id)
        self._profile_query = str(author_id)
        self._pages["profiles"].update(offset=0, loaded=False, items=[])
        self._selected["profiles"] = None
        self._switch_mode("profiles")

    def on_app_resize(self) -> None:
        self._table_layout = None
        self._render_page()


__all__ = ["TelegramPublicationScreen"]
