"""Archived downloads, profiles, jobs and catalog operations."""

from __future__ import annotations

import asyncio
import uuid
import webbrowser
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Static

from ..ai_dialogs import TelegramAIDialog
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
            yield Button("AI-анализ профиля", id="telegram-ai")
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
            table.add_columns("Имя", "Username", "Telegram ID", "Источники", "Фото", "Последнее")
            rows = status.get("profiles") or []
            for row in rows:
                self._profile_ids.append(int(row.get("author_id") or 0))
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
        self.query_one("#telegram-ai", Button).display = self.mode == "profiles"

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
        elif button_id == "telegram-ai":
            self._open_telegram_ai()

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
        if author_id is not None:
            self.run_worker(
                self._load_telegram_ai(author_id),
                name=f"telegram-ai:{author_id}",
                exit_on_error=False,
            )

    async def _load_telegram_ai(self, author_id: int) -> None:
        try:
            profile_result, prompts, analyses = await asyncio.gather(
                self.hoard_app.api.ai_call("ai_telegram_profile_get", {"author_id": author_id}),
                self.hoard_app.api.ai_call(
                    "ai_prompt_list", {"scope": "telegram", "limit": 100}
                ),
                self.hoard_app.api.ai_call(
                    "ai_telegram_analysis_list", {"author_id": author_id, "limit": 20}
                ),
            )
        except Exception as exc:
            self.hoard_app.set_notice(str(exc) or type(exc).__name__, error=True)
            return
        self.app.push_screen(
            TelegramAIDialog(
                profile_result["profile"],
                list(prompts.get("items") or []),
                list(analyses.get("items") or []),
            ),
            lambda values: self._telegram_ai_choice(author_id, values),
        )

    def _telegram_ai_choice(self, author_id: int, values: dict | None) -> None:
        if not values:
            return
        if values.get("open_photo_id"):
            self.run_worker(
                self._open_photo(author_id, str(values["open_photo_id"])),
                exit_on_error=False,
            )
            return
        self.app.push_screen(
            ConfirmDialog(
                "Подтвердить взрослого и внешний запрос?",
                "Я подтверждаю, что человеку исполнилось 18 лет на основании сведений вне "
                "изображения. Выбранные фото и текст будут отправлены DeepSeek. PSL субъективен, "
                "не является научной оценкой и запрос может быть платным. Автоповтор отключён.",
                confirm_label="18+ · отправить",
            ),
            lambda confirmed: self.run_worker(
                self._start_telegram_ai(author_id, values), exit_on_error=False
            )
            if confirmed
            else None,
        )

    async def _open_photo(self, author_id: int, photo_id: str) -> None:
        try:
            result = await self.hoard_app.api.ai_call(
                "ai_telegram_photo_get", {"author_id": author_id, "photo_id": photo_id}
            )
            path = Path(str(result["photo"]["file_path"])).resolve(strict=True)
            opened = webbrowser.open(path.as_uri(), new=2)
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            self.hoard_app.set_notice(str(exc) or "Не удалось открыть фото.", error=True)
            return
        self.hoard_app.set_notice(
            "Фото открыто во внешнем просмотрщике." if opened else "Просмотрщик не открыл фото.",
            error=not opened,
        )

    async def _start_telegram_ai(self, author_id: int, values: dict) -> None:
        try:
            result = await self.hoard_app.api.ai_call(
                "ai_telegram_psl_start",
                {
                    "author_id": author_id,
                    "photo_ids": values["photo_ids"],
                    "content": values["content"],
                    "prompt_version_id": values["prompt_version_id"],
                    "provider": "deepseek",
                    "model": values["model"],
                    "operation_id": uuid.uuid4().hex,
                    "adult_confirmed": True,
                    "external_confirmed": True,
                },
            )
        except Exception as exc:
            self.hoard_app.set_notice(str(exc) or type(exc).__name__, error=True)
            return
        analysis = result["analysis"]
        self.hoard_app.set_notice("Telegram AI-анализ поставлен в очередь.")
        await self._poll_analysis(str(analysis["id"]))

    async def _poll_analysis(self, analysis_id: str) -> None:
        for _ in range(60):
            await asyncio.sleep(1)
            try:
                result = await self.hoard_app.api.ai_call(
                    "ai_telegram_analysis_get", {"id": analysis_id}
                )
            except Exception as exc:
                self.hoard_app.set_notice(str(exc), error=True)
                return
            analysis = result["analysis"]
            state = str(analysis["attempt"].get("state") or "")
            if state in {"completed", "failed", "outcome_unknown"}:
                status = analysis.get("result_status") or state
                amount = analysis["attempt"].get("estimated_cost")
                cost = (
                    f"{amount} {analysis['attempt'].get('cost_currency') or ''}"
                    if amount is not None
                    else "стоимость неизвестна"
                )
                message = f"Telegram AI: {status}; {cost}. Результат сохранён в истории."
                self.hoard_app.set_notice(message, error=state != "completed")
                return
        self.hoard_app.set_notice("Ожидание остановлено; анализ продолжает жить в очереди.")
