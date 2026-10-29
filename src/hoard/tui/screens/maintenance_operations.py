"""Explicit global archive operations for Telegram maintenance."""

from __future__ import annotations

import asyncio
from typing import Any

from rich.text import Text
from textual.widgets import Button, Static

from ..colors import ACCENT, MUTED, TEXT
from ..dialogs import ConfirmDialog


class MaintenanceOperationsMixin:
    """HTML, enrichment and FFmpeg actions kept separate from catalog UI."""

    def _build_catalog(self) -> None:
        self.run_worker(
            self.hoard_app.execute(
                "maintenance-catalog-build",
                self.hoard_app.api.rebuild_catalog,
                pending="Собираем единый HTML-каталог…",
            ),
            exit_on_error=False,
        )

    def _open_catalog(self) -> None:
        self.run_worker(
            self.hoard_app.execute(
                "maintenance-catalog-open",
                self.hoard_app.api.open_catalog,
                pending="Проверяем и открываем готовый HTML-каталог…",
                refresh=False,
            ),
            exit_on_error=False,
        )

    def _confirm_enrich(self) -> None:
        self.app.push_screen(
            ConfirmDialog(
                "Обновить профили и публикации?",
                "Операция может обращаться к Telegram и ставить фото-задания в очередь. "
                "Она не запускается при открытии или обновлении экрана.",
                confirm_label="Поставить в очередь",
            ),
            lambda confirmed: self.hoard_app.ensure_telegram(self._run_enrich)
            if confirmed
            else None,
        )

    def _run_enrich(self) -> None:
        self.run_worker(
            self.hoard_app.execute(
                "maintenance-enrich",
                self.hoard_app.api.enrich_telegram,
                pending="Ставим обновление профилей и публикаций в очередь…",
            ),
            exit_on_error=False,
        )

    def _confirm_global_frames(self, *, retry: bool) -> None:
        frames = (self._statistics or {}).get("frames") or {}
        amount = (
            int(frames.get("failed") or 0) + int(frames.get("stale") or 0)
            if retry
            else int(frames.get("waiting") or 0) + int(frames.get("missing") or 0)
        )
        if amount <= 0:
            self.hoard_app.set_notice(
                "Ошибок кадров нет" if retry else "Ожидающих кружков нет"
            )
            return
        self.app.push_screen(
            ConfirmDialog(
                "Повторить ошибки всего архива?" if retry else "Обработать весь архив?",
                f"Область: весь локальный архив, не одна публикация. Подходящих работ: "
                f"{amount}. Очередь ограничена восемью заданиями, работает один FFmpeg; "
                "закрытие TUI не останавливает daemon-сессию.",
                confirm_label="Запустить FFmpeg",
            ),
            lambda confirmed: self._run_frame_action(
                "telegram_frames_retry_failed"
                if retry
                else "telegram_frames_backfill_start",
                {"confirmed": True},
            )
            if confirmed
            else None,
        )

    def _confirm_stop_frames(self) -> None:
        session = self._frame_session
        if not session or str(session.get("state") or "") != "running":
            self.hoard_app.set_notice("Активной FFmpeg-сессии нет.")
            return
        session_id = str(session.get("session_id") or "")
        session_type = str(session.get("session_type") or "ручная")
        source = session.get("source_id")
        scope = f"публикация {source}" if source else "весь архив"
        self.app.push_screen(
            ConfirmDialog(
                "Остановить активную FFmpeg-сессию?",
                f"Будет остановлена сессия {session_type} · {scope} · {session_id[:8]}. "
                "Daemon повторно проверит ID; если сессия успела смениться, остановка отменится.",
                confirm_label="Остановить FFmpeg",
            ),
            lambda confirmed: self._run_frame_action(
                "telegram_frames_backfill_pause",
                {
                    "expected_session_id": session_id,
                    "expected_source_id": source,
                },
            )
            if confirmed
            else None,
        )

    def _run_frame_action(self, method: str, args: dict[str, Any]) -> None:
        self.run_worker(self._perform_frame_action(method, args), exit_on_error=False)

    async def _perform_frame_action(self, method: str, args: dict[str, Any]) -> None:
        try:
            result = await self.hoard_app.api.telegram_call(method, args)
        except Exception as exc:
            self.hoard_app.set_notice(str(exc) or type(exc).__name__, error=True)
            return
        self.hoard_app.set_notice(str(result.get("message") or "Операция подтверждена"))
        await self._load_operations(self._entry_generation, self._request_ids["operations"])

    def _request_operations(self) -> None:
        if not self.is_mounted or self.mode != "operations":
            return
        self._request_ids["operations"] += 1
        request_id = self._request_ids["operations"]
        generation = self._entry_generation
        self.run_worker(
            self._load_operations(generation, request_id),
            name="maintenance-operations-status",
            exit_on_error=False,
        )

    async def _load_operations(self, generation: int, request_id: int) -> None:
        statistics, frame_result = await asyncio.gather(
            self.hoard_app.api.telegram_statistics(),
            self.hoard_app.api.telegram_call("telegram_frames_session_status", {}),
            return_exceptions=True,
        )
        if not (
            self.is_mounted
            and self.app.screen is self
            and self.mode == "operations"
            and generation == self._entry_generation
            and request_id == self._request_ids["operations"]
        ):
            return
        if isinstance(statistics, Exception):
            self._statistics_error = str(statistics) or type(statistics).__name__
        else:
            self._statistics = dict(statistics)
            self._statistics_error = ""
        if isinstance(frame_result, dict):
            session = frame_result.get("session")
            self._frame_session = dict(session) if isinstance(session, dict) else None
            self._frame_session_error = ""
        elif isinstance(frame_result, Exception):
            self._frame_session_error = str(frame_result) or type(frame_result).__name__
        self._render_operations()

    def _poll_work(self) -> None:
        if self.mode != "operations" or self._work_polling:
            return
        self._work_polling = True
        self.run_worker(self._poll_frame_session(), exit_on_error=False)

    async def _poll_frame_session(self) -> None:
        generation = self._entry_generation
        try:
            result = await self.hoard_app.api.telegram_call(
                "telegram_frames_session_status", {}
            )
        except Exception as exc:
            if (
                self.is_mounted
                and self.app.screen is self
                and generation == self._entry_generation
            ):
                self._frame_session_error = str(exc) or type(exc).__name__
                self._render_operations()
            return
        finally:
            self._work_polling = False
        if self.is_mounted and self.app.screen is self and generation == self._entry_generation:
            session = result.get("session")
            self._frame_session = dict(session) if isinstance(session, dict) else None
            self._frame_session_error = ""
            self._render_operations()

    def _resume_work_timer(self) -> None:
        if self._work_timer is not None:
            self._work_timer.resume()

    def _pause_work_timer(self) -> None:
        if self._work_timer is not None:
            self._work_timer.pause()

    def _render_operations(self) -> None:
        if not self.is_mounted:
            return
        frames = (self._statistics or {}).get("frames") or {}
        text = Text()
        text.append("АРХИВ\n", style=f"bold {MUTED}")
        text.append(
            f"Кружки {frames.get('processed_videos', 0)} обработаны · "
            f"ожидают {frames.get('waiting', 0)} · без кадров {frames.get('missing', 0)} · "
            f"ошибки {frames.get('failed', 0)} · stale {frames.get('stale', 0)}",
            style=TEXT,
        )
        session = self._frame_session
        if session:
            completed = int(session.get("completed") or 0)
            successful = int(
                session.get("successful")
                or max(0, completed - int(session.get("errors") or 0))
            )
            source = session.get("source_id")
            scope = f"публикация {source}" if source else "весь архив"
            text.append("\n\nFFMPEG\n", style=f"bold {MUTED}")
            text.append(
                f"{session.get('session_type') or 'ручная'} · {scope} · "
                f"{session.get('state') or '—'}\nЗавершено {completed}/"
                f"{session.get('selected_count', 0)} · успешно {successful} · "
                f"ошибки {session.get('errors', 0)} · ожидает "
                f"{int(session.get('waiting', 0)) + int(session.get('queued', 0))} · "
                f"в работе {session.get('running', 0)}",
                style=TEXT,
            )
        else:
            text.append("\n\nFFmpeg не запущен.", style=MUTED)
        if self._statistics_error:
            text.append(
                "\n\nПоказан последний подтверждённый снимок · "
                + self._statistics_error,
                style=ACCENT,
            )
        if self._frame_session_error:
            text.append(
                "\n\nСтатус FFmpeg устарел · " + self._frame_session_error,
                style=ACCENT,
            )
        self.query_one("#maintenance-operation-status", Static).update(text)
        active = bool(session and str(session.get("state") or "") == "running")
        self.query_one("#maintenance-frames-stop", Button).disabled = not active
        if active and self.mode == "operations" and self.app.screen is self:
            self._resume_work_timer()
        else:
            self._pause_work_timer()


__all__ = ["MaintenanceOperationsMixin"]
