"""Archived downloads, profiles, jobs and catalog operations."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Select, Static

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
        self._profile_ids: list[int] = []
        self._selected_profiles: set[int] = set()
        self._profile_source = ""
        self._profile_items: list[dict] = []
        self._profile_offset = 0
        self._profile_limit = 50
        self._profile_total = 0
        self._profiles_loading = False

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
            yield Button("Анализировать профиль", id="telegram-ai")
            yield Button("Выбрать / снять", id="profile-select")
            yield Button("Анализировать выбранные", id="profiles-ai")
            yield Button("Открыть активную AI-группу", id="batch-reopen")
        yield Select(
            [("Все публикации", "")],
            value="",
            allow_blank=False,
            id="profile-source-filter",
        )
        with Horizontal(classes="action-row", id="profile-pages"):
            yield Button("← Профили", id="profiles-previous")
            yield Static(id="profiles-page-label")
            yield Button("Профили →", id="profiles-next")
        yield DataTable(id="archive-table", cursor_type="row")

    def render_data(self, status: dict) -> None:
        stats = status.get("stats") or {}
        text = Text()
        text.append(f"Кружки {stats.get('downloads', 0)}", style="#FFFFFF")
        text.append("  ·  ", style="#888888")
        text.append(f"Профили {stats.get('authors', 0)}", style="#FFFFFF")
        text.append("  ·  ", style="#888888")
        text.append(f"Фото профилей {stats.get('profile_photos', 0)}\n", style="#FFFFFF")
        text.append("HTML  ", style="#888888")
        text.append(str(status.get("catalog") or "путь недоступен"), style="#FFFFFF")
        self.query_one("#archive-summary", Static).update(text)
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

    def _render_table(self, status: dict) -> None:
        table = self.query_one("#archive-table", DataTable)
        table.clear(columns=True)
        self._job_ids = []
        self._profile_ids = []
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
            table.add_columns(
                "✓", "Имя", "Username", "Telegram ID", "Источники", "Фото", "Последнее"
            )
            rows = self._profile_items
            for row in rows:
                self._profile_ids.append(int(row.get("author_id") or 0))
                author_id = int(row.get("author_id") or 0)
                table.add_row(
                    "●" if author_id in self._selected_profiles else "○",
                    str(row.get("display_name") or "—"),
                    "@" + str(row.get("current_username") or "—"),
                    str(row.get("author_id") or "—"),
                    str(row.get("source_count") or 0),
                    str(row.get("photo_count") or 0),
                    str(row.get("photo_error") or row.get("last_seen") or "—"),
                )
            if not rows:
                table.add_row("○", "Профилей пока нет", "—", "—", "0", "0", "—")
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
        self.query_one("#telegram-ai", Button).display = self.mode == "profiles"
        self.query_one("#profile-select", Button).display = self.mode == "profiles"
        self.query_one("#profiles-ai", Button).display = self.mode == "profiles"
        self.query_one("#profile-source-filter", Select).display = self.mode == "profiles"
        self.query_one("#batch-reopen", Button).display = self.mode == "profiles"
        self.query_one("#profile-pages", Horizontal).display = self.mode == "profiles"
        if self.mode == "profiles":
            start = self._profile_offset + 1 if rows else 0
            end = self._profile_offset + len(rows)
            self.query_one("#profiles-page-label", Static).update(
                f"{start}–{end} из {self._profile_total}"
            )
            self.query_one("#profiles-previous", Button).disabled = self._profile_offset <= 0
            self.query_one("#profiles-next", Button).disabled = end >= self._profile_total

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
            for mode in ("downloads", "profiles", "jobs"):
                self.query_one(f"#mode-{mode}", Button).variant = (
                    "primary" if mode == self.mode else "default"
                )
            self._render_table(self.hoard_app.status or {})
            if self.mode == "profiles":
                self._load_profile_page()
        elif button_id == "catalog-build":
            self._build_catalog()
        elif button_id == "telegram-enrich":
            self._confirm_enrich()
        elif button_id == "job-retry":
            self._retry_job()
        elif button_id == "telegram-ai":
            self._open_telegram_ai()
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

    def _analyze_selected_profiles(self) -> None:
        if not self._selected_profiles:
            self.hoard_app.set_notice("Сначала выбери один или несколько профилей.", error=True)
            return
        self.hoard_app.open_telegram_analysis(
            {"author_ids": sorted(self._selected_profiles)}
        )
