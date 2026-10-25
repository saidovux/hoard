"""Telegram, the first tool: the account and collector on top, the watched sources below."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Static

from ..colors import ACCENT, MUTED, TEXT
from ..dialogs import ConfirmDialog, SourceDialog
from ..state import collector_label, telegram_label
from ..widgets import Panel
from ..widgets.chrome import Hint
from .base import WorkspaceScreen

SOURCE_COLUMNS = ("Источник", "Состояние", "Кружки", "Каждые", "Проверка", "Результат")
#: Indices into SOURCE_COLUMNS that stay on terminals narrower than 110 columns.
DENSE_SOURCE_COLUMNS = (0, 1, 2, 5)
SOURCE_HINTS: tuple[Hint, ...] = (("a", "добавить"), ("e", "изменить"), ("s", "проверить"))


def _short_time(value: object) -> str:
    """``2026-10-21 10:12:13`` -> ``2026-10-21 10:12``; the seconds never help in a table."""
    text = str(value or "")
    return text[:16] if text else "ещё не проверялся"


class TelegramScreen(WorkspaceScreen):
    section = "telegram"
    HINTS: ClassVar[tuple[Hint, ...]] = (("tab", "панели"), ("[ ]", "раздел"), ("h", "назад"))
    BINDINGS = [
        Binding("a", "add_source", "Добавить"),
        Binding("e", "edit_source", "Изменить"),
        Binding("s", "scan_source", "Проверить"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._source_ids: list[int] = []
        self._dense_table: bool | None = None
        self._web_state: dict[str, Any] = {"state": "stopped"}
        self._web_loading = False

    def compose_workspace(self) -> ComposeResult:
        with Panel(title="Сборщик", id="collector-panel"):
            yield Static(id="telegram-summary")
            with Horizontal(classes="action-row", id="telegram-actions"):
                yield Button("Подключить", id="telegram-connect", variant="primary")
                yield Button("Запустить", id="collector-start")
                yield Button("Пауза", id="collector-pause")
                yield Button("Остановить", id="collector-stop")
        with Panel(title="Web", id="web-panel"):
            yield Static(id="web-summary")
            with Horizontal(classes="action-row", id="web-actions"):
                yield Button("Включить Web", id="web-start", variant="primary")
                yield Button("Открыть Web", id="web-open")
                yield Button("Выключить Web", id="web-stop")
        with Panel(title="Источники", hints=SOURCE_HINTS, id="sources-panel"):
            with Horizontal(classes="action-row", id="source-actions"):
                yield Button("Добавить", id="source-add", variant="primary")
                yield Button("Изменить", id="source-edit")
                yield Button("Вкл./выкл.", id="source-toggle")
                yield Button("Проверить", id="source-scan")
                yield Button("Проверить все", id="source-scan-all")
                yield Button("Анализ профилей", id="source-ai")
            with Horizontal(classes="action-row", id="source-ai-actions"):
                yield Button("AI: кадры", id="source-ai-frames")
                yield Button("AI: кадры + фото", id="source-ai-combined")
            yield DataTable(id="sources-table", cursor_type="row")

    def on_mount(self) -> None:
        super().on_mount()
        self._refresh_web()
        # The sources are the page's working surface: j / k and a / e / s act on them at once.
        self.query_one("#sources-table", DataTable).focus()

    def on_screen_resume(self) -> None:
        super().on_screen_resume()
        self._refresh_web()

    # ── collector ────────────────────────────────────────────────────────────

    def render_data(self, status: dict) -> None:
        self._render_collector(status)
        self._render_web()
        self._render_sources(status)

    def _render_collector(self, status: dict) -> None:
        authorized = str(status.get("telegram") or "") == "authorized"
        queue = status.get("queue") or {}
        summary = Text()
        summary.append("аккаунт".ljust(9), style=MUTED)
        summary.append(telegram_label(status), style=TEXT if authorized else ACCENT)
        summary.append("\n" + "сборщик".ljust(9), style=MUTED)
        summary.append(collector_label(status), style=TEXT)
        summary.append(
            f" · {queue.get('queued', 0)} ожидают · {queue.get('running', 0)} выполняются",
            style=MUTED,
        )
        self.query_one("#telegram-summary", Static).update(summary)

        running = bool(status.get("running"))
        collector_state = str(status.get("collector_state") or "stopped")
        connect = self.query_one("#telegram-connect", Button)
        connect.label = "Проверить сессию" if authorized else "Подключить"
        self.query_one("#collector-start", Button).label = (
            "Возобновить" if collector_state == "paused" else "Запустить"
        )
        self.query_one("#collector-start", Button).disabled = running
        self.query_one("#collector-pause", Button).disabled = not running
        self.query_one("#collector-stop", Button).disabled = (
            not running and collector_state == "stopped"
        )

    def _render_web(self) -> None:
        state = str(self._web_state.get("state") or "stopped")
        labels = {
            "stopped": "выключен",
            "starting": "запускается",
            "ready": "работает",
            "stopping": "останавливается",
            "error": "ошибка",
        }
        text = Text()
        text.append("состояние".ljust(11), style=MUTED)
        text.append(labels.get(state, state), style=ACCENT if state == "error" else TEXT)
        if state == "ready" and self._web_state.get("port"):
            text.append(f" · 127.0.0.1:{self._web_state['port']}", style=MUTED)
        if state == "error" and self._web_state.get("error"):
            text.append("\n" + str(self._web_state["error"]), style=ACCENT)
        if state == "ready" and not self._web_state.get("controllable", True):
            text.append(
                "\nWeb запущен без управляющего канала; перезапусти его вручную",
                style=ACCENT,
            )
        self.query_one("#web-summary", Static).update(text)

        busy = self._web_loading
        controllable = bool(self._web_state.get("controllable", True))
        start = self.query_one("#web-start", Button)
        open_button = self.query_one("#web-open", Button)
        stop = self.query_one("#web-stop", Button)
        start.disabled = busy or state in {"starting", "ready", "stopping"}
        open_button.disabled = busy or state != "ready" or not controllable
        stop.disabled = busy or state != "ready" or not controllable

    def _refresh_web(self) -> None:
        if self._web_loading:
            return
        self._web_loading = True
        self._render_web()
        self.run_worker(self._load_web_state(), name="web-status", exit_on_error=False)

    async def _load_web_state(self) -> None:
        try:
            self._web_state = await self.hoard_app.api.web_status()
        except Exception as exc:
            self._web_state = {"state": "error", "error": str(exc) or type(exc).__name__}
        finally:
            self._web_loading = False
            if self.is_mounted:
                self._render_web()

    def _web_start(self) -> None:
        if self._web_loading:
            return
        self._web_state = {"state": "starting"}
        self._run_web_action("web-start", self.hoard_app.api.start_web, "Включаем Web…")

    def _web_open(self) -> None:
        self._run_web_action("web-open", self.hoard_app.api.open_web, "Открываем Web…")

    def _web_stop(self) -> None:
        if self._web_loading:
            return
        self._web_state = {"state": "stopping"}
        self._run_web_action("web-stop", self.hoard_app.api.stop_web, "Останавливаем Web…")

    def _run_web_action(
        self,
        key: str,
        operation: Callable[[], Awaitable[dict[str, Any]]],
        pending: str,
    ) -> None:
        if self._web_loading:
            return
        self._web_loading = True
        self.hoard_app.set_notice(pending)
        self._render_web()
        self.run_worker(self._perform_web_action(operation), name=key, exit_on_error=False)

    async def _perform_web_action(
        self, operation: Callable[[], Awaitable[dict[str, Any]]]
    ) -> None:
        try:
            result = await operation()
        except Exception as exc:
            self.hoard_app.set_notice(str(exc) or type(exc).__name__, error=True)
        else:
            self.hoard_app.set_notice(str(result.get("message") or "Готово"))
        finally:
            await self._load_web_state()

    def _connect(self) -> None:
        self.hoard_app.open_auth()

    def _start(self) -> None:
        self.hoard_app.ensure_telegram(self._start_authorized)

    def _start_authorized(self) -> None:
        self._control(
            "collector-start",
            self.hoard_app.api.start_collector,
            "Запускаем сборщик…",
        )

    def _pause(self) -> None:
        self._confirm_interrupt("pause")

    def _stop(self) -> None:
        self._confirm_interrupt("stop")

    def _confirm_interrupt(self, operation: str) -> None:
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

    def _run_interrupt(self, operation: str) -> None:
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
        self,
        key: str,
        operation: Callable[[], Awaitable[dict[str, Any]]],
        pending: str,
    ) -> None:
        self.run_worker(
            self.hoard_app.execute(key, operation, pending=pending),
            name=key,
            exit_on_error=False,
        )

    # ── sources ──────────────────────────────────────────────────────────────

    def _render_sources(self, status: dict) -> None:
        table = self.query_one("#sources-table", DataTable)
        selected = self.selected_source_id
        # Below 110 columns the schedule columns would not fit and the table would scroll
        # sideways, so only the ones that say what happened are kept.
        dense = self.hoard_app.has_class("medium")
        short_labels = self.hoard_app.has_class("narrow")
        keep = DENSE_SOURCE_COLUMNS if dense else range(len(SOURCE_COLUMNS))
        if dense != self._dense_table:
            table.clear(columns=True)
            table.add_columns(*(SOURCE_COLUMNS[index] for index in keep))
            self._dense_table = dense
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
            if short_labels and source.get("title"):
                label = name  # the post address is in the edit dialog; here the room is for state
            cells = (
                label,
                "включён" if source.get("enabled") else "отключён",
                str(source.get("download_count", 0)),
                f"{source.get('interval', '—')} с",
                _short_time(source.get("last_checked_at")),
                str(source.get("last_error") or source.get("last_result") or "—"),
            )
            table.add_row(*(cells[index] for index in keep), key=str(source_id))
        if not sources:
            empty = ("Источников пока нет", "—", "0", "—", "—", "Добавьте пост")
            table.add_row(*(empty[index] for index in keep))
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
            "telegram-connect": self._connect,
            "collector-start": self._start,
            "collector-pause": self._pause,
            "collector-stop": self._stop,
            "web-start": self._web_start,
            "web-open": self._web_open,
            "web-stop": self._web_stop,
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
