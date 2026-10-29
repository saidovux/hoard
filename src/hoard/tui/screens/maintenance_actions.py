"""Selected-object actions for bounded Telegram maintenance catalogs."""

from __future__ import annotations

import asyncio
from typing import Any

from textual.widgets import Input, Select

from ..ai_dialogs import VideoFramesDialog
from ..maintenance_dialogs import (
    MaintenanceDetailDialog,
    ProfileDetailDialog,
    detail_text,
)
from ..state import format_bytes


class MaintenanceActionsMixin:
    """Details, retry, profile selection/PSL and stable-ID media actions."""

    def _show_details(self) -> None:
        item = self._selected_item()
        if item is None:
            return
        if self.mode == "jobs":
            self.run_worker(self._load_job_details(int(item["id"])))
        elif self.mode == "profiles":
            self.run_worker(self._load_profile_details(int(item["author_id"])))
        elif self.mode == "media":
            self.run_worker(
                self._load_media_details(int(item["source_id"]), int(item["message_id"]))
            )

    async def _load_job_details(self, job_id: int) -> None:
        try:
            result = await self.hoard_app.api.job_get(job_id)
        except Exception as exc:
            self.hoard_app.set_notice(str(exc) or type(exc).__name__, error=True)
            return
        item = dict(result["job"])
        manual = "да" if item.get("manual_permission_required") else "нет"
        self.app.push_screen(
            MaintenanceDetailDialog(
                f"Задание #{job_id}",
                detail_text(
                    (
                        ("Тип", item.get("kind")),
                        ("Состояние", item.get("state")),
                        ("Попытки", item.get("attempts")),
                        ("Создано", item.get("created_at")),
                        ("Обновлено", item.get("updated_at")),
                        ("Ручной FFmpeg", manual),
                        ("Результат / ошибка", item.get("result")),
                    )
                ),
            )
        )

    async def _load_profile_details(self, author_id: int) -> None:
        try:
            profile_result, analyses = await asyncio.gather(
                self.hoard_app.api.ai_call(
                    "ai_telegram_profile_get", {"author_id": author_id}
                ),
                self.hoard_app.api.ai_call(
                    "ai_telegram_analysis_list", {"author_id": author_id, "limit": 20}
                ),
            )
        except Exception as exc:
            self.hoard_app.set_notice(str(exc) or type(exc).__name__, error=True)
            return
        self.app.push_screen(
            ProfileDetailDialog(
                dict(profile_result["profile"]), list(analyses.get("items") or [])
            ),
            lambda value: self._profile_dialog_closed(author_id, value),
        )

    def _profile_dialog_closed(self, author_id: int, value: dict[str, Any] | None) -> None:
        if not value or not value.get("photo_id"):
            return
        self.run_worker(
            self._perform_media_open(
                {"kind": "photo", "author_id": author_id, "photo_id": value["photo_id"]}
            )
        )

    async def _load_media_details(self, source_id: int, message_id: int) -> None:
        try:
            result = await self.hoard_app.api.telegram_call(
                "telegram_media_get", {"source_id": source_id, "message_id": message_id}
            )
        except Exception as exc:
            self.hoard_app.set_notice(str(exc) or type(exc).__name__, error=True)
            return
        item = dict(result["media"])
        self.app.push_screen(
            MaintenanceDetailDialog(
                f"Видеокружок {message_id}",
                detail_text(
                    (
                        ("Source ID", item.get("source_id")),
                        ("Публикация", item.get("source_username") or "legacy / без связи"),
                        ("Message ID", item.get("message_id")),
                        ("Автор ID", item.get("author_id")),
                        ("Автор", item.get("author_name") or item.get("author_username")),
                        ("Дата", item.get("date")),
                        ("Размер", format_bytes(item.get("size"))),
                        (
                            "MP4",
                            "доступен"
                            if item.get("available")
                            else item.get("availability_error"),
                        ),
                        ("Кадры", item.get("frame_status")),
                        ("Ошибка кадров", item.get("frame_error")),
                    )
                ),
            )
        )

    def _retry_job(self) -> None:
        item = self._selected_item()
        if not item or not item.get("retryable"):
            return
        job_id = int(item["id"])
        self.run_worker(
            self._perform_job_retry(job_id),
            name="maintenance-job-retry",
            exit_on_error=False,
        )

    async def _perform_job_retry(self, job_id: int) -> None:
        await self.hoard_app.execute(
            f"maintenance-job-retry:{job_id}",
            lambda: self.hoard_app.api.retry_job(job_id),
            pending=f"Возвращаем задание #{job_id} в очередь…",
            refresh=False,
        )
        if self.is_mounted and self.mode == "jobs":
            self._request_page("jobs")

    def action_toggle_profile(self) -> None:
        if self.mode != "profiles":
            return
        item = self._selected_item()
        author_id = int(item.get("author_id") or 0) if item else 0
        if author_id <= 0:
            return
        if author_id in self.marked_profiles:
            self.marked_profiles.remove(author_id)
        elif len(self.marked_profiles) >= 100:
            self.hoard_app.set_notice("Можно выбрать не более 100 профилей.", error=True)
            return
        else:
            self.marked_profiles.add(author_id)
        self._render_table()

    def action_select_visible(self) -> None:
        if self.mode != "profiles":
            return
        visible = {
            int(item.get("author_id") or 0)
            for item in self.pages["profiles"]["items"]
            if int(item.get("author_id") or 0) > 0
        }
        combined = self.marked_profiles | visible
        if len(combined) > 100:
            self.hoard_app.set_notice("Выбор превысит предел 100 профилей.", error=True)
            return
        self.marked_profiles = combined
        self._render_table()

    def action_clear_profiles(self) -> None:
        self.marked_profiles.clear()
        if self.mode == "profiles":
            self._render_table()

    def _psl_mode(self) -> str:
        value = self.query_one("#maintenance-psl-mode", Select).value
        return str(value) if value in {"photos", "frames", "combined"} else "photos"

    def _analyze_current_profile(self) -> None:
        item = self._selected_item()
        author_id = int(item.get("author_id") or 0) if item else 0
        if author_id > 0:
            self.hoard_app.open_telegram_analysis(
                {"author_ids": [author_id], "image_mode": self._psl_mode()}
            )

    def _analyze_marked_profiles(self) -> None:
        if self.marked_profiles:
            self.hoard_app.open_telegram_analysis(
                {
                    "author_ids": sorted(self.marked_profiles),
                    "image_mode": self._psl_mode(),
                }
            )

    def _selected_media(self) -> dict[str, Any] | None:
        return self._selected_item() if self.mode == "media" else None

    def _open_media(self) -> None:
        item = self._selected_media()
        if item:
            self.run_worker(
                self._perform_media_open(
                    {
                        "kind": "video",
                        "source_id": item["source_id"],
                        "message_id": item["message_id"],
                    }
                )
            )

    async def _perform_media_open(self, args: dict[str, Any]) -> None:
        try:
            result = await self.hoard_app.api.telegram_call("telegram_media_open", args)
        except Exception as exc:
            self.hoard_app.set_notice(str(exc) or type(exc).__name__, error=True)
            return
        self.hoard_app.set_notice(str(result.get("message") or "Локальный файл открыт"))

    def _open_media_frames(self) -> None:
        item = self._selected_media()
        if item:
            self.run_worker(self._load_media_frames(dict(item)))

    async def _load_media_frames(self, item: dict[str, Any]) -> None:
        source_id = int(item.get("source_id") or 0)
        message_id = int(item.get("message_id") or 0)
        try:
            result = await self.hoard_app.api.telegram_call(
                "telegram_frames_get", {"source_id": source_id, "message_id": message_id}
            )
        except Exception as exc:
            self.hoard_app.set_notice(str(exc) or type(exc).__name__, error=True)
            return
        frames = result.get("frames")
        if not frames:
            self.hoard_app.set_notice(
                "Кадры ещё не извлечены; открой публикацию или глобальные операции FFmpeg.",
                error=True,
            )
            return
        self.app.push_screen(
            VideoFramesDialog(frames, video_id=f"{source_id}:{message_id}"),
            self._frames_dialog_closed,
        )

    def _frames_dialog_closed(self, value: dict[str, Any] | None) -> None:
        if not value or not value.get("analyze") or value.get("author_id") is None:
            return
        author_id = int(value["author_id"])
        self.hoard_app.open_telegram_analysis(
            {
                "author_ids": [author_id],
                "image_mode": "frames",
                "video_ids": {str(author_id): str(value.get("video_id") or "")},
            }
        )

    def _open_media_source(self) -> None:
        item = self._selected_media()
        if not item or not item.get("source_available"):
            self.hoard_app.set_notice("У legacy-медиа нет доступной публикации.", error=True)
            return
        self.run_worker(self._load_media_source(int(item["source_id"])))

    async def _load_media_source(self, source_id: int) -> None:
        try:
            result = await self.hoard_app.api.telegram_source(source_id)
        except Exception as exc:
            self.hoard_app.set_notice(str(exc) or type(exc).__name__, error=True)
            return
        self.hoard_app.open_publication(dict(result["source"]))

    def _open_media_author(self) -> None:
        item = self._selected_media()
        author_id = int(item.get("author_id") or 0) if item else 0
        if author_id <= 0:
            self.hoard_app.set_notice("Автор кружка не определён.", error=True)
            return
        self._query = str(author_id)
        self.query_one("#maintenance-search", Input).value = self._query
        self.pages["profiles"].update(offset=0, loaded=False)
        self._activate_mode("profiles", force=True)


__all__ = ["MaintenanceActionsMixin"]
