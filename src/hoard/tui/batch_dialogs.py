"""Read-only Telegram AI batch history and participant details."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Static

BatchCatalogLoader = Callable[[int], Awaitable[dict[str, Any]]]
BatchLoader = Callable[[str, int], Awaitable[dict[str, Any]]]


class TelegramBatchHistoryDialog(ModalScreen[None]):
    """Browse durable batches and their bounded participant pages without mutations."""

    BINDINGS = [Binding("escape", "close", "Закрыть")]
    PAGE_SIZE = 20

    def __init__(
        self,
        *,
        title: str,
        catalog_loader: BatchCatalogLoader,
        batch_loader: BatchLoader,
    ) -> None:
        super().__init__()
        self.title_text = title
        self.catalog_loader = catalog_loader
        self.batch_loader = batch_loader
        self.mode = "catalog"
        self.catalog_offset = 0
        self.total = 0
        self.items: list[dict[str, Any]] = []
        self.batch: dict[str, Any] | None = None
        self.batch_offset = 0
        self._loading = False
        self._request_id = 0
        self._loading_owner: int | None = None
        self._intent: tuple[object, ...] = ("catalog", 0)

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog batch-dialog"):
            yield Static(self.title_text, id="batch-title", classes="dialog-title")
            yield Static(
                "Только локальный просмотр: открытие группы не запускает AI, resume или retry.",
                id="batch-safety",
                classes="table-help",
            )
            yield Static(id="batch-summary")
            yield DataTable(id="batch-table", cursor_type="row")
            with Horizontal(classes="dialog-actions"):
                yield Button("←", id="batch-prev")
                yield Button("Открыть", id="batch-open", variant="primary")
                yield Button("Анализ", id="batch-analysis")
                yield Button("Назад к группам", id="batch-back")
                yield Button("→", id="batch-next")
                yield Button("Закрыть", id="batch-close")

    def on_mount(self) -> None:
        self.query_one("#batch-back", Button).display = False
        self.query_one("#batch-analysis", Button).display = False
        self._request_catalog()

    def action_close(self) -> None:
        self._invalidate_request()
        self.dismiss(None)

    def on_unmount(self) -> None:
        self._invalidate_request()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        identifier = str(event.button.id)
        if identifier == "batch-close":
            self.action_close()
        elif identifier == "batch-prev":
            self._move(-1)
        elif identifier == "batch-next":
            self._move(1)
        elif identifier == "batch-open":
            self._open_selected_batch()
        elif identifier == "batch-back":
            self._invalidate_request()
            self.mode = "catalog"
            self.batch = None
            self._intent = ("catalog", self.catalog_offset)
            self._render_catalog()
        elif identifier == "batch-analysis":
            self._show_selected_analysis()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "batch-table":
            return
        if self.mode == "catalog":
            self._open_selected_batch()
        else:
            self._show_selected_analysis()

    def _move(self, direction: int) -> None:
        if self._loading:
            return
        if self.mode == "catalog":
            target = max(0, self.catalog_offset + direction * self.PAGE_SIZE)
            if target != self.catalog_offset and (direction < 0 or target < self.total):
                self._request_catalog(target)
            return
        batch = self.batch or {}
        target = max(0, self.batch_offset + direction * self.PAGE_SIZE)
        total = int(batch.get("total") or 0)
        if target != self.batch_offset and (direction < 0 or target < total):
            self._request_batch(str(batch.get("id") or ""), target)

    def _begin_request(self, intent: tuple[object, ...]) -> int:
        self._request_id += 1
        token = self._request_id
        self._loading_owner = token
        self._loading = True
        self._intent = intent
        return token

    def _finish_request(self, token: int) -> None:
        if self._loading_owner == token:
            self._loading_owner = None
            self._loading = False

    def _invalidate_request(self) -> None:
        self._request_id += 1
        self._loading_owner = None
        self._loading = False

    def _current_request(self, token: int, intent: tuple[object, ...]) -> bool:
        return (
            self.is_mounted
            and self.app.screen is self
            and token == self._request_id
            and self._loading_owner == token
            and self._intent == intent
        )

    def _request_catalog(self, offset: int | None = None) -> None:
        if self._loading:
            return
        requested_offset = self.catalog_offset if offset is None else offset
        intent = ("catalog", requested_offset)
        token = self._begin_request(intent)
        if self.items:
            self._render_catalog(loading=True)
        self.run_worker(
            self._load_catalog(token, requested_offset, intent),
            name="batch-catalog",
            exit_on_error=False,
        )

    async def _load_catalog(
        self,
        token: int,
        offset: int,
        intent: tuple[object, ...],
    ) -> None:
        try:
            page = await self.catalog_loader(offset)
        except Exception as exc:
            if self._current_request(token, intent):
                message = str(exc) or type(exc).__name__
                self._finish_request(token)
                self._intent = ("catalog", self.catalog_offset)
                self._render_catalog(error=message)
        else:
            if self._current_request(token, intent) and self.mode == "catalog":
                self._finish_request(token)
                self.items = list(page.get("items") or [])
                self.total = int(page.get("total") or 0)
                self.catalog_offset = offset
                self._intent = ("catalog", offset)
                self._render_catalog()
        finally:
            self._finish_request(token)

    def _selected_catalog(self) -> dict[str, Any] | None:
        row = self.query_one("#batch-table", DataTable).cursor_row
        return self.items[row] if 0 <= row < len(self.items) else None

    def _open_selected_batch(self) -> None:
        if self.mode != "catalog":
            return
        item = self._selected_catalog()
        if item is None:
            return
        self._request_batch(str(item.get("id") or ""), 0)

    def _request_batch(self, batch_id: str, offset: int | None = None) -> None:
        if self._loading or not batch_id:
            return
        requested_offset = self.batch_offset if offset is None else offset
        intent = ("details", batch_id, requested_offset)
        token = self._begin_request(intent)
        if self.mode == "details":
            self._render_batch(loading=True)
        self.run_worker(
            self._load_batch(token, batch_id, requested_offset, intent),
            name="batch-participants",
            exit_on_error=False,
        )

    async def _load_batch(
        self,
        token: int,
        batch_id: str,
        offset: int,
        intent: tuple[object, ...],
    ) -> None:
        try:
            result = await self.batch_loader(batch_id, offset)
        except Exception as exc:
            if self._current_request(token, intent):
                message = str(exc) or type(exc).__name__
                self._finish_request(token)
                if self.mode == "details" and self.batch is not None:
                    current_id = str(self.batch.get("id") or "")
                    self._intent = ("details", current_id, self.batch_offset)
                    self._render_batch(error=message)
                else:
                    self.mode = "catalog"
                    self._intent = ("catalog", self.catalog_offset)
                    self._render_catalog(error=message)
        else:
            if self._current_request(token, intent):
                self._finish_request(token)
                self.mode = "details"
                self.batch = dict(result.get("batch") or result)
                self.batch_offset = offset
                self._intent = ("details", batch_id, offset)
                self._render_batch()
        finally:
            self._finish_request(token)

    @staticmethod
    def _reset_table(table: DataTable, columns: tuple[str, ...]) -> None:
        table.clear(columns=True)
        for column in columns:
            table.add_column(column)

    def _render_catalog(self, *, loading: bool = False, error: str = "") -> None:
        table = self.query_one("#batch-table", DataTable)
        self._reset_table(table, ("ID", "Источник", "Режим", "Всего", "Статус", "Создана"))
        for item in self.items:
            state = "завершена" if item.get("terminal") else "выполняется"
            if int(item.get("errors") or 0):
                state += f" · ошибок {item['errors']}"
            table.add_row(
                str(item.get("id") or "—")[:12],
                str(item.get("source_id") or "глобальная"),
                str(item.get("image_mode") or "photos"),
                str(item.get("total") or item.get("selected_count") or 0),
                state,
                str(item.get("created_at") or "—")[:16],
            )
        if not self.items:
            table.add_row("—", "—", "—", "0", "Групп пока нет", "—")
        first = self.catalog_offset + 1 if self.items else 0
        last = self.catalog_offset + len(self.items)
        summary = f"Группы {first}–{last} из {self.total}"
        if loading:
            summary += " · загрузка следующей страницы…"
        elif error:
            summary += f" · ошибка: {error}"
        self.query_one("#batch-summary", Static).update(summary)
        self.query_one("#batch-back", Button).display = False
        self.query_one("#batch-open", Button).display = True
        self.query_one("#batch-analysis", Button).display = False
        self.query_one("#batch-prev", Button).disabled = loading or self.catalog_offset <= 0
        self.query_one("#batch-next", Button).disabled = loading or last >= self.total
        self.query_one("#batch-open", Button).disabled = loading or not self.items
        self.query_one("#batch-safety", Static).update(
            "Только локальный просмотр: открытие группы не запускает AI, resume или retry."
        )

    def _render_batch(self, *, loading: bool = False, error: str = "") -> None:
        batch = self.batch or {}
        table = self.query_one("#batch-table", DataTable)
        self._reset_table(table, ("Автор", "Состояние", "Причина", "Изображения", "Анализ"))
        items = list(batch.get("items") or [])
        for item in items:
            author = str(item.get("display_name") or item.get("current_username") or "—")
            if item.get("author_id") is not None:
                author += f" · {item['author_id']}"
            images = str(item.get("photo_count") or 0)
            if item.get("video_id"):
                images += f" · video {item['video_id']}"
            table.add_row(
                author,
                str(item.get("state") or "—"),
                str(item.get("reason") or item.get("failure_category") or "—")[:48],
                images,
                str(item.get("analysis_id") or "—")[:12],
            )
        if not items:
            table.add_row("—", "—", "Участников на странице нет", "0", "—")
        successful = int(batch.get("successful") or 0)
        errors = int(batch.get("errors") or 0)
        summary = (
            f"Группа {batch.get('id')} · source {batch.get('source_id') or 'global'} · "
            f"{batch.get('image_mode') or 'photos'}\n"
            f"Всего {batch.get('total', 0)} · очередь {batch.get('queued', 0)} · "
            f"успешно {successful} · недостаточно {batch.get('insufficient_data', 0)} · "
            f"не подходит {batch.get('ineligible', 0)} · outcome_unknown "
            f"{batch.get('outcome_unknown', 0)} · ошибки {errors}"
        )
        if loading:
            summary += " · загрузка следующей страницы…"
        elif error:
            summary += f" · ошибка: {error}"
        self.query_one("#batch-summary", Static).update(summary)
        first = self.batch_offset + 1 if items else 0
        last = self.batch_offset + len(items)
        total = int(batch.get("total") or 0)
        self.query_one("#batch-back", Button).display = True
        self.query_one("#batch-back", Button).disabled = False
        self.query_one("#batch-open", Button).display = False
        self.query_one("#batch-analysis", Button).display = True
        self.query_one("#batch-analysis", Button).disabled = loading
        self.query_one("#batch-prev", Button).disabled = loading or self.batch_offset <= 0
        self.query_one("#batch-next", Button).disabled = loading or last >= total
        self.query_one("#batch-safety", Static).update(
            f"Участники {first}–{last} из {total} · только read-only локальные данные"
        )

    def _show_selected_analysis(self) -> None:
        if self.mode != "details":
            return
        batch = self.batch or {}
        items = list(batch.get("items") or [])
        row = self.query_one("#batch-table", DataTable).cursor_row
        if not 0 <= row < len(items):
            return
        item = items[row]
        analysis_id = item.get("analysis_id")
        if not analysis_id:
            self.query_one("#batch-safety", Static).update(
                "Для выбранного участника сохранённого анализа нет."
            )
            return
        scores = "—"
        if item.get("boosted_psl") is not None:
            scores = (
                f"{item.get('raw_psl_min')}–{item.get('raw_psl_max')} · "
                f"boosted {item.get('boosted_psl')}"
            )
        self.query_one("#batch-safety", Static).update(
            f"Анализ {analysis_id} · {item.get('result_status') or '—'} · "
            f"{item.get('presentation') or '—'} · возраст {item.get('estimated_age') or '—'} · "
            f"PSL {scores}"
        )
