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
        self.dismiss(None)

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
            self.mode = "catalog"
            self.batch = None
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
                self.catalog_offset = target
                self._request_catalog()
            return
        batch = self.batch or {}
        target = max(0, self.batch_offset + direction * self.PAGE_SIZE)
        total = int(batch.get("total") or 0)
        if target != self.batch_offset and (direction < 0 or target < total):
            self.batch_offset = target
            self._request_batch(str(batch.get("id") or ""))

    def _request_catalog(self) -> None:
        if self._loading:
            return
        self._loading = True
        self.run_worker(self._load_catalog(), name="batch-catalog", exit_on_error=False)

    async def _load_catalog(self) -> None:
        try:
            page = await self.catalog_loader(self.catalog_offset)
        except Exception as exc:
            if self.is_mounted:
                self.query_one("#batch-summary", Static).update(
                    str(exc) or type(exc).__name__
                )
        else:
            if self.is_mounted and self.mode == "catalog":
                self.items = list(page.get("items") or [])
                self.total = int(page.get("total") or 0)
                self._render_catalog()
        finally:
            self._loading = False

    def _selected_catalog(self) -> dict[str, Any] | None:
        row = self.query_one("#batch-table", DataTable).cursor_row
        return self.items[row] if 0 <= row < len(self.items) else None

    def _open_selected_batch(self) -> None:
        if self.mode != "catalog":
            return
        item = self._selected_catalog()
        if item is None:
            return
        self.batch_offset = 0
        self._request_batch(str(item.get("id") or ""))

    def _request_batch(self, batch_id: str) -> None:
        if self._loading or not batch_id:
            return
        self._loading = True
        self.run_worker(
            self._load_batch(batch_id), name="batch-participants", exit_on_error=False
        )

    async def _load_batch(self, batch_id: str) -> None:
        try:
            result = await self.batch_loader(batch_id, self.batch_offset)
        except Exception as exc:
            if self.is_mounted:
                self.query_one("#batch-summary", Static).update(
                    str(exc) or type(exc).__name__
                )
        else:
            if self.is_mounted:
                self.mode = "details"
                self.batch = dict(result.get("batch") or result)
                self._render_batch()
        finally:
            self._loading = False

    @staticmethod
    def _reset_table(table: DataTable, columns: tuple[str, ...]) -> None:
        table.clear(columns=True)
        for column in columns:
            table.add_column(column)

    def _render_catalog(self) -> None:
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
        self.query_one("#batch-summary", Static).update(
            f"Группы {first}–{last} из {self.total}"
        )
        self.query_one("#batch-back", Button).display = False
        self.query_one("#batch-open", Button).display = True
        self.query_one("#batch-analysis", Button).display = False
        self.query_one("#batch-prev", Button).disabled = self.catalog_offset <= 0
        self.query_one("#batch-next", Button).disabled = last >= self.total

    def _render_batch(self) -> None:
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
        self.query_one("#batch-summary", Static).update(
            f"Группа {batch.get('id')} · source {batch.get('source_id') or 'global'} · "
            f"{batch.get('image_mode') or 'photos'}\n"
            f"Всего {batch.get('total', 0)} · очередь {batch.get('queued', 0)} · "
            f"успешно {successful} · недостаточно {batch.get('insufficient_data', 0)} · "
            f"не подходит {batch.get('ineligible', 0)} · outcome_unknown "
            f"{batch.get('outcome_unknown', 0)} · ошибки {errors}"
        )
        first = self.batch_offset + 1 if items else 0
        last = self.batch_offset + len(items)
        total = int(batch.get("total") or 0)
        self.query_one("#batch-back", Button).display = True
        self.query_one("#batch-open", Button).display = False
        self.query_one("#batch-analysis", Button).display = True
        self.query_one("#batch-prev", Button).disabled = self.batch_offset <= 0
        self.query_one("#batch-next", Button).disabled = last >= total
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
