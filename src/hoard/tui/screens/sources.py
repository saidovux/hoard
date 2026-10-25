"""Source list, validation, editing and targeted scans."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Static

from ..dialogs import SourceDialog
from .base import WorkspaceScreen


class SourcesScreen(WorkspaceScreen):
    section = "sources"
    screen_title = "Источники"
    screen_subtitle = "Посты Telegram, которые Hoard проверяет в фоне"
    BINDINGS = [
        Binding("a", "add_source", "Добавить"),
        Binding("e", "edit_source", "Изменить"),
        Binding("s", "scan_source", "Проверить"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._source_ids: list[int] = []

    def compose_workspace(self) -> ComposeResult:
        with Horizontal(classes="action-row", id="source-actions"):
            yield Button("Добавить", id="source-add", variant="primary")
            yield Button("Изменить", id="source-edit")
            yield Button("Вкл./выкл.", id="source-toggle")
            yield Button("Проверить выбранный", id="source-scan")
            yield Button("Проверить все", id="source-scan-all")
            yield Button("Анализировать профили", id="source-ai")
            yield Button("AI: кадры", id="source-ai-frames")
            yield Button("AI: кадры + фото", id="source-ai-combined")
        yield Static(
            "Enter — изменить · a — добавить · e — изменить · s — проверить выбранный",
            classes="table-help",
        )
        yield DataTable(id="sources-table", cursor_type="row")

    def render_data(self, status: dict) -> None:
        table = self.query_one("#sources-table", DataTable)
        selected = self.selected_source_id
        if not table.columns:
            table.add_columns(
                "ID",
                "Источник",
                "Состояние",
                "Кружки",
                "Интервал",
                "Последняя проверка",
                "Результат / ошибка",
            )
        table.clear()
        sources = list(status.get("sources") or [])
        self._source_ids = []
        for source in sources:
            source_id = int(source.get("id") or 0)
            self._source_ids.append(source_id)
            name = str(source.get("title") or source.get("username") or "—")
            username = str(source.get("username") or "")
            post_id = source.get("post_id", "—")
            label = f"{name}  @{username}/{post_id}" if username else f"{name}/{post_id}"
            result = str(source.get("last_error") or source.get("last_result") or "—")
            table.add_row(
                str(source_id),
                label,
                "включён" if source.get("enabled") else "отключён",
                str(source.get("download_count", 0)),
                f"{source.get('interval', '—')} с",
                str(source.get("last_checked_at") or "ещё не проверялся"),
                result,
                key=str(source_id),
            )
        if not sources:
            table.add_row("—", "Источников пока нет", "—", "0", "—", "—", "Добавьте пост")
        elif selected in self._source_ids:
            table.move_cursor(row=self._source_ids.index(selected))

    @property
    def selected_source_id(self) -> int | None:
        table = self.query_one("#sources-table", DataTable)
        row = table.cursor_row
        if 0 <= row < len(self._source_ids):
            return self._source_ids[row]
        return None

    def selected_source(self) -> dict[str, Any] | None:
        source_id = self.selected_source_id
        return next(
            (
                source
                for source in (self.hoard_app.status or {}).get("sources", [])
                if int(source.get("id") or 0) == source_id
            ),
            None,
        )

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
            self.action_edit_source()

    def action_add_source(self) -> None:
        self.hoard_app.ensure_telegram(lambda: self._open_source_dialog(None))

    def action_edit_source(self) -> None:
        source = self.selected_source()
        if source is None:
            self.hoard_app.set_notice("Сначала выбери источник.", error=True)
            return
        self.hoard_app.ensure_telegram(lambda: self._open_source_dialog(source))

    def _open_source_dialog(self, source: dict[str, Any] | None) -> None:
        status = self.hoard_app.status or {}
        output = str((source or {}).get("output") or status.get("archive_root") or "~/Videos/hoard")
        self.app.push_screen(
            SourceDialog(self.hoard_app.api, source=source, default_output=output),
            self._source_dialog_closed,
        )

    def _source_dialog_closed(self, changed: bool | None) -> None:
        if changed:
            self.run_worker(
                self.hoard_app.refresh_status(), name="source-refresh", exit_on_error=False
            )

    def _toggle(self) -> None:
        source = self.selected_source()
        if source is None:
            self.hoard_app.set_notice("Сначала выбери источник.", error=True)
            return
        source_id = int(source["id"])
        enabled = not bool(source.get("enabled"))
        self.run_worker(
            self.hoard_app.execute(
                f"source-toggle:{source_id}",
                lambda: self.hoard_app.api.set_source_enabled(source_id, enabled),
                pending="Обновляем состояние источника…",
            ),
            name="source-toggle",
            exit_on_error=False,
        )

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
            ),
            name="source-scan-all",
            exit_on_error=False,
        )
