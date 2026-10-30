"""Contextual global Telegram maintenance catalogs and explicit operations."""

from __future__ import annotations

from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.timer import Timer
from textual.widgets import Button, DataTable, Input, Select, Static

from ..state import format_bytes
from ..widgets import Panel
from ..widgets.chrome import Hint
from .base import WorkspaceScreen
from .maintenance_actions import MaintenanceActionsMixin
from .maintenance_operations import MaintenanceOperationsMixin

MODES = ("jobs", "profiles", "media", "operations")
PAGE_SIZE = 50


def _fit_widths(weights: tuple[int, ...], table_width: int) -> tuple[int, ...]:
    available = max(len(weights), table_width - len(weights) * 2 - 1)
    total = sum(weights)
    widths = [max(1, available * weight // total) for weight in weights]
    widths[0] += available - sum(widths)
    return tuple(widths)


class TelegramMaintenanceScreen(
    MaintenanceActionsMixin,
    MaintenanceOperationsMixin,
    WorkspaceScreen,
):
    """Small contextual workspace replacing the old global Archive screen."""

    section = "telegram_maintenance"
    HINTS: ClassVar[tuple[Hint, ...]] = (
        ("j k", "выбор"),
        ("space", "отметить профиль"),
        ("h", "Статус"),
        ("[ ]", "раздел"),
    )
    BINDINGS = [
        Binding("space", "toggle_profile", "Выбрать профиль", show=False),
        Binding("v", "select_visible", "Выбрать видимые", show=False),
        Binding("x", "clear_profiles", "Снять выбор", show=False),
        Binding("r", "refresh_maintenance", "Обновить", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.mode = "jobs"
        self.pages: dict[str, dict[str, Any]] = {
            name: {
                "items": [],
                "total": 0,
                "offset": 0,
                "requested_offset": 0,
                "loaded": False,
                "key": None,
                "error": "",
                "error_key": None,
            }
            for name in ("jobs", "profiles", "media")
        }
        self.selected: dict[str, object | None] = {name: None for name in MODES}
        self.marked_profiles: set[int] = set()
        self.queries = {name: "" for name in ("jobs", "profiles", "media")}
        self.job_state = ""
        self.media_sort = "newest"
        self._request_ids = {name: 0 for name in MODES}
        self._loading: set[str] = set()
        self._loading_keys: dict[str, tuple[object, ...]] = {}
        self._loading_generations: dict[str, int] = {}
        self._entry_generation = 0
        self._search_revision = 0
        self._search_timer: Timer | None = None
        self._work_timer: Timer | None = None
        self._work_polling = False
        self._statistics: dict[str, Any] | None = None
        self._statistics_error = ""
        self._frame_session: dict[str, Any] | None = None
        self._frame_session_error = ""
        self._table_layout: tuple[str, tuple[int, ...], tuple[int, ...]] | None = None
        self._row_ids: list[object] = []

    def compose_workspace(self) -> ComposeResult:
        with Panel(title="Обслуживание", id="maintenance-main"):
            with Horizontal(classes="mode-row", id="maintenance-modes"):
                yield Button("Задания", id="maintenance-mode-jobs", variant="primary")
                yield Button("Профили", id="maintenance-mode-profiles")
                yield Button("Медиа", id="maintenance-mode-media")
                yield Button("Операции", id="maintenance-mode-operations")
                yield Button("Назад", id="maintenance-back")
            with Horizontal(id="maintenance-filters"):
                yield Input(
                    placeholder="Тип задания",
                    id="maintenance-search",
                )
                yield Select(
                    (
                        ("Все состояния", ""),
                        ("queued", "queued"),
                        ("running", "running"),
                        ("waiting", "waiting"),
                        ("done", "done"),
                        ("failed", "failed"),
                    ),
                    value="",
                    allow_blank=False,
                    id="maintenance-job-state",
                )
                yield Select(
                    (
                        ("Новые сначала", "newest"),
                        ("Старые сначала", "oldest"),
                        ("По дате", "date_desc"),
                        ("По размеру", "size_desc"),
                    ),
                    value="newest",
                    allow_blank=False,
                    id="maintenance-media-sort",
                )
            with Horizontal(classes="action-row", id="maintenance-common-actions"):
                yield Button("Обновить", id="maintenance-refresh", variant="primary")
                yield Button("Подробности", id="maintenance-details")
                yield Button("←", id="maintenance-prev")
                yield Static("0 из 0", id="maintenance-range")
                yield Button("→", id="maintenance-next")
            with Horizontal(classes="action-row", id="maintenance-job-actions"):
                yield Button("Повторить failed", id="maintenance-job-retry")
            with Vertical(id="maintenance-profile-actions"):
                with Horizontal(classes="action-row"):
                    yield Button("Выбрать / снять", id="maintenance-profile-toggle")
                    yield Button("Выбрать видимые", id="maintenance-profile-visible")
                    yield Button("Снять все", id="maintenance-profile-clear")
                with Horizontal(classes="action-row"):
                    yield Select(
                        (
                            ("Фотографии", "photos"),
                            ("Кадры", "frames"),
                            ("Кадры + фото", "combined"),
                        ),
                        value="photos",
                        allow_blank=False,
                        id="maintenance-psl-mode",
                    )
                    yield Button("Анализ профиля", id="maintenance-profile-analyze")
                    yield Button("Анализ выбранных", id="maintenance-profiles-analyze")
            with Horizontal(classes="action-row", id="maintenance-media-actions"):
                yield Button("Открыть MP4", id="maintenance-media-open")
                yield Button("Посмотреть кадры", id="maintenance-media-frames")
                yield Button("К публикации", id="maintenance-media-source")
                yield Button("К автору", id="maintenance-media-author")
            with Vertical(id="maintenance-operations"):
                with Horizontal(classes="action-row"):
                    yield Button("Обновить HTML", id="maintenance-catalog-build")
                    yield Button("Открыть HTML", id="maintenance-catalog-open")
                    yield Button(
                        "Обновить профили и публикации", id="maintenance-enrich"
                    )
                    yield Button("История AI-групп", id="maintenance-batches")
                with Horizontal(classes="action-row"):
                    yield Button("Обработать весь архив", id="maintenance-frames-start")
                    yield Button("Повторить ошибки архива", id="maintenance-frames-retry")
                    yield Button("Остановить FFmpeg", id="maintenance-frames-stop")
                yield Static(id="maintenance-operation-status")
            yield DataTable(id="maintenance-table", cursor_type="row")
            yield Static("Выберите объект в таблице.", id="maintenance-object")

    def on_mount(self) -> None:
        super().on_mount()
        self._work_timer = self.set_interval(1.5, self._poll_work, pause=True)
        self._activate_mode("jobs", force=True)
        self.call_after_refresh(
            lambda: self.focus_working_surface(
                self.query_one("#maintenance-table", DataTable)
            )
        )

    def on_screen_resume(self) -> None:
        super().on_screen_resume()
        if self.mode == "operations":
            # Load one fresh snapshot on entry.  The renderer starts polling only
            # when that snapshot confirms an active FFmpeg session.
            self._pause_work_timer()
            self._request_operations()
        else:
            self._sync_search_input(self.mode)
            self._ensure_page(self.mode)

    def on_screen_suspend(self) -> None:
        self._save_search_draft()
        self._entry_generation += 1
        self._cancel_search()
        self._pause_work_timer()

    def on_unmount(self) -> None:
        self._entry_generation += 1
        self._cancel_search()
        if self._work_timer is not None:
            self._work_timer.stop()

    def _activate_mode(self, mode: str, *, force: bool = False) -> None:
        if mode not in MODES:
            return
        self._save_search_draft()
        self._cancel_search()
        self.mode = mode
        self._entry_generation += 1
        self._table_layout = None
        for name in MODES:
            self.query_one(f"#maintenance-mode-{name}", Button).variant = (
                "primary" if name == mode else "default"
            )
        self._render_visibility()
        if mode == "operations":
            self._pause_work_timer()
            self._request_operations()
        else:
            self._pause_work_timer()
            self._sync_search_input(mode)
            self._ensure_page(mode, force=force)

    def _render_visibility(self) -> None:
        jobs = self.mode == "jobs"
        profiles = self.mode == "profiles"
        media = self.mode == "media"
        operations = self.mode == "operations"
        self.query_one("#maintenance-filters", Horizontal).display = not operations
        search = self.query_one("#maintenance-search", Input)
        search.placeholder = {
            "jobs": "Тип задания",
            "profiles": "ID, имя или username",
            "media": "Автор, message ID или источник",
        }.get(self.mode, "Поиск")
        self.query_one("#maintenance-job-state", Select).display = jobs
        self.query_one("#maintenance-media-sort", Select).display = media
        self.query_one("#maintenance-common-actions", Horizontal).display = not operations
        self.query_one("#maintenance-job-actions", Horizontal).display = jobs
        self.query_one("#maintenance-profile-actions", Vertical).display = profiles
        self.query_one("#maintenance-media-actions", Horizontal).display = media
        self.query_one("#maintenance-operations", Vertical).display = operations
        self.query_one("#maintenance-table", DataTable).display = not operations
        self.query_one("#maintenance-object", Static).display = not operations

    def on_button_pressed(self, event: Button.Pressed) -> None:
        identifier = str(event.button.id)
        if identifier.startswith("maintenance-mode-"):
            self._activate_mode(identifier.removeprefix("maintenance-mode-"))
        elif identifier == "maintenance-back":
            self.hoard_app.open_section("telegram_status")
        elif identifier == "maintenance-refresh":
            self.action_refresh_maintenance()
        elif identifier == "maintenance-prev":
            self._move_page(-1)
        elif identifier == "maintenance-next":
            self._move_page(1)
        elif identifier == "maintenance-details":
            self._show_details()
        elif identifier == "maintenance-job-retry":
            self._retry_job()
        elif identifier == "maintenance-profile-toggle":
            self.action_toggle_profile()
        elif identifier == "maintenance-profile-visible":
            self.action_select_visible()
        elif identifier == "maintenance-profile-clear":
            self.action_clear_profiles()
        elif identifier == "maintenance-profile-analyze":
            self._analyze_current_profile()
        elif identifier == "maintenance-profiles-analyze":
            self._analyze_marked_profiles()
        elif identifier == "maintenance-media-open":
            self._open_media()
        elif identifier == "maintenance-media-frames":
            self._open_media_frames()
        elif identifier == "maintenance-media-source":
            self._open_media_source()
        elif identifier == "maintenance-media-author":
            self._open_media_author()
        elif identifier == "maintenance-catalog-build":
            self._build_catalog()
        elif identifier == "maintenance-catalog-open":
            self._open_catalog()
        elif identifier == "maintenance-enrich":
            self._confirm_enrich()
        elif identifier == "maintenance-batches":
            self.hoard_app.open_telegram_batch_history()
        elif identifier == "maintenance-frames-start":
            self._confirm_global_frames(retry=False)
        elif identifier == "maintenance-frames-retry":
            self._confirm_global_frames(retry=True)
        elif identifier == "maintenance-frames-stop":
            self._confirm_stop_frames()

    def on_select_changed(self, event: Select.Changed) -> None:
        value = "" if event.value is Select.BLANK else str(event.value)
        if event.select.id == "maintenance-job-state" and value != self.job_state:
            self.job_state = value
            self._reset_and_request("jobs")
        elif event.select.id == "maintenance-media-sort" and value != self.media_sort:
            self.media_sort = value
            self._reset_and_request("media")

    def on_input_changed(self, event: Input.Changed) -> None:
        mode = self.mode
        if event.input.id != "maintenance-search" or mode not in self.pages:
            return
        # Textual may deliver a programmatic Input.Changed after another mode
        # has already replaced the field.  Such an event must not become the
        # new mode's draft.
        if event.value != event.input.value:
            return
        value = self._normalize_query(event.value)
        if value == self.queries[mode]:
            return
        self._cancel_search()
        self._set_mode_query(mode, value)
        revision = self._search_revision
        self._render_table()
        self._search_timer = self.set_timer(
            0.25,
            lambda: self._apply_search(mode, revision, value),
        )

    def _cancel_search(self) -> None:
        self._search_revision += 1
        if self._search_timer is not None:
            self._search_timer.stop()
            self._search_timer = None

    @staticmethod
    def _normalize_query(value: object) -> str:
        return str(value or "").strip()

    def _sync_search_input(self, mode: str) -> None:
        search = self.query_one("#maintenance-search", Input)
        value = self.queries[mode]
        if search.value != value:
            search.value = value

    def _save_search_draft(self) -> None:
        mode = self.mode
        if mode not in self.pages or not self.is_mounted:
            return
        value = self._normalize_query(
            self.query_one("#maintenance-search", Input).value
        )
        if value != self.queries[mode]:
            self._set_mode_query(mode, value)

    def _apply_search(self, mode: str, revision: int, value: str) -> None:
        self._search_timer = None
        if (
            revision != self._search_revision
            or self.mode != mode
            or self.queries[mode] != value
            or self._normalize_query(
                self.query_one("#maintenance-search", Input).value
            )
            != value
        ):
            return
        self._request_page(mode)

    def _set_mode_query(self, mode: str, value: str, *, force: bool = False) -> None:
        value = self._normalize_query(value)
        page = self.pages[mode]
        if not force and value == self.queries[mode] and int(
            page["requested_offset"]
        ) == 0:
            return
        self.queries[mode] = value
        page.update(
            requested_offset=0,
            loaded=False,
            error="",
            error_key=None,
        )
        self.selected[mode] = None

    def _reset_and_request(self, mode: str) -> None:
        self.pages[mode].update(
            requested_offset=0,
            loaded=False,
            error="",
            error_key=None,
        )
        self.selected[mode] = None
        if self.mode == mode:
            self._render_table()
            self._request_page(mode)

    def _page_key(
        self,
        mode: str,
        *,
        offset: int | None = None,
        query: str | None = None,
        job_state: str | None = None,
        media_sort: str | None = None,
    ) -> tuple[object, ...]:
        page = self.pages[mode]
        actual_offset = (
            int(page["requested_offset"]) if offset is None else int(offset)
        )
        actual_query = self.queries[mode] if query is None else query
        if mode == "jobs":
            return (
                mode,
                actual_query,
                actual_offset,
                self.job_state if job_state is None else job_state,
            )
        if mode == "media":
            return (
                mode,
                actual_query,
                actual_offset,
                self.media_sort if media_sort is None else media_sort,
            )
        return (mode, actual_query, actual_offset)

    @staticmethod
    def _same_page_conditions(
        first: tuple[object, ...] | None,
        second: tuple[object, ...],
    ) -> bool:
        return bool(
            first
            and len(first) == len(second)
            and first[:2] == second[:2]
            and first[3:] == second[3:]
        )

    def _has_current_snapshot(self, mode: str) -> bool:
        page = self.pages[mode]
        return bool(page["loaded"] and page["key"] == self._page_key(mode))

    def _visible_items(self, mode: str | None = None) -> list[dict[str, Any]]:
        actual_mode = self.mode if mode is None else mode
        if actual_mode not in self.pages or not self._has_current_snapshot(actual_mode):
            return []
        return list(self.pages[actual_mode]["items"])

    def _ensure_page(self, mode: str, *, force: bool = False) -> None:
        self._render_table()
        if force or not self._has_current_snapshot(mode):
            self._request_page(mode, force=force)

    def _request_page(self, mode: str, *, force: bool = False) -> None:
        if mode not in self.pages or not self.is_mounted:
            return
        page = self.pages[mode]
        offset = int(page["requested_offset"])
        query = self.queries[mode]
        job_state = self.job_state
        media_sort = self.media_sort
        key = self._page_key(
            mode,
            offset=offset,
            query=query,
            job_state=job_state,
            media_sort=media_sort,
        )
        if (
            not force
            and mode in self._loading
            and self._loading_keys.get(mode) == key
            and self._loading_generations.get(mode) == self._entry_generation
        ):
            return
        self._request_ids[mode] += 1
        request_id = self._request_ids[mode]
        generation = self._entry_generation
        self._loading.add(mode)
        self._loading_keys[mode] = key
        self._loading_generations[mode] = generation
        if self.mode == mode:
            self._render_table()
        self.run_worker(
            self._load_page(
                mode,
                request_id,
                generation,
                key,
                offset,
                query,
                job_state,
                media_sort,
            ),
            name=f"maintenance-{mode}-page",
            exit_on_error=False,
        )

    async def _load_page(
        self,
        mode: str,
        request_id: int,
        generation: int,
        key: tuple[object, ...],
        offset: int,
        query: str,
        job_state: str,
        media_sort: str,
    ) -> None:
        try:
            if mode == "jobs":
                result = await self.hoard_app.api.jobs_list(
                    limit=PAGE_SIZE,
                    offset=offset,
                    state=job_state,
                    kind=query,
                )
            elif mode == "profiles":
                result = await self.hoard_app.api.ai_call(
                    "ai_telegram_profile_list",
                    {"limit": PAGE_SIZE, "offset": offset, "query": query},
                )
            else:
                result = await self.hoard_app.api.telegram_call(
                    "telegram_media_list",
                    {
                        "limit": PAGE_SIZE,
                        "offset": offset,
                        "query": query,
                        "sort": media_sort,
                    },
                )
        except Exception as exc:
            if self._current(mode, request_id, generation):
                page = self.pages[mode]
                message = str(exc) or type(exc).__name__
                if self._same_page_conditions(page["key"], key):
                    page.update(
                        requested_offset=int(page["offset"]),
                        loaded=True,
                        error=message,
                        error_key=page["key"],
                    )
                else:
                    page.update(loaded=False, error=message, error_key=key)
        else:
            if self._current(mode, request_id, generation):
                self.pages[mode].update(
                    items=list(result.get("items") or []),
                    total=int(result.get("total") or 0),
                    offset=offset,
                    requested_offset=offset,
                    loaded=True,
                    key=key,
                    error="",
                    error_key=None,
                )
        finally:
            if request_id == self._request_ids[mode]:
                self._loading.discard(mode)
                self._loading_keys.pop(mode, None)
                self._loading_generations.pop(mode, None)
            if self._current(mode, request_id, generation):
                self._render_table()

    def _current(self, mode: str, request_id: int, generation: int) -> bool:
        return (
            self.is_mounted
            and self.app.screen is self
            and self.mode == mode
            and self._entry_generation == generation
            and self._request_ids[mode] == request_id
        )

    def _move_page(self, direction: int) -> None:
        if self.mode not in self.pages or not self._has_current_snapshot(self.mode):
            return
        page = self.pages[self.mode]
        target = max(0, int(page["offset"]) + direction * PAGE_SIZE)
        if target == int(page["offset"]):
            return
        if direction > 0 and target >= int(page["total"]):
            return
        page.update(
            requested_offset=target,
            loaded=False,
            error="",
            error_key=None,
        )
        self.selected[self.mode] = None
        self._render_table()
        self._request_page(self.mode)

    def action_refresh_maintenance(self) -> None:
        if self.mode == "operations":
            self._request_operations()
        else:
            self._request_page(self.mode, force=True)

    def _layout(self) -> tuple[tuple[str, ...], tuple[int, ...], tuple[int, ...]]:
        compact = self.hoard_app.has_class("compact")
        narrow = self.hoard_app.has_class("narrow")
        if self.mode == "jobs":
            columns = ("ID", "Тип", "Состояние", "Попытки", "Обновлено")
            keep, weights = ((0, 1, 2), (5, 18, 10)) if compact else (
                tuple(range(5)), (5, 22, 10, 8, 16)
            )
        elif self.mode == "profiles":
            columns = ("✓", "Имя", "Username", "ID", "Фото", "Кружки", "Ист.", "Последнее")
            if compact:
                keep, weights = (0, 1, 2), (2, 16, 12)
            elif narrow:
                keep, weights = (0, 1, 2, 4, 5), (2, 18, 14, 7, 8)
            else:
                keep, weights = tuple(range(8)), (2, 18, 14, 12, 7, 8, 7, 14)
        else:
            columns = (
                "Источник",
                "Автор",
                "Message",
                "Размер",
                "Файл",
                "Кадры",
                "Дата",
            )
            if compact:
                keep, weights = (1, 2, 5), (18, 8, 10)
            elif narrow:
                keep, weights = (0, 1, 2, 4, 5), (16, 18, 8, 8, 10)
            else:
                keep, weights = tuple(range(7)), (18, 20, 8, 9, 8, 10, 15)
        return columns, keep, tuple(weights)

    def _render_table(self) -> None:
        if self.mode not in self.pages or not self.is_mounted:
            return
        page = self.pages[self.mode]
        items = self._visible_items()
        desired_key = self._page_key(self.mode)
        error = (
            str(page["error"])
            if page["error"] and page["error_key"] == desired_key
            else ""
        )
        table = self.query_one("#maintenance-table", DataTable)
        columns, keep, weights = self._layout()
        table_width = max(
            20,
            self.hoard_app.size.width
            - (6 if self.hoard_app.has_class("sidebar-hidden") else 28),
        )
        widths = _fit_widths(weights, table_width)
        layout = (self.mode, keep, widths)
        if layout != self._table_layout:
            table.clear(columns=True)
            for index, width in zip(keep, widths, strict=True):
                table.add_column(columns[index], width=width)
            self._table_layout = layout
        table.clear()
        self._row_ids = []
        for item in items:
            if self.mode == "jobs":
                identifier: object = int(item.get("id") or 0)
                state = str(item.get("state") or "—")
                if item.get("manual_permission_required"):
                    state = "waiting · ручной FFmpeg"
                cells = (
                    str(identifier), str(item.get("kind") or "—"), state,
                    str(item.get("attempts") or 0), str(item.get("updated_at") or "—")[:16],
                )
            elif self.mode == "profiles":
                author_id = int(item.get("author_id") or 0)
                identifier = author_id
                cells = (
                    "●" if author_id in self.marked_profiles else "○",
                    str(item.get("display_name") or "—"),
                    "@" + str(item.get("current_username") or "—").lstrip("@"),
                    str(author_id), str(item.get("photo_count") or 0),
                    str(item.get("video_count") or 0), str(item.get("source_count") or 0),
                    str(item.get("last_seen") or "—")[:16],
                )
            else:
                source_id = int(item.get("source_id") or 0)
                message_id = int(item.get("message_id") or 0)
                identifier = (source_id, message_id)
                source = (
                    f"@{item.get('source_username')}/{item.get('post_id')}"
                    if item.get("source_available") else f"legacy #{source_id}"
                )
                author = str(item.get("author_name") or item.get("author_username") or "—")
                cells = (
                    source, author, str(message_id), format_bytes(item.get("size")),
                    str(item.get("state") or "—"),
                    str(item.get("frame_status") or "missing"),
                    str(item.get("date") or "—")[:16],
                )
            self._row_ids.append(identifier)
            table.add_row(*(cells[index] for index in keep))
        if not items:
            placeholder = (
                "Ошибка загрузки"
                if error
                else "Загрузка…"
                if self.mode in self._loading or not self._has_current_snapshot(self.mode)
                else "—"
            )
            table.add_row(placeholder, *("—" for _ in keep[1:]))
        selected_id = self.selected[self.mode]
        if selected_id in self._row_ids:
            table.move_cursor(row=self._row_ids.index(selected_id), scroll=False)
        elif self._row_ids:
            self.selected[self.mode] = self._row_ids[0]
            table.move_cursor(row=0, scroll=False)
        else:
            self.selected[self.mode] = None
        self._render_pagination()
        self._render_object()
        self._render_actions()

    def _render_pagination(self) -> None:
        if self.mode not in self.pages or not self.is_mounted:
            return
        page = self.pages[self.mode]
        desired_key = self._page_key(self.mode)
        valid = self._has_current_snapshot(self.mode)
        items = self._visible_items()
        error = (
            str(page["error"])
            if page["error"] and page["error_key"] == desired_key
            else ""
        )
        if valid:
            first = int(page["offset"]) + 1 if items else 0
            last = int(page["offset"]) + len(items)
            label = f"{first}–{last} из {page['total']}"
        else:
            first = last = 0
            label = f"Ошибка загрузки: {error}" if error else "Загрузка…"
        if self.mode == "profiles" and self.marked_profiles:
            label += f" · выбрано {len(self.marked_profiles)}"
        if valid and error:
            label += " · ошибка обновления: " + error
        elif valid and self.mode in self._loading:
            label += " · обновляется…"
        self.query_one("#maintenance-range", Static).update(label)
        self.query_one("#maintenance-prev", Button).disabled = (
            not valid or self.mode in self._loading or int(page["offset"]) <= 0
        )
        self.query_one("#maintenance-next", Button).disabled = (
            not valid or self.mode in self._loading or last >= int(page["total"])
        )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "maintenance-table" or self.mode not in self.pages:
            return
        row = event.data_table.cursor_row
        self.selected[self.mode] = self._row_ids[row] if 0 <= row < len(self._row_ids) else None
        self._render_object()
        self._render_actions()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "maintenance-table":
            self._show_details()

    def open_selected(self) -> None:
        if self.mode != "operations":
            self._show_details()

    def _selected_item(self) -> dict[str, Any] | None:
        if self.mode not in self.pages:
            return None
        table = self.query_one("#maintenance-table", DataTable)
        row = table.cursor_row
        if 0 <= row < len(self._row_ids):
            self.selected[self.mode] = self._row_ids[row]
        identifier = self.selected[self.mode]
        for item in self._visible_items():
            if self.mode == "jobs" and int(item.get("id") or 0) == identifier:
                return item
            if self.mode == "profiles" and int(item.get("author_id") or 0) == identifier:
                return item
            if self.mode == "media" and (
                int(item.get("source_id") or 0), int(item.get("message_id") or 0)
            ) == identifier:
                return item
        return None

    def _render_object(self) -> None:
        item = self._selected_item()
        if item is None:
            self.query_one("#maintenance-object", Static).update("Выберите объект в таблице.")
            return
        if self.mode == "jobs":
            text = f"#{item.get('id')} · {item.get('kind')} · {item.get('state')}"
            if item.get("result"):
                text += f"\n{item['result']}"
        elif self.mode == "profiles":
            text = (
                f"{item.get('display_name') or '—'} · @{item.get('current_username') or '—'} · "
                f"ID {item.get('author_id')}\nФото {item.get('photo_count', 0)} · "
                f"кружки {item.get('video_count', 0)} · публикации {item.get('source_count', 0)}"
            )
        else:
            text = (
                f"Message {item.get('message_id')} · автор {item.get('author_id') or '—'} · "
                f"{format_bytes(item.get('size'))}\nКадры: {item.get('frame_status') or 'missing'}"
            )
        self.query_one("#maintenance-object", Static).update(text)

    def _render_actions(self) -> None:
        item = self._selected_item()
        self.query_one("#maintenance-details", Button).disabled = item is None
        if self.mode == "jobs":
            self.query_one("#maintenance-job-retry", Button).disabled = not bool(
                item and item.get("retryable")
            )
        elif self.mode == "profiles":
            author_id = int(item.get("author_id") or 0) if item else 0
            self.query_one("#maintenance-profile-toggle", Button).disabled = author_id <= 0
            self.query_one("#maintenance-profile-analyze", Button).disabled = author_id <= 0
            self.query_one("#maintenance-profiles-analyze", Button).disabled = (
                not self.marked_profiles
            )
            self.query_one("#maintenance-profile-clear", Button).disabled = (
                not self.marked_profiles
            )
        elif self.mode == "media":
            disabled = item is None
            for identifier in ("open", "frames", "author"):
                self.query_one(f"#maintenance-media-{identifier}", Button).disabled = disabled
            self.query_one("#maintenance-media-source", Button).disabled = (
                disabled or not bool(item and item.get("source_available"))
            )
