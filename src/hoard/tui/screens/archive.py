"""Archived downloads, profiles, jobs and catalog operations."""

from __future__ import annotations

import webbrowser
from pathlib import Path
from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Select, Static

from ..ai_dialogs import VideoFramesDialog
from ..colors import ACCENT, MUTED, TEXT
from ..dialogs import ConfirmDialog
from ..state import format_bytes
from ..widgets import Panel
from ..widgets.chrome import Hint
from .base import WorkspaceScreen

DOWNLOAD_COLUMNS = ("Дата", "Автор", "Источник", "Размер", "Кадры", "Статус")
PROFILE_COLUMNS = (
    "✓", "Имя", "Username", "TG ID", "Ист.", "Фото",
    "Кружки", "Кадры", "Последнее",
)
JOB_COLUMNS = ("ID", "Тип", "Состояние", "Попытки", "Результат", "Обновлено")


def _short_date(value: object) -> str:
    text = str(value or "")
    return text.replace("T", " ")[:16] if text else "—"


def _fit_widths(weights: tuple[int, ...], table_width: int) -> tuple[int, ...]:
    """Fit fixed column widths into the table without a horizontal scrollbar."""
    available = max(len(weights), table_width - len(weights) * 2 - 1)
    total = sum(weights)
    widths = [max(1, available * weight // total) for weight in weights]
    widest = max(range(len(widths)), key=widths.__getitem__)
    widths[widest] += available - sum(widths)
    return tuple(widths)


def _archive_layout(
    mode: str, *, compact: bool, narrow: bool, medium: bool, table_width: int
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    if mode == "downloads":
        if compact:
            keep, weights = (1, 4, 5), (20, 7, 8)
        elif medium:
            keep, weights = (1, 2, 4, 5), (18, 20, 7, 8)
        else:
            keep, weights = tuple(range(6)), (18, 20, 20, 7, 7, 9)
    elif mode == "profiles":
        if compact:
            keep, weights = (0, 1, 2, 7), (1, 10, 7, 5)
        elif narrow:
            keep, weights = (0, 1, 2, 6, 7), (1, 16, 12, 6, 6)
        elif medium:
            keep, weights = (0, 1, 2, 5, 6, 7), (1, 20, 14, 5, 8, 11)
        else:
            keep, weights = (0, 1, 2, 3, 4, 5, 6, 7), (1, 18, 14, 10, 7, 5, 7, 8)
    else:
        if compact:
            keep, weights = (0, 1, 2), (4, 11, 10)
        elif medium:
            keep, weights = (0, 1, 2, 4), (4, 16, 9, 28)
        else:
            keep, weights = tuple(range(6)), (5, 16, 10, 8, 28, 12)
    return keep, _fit_widths(tuple(weights), table_width)


class ArchiveScreen(WorkspaceScreen):
    section = "archive"
    HINTS: ClassVar[tuple[Hint, ...]] = (
        ("j k", "выбор"),
        ("tab", "действия"),
        ("[ ]", "раздел"),
    )

    def __init__(self) -> None:
        super().__init__()
        self.mode = "downloads"
        self._table_mode: str | None = None
        self._table_layout: tuple[str, tuple[int, ...], tuple[int, ...]] | None = None
        self._advanced_open = False
        self._job_ids: list[int] = []
        self._profile_ids: list[int] = []
        self._selected_profiles: set[int] = set()
        self._profile_source = ""
        self._profile_items: list[dict] = []
        self._profile_offset = 0
        self._profile_limit = 50
        self._profile_total = 0
        self._profiles_loading = False
        self._downloads: list[dict] = []

    def compose_workspace(self) -> ComposeResult:
        with Panel(title="Архив", id="archive-main"):
            yield Static(id="archive-summary")
            with Horizontal(classes="mode-row", id="archive-modes"):
                yield Button("Кружки", id="mode-downloads", variant="primary")
                yield Button("Профили", id="mode-profiles")
                yield Button("Задания", id="mode-jobs")
                yield Button("Ещё…", id="archive-more")
            with Horizontal(classes="action-row", id="archive-actions"):
                yield Button("Обновить HTML", id="catalog-build")
                yield Button("Открыть HTML", id="catalog-open")
                yield Button("Обновить профили", id="telegram-enrich")
                yield Button("Повторить ошибку", id="job-retry")
                yield Button("Анализ профиля", id="telegram-ai")
                yield Button("Выбрать / снять", id="profile-select")
                yield Button("Анализ выбранных", id="profiles-ai")
                yield Button("Кадры кружка", id="frames-view")
                yield Button("Извлечь кадры", id="frames-process")
                yield Button("AI по кадрам", id="frames-ai")
            with Horizontal(classes="action-row", id="archive-advanced"):
                yield Button("AI: кадры", id="telegram-ai-frames")
                yield Button("AI: кадры + фото", id="telegram-ai-combined")
                yield Button("Выбранные: кадры", id="profiles-ai-frames")
                yield Button("Выбранные: 3+фото", id="profiles-ai-combined")
                yield Button("Активная AI-группа", id="batch-reopen")
                yield Button("Обработать ожидающие", id="frames-backfill")
                yield Button("Остановить обработку", id="frames-backfill-pause")
                yield Button("Повторить ошибки", id="frames-retry")
            yield Select(
                [("Все публикации", "")],
                value="",
                allow_blank=False,
                id="profile-source-filter",
            )
            with Horizontal(classes="mode-row", id="profile-pages"):
                yield Button("← Профили", id="profiles-previous")
                yield Static(id="profiles-page-label")
                yield Button("Профили →", id="profiles-next")
            yield DataTable(id="archive-table", cursor_type="row")

    def on_mount(self) -> None:
        self.query_one("#archive-advanced").display = False
        super().on_mount()
        self.call_after_refresh(
            lambda: self.focus_working_surface(self.query_one("#archive-table", DataTable))
        )

    def render_data(self, status: dict) -> None:
        stats = status.get("stats") or {}
        frame_stats = status.get("video_frames") or {}
        text = Text()
        narrow = self.hoard_app.has_class("narrow")
        compact = self.hoard_app.has_class("compact")
        if compact:
            text.append(
                f"Кружки {stats.get('downloads', 0)} · Профили {stats.get('authors', 0)}\n",
                style=TEXT,
            )
            text.append(
                f"Фото {stats.get('profile_photos', 0)} · Кадры {frame_stats.get('saved', 0)}\n",
                style=MUTED,
            )
            text.append(
                f"Ожидают {frame_stats.get('actionable', 0)} · "
                f"Ошибки {frame_stats.get('failed', 0)}",
                style=MUTED,
            )
        else:
            labels = (
                ("Кружки", "downloads"),
                ("Профили", "authors"),
                ("Фото" if narrow else "Фото профилей", "profile_photos"),
            )
            for position, (label, key) in enumerate(labels):
                if position:
                    text.append("   ·   ", style=MUTED)
                text.append(f"{label} ", style=MUTED)
                text.append(str(stats.get(key, 0)), style=f"bold {TEXT}")
            text.append("\nКадры ", style=MUTED)
            backfill = (
                "идёт"
                if frame_stats.get("backfill_active")
                else ("завершён" if frame_stats.get("backfill_complete") else "выкл.")
            )
            if narrow:
                text.append(
                    f"{frame_stats.get('saved', 0)} · "
                    f"{frame_stats.get('actionable', 0)} ждут · "
                    f"{frame_stats.get('failed', 0)} ошибок · "
                    f"{frame_stats.get('processing', 0)} в работе",
                    style=TEXT,
                )
            elif self.hoard_app.has_class("medium"):
                text.append(
                    f"{frame_stats.get('saved', 0)} готовы · "
                    f"{frame_stats.get('actionable', 0)} ожидают · "
                    f"{frame_stats.get('processing', 0)} в работе · "
                    f"{frame_stats.get('failed', 0)} ошибок · "
                    f"старые: {backfill}",
                    style=TEXT,
                )
            else:
                text.append(
                    f"{frame_stats.get('saved', 0)} готовы · "
                    f"{frame_stats.get('actionable', 0)} ожидают · "
                    f"{frame_stats.get('processing', 0)} в работе · "
                    f"{frame_stats.get('failed', 0)} ошибок · "
                    f"{frame_stats.get('missing', 0)} без набора",
                    style=TEXT,
                )
                text.append(
                    " · сбор старых: " + (
                        "идёт" if frame_stats.get("backfill_active")
                        else ("завершён" if frame_stats.get("backfill_complete") else "выключен")
                    ),
                    style=MUTED,
                )
        if not (status.get("integrations") or {}).get("ffmpeg", True):
            text.append(
                "\nFFmpeg не найден; новые кадры не будут извлечены. "
                "Установи FFmpeg и повтори ручную обработку.",
                style=ACCENT,
            )
        text.append("\nHTML  ", style=MUTED)
        text.append(str(status.get("catalog") or "путь недоступен"), style=TEXT)
        self.query_one("#archive-summary", Static).update(text)
        self._downloads = list(status.get("downloads") or [])
        options = [("Все публикации", "")]
        options.extend(
            (
                f"{source.get('title') or '@' + str(source.get('username') or '—')} "
                f"#{source.get('post_id') or '—'}",
                str(source.get("id")),
            )
            for source in status.get("sources") or []
        )
        selector = self.query_one("#profile-source-filter", Select)
        selector.set_options(options)
        selector.value = self._profile_source
        self._render_table(status)

    @staticmethod
    def _download_key(row: dict) -> tuple[int, int] | None:
        try:
            return int(row.get("source_id") or 0), int(row.get("message_id") or 0)
        except (TypeError, ValueError):
            return None

    def _render_table(self, status: dict) -> None:
        table = self.query_one("#archive-table", DataTable)
        # Status and notice updates repaint the current screen.  A full DataTable rebuild
        # must not move the cursor back to the first row while the user is selecting rows.
        preserve = self._table_mode == self.mode
        old_row = table.cursor_row if preserve else 0
        old_scroll = table.scroll_offset if preserve else None
        old_download = (
            self._download_key(self._downloads[old_row])
            if preserve and self.mode == "downloads" and 0 <= old_row < len(self._downloads)
            else None
        )
        old_profile = (
            self._profile_ids[old_row]
            if preserve and self.mode == "profiles" and 0 <= old_row < len(self._profile_ids)
            else None
        )
        old_job = (
            self._job_ids[old_row]
            if preserve and self.mode == "jobs" and 0 <= old_row < len(self._job_ids)
            else None
        )
        self._job_ids = []
        self._profile_ids = []
        cells_rows: list[tuple[str, ...]] = []
        row_keys: list[str | None] = []
        data_rows: list[dict] = []
        if self.mode == "downloads":
            columns = DOWNLOAD_COLUMNS
            data_rows = self._downloads
            for row in data_rows:
                author = str(row.get("author_name") or row.get("author_username") or "—")
                source = f"@{row.get('source_name', '—')}/{row.get('post_id', '—')}"
                frame_label = {
                    "saved": f"{row.get('frame_count', 3)}/готовы",
                    "pending": "ожидают",
                    "running": "извлекаются",
                    "failed": "ошибка",
                    "stale": "устарели",
                    "missing": "—",
                }.get(str(row.get("frame_status") or ""), "—")
                cells_rows.append(
                    (
                        _short_date(row.get("date")),
                        author,
                        source,
                        format_bytes(row.get("size")),
                        frame_label,
                        str(row.get("state") or "saved"),
                    )
                )
                row_keys.append(None)
            if not data_rows:
                cells_rows.append(("—", "Кружков пока нет", "—", "—", "—", "—"))
                row_keys.append(None)
        elif self.mode == "profiles":
            columns = PROFILE_COLUMNS
            data_rows = self._profile_items
            for row in data_rows:
                self._profile_ids.append(int(row.get("author_id") or 0))
                author_id = int(row.get("author_id") or 0)
                cells_rows.append(
                    (
                        "●" if author_id in self._selected_profiles else "○",
                        str(row.get("display_name") or "—"),
                        "@" + str(row.get("current_username") or "—"),
                        str(row.get("author_id") or "—"),
                        str(row.get("source_count") or 0),
                        str(row.get("photo_count") or 0),
                        str(row.get("video_count") or 0),
                        str(row.get("frame_set_count") or 0),
                        str(row.get("photo_error") or row.get("last_seen") or "—"),
                    )
                )
                row_keys.append(None)
            if not data_rows:
                cells_rows.append(
                    ("○", "Профилей пока нет", "—", "—", "0", "0", "0", "0", "—")
                )
                row_keys.append(None)
        else:
            columns = JOB_COLUMNS
            data_rows = list(status.get("jobs") or [])
            for row in data_rows:
                job_id = int(row.get("id") or 0)
                self._job_ids.append(job_id)
                cells_rows.append(
                    (
                        str(job_id),
                        str(row.get("kind") or "—"),
                        str(row.get("state") or "—"),
                        str(row.get("attempts") or 0),
                        str(row.get("result") or "—"),
                        str(row.get("updated_at") or "—"),
                    )
                )
                row_keys.append(str(job_id))
            if not data_rows:
                cells_rows.append(("—", "Заданий пока нет", "—", "0", "—", "—"))
                row_keys.append(None)
        table_width = max(
            20,
            self.hoard_app.size.width
            - (6 if self.hoard_app.has_class("sidebar-hidden") else 28),
        )
        keep, widths = _archive_layout(
            self.mode,
            compact=self.hoard_app.has_class("compact"),
            narrow=self.hoard_app.has_class("narrow"),
            medium=self.hoard_app.has_class("medium"),
            table_width=table_width,
        )
        layout = (self.mode, keep, widths)
        if layout != self._table_layout:
            table.clear(columns=True)
            for index, width in zip(keep, widths, strict=True):
                table.add_column(columns[index], width=width)
            self._table_layout = layout
        table.clear()
        for cells, key in zip(cells_rows, row_keys, strict=True):
            table.add_row(*(cells[index] for index in keep), key=key)
        self.query_one("#job-retry", Button).display = self.mode == "jobs"
        for identifier in ("telegram-ai", "profile-select", "profiles-ai"):
            self.query_one(f"#{identifier}", Button).display = self.mode == "profiles"
        for identifier in (
            "telegram-ai-frames", "telegram-ai-combined",
            "profiles-ai-frames", "profiles-ai-combined",
        ):
            self.query_one(f"#{identifier}", Button).display = self.mode == "profiles"
        self.query_one("#profile-source-filter", Select).display = self.mode == "profiles"
        self.query_one("#batch-reopen", Button).display = self.mode == "profiles"
        self.query_one("#profile-pages", Horizontal).display = self.mode == "profiles"
        for identifier in ("frames-view", "frames-process", "frames-ai"):
            self.query_one(f"#{identifier}", Button).display = self.mode == "downloads"
        for identifier in ("frames-backfill", "frames-backfill-pause", "frames-retry"):
            self.query_one(f"#{identifier}", Button).display = self.mode == "downloads"
        more = self.query_one("#archive-more", Button)
        more.display = self.mode in {"downloads", "profiles"}
        more.label = "Скрыть" if self._advanced_open else "Ещё…"
        if self.mode == "profiles":
            start = self._profile_offset + 1 if data_rows else 0
            end = self._profile_offset + len(data_rows)
            self.query_one("#profiles-page-label", Static).update(
                f"{start}–{end} из {self._profile_total}"
            )
            self.query_one("#profiles-previous", Button).disabled = self._profile_offset <= 0
            self.query_one("#profiles-next", Button).disabled = end >= self._profile_total
        target = old_row
        if preserve:
            if self.mode == "downloads" and old_download is not None:
                target = next(
                    (
                        index
                        for index, item in enumerate(self._downloads)
                        if self._download_key(item) == old_download
                    ),
                    old_row,
                )
            elif self.mode == "profiles" and old_profile is not None:
                target = next(
                    (
                        index
                        for index, item_id in enumerate(self._profile_ids)
                        if item_id == old_profile
                    ),
                    old_row,
                )
            elif self.mode == "jobs" and old_job is not None:
                target = next(
                    (
                        index
                        for index, item_id in enumerate(self._job_ids)
                        if item_id == old_job
                    ),
                    old_row,
                )
        if table.row_count:
            table.move_cursor(row=min(max(0, target), table.row_count - 1), scroll=False)
            if old_scroll is not None:
                # Restore the exact viewport after the next layout pass; move_cursor() may
                # otherwise force the selected row into the middle of the table.
                self.call_after_refresh(
                    lambda: table.scroll_to(
                        x=old_scroll.x, y=old_scroll.y, animate=False
                    )
                )
        self._table_mode = self.mode

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "profile-source-filter":
            return
        self._profile_source = "" if event.value is Select.BLANK else str(event.value)
        self._profile_offset = 0
        self._load_profile_page()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = str(event.button.id)
        if button_id.startswith("mode-"):
            self.mode = button_id.removeprefix("mode-")
            self._advanced_open = False
            self.query_one("#archive-advanced").display = False
            for mode in ("downloads", "profiles", "jobs"):
                self.query_one(f"#mode-{mode}", Button).variant = (
                    "primary" if mode == self.mode else "default"
                )
            self._render_table(self.hoard_app.status or {})
            if self.mode == "profiles":
                self._load_profile_page()
        elif button_id == "archive-more":
            self._advanced_open = not self._advanced_open
            self.query_one("#archive-advanced").display = self._advanced_open
            self.query_one("#archive-more", Button).label = (
                "Скрыть" if self._advanced_open else "Ещё…"
            )
        elif button_id == "catalog-build":
            self._build_catalog()
        elif button_id == "catalog-open":
            self._open_catalog()
        elif button_id == "telegram-enrich":
            self._confirm_enrich()
        elif button_id == "job-retry":
            self._retry_job()
        elif button_id == "telegram-ai":
            self._open_telegram_ai()
        elif button_id == "telegram-ai-frames":
            self._open_profile_frames("frames")
        elif button_id == "telegram-ai-combined":
            self._open_profile_frames("combined")
        elif button_id == "frames-view":
            self._open_selected_frames()
        elif button_id == "frames-process":
            self._process_selected_frames()
        elif button_id == "frames-ai":
            self._analyze_selected_frames()
        elif button_id == "frames-backfill":
            self._confirm_frames_bulk(retry_failed=False)
        elif button_id == "frames-backfill-pause":
            self._frames_action("telegram_frames_backfill_pause")
        elif button_id == "frames-retry":
            self._confirm_frames_bulk(retry_failed=True)
        elif button_id == "profiles-ai-frames":
            self._analyze_selected_profiles("frames")
        elif button_id == "profiles-ai-combined":
            self._analyze_selected_profiles("combined")
        elif button_id == "profile-select":
            self._toggle_profile_selection()
        elif button_id == "profiles-ai":
            self._analyze_selected_profiles()
        elif button_id == "profiles-previous":
            self._profile_offset = max(0, self._profile_offset - self._profile_limit)
            self._load_profile_page()
        elif button_id == "profiles-next":
            self._profile_offset += self._profile_limit
            self._load_profile_page()
        elif button_id == "batch-reopen":
            self.hoard_app.reopen_active_telegram_batch()

    def _load_profile_page(self) -> None:
        if self._profiles_loading:
            return
        self._profiles_loading = True
        self.run_worker(
            self._load_profiles(), name="archive-profile-page", exit_on_error=False
        )

    async def _load_profiles(self) -> None:
        try:
            args = {"limit": self._profile_limit, "offset": self._profile_offset}
            if self._profile_source:
                args["source_id"] = int(self._profile_source)
            page = await self.hoard_app.api.ai_call("ai_telegram_profile_list", args)
            if "items" in page:
                self._profile_items = list(page.get("items") or [])
                self._profile_total = int(page.get("total") or 0)
            else:
                self._profile_items = list((self.hoard_app.status or {}).get("profiles") or [])
                self._profile_total = len(self._profile_items)
        except Exception as exc:
            self.hoard_app.set_notice(str(exc) or type(exc).__name__, error=True)
        finally:
            self._profiles_loading = False
        if self.is_mounted and self.mode == "profiles":
            self._render_table(self.hoard_app.status or {})

    def _build_catalog(self) -> None:
        self.run_worker(
            self.hoard_app.execute(
                "catalog-build",
                self.hoard_app.api.rebuild_catalog,
                pending="Собираем единый HTML-каталог…",
            ),
            name="catalog-build",
            exit_on_error=False,
        )

    def _open_catalog(self) -> None:
        value = str((self.hoard_app.status or {}).get("catalog") or "").strip()
        if not value:
            self.hoard_app.set_notice("Путь HTML-каталога недоступен", error=True)
            return
        try:
            path = Path(value).expanduser().resolve(strict=True)
            if not path.is_file():
                raise OSError("not a file")
        except (OSError, RuntimeError, ValueError):
            self.hoard_app.set_notice("HTML-каталог ещё не создан", error=True)
            return
        if webbrowser.open(path.as_uri(), new=2):
            self.hoard_app.set_notice("HTML-каталог открыт в браузере.")
        else:
            self.hoard_app.set_notice(
                f"Не удалось открыть браузер; каталог: {path}", error=True
            )

    def _confirm_enrich(self) -> None:
        self.app.push_screen(
            ConfirmDialog(
                "Обновить профили и посты?",
                "Операция может поставить в очередь обработку многих профилей и запросы "
                "фотографий. Она не запускается автоматически при открытии экрана.",
                confirm_label="Поставить в очередь",
            ),
            lambda confirmed: self._enrich() if confirmed else None,
        )

    def _enrich(self) -> None:
        self.hoard_app.ensure_telegram(self._enrich_authorized)

    def _enrich_authorized(self) -> None:
        self.run_worker(
            self.hoard_app.execute(
                "telegram-enrich",
                self.hoard_app.api.enrich_telegram,
                pending="Ставим обновление метаданных и фотографий в очередь…",
            ),
            name="telegram-enrich",
            exit_on_error=False,
        )

    def _retry_job(self) -> None:
        table = self.query_one("#archive-table", DataTable)
        row = table.cursor_row
        if self.mode != "jobs" or not 0 <= row < len(self._job_ids):
            self.hoard_app.set_notice("Выбери неудачное задание.", error=True)
            return
        job_id = self._job_ids[row]
        job = next(
            (
                item
                for item in (self.hoard_app.status or {}).get("jobs", [])
                if int(item.get("id") or 0) == job_id
            ),
            {},
        )
        if job.get("state") != "failed":
            self.hoard_app.set_notice(
                "Повторить можно только задание со статусом failed.", error=True
            )
            return
        self.run_worker(
            self.hoard_app.execute(
                f"job-retry:{job_id}",
                lambda: self.hoard_app.api.retry_job(job_id),
                pending=f"Возвращаем задание #{job_id} в очередь…",
            ),
            name="job-retry",
            exit_on_error=False,
        )

    def _selected_profile_id(self) -> int | None:
        row = self.query_one("#archive-table", DataTable).cursor_row
        if self.mode == "profiles" and 0 <= row < len(self._profile_ids):
            return self._profile_ids[row]
        self.hoard_app.set_notice("Выбери сохранённый профиль с локальными фото.", error=True)
        return None

    def _open_telegram_ai(self) -> None:
        author_id = self._selected_profile_id()
        if author_id is not None and author_id > 0:
            self.hoard_app.open_telegram_analysis({"author_ids": [author_id]})
        elif author_id is not None:
            self.hoard_app.set_notice(
                "Каналы и чаты нельзя анализировать как профиль человека.", error=True
            )

    def _selected_download(self) -> dict | None:
        row = self.query_one("#archive-table", DataTable).cursor_row
        if self.mode == "downloads" and 0 <= row < len(self._downloads):
            return self._downloads[row]
        self.hoard_app.set_notice("Выбери сохранённый видеокружок.", error=True)
        return None

    def _open_selected_frames(self) -> None:
        row = self._selected_download()
        if row is None:
            return
        self.run_worker(self._load_frames(row), name="frames-load", exit_on_error=False)

    async def _load_frames(self, row: dict) -> None:
        source_id = int(row.get("source_id") or 0)
        message_id = int(row.get("message_id") or 0)
        try:
            result = await self.hoard_app.api.telegram_call(
                "telegram_frames_get", {"source_id": source_id, "message_id": message_id}
            )
        except Exception as exc:
            self.hoard_app.set_notice(str(exc), error=True)
            return
        details = result.get("frames")
        if not details:
            self.hoard_app.set_notice(
                "Для этого кружка ещё нет записи об извлечении кадров.", error=True
            )
            return
        self.app.push_screen(
            VideoFramesDialog(details, video_id=f"{source_id}:{message_id}"),
            self._frames_dialog_closed,
        )

    def _frames_dialog_closed(self, value: dict | None) -> None:
        if not value or not value.get("analyze"):
            return
        author_id = value.get("author_id")
        if author_id is None:
            self.hoard_app.set_notice("Автор видеокружка не определён.", error=True)
            return
        self.hoard_app.open_telegram_analysis(
            {
                "author_ids": [int(author_id)],
                "image_mode": "frames",
                "video_ids": {str(author_id): str(value.get("video_id") or "")},
            }
        )

    def _analyze_selected_frames(self) -> None:
        row = self._selected_download()
        if row is None:
            return
        author_id = row.get("author_id")
        if author_id is None:
            self.hoard_app.set_notice("Автор видеокружка не определён.", error=True)
            return
        self.hoard_app.open_telegram_analysis(
            {
                "author_ids": [int(author_id)],
                "image_mode": "frames",
                "video_ids": {
                    str(author_id): f"{int(row['source_id'])}:{int(row['message_id'])}"
                },
            }
        )

    def _open_profile_frames(self, mode: str) -> None:
        author_id = self._selected_profile_id()
        if author_id is None:
            return
        if author_id <= 0:
            self.hoard_app.set_notice(
                "Каналы и чаты нельзя анализировать как профиль.", error=True
            )
            return
        self.hoard_app.open_telegram_analysis(
            {"author_ids": [author_id], "image_mode": mode}
        )

    def _process_selected_frames(self) -> None:
        row = self._selected_download()
        if row is None:
            return
        source_id = int(row.get("source_id") or 0)
        message_id = int(row.get("message_id") or 0)
        self.run_worker(
            self.hoard_app.execute(
                f"frames-process:{source_id}:{message_id}",
                lambda: self.hoard_app.api.telegram_call(
                    "telegram_frames_process",
                    {"source_id": source_id, "message_id": message_id},
                ),
                pending="Запускаем FFmpeg для выбранного кружка…",
            ),
            name="frames-process",
            exit_on_error=False,
        )

    def _confirm_frames_bulk(self, *, retry_failed: bool) -> None:
        stats = (self.hoard_app.status or {}).get("video_frames") or {}
        amount = int(stats.get("failed" if retry_failed else "actionable") or 0)
        if amount <= 0:
            self.hoard_app.set_notice(
                "Ошибок кадров нет" if retry_failed else "Ожидающих кружков нет"
            )
            return
        title = "Повторить ошибки кадров?" if retry_failed else "Обработать ожидающие кружки?"
        self.app.push_screen(
            ConfirmDialog(
                title,
                f"Будет обработано до {amount} наборов. FFmpeg заметно нагружает CPU и "
                "работает только при остановленном Telegram-сборщике. Закрытие TUI не "
                "останавливает демон; для остановки используй кнопку в Архиве.",
                confirm_label="Запустить FFmpeg",
            ),
            lambda confirmed: self._frames_action(
                "telegram_frames_retry_failed"
                if retry_failed
                else "telegram_frames_backfill_start",
                {"confirmed": True},
            )
            if confirmed
            else None,
        )

    def _frames_action(self, method: str, args: dict[str, Any] | None = None) -> None:
        self.run_worker(
            self._run_frames_action(method, args or {}),
            name="frames-backfill",
            exit_on_error=False,
        )

    async def _run_frames_action(self, method: str, args: dict[str, Any]) -> None:
        try:
            result = await self.hoard_app.api.telegram_call(method, args)
        except Exception as exc:
            self.hoard_app.set_notice(str(exc), error=True)
            return
        self.hoard_app.set_notice(str(result.get("message") or "Готово"))
        await self.hoard_app.refresh_status()

    def _toggle_profile_selection(self) -> None:
        author_id = self._selected_profile_id()
        if author_id is None:
            return
        if author_id <= 0:
            self.hoard_app.set_notice(
                "Каналы и чаты нельзя выбирать для анализа профиля.", error=True
            )
            return
        if author_id in self._selected_profiles:
            self._selected_profiles.remove(author_id)
        else:
            self._selected_profiles.add(author_id)
        self._render_table(self.hoard_app.status or {})

    def _analyze_selected_profiles(self, image_mode: str = "photos") -> None:
        if not self._selected_profiles:
            self.hoard_app.set_notice("Сначала выбери один или несколько профилей.", error=True)
            return
        selector: dict[str, Any] = {"author_ids": sorted(self._selected_profiles)}
        if image_mode != "photos":
            selector["image_mode"] = image_mode
        self.hoard_app.open_telegram_analysis(selector)
