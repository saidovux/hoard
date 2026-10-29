"""The four pages of the Telegram workspace."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.timer import Timer
from textual.widgets import Button, DataTable, Input, Select, Static

from ..colors import ACCENT, MUTED, TEXT
from ..dialogs import ConfirmDialog, SourceDialog
from ..state import format_bytes
from ..widgets import Panel, Sidebar
from ..widgets.chrome import Hint
from .base import WorkspaceScreen

SOURCE_COLUMNS = (
    "Источник",
    "Состояние",
    "Кружки",
    "Профили",
    "FFmpeg",
    "PSL",
    "Проверка",
)
DENSE_SOURCE_COLUMNS = (0, 2, 3, 1)
COMPACT_SOURCE_COLUMNS = (0, 2)
SOURCE_HINTS: tuple[Hint, ...] = (
    ("a", "добавить"),
    ("e", "изменить"),
    ("s", "проверить"),
    ("r", "обновить"),
)


def _short_time(value: object) -> str:
    text = str(value or "")
    return text[:16] if text else "ещё не проверялся"


def _source_widths(columns: tuple[int, ...], table_width: int) -> tuple[int, ...]:
    """Fixed widths keep long usernames/errors from creating horizontal scrolling."""
    count = len(columns)
    available = max(count * 4, table_width - count * 2 - 1)
    if count == 7:
        state, downloads, profiles, frames, psl, checked = 9, 7, 7, 7, 5, 16
        source = max(
            8,
            available - state - downloads - profiles - frames - psl - checked,
        )
        return source, state, downloads, profiles, frames, psl, checked
    if count == 4:
        downloads, profiles, state = 7, 7, 9
        source = max(8, available - downloads - profiles - state)
        return source, downloads, profiles, state
    downloads = 7
    source = max(7, available - downloads)
    return source, downloads


class _TelegramWorkspaceScreen(WorkspaceScreen):
    HINTS: ClassVar[tuple[Hint, ...]] = (("tab", "панели"), ("[ ]", "раздел"), ("h", "назад"))


class _CollectorControls:
    """Existing collector lifecycle shared only by the status page."""

    def _start(self: TelegramStatusScreen) -> None:
        if str((self.hoard_app.status or {}).get("telegram") or "") != "authorized":
            self.hoard_app.set_notice("Сначала подключи Telegram на странице «Вход».")
            self.hoard_app.open_section("telegram_login")
            return
        self._control(
            "collector-start",
            self.hoard_app.api.start_collector,
            "Запускаем сборщик…",
        )

    def _pause(self: TelegramStatusScreen) -> None:
        self._confirm_interrupt("pause")

    def _stop(self: TelegramStatusScreen) -> None:
        self._confirm_interrupt("stop")

    def _confirm_interrupt(self: TelegramStatusScreen, operation: str) -> None:
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

    def _run_interrupt(self: TelegramStatusScreen, operation: str) -> None:
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
        self: TelegramStatusScreen,
        key: str,
        operation: Callable[[], Awaitable[dict[str, Any]]],
        pending: str,
    ) -> None:
        self.run_worker(
            self.hoard_app.execute(key, operation, pending=pending),
            name=key,
            exit_on_error=False,
        )


class TelegramStatusScreen(_CollectorControls, _TelegramWorkspaceScreen):
    section = "telegram_status"
    POLL_INTERVAL = 1.5

    def __init__(self) -> None:
        super().__init__()
        self.poll_timer: Timer | None = None
        self._status_error: str | None = None
        self._statistics: dict[str, Any] | None = None
        self._statistics_error: str | None = None
        self._statistics_updated_at = ""
        self._statistics_loading = False
        self._statistics_request_active = False
        self._statistics_request_generation = -1
        self._statistics_pending_generation: int | None = None
        self._entry_generation = 0

    def compose_workspace(self) -> ComposeResult:
        with Panel(title="Текущая работа", id="collector-panel"):
            yield Static(id="telegram-current")
            with Horizontal(classes="action-row", id="telegram-actions"):
                yield Button("Запустить", id="collector-start", variant="primary")
                yield Button("Пауза", id="collector-pause")
                yield Button("Остановить", id="collector-stop")
                yield Button("Перейти ко входу", id="telegram-login")
        with Panel(title="Статистика Telegram", id="telegram-statistics-panel"):
            with Horizontal(classes="action-row", id="telegram-statistics-actions"):
                yield Button("Обновить", id="telegram-statistics-refresh", variant="primary")
                yield Button("Обслуживание", id="telegram-maintenance")
            yield Static(id="telegram-statistics")

    def on_screen_resume(self) -> None:
        super().on_screen_resume()
        self._entry_generation += 1
        if self.poll_timer is None:
            self.poll_timer = self.set_interval(self.POLL_INTERVAL, self._poll)
        self.run_worker(self._poll(), name="telegram-status-live", exit_on_error=False)
        self._request_statistics()

    def on_screen_suspend(self) -> None:
        self._entry_generation += 1
        self.stop_polling()

    def on_unmount(self) -> None:
        self._entry_generation += 1
        self.stop_polling()

    async def _poll(self) -> None:
        if self.is_mounted and self.app.screen is self:
            await self.hoard_app.refresh_status(brief=True, render=False)
            if self.is_mounted and self.app.screen is self:
                self.render_status(self.hoard_app.status, self.hoard_app.status_error)

    def stop_polling(self) -> None:
        if self.poll_timer is not None:
            self.poll_timer.stop()
            self.poll_timer = None

    def render_status(self, status: dict | None, error: str | None = None) -> None:
        self._status_error = error
        super().render_status(status, error)

    @staticmethod
    def _count(value: object) -> str:
        if isinstance(value, bool):
            return "—"
        try:
            number = max(0, int(value))  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError):
            return "—"
        return f"{number:,}".replace(",", " ")

    @staticmethod
    def _collector_state(status: dict, error: str | None) -> str:
        if error:
            return "Служба недоступна"
        account = status.get("telegram_account") or {}
        if (
            str(status.get("telegram") or "") != "authorized"
            or str(account.get("state") or "") != "connected"
        ):
            return "Требуется подключение"
        state = str(status.get("collector_state") or "")
        if state == "paused":
            return "На паузе"
        if state == "stopped" or status.get("running") is False:
            return "Остановлен"
        if state == "running" and status.get("running") is True:
            return "Работает"
        return "Состояние неизвестно"

    @staticmethod
    def _stage(status: dict) -> str:
        if status.get("running") is not True:
            return "—"
        progress = status.get("progress") or {}
        try:
            active = int(progress.get("active_downloads") or 0)
        except (TypeError, ValueError):
            active = 0
        if active > 0:
            return "Скачивание медиа"
        if progress:
            return "Проверка публикации"
        return "Ожидание"

    def render_data(self, status: dict) -> None:
        account = status.get("telegram_account") or {}
        account_state = str(account.get("state") or "unknown")
        progress = status.get("progress") or {}
        summary = Text()
        source = str(progress.get("source") or "—")
        remaining = self._count(progress.get("remaining"))
        speed = format_bytes(progress.get("aggregate_speed_bps"))
        if speed != "—":
            speed += "/s"
        values = (
            ("Источник", source),
            ("Сборщик", self._collector_state(status, self._status_error)),
            ("Этап", self._stage(status)),
            ("Проверено", self._count(progress.get("checked"))),
            ("Скачано", self._count(progress.get("downloaded")) + " новых кружков"),
            ("Осталось", remaining),
            ("Загрузки", self._count(progress.get("active_downloads")) + " активных"),
            ("Скорость", speed),
        )
        for index, (label, value) in enumerate(values):
            if index:
                summary.append("\n")
            summary.append(label.ljust(14), style=MUTED)
            summary.append(value, style=TEXT)
        self.query_one("#telegram-current", Static).update(summary)

        running = bool(status.get("running"))
        collector_state = str(status.get("collector_state") or "stopped")
        unavailable = self._status_error is not None
        self.query_one("#collector-start", Button).label = (
            "Возобновить" if collector_state == "paused" else "Запустить"
        )
        self.query_one("#collector-start", Button).disabled = unavailable or running
        self.query_one("#collector-pause", Button).disabled = unavailable or not running
        self.query_one("#collector-stop", Button).disabled = (
            unavailable or (not running and collector_state == "stopped")
        )
        self.query_one("#telegram-login", Button).display = account_state != "connected"
        self.query_one("#telegram-login", Button).disabled = unavailable

    def _request_statistics(self) -> None:
        generation = self._entry_generation
        if self._statistics_request_active:
            if self._statistics_request_generation != generation:
                self._statistics_pending_generation = generation
                self._statistics_loading = True
                self._render_statistics()
            return
        self._statistics_request_active = True
        self._statistics_request_generation = generation
        self._statistics_loading = True
        self._statistics_error = None
        self._render_statistics()
        self.run_worker(
            self._load_statistics(generation),
            name="telegram-statistics",
            exit_on_error=False,
        )

    async def _load_statistics(self, generation: int) -> None:
        try:
            result = await self.hoard_app.api.telegram_statistics()
        except Exception as exc:
            if generation == self._entry_generation and self.app.screen is self:
                self._statistics_error = str(exc) or type(exc).__name__
        else:
            if generation == self._entry_generation and self.app.screen is self:
                self._statistics = dict(result)
                self._statistics_error = None
                self._statistics_updated_at = datetime.now().strftime("%H:%M:%S")
        finally:
            self._statistics_request_active = False
            if generation == self._entry_generation and self.app.screen is self:
                self._statistics_loading = False
                self._render_statistics()
            pending = self._statistics_pending_generation
            self._statistics_pending_generation = None
            if (
                pending == self._entry_generation
                and self.is_mounted
                and self.app.screen is self
            ):
                self._request_statistics()

    def _render_statistics(self) -> None:
        if not self.is_mounted:
            return
        refresh = self.query_one("#telegram-statistics-refresh", Button)
        refresh.disabled = self._statistics_loading
        view = self.query_one("#telegram-statistics", Static)
        if self._statistics is None:
            message = (
                "Загрузка статистики…"
                if self._statistics_loading
                else "Статистика недоступна"
            )
            if self._statistics_error:
                message += f"\n{self._statistics_error}"
            view.update(Text(message, style=ACCENT if self._statistics_error else MUTED))
            return

        text = Text()
        groups = (
            (
                "ИСТОЧНИКИ",
                self._statistics.get("sources") or {},
                (("Всего", "total"), ("Включено", "enabled"), ("Отключено", "disabled")),
            ),
            (
                "МЕДИА",
                self._statistics.get("media") or {},
                (
                    ("Сохранённые кружки", "video_notes"),
                    ("Фото публикаций", "post_images"),
                    ("Видео публикаций", "post_videos"),
                    ("Другие файлы", "post_files"),
                ),
            ),
            (
                "ПРОФИЛИ",
                {
                    **(self._statistics.get("profiles") or {}),
                    "profile_photos": (self._statistics.get("media") or {}).get(
                        "profile_photos"
                    ),
                },
                (("Уникальных", "unique"), ("Сохранено фото", "profile_photos")),
            ),
            (
                "КАДРЫ FFMPEG",
                self._statistics.get("frames") or {},
                (
                    ("Обработано кружков", "processed_videos"),
                    ("Создано кадров", "created_frames"),
                    ("Ожидают запуска", "waiting"),
                    ("В очереди", "queued"),
                    ("Обрабатывается", "processing"),
                    ("Без кадров", "missing"),
                    ("Ошибки", "failed"),
                    ("Устаревшие наборы", "stale"),
                ),
            ),
            (
                "PSL",
                self._statistics.get("psl") or {},
                (
                    ("Профилей", "unique_profiles"),
                    ("Успешных анализов", "completed_analyses"),
                    ("Технических ошибок", "failed_analyses"),
                    ("Исход неизвестен", "unknown_outcome"),
                    ("Без результата", "without_result"),
                    ("В очереди", "queued"),
                    ("Выполняется", "running"),
                ),
            ),
        )
        for group_index, (title, data, rows) in enumerate(groups):
            if group_index:
                text.append("\n\n")
            text.append(title, style=f"bold {MUTED}")
            for label, key in rows:
                text.append("\n" + label.ljust(22), style=MUTED)
                text.append(self._count(data.get(key)), style=TEXT)
        frames = self._statistics.get("frames") or {}
        text.append("\n" + "FFmpeg".ljust(22), style=MUTED)
        text.append(
            "работает" if frames.get("active") else "остановлен",
            style=TEXT,
        )
        mode = {
            "off": "выключен",
            "single": "один кружок",
            "bulk": "архив",
            "stopping": "останавливается",
        }.get(str(frames.get("processing_mode") or "off"), "неизвестен")
        text.append("\n" + "Режим".ljust(22), style=MUTED)
        text.append(mode, style=TEXT)
        if self._statistics_updated_at:
            text.append(f"\n\nОбновлено: {self._statistics_updated_at}", style=MUTED)
        if self._statistics_loading:
            text.append(" · обновляется…", style=MUTED)
        if self._statistics_error:
            text.append(
                f"\nДанные предыдущего обновления · {self._statistics_error}",
                style=ACCENT,
            )
        view.update(text)

    def on_app_resize(self) -> None:
        super().on_app_resize()
        self._render_statistics()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "collector-start": self._start,
            "collector-pause": self._pause,
            "collector-stop": self._stop,
            "telegram-login": lambda: self.hoard_app.open_section("telegram_login"),
            "telegram-statistics-refresh": self._request_statistics,
            "telegram-maintenance": lambda: self.hoard_app.open_section(
                "telegram_maintenance"
            ),
        }
        action = actions.get(str(event.button.id))
        if action:
            action()


class TelegramSourcesScreen(_TelegramWorkspaceScreen):
    section = "telegram_sources"
    PAGE_SIZE = 50
    SEARCH_DEBOUNCE = 0.3
    BINDINGS = [
        Binding("a", "add_source", "Добавить"),
        Binding("e", "edit_source", "Изменить"),
        Binding("s", "scan_source", "Проверить"),
        Binding("r", "refresh_sources", "Обновить"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._items: list[dict[str, Any]] = []
        self._source_ids: list[int] = []
        self._source_layout: tuple[tuple[int, ...], tuple[int, ...]] | None = None
        self._selected_source_id: int | None = None
        self._total = 0
        self._offset = 0
        self._query = ""
        self._enabled: bool | None = None
        self._sort = "id_desc"
        self._catalog_error = ""
        self._loading = False
        self._request_active = False
        self._pending_reload = False
        self._query_generation = 0
        self._entry_generation = 0
        self._search_timer: Timer | None = None
        self._resumed_once = False
        self._edit_loading = False

    def compose_workspace(self) -> ComposeResult:
        with Panel(title="Источники", hints=SOURCE_HINTS, id="sources-panel"):
            with Horizontal(id="source-filters"):
                yield Input(placeholder="Поиск по публикациям", id="source-search")
                yield Select(
                    (("Все", "all"), ("Включённые", "enabled"), ("Отключённые", "disabled")),
                    value="all",
                    allow_blank=False,
                    id="source-filter",
                )
                yield Select(
                    (
                        ("Сначала новые", "id_desc"),
                        ("Последняя проверка", "last_checked_desc"),
                        ("Больше кружков", "downloads_desc"),
                    ),
                    value="id_desc",
                    allow_blank=False,
                    id="source-sort",
                )
            yield DataTable(id="sources-table", cursor_type="row")
            with Horizontal(id="source-pagination"):
                yield Static("0 из 0", id="source-range")
                yield Button("Назад", id="source-prev")
                yield Button("Далее", id="source-next")
                yield Button("Обновить", id="source-refresh")
        with Panel(title="Выбранный источник", id="source-summary-panel"):
            with Horizontal(classes="action-row", id="source-actions"):
                yield Button("Добавить", id="source-add", variant="primary")
                yield Button("Изменить", id="source-edit")
                yield Button("Отключить", id="source-toggle")
                yield Button("Проверить", id="source-scan")
                yield Button("Проверить все", id="source-scan-all")
            with Horizontal(classes="action-row", id="source-ai-actions"):
                yield Button("AI: фото", id="source-ai")
                yield Button("AI: кадры", id="source-ai-frames")
                yield Button("AI: кадры + фото", id="source-ai-combined")
            yield Static("Выберите публикацию в таблице.", id="source-summary")

    def on_mount(self) -> None:
        super().on_mount()
        self.call_after_refresh(self._focus_initial)
        self._request_catalog()

    def on_screen_resume(self) -> None:
        super().on_screen_resume()
        if self._resumed_once:
            self._entry_generation += 1
            self._request_catalog()
        self._resumed_once = True

    def on_screen_suspend(self) -> None:
        self._entry_generation += 1
        self._pending_reload = False
        self._cancel_search_timer()

    def on_unmount(self) -> None:
        self._entry_generation += 1
        self._pending_reload = False
        self._cancel_search_timer()

    def _focus_initial(self) -> None:
        self.focus_working_surface(self.query_one("#sources-table", DataTable))

    def render_data(self, status: dict) -> None:
        del status
        self._render_catalog()

    @staticmethod
    def _count(value: object) -> str:
        try:
            number = max(0, int(value))  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError):
            return "—"
        return f"{number:,}".replace(",", " ")

    def _render_catalog(self) -> None:
        if not self.is_mounted:
            return
        table = self.query_one("#sources-table", DataTable)
        selected = self._selected_source_id
        old_row = table.cursor_row
        compact = self.hoard_app.has_class("compact")
        dense = self.hoard_app.has_class("medium")
        keep = COMPACT_SOURCE_COLUMNS if compact else DENSE_SOURCE_COLUMNS if dense else tuple(
            range(len(SOURCE_COLUMNS))
        )
        table_width = max(
            20,
            self.hoard_app.size.width
            - (6 if self.hoard_app.has_class("sidebar-hidden") else 28),
        )
        widths = _source_widths(keep, table_width)
        layout = (keep, widths)
        if layout != self._source_layout:
            table.clear(columns=True)
            for index, width in zip(keep, widths, strict=True):
                table.add_column(SOURCE_COLUMNS[index], width=width)
            self._source_layout = layout
        table.clear()
        self._source_ids = []
        short_labels = self.hoard_app.has_class("narrow") or compact
        for source in self._items:
            source_id = int(source.get("id") or 0)
            self._source_ids.append(source_id)
            title = str(source.get("title") or "")
            username = str(source.get("username") or "")
            post_id = source.get("post_id", "—")
            name = title or username or "—"
            if short_labels:
                label = name
            elif title and username and title != username:
                label = f"{title} @{username}/{post_id}"
            elif username:
                label = f"@{username}/{post_id}"
            else:
                label = f"{name}/{post_id}"
            cells = (
                label,
                "включён" if source.get("enabled") else "отключён",
                self._count(source.get("download_count")),
                self._count(source.get("profile_count")),
                self._count(source.get("frame_set_count")),
                self._count(source.get("psl_count")),
                _short_time(source.get("last_checked_at")),
            )
            table.add_row(*(cells[index] for index in keep), key=str(source_id))
        if not self._items:
            empty_label = "Загрузка…" if self._loading else "Источники не найдены"
            empty = (empty_label, "—", "0", "0", "0", "0", "—")
            table.add_row(*(empty[index] for index in keep))
        elif selected in self._source_ids:
            table.move_cursor(row=self._source_ids.index(selected))
        else:
            row = min(max(old_row, 0), len(self._source_ids) - 1)
            table.move_cursor(row=row)
            self._selected_source_id = self._source_ids[row]
        self._render_pagination()
        self._render_summary()

    @property
    def selected_source_id(self) -> int | None:
        return self._selected_source_id if self._selected_source_id in self._source_ids else None

    def selected_source(self) -> dict[str, Any] | None:
        source_id = self.selected_source_id
        return next(
            (source for source in self._items if int(source.get("id") or 0) == source_id),
            None,
        )

    def _render_pagination(self) -> None:
        if self._items:
            first = self._offset + 1
            last = self._offset + len(self._items)
            label = f"{first}–{last} из {self._total}"
        else:
            label = f"0 из {self._total}"
        if self._catalog_error:
            label += " · ошибка обновления"
        elif self._loading:
            label += " · обновляется…"
        self.query_one("#source-range", Static).update(label)
        self.query_one("#source-prev", Button).disabled = self._loading or self._offset <= 0
        self.query_one("#source-next", Button).disabled = (
            self._loading or self._offset + len(self._items) >= self._total
        )

    def _render_summary(self) -> None:
        source = self.selected_source()
        actions = (
            "source-edit",
            "source-toggle",
            "source-scan",
            "source-ai",
            "source-ai-frames",
            "source-ai-combined",
        )
        for button_id in actions:
            self.query_one(f"#{button_id}", Button).disabled = source is None
        if source is None:
            message = self._catalog_error or "Выберите публикацию в таблице."
            self.query_one("#source-summary", Static).update(message)
            return
        mode = "Полный" if source.get("collection_mode") == "full" else "Минимальный"
        title = str(source.get("title") or "").strip()
        username = str(source.get("username") or "")
        heading = f"@{username} / {source.get('post_id', '—')}"
        if title:
            heading = f"{title} · {heading}"
        values = (
            ("Состояние", "Включён" if source.get("enabled") else "Отключён"),
            ("Сбор профилей", mode),
            ("Интервал", f"{source.get('interval', '—')} секунд"),
            ("Последняя", _short_time(source.get("last_checked_at"))),
            ("Результат", str(source.get("last_result") or "—")),
            ("Ошибка", str(source.get("last_error") or "—")),
            ("Кружки", self._count(source.get("download_count"))),
            ("Профили", self._count(source.get("profile_count"))),
            ("Фото связанных авторов", self._count(source.get("profile_photo_count"))),
            (
                "FFmpeg",
                f"наборов {self._count(source.get('frame_set_count'))}, "
                f"кадров {self._count(source.get('frame_count'))}, "
                f"ошибок {self._count(source.get('frame_failed_count'))}, "
                f"устарело {self._count(source.get('frame_stale_count'))}",
            ),
            ("PSL", self._count(source.get("psl_count"))),
        )
        text = Text(heading, style=TEXT)
        for label, value in values:
            text.append("\n" + label.ljust(23), style=MUTED)
            text.append(value, style=TEXT)
        self.query_one("#source-summary", Static).update(text)
        self.query_one("#source-toggle", Button).label = (
            "Отключить" if source.get("enabled") else "Включить"
        )

    def _cancel_search_timer(self) -> None:
        if self._search_timer is not None:
            self._search_timer.stop()
            self._search_timer = None

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "source-search":
            return
        self._cancel_search_timer()
        self._search_timer = self.set_timer(self.SEARCH_DEBOUNCE, self._apply_search)

    def _apply_search(self) -> None:
        self._search_timer = None
        value = self.query_one("#source-search", Input).value.strip()
        if value == self._query:
            return
        self._query = value
        self._reset_query()

    def on_select_changed(self, event: Select.Changed) -> None:
        value = str(event.value)
        if event.select.id == "source-filter":
            enabled = {"all": None, "enabled": True, "disabled": False}.get(value)
            if value not in {"all", "enabled", "disabled"} or enabled == self._enabled:
                return
            self._enabled = enabled
            self._reset_query()
        elif event.select.id == "source-sort" and value != self._sort:
            self._sort = value
            self._reset_query()

    def _reset_query(self) -> None:
        self._offset = 0
        self._selected_source_id = None
        self._items = []
        self._total = 0
        self._render_catalog()
        self._request_catalog()

    def _request_catalog(self) -> None:
        self._query_generation += 1
        if self._request_active:
            self._pending_reload = True
            return
        self._start_catalog_request(self._query_generation)

    def _start_catalog_request(self, generation: int) -> None:
        self._request_active = True
        self._pending_reload = False
        self._loading = True
        self._catalog_error = ""
        self._render_pagination()
        entry_generation = self._entry_generation
        self.run_worker(
            self._load_catalog(generation, entry_generation),
            name="sources-overview",
            exit_on_error=False,
        )

    async def _load_catalog(self, generation: int, entry_generation: int) -> None:
        try:
            result = await self.hoard_app.api.telegram_sources_overview(
                limit=self.PAGE_SIZE,
                offset=self._offset,
                query=self._query,
                enabled=self._enabled,
                sort=self._sort,
            )
            if (
                not self.is_mounted
                or self.app.screen is not self
                or generation != self._query_generation
                or entry_generation != self._entry_generation
            ):
                return
            total = max(0, int(result.get("total") or 0))
            if total and self._offset >= total:
                self._offset = ((total - 1) // self.PAGE_SIZE) * self.PAGE_SIZE
                self._query_generation += 1
                self._pending_reload = True
                return
            self._items = list(result.get("items") or [])
            self._total = total
            self._catalog_error = ""
            self._render_catalog()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if (
                self.is_mounted
                and self.app.screen is self
                and generation == self._query_generation
                and entry_generation == self._entry_generation
            ):
                self._catalog_error = str(exc) or type(exc).__name__
                self._render_catalog()
        finally:
            self._request_active = False
            self._loading = False
            if self.is_mounted:
                self._render_pagination()
            if (
                self._pending_reload
                and self.is_mounted
                and self.app.screen is self
            ):
                self._start_catalog_request(self._query_generation)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "sources-table":
            return
        row = event.data_table.cursor_row
        self._selected_source_id = (
            self._source_ids[row] if 0 <= row < len(self._source_ids) else None
        )
        self._render_summary()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "source-add": self.action_add_source,
            "source-edit": self.action_edit_source,
            "source-toggle": self._toggle,
            "source-scan": self.action_scan_source,
            "source-scan-all": self._scan_all,
            "source-ai": self._analyze_profiles,
            "source-ai-frames": lambda: self._analyze_profiles("frames"),
            "source-ai-combined": lambda: self._analyze_profiles("combined"),
            "source-prev": self._previous_page,
            "source-next": self._next_page,
            "source-refresh": self.action_refresh_sources,
        }
        action = actions.get(str(event.button.id))
        if action:
            action()

    def _analyze_profiles(self, image_mode: str = "photos") -> None:
        source_id = self.selected_source_id
        if source_id is None:
            self.hoard_app.set_notice("Сначала выбери публикацию.", error=True)
            return
        selector: dict[str, Any] = {"source_id": source_id}
        if image_mode != "photos":
            selector["image_mode"] = image_mode
        self.hoard_app.open_telegram_analysis(selector)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "sources-table" and self.selected_source_id is not None:
            self.open_selected()

    def open_selected(self) -> None:
        source = self.selected_source()
        if source is not None:
            self.hoard_app.open_publication(source)

    def action_add_source(self) -> None:
        self.hoard_app.ensure_telegram(lambda: self._open_source_dialog(None))

    def action_edit_source(self) -> None:
        source_id = self.selected_source_id
        if source_id is None:
            self.hoard_app.set_notice("Сначала выбери источник.", error=True)
            return
        if self._edit_loading:
            self.hoard_app.set_notice("Параметры источника уже загружаются.", error=True)
            return
        self._edit_loading = True
        self.run_worker(
            self._load_source_dialog(source_id),
            name="source-settings",
            exit_on_error=False,
        )

    async def _load_source_dialog(self, source_id: int) -> None:
        self.hoard_app.set_notice("Загружаем актуальные параметры источника…")
        try:
            result = await self.hoard_app.api.telegram_source(source_id)
        except Exception as exc:
            self.hoard_app.set_notice(str(exc) or type(exc).__name__, error=True)
            return
        finally:
            self._edit_loading = False
        if self.is_mounted and self.app.screen is self and self.selected_source_id == source_id:
            self._open_source_dialog(dict(result["source"]))

    def _open_source_dialog(self, source: dict[str, Any] | None) -> None:
        status = self.hoard_app.status or {}
        output = str((source or {}).get("output") or status.get("archive_root") or "~/Videos/hoard")
        self.app.push_screen(
            SourceDialog(self.hoard_app.api, source=source, default_output=output),
            self._source_dialog_closed,
        )

    def _source_dialog_closed(self, changed: bool | None) -> None:
        if changed:
            self._request_catalog()

    def _toggle(self) -> None:
        source = self.selected_source()
        if source is None:
            self.hoard_app.set_notice("Сначала выбери источник.", error=True)
            return
        source_id = int(source["id"])
        enabled = not bool(source.get("enabled"))
        self.run_worker(
            self._toggle_source(source_id, enabled),
            name="source-toggle",
            exit_on_error=False,
        )

    async def _toggle_source(self, source_id: int, enabled: bool) -> None:
        result = await self.hoard_app.execute(
            f"source-toggle:{source_id}",
            lambda: self.hoard_app.api.set_source_enabled(source_id, enabled),
            pending="Обновляем состояние источника…",
            refresh=False,
        )
        if result is not None and self.is_mounted and self.app.screen is self:
            self._selected_source_id = source_id
            self._request_catalog()

    def action_scan_source(self) -> None:
        source_id = self.selected_source_id
        if source_id is None:
            self.hoard_app.set_notice("Сначала выбери источник.", error=True)
            return
        self.hoard_app.ensure_telegram(lambda: self._scan_selected_authorized(source_id))

    def _scan_selected_authorized(self, source_id: int) -> None:
        self.run_worker(
            self.hoard_app.execute(
                f"source-scan:{source_id}",
                lambda: self.hoard_app.api.scan_source(source_id),
                pending=f"Назначаем проверку только источника #{source_id}…",
                refresh=False,
            ),
            name="source-scan",
            exit_on_error=False,
        )

    def _scan_all(self) -> None:
        self.hoard_app.ensure_telegram(self._scan_all_authorized)

    def _scan_all_authorized(self) -> None:
        self.run_worker(
            self.hoard_app.execute(
                "source-scan-all",
                self.hoard_app.api.scan_all,
                pending="Назначаем проверку всех включённых источников…",
                refresh=False,
            ),
            name="source-scan-all",
            exit_on_error=False,
        )

    def _previous_page(self) -> None:
        if self._loading or self._offset <= 0:
            return
        self._offset = max(0, self._offset - self.PAGE_SIZE)
        self._selected_source_id = None
        self._items = []
        self._render_catalog()
        self._request_catalog()

    def _next_page(self) -> None:
        if self._loading or self._offset + len(self._items) >= self._total:
            return
        self._offset += self.PAGE_SIZE
        self._selected_source_id = None
        self._items = []
        self._render_catalog()
        self._request_catalog()

    def action_refresh_sources(self) -> None:
        self._request_catalog()

    def on_app_resize(self) -> None:
        self._render_catalog()


class TelegramWebScreen(_TelegramWorkspaceScreen):
    section = "telegram_web"

    def __init__(self) -> None:
        super().__init__()
        self._web_loading = False

    def compose_workspace(self) -> ComposeResult:
        with Panel(title="Web Viewer", id="web-panel"):
            yield Static(id="web-summary")
            with Horizontal(classes="action-row", id="web-actions"):
                yield Button("Включить", id="web-start", variant="primary")
                yield Button("Открыть", id="web-open")
                yield Button("Выключить", id="web-stop")

    def on_mount(self) -> None:
        super().on_mount()
        self._refresh_web()

    def on_screen_resume(self) -> None:
        super().on_screen_resume()
        self._refresh_web()

    def render_data(self, status: dict) -> None:
        del status
        self._render_web()

    def _render_web(self) -> None:
        web_state = self.hoard_app.web_state
        state = str(web_state.get("state") or "unknown")
        labels = {
            "unknown": "проверяем",
            "stopped": "выключен",
            "starting": "запускается",
            "ready": "работает",
            "stopping": "останавливается",
            "error": "ошибка",
        }
        text = Text()
        text.append("состояние".ljust(11), style=MUTED)
        text.append(labels.get(state, state), style=ACCENT if state == "error" else TEXT)
        if state == "ready" and web_state.get("port"):
            text.append("\n" + "адрес".ljust(11), style=MUTED)
            text.append(f"127.0.0.1:{web_state['port']}", style=TEXT)
        if state == "error" and web_state.get("error"):
            text.append("\n" + str(web_state["error"]), style=ACCENT)
        if state == "ready" and not web_state.get("controllable", True):
            text.append(
                "\nWeb запущен без управляющего канала; перезапусти его вручную",
                style=ACCENT,
            )
        self.query_one("#web-summary", Static).update(text)

        busy = self._web_loading
        controllable = bool(web_state.get("controllable", True))
        self.query_one("#web-start", Button).disabled = busy or state in {
            "starting", "ready", "stopping"
        }
        self.query_one("#web-open", Button).disabled = busy or state != "ready" or not controllable
        self.query_one("#web-stop", Button).disabled = busy or state != "ready" or not controllable
        if self.is_mounted:
            self.query_one(Sidebar).set_indicators(web=state)

    def _refresh_web(self) -> None:
        if self._web_loading:
            return
        self._web_loading = True
        self._render_web()
        self.run_worker(self._load_web_state(), name="web-status", exit_on_error=False)

    async def _load_web_state(self) -> None:
        try:
            await self.hoard_app.refresh_web_state()
        finally:
            self._web_loading = False
            if self.is_mounted:
                self._render_web()

    def _run_web_action(self, key: str, operation: str) -> None:
        if self._web_loading:
            return
        self._web_loading = True
        state = {
            "start": "starting",
            "stop": "stopping",
            "open": "ready",
        }[operation]
        if operation != "open":
            self.hoard_app.remember_web_state({"state": state})
        messages = {
            "start": "Включаем Web Viewer…",
            "open": "Открываем Web Viewer…",
            "stop": "Останавливаем Web Viewer…",
        }
        self.hoard_app.set_notice(messages[operation])
        self._render_web()
        self.run_worker(
            self._perform_web_action(operation), name=key, exit_on_error=False
        )

    async def _perform_web_action(self, operation: str) -> None:
        refreshed = False
        try:
            if operation == "start":
                await self.hoard_app.api.start_web()
                ready = await self.hoard_app.refresh_web_state(force=True)
                refreshed = True
                if str(ready.get("state") or "") != "ready":
                    raise RuntimeError("Web Viewer не подтвердил готовность")
                try:
                    result = await self.hoard_app.api.open_web()
                except Exception as exc:
                    self.hoard_app.set_notice(
                        "Web Viewer работает, но браузер не открылся: "
                        f"{str(exc) or type(exc).__name__}",
                        error=True,
                    )
                else:
                    self.hoard_app.set_notice(
                        str(result.get("message") or "Web Viewer открыт в браузере"),
                        error=result.get("opened") is False,
                    )
            elif operation == "open":
                result = await self.hoard_app.api.open_web()
                opened = result.get("opened")
                self.hoard_app.set_notice(
                    str(result.get("message") or "Web Viewer открыт в браузере"),
                    error=opened is False,
                )
            else:
                result = await self.hoard_app.api.stop_web()
                self.hoard_app.set_notice(str(result.get("message") or "Web Viewer выключен"))
        except Exception as exc:
            self.hoard_app.set_notice(str(exc) or type(exc).__name__, error=True)
        finally:
            if not refreshed:
                await self.hoard_app.refresh_web_state(force=True)
            self._web_loading = False
            if self.is_mounted and self.app.screen is self:
                self._render_web()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "web-start": ("web-start", "start"),
            "web-open": ("web-open", "open"),
            "web-stop": ("web-stop", "stop"),
        }
        action = actions.get(str(event.button.id))
        if action:
            self._run_web_action(*action)


class TelegramLoginScreen(_TelegramWorkspaceScreen):
    section = "telegram_login"

    def compose_workspace(self) -> ComposeResult:
        with Panel(title="Telegram Account", id="account-panel"):
            yield Static(id="account-summary")
            with Horizontal(classes="action-row", id="account-actions"):
                yield Button("Подключить", id="account-connect", variant="primary")
                yield Button("Проверить сессию", id="account-check")
                yield Button("Переподключиться", id="account-reconnect")
                yield Button("Отключить соединение", id="account-disconnect")

    def render_data(self, status: dict) -> None:
        account = status.get("telegram_account") or {}
        state = str(account.get("state") or "unknown")
        labels = {
            "connected": "подключён",
            "saved": "сессия сохранена, соединение не проверено",
            "login_required": "требуется вход",
            "unknown": "не определено",
        }
        summary = Text()
        summary.append("состояние".ljust(14), style=MUTED)
        summary.append(labels.get(state, state), style=TEXT if state == "connected" else ACCENT)
        summary.append("\n" + "сессия".ljust(14), style=MUTED)
        summary.append("сохранена" if account.get("session_present") else "нет", style=TEXT)
        summary.append("\n" + "соединение".ljust(14), style=MUTED)
        summary.append("установлено" if account.get("connected") else "отключено", style=TEXT)
        profile = account.get("profile") or {}
        show_profile = state in {"connected", "saved"}
        if state == "saved" and profile:
            summary.append(
                "\n\nПоследний подтверждённый аккаунт — сейчас не подключён",
                style=MUTED,
            )
        if show_profile and profile.get("name"):
            summary.append("\n" + "имя".ljust(14), style=MUTED)
            summary.append(str(profile["name"]), style=TEXT)
        if show_profile and profile.get("username"):
            summary.append("\n" + "username".ljust(14), style=MUTED)
            summary.append("@" + str(profile["username"]).lstrip("@"), style=TEXT)
        if show_profile and profile.get("id") is not None:
            summary.append("\n" + "Telegram ID".ljust(14), style=MUTED)
            summary.append(str(profile["id"]), style=TEXT)
        if bool(status.get("running")):
            summary.append(
                "\n\nСборщик работает. Перед отключением соединения "
                "останови или поставь его на паузу.",
                style=ACCENT,
            )
        elif state == "login_required" and not status.get("configured"):
            summary.append(
                "\n\nДля входа понадобятся API ID и API Hash с my.telegram.org/apps.",
                style=MUTED,
            )
        self.query_one("#account-summary", Static).update(summary)

        self.query_one("#account-connect", Button).disabled = state == "connected"
        self.query_one("#account-check", Button).disabled = not bool(status.get("configured"))
        self.query_one("#account-reconnect", Button).disabled = not bool(
            account.get("session_present")
        )
        self.query_one("#account-disconnect", Button).disabled = not bool(account.get("connected"))

    def _open_auth(self) -> None:
        self.hoard_app.open_auth()

    def _check(self, *, reconnect: bool = False) -> None:
        self.run_worker(
            self.hoard_app.execute(
                "telegram-account-check",
                self._check_account,
                pending=(
                    "Переподключаем Telegram и проверяем сессию…"
                    if reconnect
                    else "Проверяем сохранённую сессию Telegram…"
                ),
            ),
            name="telegram-account-check",
            exit_on_error=False,
        )

    async def _check_account(self) -> dict[str, Any]:
        result = await self.hoard_app.api.telegram_account_check()
        error = str(result.get("error") or "")
        if error:
            raise RuntimeError(error)
        if not result.get("authorized") and not result.get("message"):
            result["message"] = "Сессия не подтверждена; выполни вход в Telegram"
        return result

    def _disconnect(self) -> None:
        if bool((self.hoard_app.status or {}).get("running")):
            self.hoard_app.set_notice(
                "Сначала останови или поставь Telegram-сборщик на паузу.", error=True
            )
            return
        self.run_worker(
            self.hoard_app.execute(
                "telegram-account-disconnect",
                self.hoard_app.api.telegram_account_disconnect,
                pending="Отключаем Telegram, сохраняя авторизованную сессию…",
            ),
            name="telegram-account-disconnect",
            exit_on_error=False,
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "account-connect": self._open_auth,
            "account-check": self._check,
            "account-reconnect": lambda: self._check(reconnect=True),
            "account-disconnect": self._disconnect,
        }
        action = actions.get(str(event.button.id))
        if action:
            action()


# Import compatibility for integrations that treated the former monolithic
# Telegram page as its default destination.
TelegramScreen = TelegramStatusScreen


__all__ = [
    "TelegramLoginScreen",
    "TelegramScreen",
    "TelegramSourcesScreen",
    "TelegramStatusScreen",
    "TelegramWebScreen",
]
