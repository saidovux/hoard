"""Archived downloads, profiles, jobs and catalog operations."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Static

from ..dialogs import ConfirmDialog
from ..state import format_bytes
from .base import WorkspaceScreen


class ArchiveScreen(WorkspaceScreen):
    section = "archive"
    screen_title = "Архив"
    screen_subtitle = "Кружки, профили, очередь и переносимый HTML-каталог"

    def __init__(self) -> None:
        super().__init__()
        self.mode = "downloads"
        self._job_ids: list[int] = []

    def compose_workspace(self) -> ComposeResult:
        yield Static(id="archive-summary")
        with Horizontal(classes="action-row", id="archive-modes"):
            yield Button("Кружки", id="mode-downloads", variant="primary")
            yield Button("Профили", id="mode-profiles")
            yield Button("Задания", id="mode-jobs")
        with Horizontal(classes="action-row", id="archive-actions"):
            yield Button("Обновить HTML", id="catalog-build")
            yield Button("Обновить профили", id="telegram-enrich")
            yield Button("Повторить ошибку", id="job-retry")
        yield DataTable(id="archive-table", cursor_type="row")

    def render_data(self, status: dict) -> None:
        stats = status.get("stats") or {}
        profiles = status.get("profiles") or []
        photo_count = sum(int(item.get("photo_count") or 0) for item in profiles)
        text = Text()
        text.append(f"Кружки {stats.get('downloads', 0)}", style="#FFFFFF")
        text.append("  ·  ", style="#888888")
        text.append(f"Профили {stats.get('authors', 0)}", style="#FFFFFF")
        text.append("  ·  ", style="#888888")
        text.append(f"Фото профилей {photo_count}\n", style="#FFFFFF")
        text.append("HTML  ", style="#888888")
        text.append(str(status.get("catalog") or "путь недоступен"), style="#FFFFFF")
        self.query_one("#archive-summary", Static).update(text)
        self._render_table(status)

    def _render_table(self, status: dict) -> None:
        table = self.query_one("#archive-table", DataTable)
        table.clear(columns=True)
        self._job_ids = []
        if self.mode == "downloads":
            table.add_columns("Дата", "Автор", "Источник", "Размер", "Состояние")
            rows = status.get("downloads") or []
            for row in rows:
                author = str(row.get("author_name") or row.get("author_username") or "—")
                source = f"@{row.get('source_name', '—')}/{row.get('post_id', '—')}"
                table.add_row(
                    str(row.get("date") or "—"),
                    author,
                    source,
                    format_bytes(row.get("size")),
                    str(row.get("state") or "saved"),
                )
            if not rows:
                table.add_row("—", "Кружков пока нет", "—", "—", "—")
        elif self.mode == "profiles":
            table.add_columns("Имя", "Username", "Telegram ID", "Источники", "Фото", "Последнее")
            rows = status.get("profiles") or []
            for row in rows:
                table.add_row(
                    str(row.get("display_name") or "—"),
                    "@" + str(row.get("current_username") or "—"),
                    str(row.get("author_id") or "—"),
                    str(row.get("source_count") or 0),
                    str(row.get("photo_count") or 0),
                    str(row.get("photo_error") or row.get("last_seen") or "—"),
                )
            if not rows:
                table.add_row("Профилей пока нет", "—", "—", "0", "0", "—")
        else:
            table.add_columns("ID", "Тип", "Состояние", "Попытки", "Результат", "Обновлено")
            rows = status.get("jobs") or []
            for row in rows:
                job_id = int(row.get("id") or 0)
                self._job_ids.append(job_id)
                table.add_row(
                    str(job_id),
                    str(row.get("kind") or "—"),
                    str(row.get("state") or "—"),
                    str(row.get("attempts") or 0),
                    str(row.get("result") or "—"),
                    str(row.get("updated_at") or "—"),
                    key=str(job_id),
                )
            if not rows:
                table.add_row("—", "Заданий пока нет", "—", "0", "—", "—")
        self.query_one("#job-retry", Button).display = self.mode == "jobs"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = str(event.button.id)
        if button_id.startswith("mode-"):
            self.mode = button_id.removeprefix("mode-")
            for mode in ("downloads", "profiles", "jobs"):
                self.query_one(f"#mode-{mode}", Button).variant = (
                    "primary" if mode == self.mode else "default"
                )
            self._render_table(self.hoard_app.status or {})
        elif button_id == "catalog-build":
            self._build_catalog()
        elif button_id == "telegram-enrich":
            self._confirm_enrich()
        elif button_id == "job-retry":
            self._retry_job()

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
