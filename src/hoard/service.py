"""Local supervisor and IPC; no HTTP server and no Telegram client in the UI."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import shutil
import signal
import tempfile
import uuid
from collections.abc import Callable
from functools import partial
from pathlib import Path

from . import __version__
from .ai import AICore, AIRequest, create_default_core, read_ai_config, write_ai_config
from .ai.chat_service import AIChatService
from .ai.config import AIConfig
from .ai.errors import AIAuthenticationError, AIConfigurationError
from .ai.ipc_api import AIIPC
from .archive_paths import telegram_catalog_file
from .catalog import build_catalog_from_db
from .config import read_credentials, write_credentials
from .integrations.media import download, validate_media
from .integrations.rclone import upload, validate_upload
from .ipc import IPC_MAX_BYTES, IPC_PROTOCOL, encode_ipc_message
from .jobs import JobRunner
from .media_open import open_local_file
from .models import parse_post_url
from .paths import db_path, endpoint_path, ensure_private, socket_path, state_dir
from .platform_support import is_windows, remove_endpoint_if_owned, single_instance, write_endpoint
from .storage import Store
from .telegram import AuthFlowError, AuthState, TelegramEngine
from .video_frames import (
    FRAME_ALGORITHM_VERSION,
    ExtractedFrames,
    FrameExtractionError,
    extract_frames,
)

AUTH_OPERATION_TIMEOUT = 80


class Service:
    def __init__(
        self,
        store: Store | None = None,
        engine: TelegramEngine | None = None,
        ai_core: AICore | None = None,
        media_opener: Callable[[Path], bool] | None = None,
    ):
        self.store = store or Store(db_path())
        self.progress: dict = {}
        self._telegram_lock = asyncio.Lock()
        self._report_lock = asyncio.Lock()
        self._stop_lock = asyncio.Lock()
        self._mode_lock = asyncio.Lock()
        self.engine = engine or TelegramEngine(self.store, self._progress)
        self._media_opener = media_opener or open_local_file
        self.runner = JobRunner(
            self.store,
            {
                "telegram.scan": self._telegram_scan,
                "telegram.enrich_source": self._telegram_enrich_source,
                "telegram.post_media": self._telegram_post_media,
                "telegram.profile_photos": self._telegram_profile_photos,
                "telegram.report": self._telegram_report,
                "cloud.copy": self._cloud_copy,
                "media.download": self._media_download,
            },
            parallel=int(os.environ.get("HOARD_MAX_JOBS", "3")),
            name="telegram",
        )
        try:
            ai_config = read_ai_config()
        except AIConfigurationError:
            # A broken optional AI config must not take down Telegram management.
            # The unconfigured provider will report an actionable error only if used.
            ai_config = AIConfig()
        self._injected_ai_core = ai_core is not None
        self.ai_core = ai_core or create_default_core(ai_config)
        self._ai_key_state = "key_saved" if self.ai_core.is_configured() else "not_configured"
        self.ai_service = AIChatService(self.store.ai, self.ai_core, self.store.telegram)
        self.ai_runner = JobRunner(
            self.store,
            {"ai.request": self._ai_request, "ai.chat": self._ai_chat},
            parallel=ai_config.max_parallel,
            # A cancelled or crash-interrupted paid request may already have reached
            # the provider. It must require an explicit retry to avoid duplicate cost.
            retry_interrupted=False,
            name="ai",
            include_job_id=True,
            redact_errors=True,
        )
        self.ai_ipc = AIIPC(
            self.store.ai, self.ai_service, self.ai_runner, self.store.telegram
        )
        self.media_runner = JobRunner(
            self.store,
            {"telegram.video_frames": self._telegram_video_frames},
            parallel=1,
            retry_interrupted=True,
            name="media",
            scheduler=self._enqueue_frame_backfill,
        )
        # Frame jobs are durable, but permission to execute them is deliberately
        # process-local.  A new daemon always revokes an interrupted permission.
        self.store.telegram.disable_frame_processing()
        self._frame_mode = "off"
        self._frame_session_task: asyncio.Task | None = None
        self._catalog_task: asyncio.Task | None = None
        self.stop_requested = asyncio.Event()
        self.collector_state = "stopped"

    @property
    def running(self) -> bool:
        return self.runner.running

    @property
    def ai_running(self) -> bool:
        return self.ai_runner.running

    def _progress(self, value: dict) -> None:
        self.progress = value

    def _source(self, source_id: int) -> dict | None:
        return self.store.source(source_id)

    def _queue_report(self) -> int | None:
        if not self.store.telegram.catalog_report_due():
            self.runner.wakeup()
            return None
        job_id = self.store.enqueue(
            "telegram.report", {}, unique_key="telegram-report"
        )
        self.runner.wakeup()
        return job_id

    def enqueue_ai(
        self,
        request: AIRequest,
        *,
        provider: str | None = None,
        unique_key: str | None = None,
    ) -> int:
        """Queue an explicit AI request for current and future Hoard modules."""
        payload = {"provider": provider, "request": request.to_dict()}
        jid = self.store.enqueue("ai.request", payload, unique_key=unique_key)
        self.ai_service.register_legacy_attempt(jid, payload)
        self.ai_runner.wakeup()
        return jid

    def _schedule_catalog_publish(self) -> None:
        """Coalesce an eventual publication without blocking the caller."""
        if not self.store.telegram.catalog_dirty():
            return
        if self.runner.running:
            self._queue_report()
            return
        if self._catalog_task is not None and not self._catalog_task.done():
            return
        self._catalog_task = asyncio.create_task(
            self._publish_catalog_when_due(), name="hoard-catalog-deferred"
        )

    async def _publish_catalog_when_due(self) -> None:
        """Honour the existing cooldown/backoff and publish if no runner is active."""
        try:
            for attempt in range(3):
                while self.store.telegram.catalog_dirty():
                    if self.store.telegram.catalog_report_due():
                        break
                    await asyncio.sleep(0.25)
                if not self.store.telegram.catalog_dirty():
                    return
                if self.runner.running:
                    self._queue_report()
                    return
                try:
                    await self._telegram_report({})
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if attempt >= 2:
                        return
                    continue
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            # A closing store or a transient local error must not surface as an
            # unhandled background-task exception.  The durable dirty marker
            # remains for the normal startup/report retry path.
            return
        finally:
            self._catalog_task = None

    async def _publish_ai_catalog(self) -> None:
        """Publish AI-visible changes even when the Telegram collector is stopped."""
        if not self.store.telegram.catalog_dirty():
            return
        if self.runner.running:
            self._queue_report()
            return
        if not self.store.telegram.catalog_report_due():
            # A recent cooldown or a previous failure must not swallow the dirty
            # marker.  A bounded deferred task publishes when it is due.
            self._schedule_catalog_publish()
            return
        try:
            await self._telegram_report({})
        except Exception:
            # The AI result remains saved; keep the existing dirty marker and
            # schedule the normal cooldown-aware retry instead of dropping it.
            self._schedule_catalog_publish()

    def _enqueue_frame_backfill(self) -> None:
        """Feed a bounded old-archive backfill without touching the Telegram path."""
        telegram = self.store.telegram
        if self._frame_mode != "bulk" or not telegram.frame_backfill_active():
            return
        work = telegram.frame_work_state()
        slots = max(0, 8 - int(work["queued"]) - int(work["running"]))
        if slots == 0:
            return
        slots -= telegram.authorize_waiting_frame_jobs(slots)
        if slots == 0:
            return
        cursor = int(telegram.get_setting("frames_backfill_cursor", "0") or 0)
        rows = telegram.next_frame_backfill_downloads(cursor, 50)
        if not rows:
            telegram.set_frame_backfill_cursor(cursor, complete=True)
            return
        last_processed = cursor
        stopped_for_capacity = False
        for row in rows:
            status = str(row.get("frame_status") or "")
            if status in {"saved", "running", "failed", "stale"}:
                last_processed = int(row["cursor_rowid"])
                continue
            if slots <= 0:
                stopped_for_capacity = True
                break
            try:
                telegram.queue_frame_job(
                    int(row["source_id"]),
                    int(row["message_id"]),
                    authorized=True,
                )
            except ValueError:
                # A missing/corrupt old MP4 is recorded by its status and must
                # not stall the traversal.
                pass
            last_processed = int(row["cursor_rowid"])
            slots -= 1
        telegram.set_frame_backfill_cursor(
            last_processed,
            complete=(not stopped_for_capacity and len(rows) < 50),
        )

    def _require_frame_tools(self) -> None:
        missing = [name for name in ("ffmpeg", "ffprobe") if shutil.which(name) is None]
        if missing:
            raise RuntimeError(
                f"Не найден {', '.join(missing)}; установи FFmpeg и повтори ручной запуск"
            )

    def _ensure_frames_can_start_locked(self) -> None:
        if self.runner.running or self._telegram_lock.locked():
            raise RuntimeError(
                "Сначала приостанови Telegram-сборщик и дождись завершения текущей "
                "Telegram-операции"
            )
        if self._frame_mode != "off" or self.media_runner.running:
            raise RuntimeError("Обработка кадров уже выполняется")
        self._require_frame_tools()

    async def _begin_frame_session_locked(self, mode: str, job_id: int | None = None) -> None:
        self._frame_mode = mode
        await self.media_runner.start()
        self.media_runner.wakeup()
        self._frame_session_task = asyncio.create_task(
            self._monitor_frame_session(mode, job_id),
            name=f"hoard-frame-session-{mode}",
        )

    async def _start_single_frame_processing(
        self, source_id: int, message_id: int
    ) -> dict:
        details = self.store.telegram.frame_details(source_id, message_id)
        if details is not None and details.get("valid"):
            return {
                "message": "Кадры уже готовы и прошли локальную проверку",
                "job_id": None,
            }
        async with self._mode_lock:
            self._ensure_frames_can_start_locked()
            job_id = self.store.telegram.queue_frame_job(
                source_id,
                message_id,
                reset_failed=True,
                authorized=True,
            )
            if job_id is None:
                return {
                    "message": "Кадры уже готовы и прошли локальную проверку",
                    "job_id": None,
                }
            await self._begin_frame_session_locked("single", job_id)
            return {
                "message": f"Извлекаем кадры выбранного кружка: задание #{job_id}",
                "job_id": job_id,
            }

    async def _start_bulk_frame_processing(self, *, retry_failed: bool = False) -> dict:
        async with self._mode_lock:
            self._ensure_frames_can_start_locked()
            if retry_failed:
                self.store.telegram.retry_failed_frames(limit=1000, authorized=False)
            stats = self.store.telegram.frame_stats()
            if int(stats.get("actionable") or 0) == 0:
                return {
                    "message": "Ожидающих обработки кружков нет",
                    "stats": stats,
                }
            self.store.telegram.start_frame_backfill()
            await self._begin_frame_session_locked("bulk")
            stats = self.store.telegram.frame_stats()
            return {
                "message": (
                    "Ручная обработка запущена. Закрытие TUI не остановит демон; "
                    "используй «Остановить обработку», чтобы завершить FFmpeg."
                ),
                "stats": stats,
            }

    async def _monitor_frame_session(self, mode: str, job_id: int | None) -> None:
        try:
            while self._frame_mode == mode:
                await asyncio.sleep(0.1)
                if mode == "single":
                    row = self.store.db.execute(
                        "SELECT state FROM jobs WHERE id=?", (int(job_id or 0),)
                    ).fetchone()
                    finished = row is None or str(row["state"]) in {"done", "failed", "waiting"}
                else:
                    state = self.store.telegram.frame_work_state()
                    finished = bool(state["backfill_complete"]) and not any(
                        int(state[key]) for key in ("waiting", "queued", "running")
                    )
                if finished:
                    break
        except asyncio.CancelledError:
            raise
        else:
            async with self._mode_lock:
                if self._frame_session_task is not asyncio.current_task():
                    return
                await self.media_runner.stop()
                self.store.telegram.pause_frame_backfill()
                self._frame_mode = "off"
                self._frame_session_task = None
                self._schedule_catalog_publish()

    async def _stop_frame_processing_locked(
        self, *, reason: str, publish: bool = True
    ) -> bool:
        was_active = self._frame_mode != "off" or self.media_runner.running
        task = self._frame_session_task
        self._frame_session_task = None
        self._frame_mode = "stopping" if was_active else "off"
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.media_runner.stop()
        self.store.telegram.disable_frame_processing()
        self._frame_mode = "off"
        if was_active:
            self.store.event("info", reason)
        if publish:
            self._schedule_catalog_publish()
        return was_active

    async def _stop_frame_processing(self) -> bool:
        async with self._mode_lock:
            return await self._stop_frame_processing_locked(
                reason="Обработка кадров остановлена пользователем"
            )

    @contextlib.asynccontextmanager
    async def _telegram_operation(self):
        """Give an explicit Telegram operation priority over local FFmpeg work."""
        switched = False
        async with self._mode_lock:
            if self._frame_mode != "off" or self.media_runner.running:
                switched = await self._stop_frame_processing_locked(
                    reason="Обработка кадров остановлена: Telegram получил приоритет",
                    publish=False,
                )
            await self._telegram_lock.acquire()
        try:
            yield switched
        finally:
            self._telegram_lock.release()
            if switched:
                self._schedule_catalog_publish()

    def _schedule_frame_catalog_publish(self) -> None:
        # A manual bulk run may complete many sets.  Publish once when its
        # session ends instead of competing with FFmpeg after every frame set.
        if self._frame_mode == "off":
            self._schedule_catalog_publish()

    async def _telegram_video_frames(self, payload: dict) -> str:
        source_id = int(payload.get("source_id") or 0)
        message_id = int(payload.get("message_id") or 0)
        details = self.store.telegram.frame_set(source_id, message_id)
        if details is None:
            raise ValueError("Набор кадров для видеокружка не найден")
        if str(details.get("status")) == "saved":
            valid, _reason = self.store.telegram._frame_set_is_valid(
                details, self.store.telegram.frame_rows(source_id, message_id)
            )
            if valid:
                return "Кадры уже извлечены"
        video_path = Path(str(details["video_path"]))
        try:
            before = video_path.stat()
            if not video_path.is_file() or before.st_size <= 0:
                raise OSError
        except OSError as exc:
            self.store.telegram.mark_frame_failed(
                source_id,
                message_id,
                error="Исходный MP4 недоступен локально",
                error_code="source_missing",
            )
            raise RuntimeError("Исходный MP4 недоступен локально") from exc
        self.store.telegram.mark_frame_running(source_id, message_id)
        final_dir = video_path.with_name(f"{video_path.stem}_frames")
        parent = final_dir.parent
        ensure_private(parent)
        temp_dir = Path(
            tempfile.mkdtemp(prefix=f".{final_dir.name}-", suffix=".tmp", dir=parent)
        )
        backup: Path | None = None
        try:
            extracted: ExtractedFrames = await extract_frames(video_path, temp_dir)
            after = video_path.stat()
            if (
                after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
            ):
                raise FrameExtractionError(
                    "source_changed",
                    "Исходный MP4 изменился во время обработки",
                    retryable=True,
                )
            backup = final_dir.with_name(f".{final_dir.name}.old-{uuid.uuid4().hex}")
            if final_dir.exists():
                os.replace(final_dir, backup)
            try:
                os.replace(temp_dir, final_dir)
            except BaseException:
                if backup.exists() and not final_dir.exists():
                    os.replace(backup, final_dir)
                raise
            frames: list[dict[str, object]] = []
            for item in extracted.frames:
                installed = final_dir / item.path.name
                installed.chmod(0o600)
                frames.append(
                    {
                        "position": item.position,
                        "timestamp_seconds": item.timestamp_seconds,
                        "file_path": str(installed.resolve(strict=False)),
                        "size": installed.stat().st_size,
                        "sha256": item.sha256,
                    }
                )
            self.store.telegram.save_frame_set(
                source_id,
                message_id,
                frame_set_id=uuid.uuid4().hex,
                algorithm_version=FRAME_ALGORITHM_VERSION,
                duration=extracted.duration,
                video_size=after.st_size,
                video_mtime_ns=after.st_mtime_ns,
                frames=frames,
            )
            shutil.rmtree(backup, ignore_errors=True)
            self.store.event(
                "ok",
                f"Кадры видеокружка {source_id}:{message_id} готовы",
            )
            self._schedule_frame_catalog_publish()
            return "Извлечено три кадра"
        except asyncio.CancelledError:
            self.store.telegram.mark_frame_pending(
                source_id,
                message_id,
                error="Обработка остановлена; требуется ручной запуск",
            )
            raise
        except FrameExtractionError as exc:
            self.store.telegram.mark_frame_failed(
                source_id,
                message_id,
                error=str(exc) or "Не удалось извлечь кадры",
                error_code=exc.code,
                retry_after=0,
            )
            self.store.event(
                "error",
                f"Кадры видеокружка {source_id}:{message_id} не созданы: {exc}",
            )
            self._schedule_frame_catalog_publish()
            raise RuntimeError(f"Не удалось извлечь кадры: {exc}") from exc
        except Exception as exc:
            self.store.telegram.mark_frame_failed(
                source_id,
                message_id,
                error=f"{type(exc).__name__}: {exc}",
                error_code="internal_error",
                retry_after=0,
            )
            self._schedule_frame_catalog_publish()
            raise
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
            if backup is not None and backup.exists():
                shutil.rmtree(backup, ignore_errors=True)

    async def _ai_request(self, payload: dict) -> str:
        try:
            result = await self.ai_service.execute_legacy(payload)
        except AIAuthenticationError:
            self._ai_key_state = "authorization_error"
            raise
        else:
            self._ai_key_state = "verified"
            return result
        finally:
            await self._publish_ai_catalog()

    async def _ai_chat(self, payload: dict) -> str:
        try:
            result = await self.ai_service.execute_attempt(str(payload.get("attempt_id") or ""))
        except AIAuthenticationError:
            self._ai_key_state = "authorization_error"
            raise
        else:
            self._ai_key_state = "verified"
            return result
        finally:
            await self._publish_ai_catalog()

    @staticmethod
    def _scan_error(exc: Exception) -> str:
        """Keep routine failures useful without exposing implementation details."""
        if isinstance(exc, AuthFlowError):
            return str(exc)
        try:
            from telethon.errors import FloodWaitError

            if isinstance(exc, FloodWaitError):
                return f"Telegram ограничил запросы; повтор через {exc.seconds} с."
        except ImportError:
            pass
        if isinstance(exc, (OSError, TimeoutError)):
            return "Нет связи с Telegram; проверка будет повторена автоматически."
        if isinstance(exc, (RuntimeError, ValueError)):
            return str(exc)
        return "Проверка Telegram завершилась ошибкой; смотри журнал диагностики."

    async def _telegram_scan(self, payload: dict) -> str:
        source_id = int(payload["source_id"])
        source = self._source(source_id)
        if source is None or not source["enabled"]:
            return "Источник отключён"
        try:
            async with self._telegram_operation():
                checked, saved = await self.engine.scan(source)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                from telethon.errors import FloodWaitError
                delay = max(120, exc.seconds + 3) if isinstance(exc, FloodWaitError) else 120
            except ImportError:
                delay = 120
            message = self._scan_error(exc)
            self.store.schedule(source_id, delay, error=message, result="Ошибка проверки")
            self.store.event(
                "error", f"{source['username']}/{source['post_id']}: {message}"
            )
            raise RuntimeError(message) from exc
        else:
            result = f"Проверено: {checked}; новых кружков: {saved}"
            self.store.schedule(source_id, source["interval"], result=result)
            if checked:
                self.store.event(
                    "info",
                    f"{source['username']}: {checked} комментариев, {saved} кружков",
                )
            return result
        finally:
            self.progress = {}
            if self.store.telegram.catalog_dirty():
                self._queue_report()

    async def _telegram_enrich_source(self, payload: dict) -> str:
        source = self._source(int(payload["source_id"]))
        if source is None:
            return "Источник не найден"
        try:
            async with self._telegram_operation():
                return await self.engine.enrich_source(source)
        finally:
            if self.store.telegram.catalog_dirty():
                self._queue_report()

    async def _telegram_post_media(self, payload: dict) -> str:
        source = self._source(int(payload["source_id"]))
        if source is None:
            return "Источник не найден"
        try:
            async with self._telegram_operation():
                return await self.engine.download_post_media(source)
        finally:
            if self.store.telegram.catalog_dirty():
                self._queue_report()

    async def _telegram_profile_photos(self, payload: dict) -> str:
        author_id = int(payload["author_id"])
        try:
            async with self._telegram_operation():
                return await self.engine.refresh_profile_photos(author_id)
        finally:
            if self.store.telegram.catalog_dirty():
                self._queue_report()

    @staticmethod
    async def _wait_for_report_worker(
        worker: asyncio.Task,
    ) -> tuple[bool, object | None, BaseException | None]:
        """Wait for a build thread even when the report task is cancelled again.

        ``asyncio.to_thread`` cannot stop its thread.  A repeated ``cancel()``
        used to interrupt ``await worker`` inside the cancellation handler and
        release ``_report_lock`` while ``build_catalog_from_db`` could still
        replace the HTML file.  Absorb every cancellation until the worker is
        really finished, then let the caller re-raise it.
        """
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(worker)
            except asyncio.CancelledError:
                cancelled = True
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
                if worker.done():
                    break
                continue
            except BaseException as exc:
                return cancelled, None, exc
            return cancelled, result, None
        try:
            result = worker.result()
        except BaseException as exc:
            return cancelled, None, exc
        return cancelled, result, None

    async def _telegram_report(self, payload: dict) -> str:
        async with self._report_lock:
            preferred = payload.get("archive_root")
            report = None
            worker_error: BaseException | None = None
            try:
                # A Telegram or AI job may write while the snapshot is being built.
                # Three immediate passes absorb short bursts; durable reconciliation
                # schedules a cooled-down follow-up if mutations continue.
                for _ in range(3):
                    worker = asyncio.create_task(
                        asyncio.to_thread(
                            partial(
                                build_catalog_from_db,
                                self.store.filename,
                                archive_root=preferred,
                            )
                        )
                    )
                    cancelled, result, worker_error = await self._wait_for_report_worker(
                        worker
                    )
                    if worker_error is not None:
                        if cancelled:
                            # A cancellation must win after the thread exits.
                            raise asyncio.CancelledError
                        raise worker_error
                    if cancelled:
                        # The build completed, but the job was cancelled while
                        # waiting for it.  Do not run another pass or report success.
                        raise asyncio.CancelledError
                    report = result
                    if not self.store.telegram.catalog_dirty():
                        break
            except asyncio.CancelledError:
                if worker_error is not None and not isinstance(
                    worker_error, asyncio.CancelledError
                ):
                    self.store.telegram.record_catalog_report_failure(
                        f"{type(worker_error).__name__}: {worker_error}"
                    )
                raise
            except Exception as exc:
                self.store.telegram.record_catalog_report_failure(
                    f"{type(exc).__name__}: {exc}"
                )
                raise
            assert report is not None
            self.store.telegram.record_catalog_report_success()
            self.store.event("ok", f"HTML-каталог обновлён: {report}")
            return f"HTML-каталог обновлён: {report}"

    async def _cloud_copy(self, payload: dict) -> str:
        result = await upload(payload["source"], payload["destination"])
        self.store.event("ok", result)
        return result

    async def _media_download(self, payload: dict) -> str:
        result = await download(payload["url"], payload["output"])
        self.store.event("ok", result)
        return result

    async def start(self) -> str:
        async with self._stop_lock:
            if self.running:
                return "Обработчик задач уже работает"
            async with self._mode_lock:
                switched = False
                if self._frame_mode != "off" or self.media_runner.running:
                    switched = await self._stop_frame_processing_locked(
                        reason="Обработка кадров остановлена: Telegram получил приоритет",
                        publish=False,
                    )
                await self.runner.start()
                was_paused = self.collector_state == "paused"
                self.collector_state = "running"
            message = "Сборщик продолжил работу" if was_paused else "Сборщик запущен"
            if switched:
                message = "Обработка кадров остановлена; " + message.lower()
            return message

    async def start_background(self) -> None:
        """Start daemon-owned AI work; frame execution always requires a user action."""
        if not self.ai_runner.running:
            self.ai_service.register_interrupted_legacy_attempts()
            self.store.ai.recover_interrupted_attempts()
            await self.ai_service.resume_pending_telegram_batches()
        await self.ai_runner.start()

    async def stop_background(self) -> None:
        async with self._mode_lock:
            await self._stop_frame_processing_locked(
                reason="Обработка кадров остановлена вместе со службой",
                publish=False,
            )
        task = self._catalog_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._catalog_task = None
        await self.ai_runner.stop()

    async def stop(self, *, paused: bool = False) -> str:
        # Pause and shutdown may arrive through different IPC connections at
        # the same time.  Serialize the whole transition so the runner is
        # cancelled once and the engine is disconnected once.
        async with self._stop_lock:
            await self.runner.stop()
            async with self._telegram_lock:
                await self.engine.disconnect()
            self.progress = {}
            self.collector_state = "paused" if paused else "stopped"
            if paused:
                return "Сборщик приостановлен; незавершённые задания сохранены"
            return "Сборщик остановлен; служба доступна для управления"

    async def _auth_call(self, operation) -> dict:
        try:
            return await asyncio.wait_for(operation, timeout=AUTH_OPERATION_TIMEOUT)
        except TimeoutError:
            return {
                "state": "unknown",
                "authorized": False,
                "error": "Telegram не ответил вовремя. Проверь сеть или VPN и повтори попытку.",
                "error_code": "timeout",
                "retryable": True,
            }
        except AuthFlowError as exc:
            return {
                "state": str(exc.state),
                "authorized": False,
                "error": str(exc),
                "error_code": exc.code,
                "retryable": exc.retryable,
            }

    def _telegram_account_snapshot(self, *, configured: bool) -> dict:
        snapshot = getattr(self.engine, "account_snapshot", None)
        if callable(snapshot):
            result = dict(snapshot())
        else:
            # Compatibility with injected engines used by integrations/tests.
            authorized = self.engine.authorization_step == AuthState.AUTHORIZED
            result = {
                "state": "connected" if authorized else "login_required",
                "connected": authorized,
                "session_present": False,
                "verified": True if authorized else None,
                "profile": {},
            }
        if not configured:
            result.update(
                state="login_required",
                connected=False,
                verified=False,
                profile={},
            )
        return result

    def _status_snapshot(self, *, detailed: bool) -> dict:
        configured = read_credentials() is not None
        auth_state = self.engine.authorization_step
        if not configured:
            auth_state = AuthState.API_REQUIRED
        result = {
            "version": __version__,
            "protocol": IPC_PROTOCOL,
            "running": self.running,
            "collector_state": self.collector_state,
            "telegram": str(auth_state),
            "telegram_account": self._telegram_account_snapshot(configured=configured),
            "configured": configured,
            "progress": self.progress.copy(),
            "stats": self.store.stats(),
            "queue": self.store.queue_stats(),
            "media_running": self.media_runner.running,
            "frame_processing_mode": self._frame_mode,
            "events": self.store.recent_events(12 if not detailed else 20),
            "integrations": {
                "telegram": True,
                "rclone": shutil.which("rclone") is not None,
                "yt_dlp": shutil.which("yt-dlp") is not None,
                "ffmpeg": (
                    shutil.which("ffmpeg") is not None
                    and shutil.which("ffprobe") is not None
                ),
                "browser": False,
                "ai": self.ai_core.is_configured(),
            },
        }
        if not detailed:
            return result
        archive_root = self.store.telegram.archive_root()
        result.update(
            {
                "sources": self.store.sources(),
                "downloads": self.store.recent_downloads(),
                # Profiles have their own paginated IPC API.  This bounded
                # compatibility sample keeps `status` below the 64 KiB boundary.
                "profiles": self.store.telegram.recent_profiles(20),
                "video_frames": {
                    **self.store.telegram.frame_stats(),
                    "processing_mode": self._frame_mode,
                },
                "jobs": self.store.recent_jobs(40),
                "archive_root": str(archive_root),
                "catalog": str(telegram_catalog_file(archive_root)),
            }
        )
        self._bound_detailed_status(result)
        return result

    @staticmethod
    def _bound_detailed_status(result: dict) -> None:
        """Retain the public shape while guaranteeing one IPC-sized response."""
        envelope = {"ok": True, "data": result}
        try:
            encode_ipc_message(envelope)
            return
        except ValueError:
            pass
        original = {
            name: len(result.get(name) or [])
            for name in ("jobs", "profiles", "downloads", "events", "sources")
        }
        result["status_truncated"] = {}
        for name in original:
            values = result.get(name)
            while values:
                keep = len(values) // 2
                result[name] = values = values[:keep]
                result["status_truncated"][name] = {
                    "shown": len(values),
                    "available": original[name],
                }
                try:
                    encode_ipc_message(envelope)
                    return
                except ValueError:
                    continue
        # The fixed fields are intentionally tiny; reaching this point would
        # indicate a new unbounded scalar that must not be silently hidden.
        encode_ipc_message(envelope)

    async def handle(self, method: str, args: dict) -> dict:
        if method == "ai_config_get":
            config = read_ai_config()
            return {
                "provider": config.provider,
                "model": config.model,
                "timeout": config.timeout,
                "max_parallel": config.max_parallel,
                "configured": self.ai_core.is_configured(config.provider),
                "key_status": self._ai_key_state,
                "key_source": config.key_source,
            }
        if method == "ai_config_set":
            current = read_ai_config()
            api_key = str(args.get("api_key") or "").strip()
            if not api_key:
                raise ValueError("Укажи новый API-ключ; сохранённый ключ никогда не возвращается")
            config = AIConfig(
                provider=str(args.get("provider") or current.provider),
                api_key=api_key,
                model=str(args.get("model") or current.model),
                timeout=float(args.get("timeout", current.timeout)),
                max_parallel=int(args.get("max_parallel", current.max_parallel)),
            )
            write_ai_config(config)
            effective = read_ai_config()
            if not self._injected_ai_core:
                self.ai_core = create_default_core(effective)
                self.ai_service.core = self.ai_core
            self._ai_key_state = "key_saved"
            message = "Ключ сохранён; он будет проверен при первом AI-запросе"
            if effective.key_source == "env":
                message = (
                    "Ключ сохранён в файл, но сейчас используется DEEPSEEK_API_KEY "
                    "из окружения"
                )
            return {
                "message": message,
                "provider": effective.provider,
                "model": effective.model,
                "configured": True,
                "key_status": self._ai_key_state,
                "key_source": effective.key_source,
            }
        if method.startswith("ai_"):
            return await self.ai_ipc.handle(method, args)
        if method == "status":
            return self._status_snapshot(detailed=True)
        if method == "status_brief":
            return self._status_snapshot(detailed=False)
        if method == "telegram_statistics_get":
            statistics = self.store.telegram_statistics()
            statistics["frames"].update(
                active=bool(self.media_runner.running),
                processing_mode=self._frame_mode,
            )
            return statistics
        if method == "telegram_sources_overview":
            raw_enabled = args.get("enabled")
            if raw_enabled is not None and not isinstance(raw_enabled, bool):
                raise ValueError("enabled должен быть true, false или null")
            return self.store.telegram_sources_overview(
                limit=int(args.get("limit", 50)),
                offset=int(args.get("offset", 0)),
                query=str(args.get("query") or ""),
                enabled=raw_enabled,
                sort=str(args.get("sort") or "id_desc"),
            )
        if method == "telegram_source_get":
            source = self.store.source_settings(int(args.get("id") or 0))
            if source is None:
                raise ValueError("Источник не найден")
            return {"source": source}
        if method == "telegram_publication_get":
            return {"publication": self.store.telegram_publication(int(args.get("source_id") or 0))}
        if method == "telegram_publication_downloads_list":
            return self.store.telegram_publication_downloads(
                int(args.get("source_id") or 0),
                limit=int(args.get("limit", 50)),
                offset=int(args.get("offset", 0)),
            )
        if method == "telegram_publication_download_get":
            return {
                "download": self.store.telegram_publication_download_details(
                    int(args.get("source_id") or 0), int(args.get("message_id") or 0)
                )
            }
        if method == "telegram_publication_frames_list":
            return self.store.telegram_publication_frames(
                int(args.get("source_id") or 0),
                limit=int(args.get("limit", 50)),
                offset=int(args.get("offset", 0)),
            )
        if method == "telegram_publication_profile_get":
            return {
                "profile": self.store.telegram_publication_profile(
                    int(args.get("source_id") or 0), int(args.get("author_id") or 0)
                )
            }
        if method == "telegram_publication_psl_list":
            return self.store.telegram_publication_psl(
                int(args.get("source_id") or 0),
                limit=int(args.get("limit", 50)),
                offset=int(args.get("offset", 0)),
            )
        if method == "telegram_publication_psl_get":
            return {
                "analysis": self.store.telegram_publication_psl_details(
                    int(args.get("source_id") or 0), str(args.get("analysis_id") or "")
                )
            }
        if method == "telegram_publication_media_open":
            path = self.store.resolve_publication_media(
                int(args.get("source_id") or 0),
                str(args.get("kind") or ""),
                message_id=(
                    int(args["message_id"]) if args.get("message_id") is not None else None
                ),
                author_id=(
                    int(args["author_id"]) if args.get("author_id") is not None else None
                ),
                photo_id=(str(args["photo_id"]) if args.get("photo_id") is not None else None),
                position=(int(args["position"]) if args.get("position") is not None else None),
            )
            if not self._media_opener(path):
                raise RuntimeError("Внешнее приложение не подтвердило открытие файла")
            return {"opened": True, "message": "Локальный файл открыт во внешнем приложении"}
        if method == "credentials":
            api_id = int(args.get("api_id", 0))
            api_hash = str(args.get("api_hash", ""))
            if api_id <= 0 or not api_hash.strip():
                raise ValueError("Укажи корректные API ID и API Hash")
            async with self._telegram_operation():
                await self.engine.disconnect()
                write_credentials(api_id, api_hash)
                if hasattr(self.engine, "session_verified"):
                    self.engine.session_verified = None
                if hasattr(self.engine, "account_info"):
                    self.engine.account_info = {}
            return {"message": "Параметры API сохранены в закрытом локальном файле"}
        if method == "telegram_account_check":
            async with self._telegram_operation():
                result = await self._auth_call(self.engine.begin_authorization())
            if result.get("authorized"):
                result["message"] = result.get("message") or "Сессия Telegram подтверждена"
            result["account"] = self._telegram_account_snapshot(
                configured=read_credentials() is not None
            )
            return result
        if method == "telegram_account_disconnect":
            async with self._mode_lock:
                if self.runner.running:
                    raise RuntimeError(
                        "Сначала останови или поставь Telegram-сборщик на паузу"
                    )
                async with self._telegram_lock:
                    await self.engine.disconnect()
            return {
                "message": "Соединение Telegram отключено; сохранённая сессия не удалена",
                "account": self._telegram_account_snapshot(
                    configured=read_credentials() is not None
                ),
            }
        if method in {
            "auth_begin",
            "auth_send_code",
            "auth_submit_code",
            "auth_submit_password",
            "auth_cancel",
            # Compatibility with the pre-0.3 IPC names.
            "auth_code",
            "auth_sign_in",
            "auth_password",
            "auth_check",
        }:
            async with self._telegram_operation():
                if method == "auth_begin":
                    return await self._auth_call(self.engine.begin_authorization())
                if method in {"auth_send_code", "auth_code"}:
                    return await self._auth_call(
                        self.engine.send_code(
                            str(args.get("phone", "")),
                            force=bool(args.get("force", False)),
                        )
                    )
                if method in {"auth_submit_code", "auth_sign_in"}:
                    return await self._auth_call(
                        self.engine.sign_in(str(args.get("code", "")))
                    )
                if method in {"auth_submit_password", "auth_password"}:
                    return await self._auth_call(
                        self.engine.sign_in_password(str(args.get("password", "")))
                    )
                if method == "auth_cancel":
                    return await self._auth_call(self.engine.cancel_authorization())
                return await self._auth_call(self.engine.begin_authorization())
        if method == "source_validate":
            post = parse_post_url(str(args.get("url", "")))
            async with self._telegram_operation():
                return await self.engine.validate_post(post)
        if method == "source_add":
            post = parse_post_url(str(args.get("url", "")))
            source_id, created = self.store.add_source_result(
                post,
                str(args.get("output", "~/Videos/hoard")),
                int(args.get("interval", 120)),
                title=str(args.get("title", "")),
                collection_mode=str(args.get("collection_mode") or "full"),
            )
            if created:
                self.runner.wakeup()
                message = f"Добавлен источник #{source_id}: {post.link}"
            else:
                message = f"Источник #{source_id} уже существует: {post.link}"
            return {"message": message, "created": created, "source_id": source_id}
        if method == "source_update":
            source_id = int(args.get("id") or 0)
            source = self.store.update_source(
                source_id,
                title=str(args.get("title") or ""),
                output=str(args.get("output") or ""),
                interval=int(args.get("interval", 120)),
                collection_mode=str(args.get("collection_mode") or ""),
            )
            return {
                "message": f"Параметры источника #{source_id} обновлены",
                "source": source,
            }
        if method == "source_enable":
            self.store.set_enabled(int(args["id"]), bool(args["enabled"]))
            self.runner.wakeup()
            return {"message": "Источник обновлён"}
        if method == "source_remove":
            self.store.remove_source(int(args["id"]))
            return {"message": "Источник отключён; файлы не удалены"}
        if method == "telegram_report":
            requested_root = str(args.get("archive_root") or "") or None
            result = await self._telegram_report({"archive_root": requested_root})
            root = self.store.telegram.archive_root(requested_root)
            return {
                "message": result,
                "catalog": str(telegram_catalog_file(root)),
                "media_roots": [
                    str(path) for path in self.store.telegram.allowed_media_roots(root)
                ],
            }
        if method in {
            "telegram_frames_get",
            "telegram_frames_process",
        }:
            source_id = int(args.get("source_id") or 0)
            message_id = int(args.get("message_id") or 0)
            video_id = str(args.get("video_id") or "").strip()
            if video_id and (not source_id or not message_id):
                try:
                    source_id, message_id = (int(part) for part in video_id.split(":", 1))
                except (TypeError, ValueError) as exc:
                    raise ValueError("video_id должен иметь вид source_id:message_id") from exc
            if not source_id or not message_id:
                raise ValueError("Нужны source_id и message_id видеокружка")
            if method == "telegram_frames_process":
                started = await self._start_single_frame_processing(source_id, message_id)
                return {
                    **started,
                    "frames": self.store.telegram.frame_details(source_id, message_id),
                    "stats": {
                        **self.store.telegram.frame_stats(),
                        "processing_mode": self._frame_mode,
                    },
                }
            return {
                "frames": self.store.telegram.frame_details(source_id, message_id),
                "stats": self.store.telegram.frame_stats(),
            }
        if method in {
            "telegram_frames_backfill_start",
            "telegram_frames_backfill_pause",
            "telegram_frames_backfill_status",
            "telegram_frames_retry_failed",
        }:
            if method == "telegram_frames_backfill_start":
                if not bool(args.get("confirmed")):
                    raise ValueError(
                        "Массовая обработка требует подтверждения нагрузки FFmpeg"
                    )
                return await self._start_bulk_frame_processing()
            if method == "telegram_frames_backfill_pause":
                stopped = await self._stop_frame_processing()
                return {
                    "message": (
                        "FFmpeg остановлен; незавершённое задание ожидает ручного запуска"
                        if stopped
                        else "Обработка кадров уже остановлена"
                    ),
                    "stats": self.store.telegram.pause_frame_backfill(),
                }
            if method == "telegram_frames_retry_failed":
                if not bool(args.get("confirmed")):
                    raise ValueError(
                        "Повторная обработка требует подтверждения нагрузки FFmpeg"
                    )
                return await self._start_bulk_frame_processing(retry_failed=True)
            return {
                "stats": {
                    **self.store.telegram.frame_stats(),
                    "processing_mode": self._frame_mode,
                }
            }
        if method == "telegram_enrich":
            jobs = 0
            for source in self.store.sources():
                self.store.enqueue(
                    "telegram.enrich_source",
                    {"source_id": source["id"]},
                    unique_key=f"telegram-enrich:{source['id']}",
                )
                jobs += 1
            for author_id in self.store.telegram.enrichment_profile_photo_ids():
                self.store.enqueue(
                    "telegram.profile_photos",
                    {"author_id": author_id},
                    unique_key=f"telegram-profile-photos:{author_id}",
                )
                jobs += 1
            self.runner.wakeup()
            suffix = "" if self.running else "; запусти обработчик"
            return {"message": f"Telegram enrichment в очереди: {jobs}{suffix}"}
        if method == "cloud_upload":
            # Validate synchronously, then enqueue. rclone installation is optional.
            source, destination = validate_upload(str(args.get("source", "")),
                                                  str(args.get("destination", "")))
            jid = self.store.enqueue("cloud.copy", {"source": source, "destination": destination})
            self.runner.wakeup()
            return {"message": f"Облачная загрузка поставлена в очередь: #{jid}", "job_id": jid}
        if method == "media_download":
            url, output = validate_media(
                str(args.get("url", "")), str(args.get("output", "~/Videos/hoard"))
            )
            jid = self.store.enqueue("media.download", {"url": url, "output": output})
            self.runner.wakeup()
            return {"message": f"Медиа добавлено в очередь: #{jid}", "job_id": jid}
        if method == "job_retry":
            job = self.store.db.execute(
                "SELECT kind FROM jobs WHERE id=?", (int(args["id"]),)
            ).fetchone()
            if job is not None and str(job["kind"]).startswith("ai."):
                raise ValueError("AI-запросы повторяются только через ai_attempt_retry")
            is_frame_job = (
                job is not None and str(job["kind"]) == "telegram.video_frames"
            )
            job_id = self.store.retry_job(
                int(args["id"]),
                target_state="waiting" if is_frame_job else "queued",
            )
            if job is not None and str(job["kind"]) == "telegram.report":
                # A user retry is explicit: it must not wait for the automatic
                # filesystem-failure backoff.
                self.store.telegram.clear_catalog_report_retry()
            if not is_frame_job:
                self.runner.wakeup()
            message = (
                (
                    "Задание ожидает явного запуска обработки кадров"
                    if is_frame_job
                    else "Задание возвращено в очередь"
                )
                if job_id == int(args["id"])
                else f"Задание уже выполняется или стоит в очереди: #{job_id}"
            )
            return {"message": message, "job_id": job_id}
        if method == "start":
            return {"message": await self.start()}
        if method == "pause":
            return {"message": await self.stop(paused=True)}
        if method == "stop":
            return {"message": await self.stop()}
        if method == "scan":
            with self.store.db:
                self.store.db.execute("UPDATE sources SET next_at=0 WHERE enabled=1")
            self.runner.wakeup()
            return {"message": "Проверка назначена" if self.running else
                    "Проверка назначена; запусти обработчик"}
        if method == "scan_source":
            source_id = int(args.get("id", 0))
            source = self._source(source_id)
            if source is None:
                raise ValueError("Источник не найден")
            if not source["enabled"]:
                raise ValueError("Сначала включи источник")
            job_id = self.store.enqueue(
                "telegram.scan",
                {"source_id": source_id},
                unique_key=f"telegram:{source_id}",
            )
            self.runner.wakeup()
            suffix = "" if self.running else "; запусти обработчик"
            return {
                "message": f"Проверка источника #{source_id} назначена{suffix}",
                "job_id": job_id,
            }
        if method == "shutdown":
            await self.stop()
            self.stop_requested.set()
            return {"message": "Служба завершает работу"}
        raise ValueError(f"Неизвестная команда: {method}")


async def serve_connection(service: Service, reader: asyncio.StreamReader,
                           writer: asyncio.StreamWriter, auth_token: str | None = None):
    try:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=20)
        except ValueError as exc:
            raise ValueError("IPC-запрос превышает лимит 64 KiB") from exc
        if len(line) > IPC_MAX_BYTES:
            raise ValueError("IPC-запрос превышает лимит 64 KiB")
        req = json.loads(line)
        if auth_token is not None:
            provided = req.get("token", "") if isinstance(req, dict) else ""
            if not isinstance(provided, str) or not secrets.compare_digest(provided, auth_token):
                raise ValueError("Доступ к службе запрещён")
        if not isinstance(req, dict) or not isinstance(req.get("args", {}), dict):
            raise ValueError("Неверный запрос")
        result = await service.handle(req.get("method", ""), req.get("args", {}))
        response = {"ok": True, "data": result}
    except Exception as exc:
        if os.environ.get("HOARD_DEBUG") == "1":
            error = f"{type(exc).__name__}: {exc}"
        else:
            error = str(exc) or "Внутренняя ошибка службы"
        response = {"ok": False, "error": error}
    try:
        try:
            encoded = encode_ipc_message(response)
        except ValueError:
            encoded = encode_ipc_message(
                {"ok": False, "error": "IPC-ответ слишком велик; уменьши limit или размер записи"}
            )
        writer.write(encoded)
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def run_daemon():
    ensure_private(state_dir())
    with single_instance(state_dir() / "service.lock"):
        service = Service()
        server = None
        token = None
        sock = socket_path()
        try:
            await service.start_background()
            if is_windows():
                token = secrets.token_urlsafe(32)
                server = await asyncio.start_server(
                    lambda r, w: serve_connection(service, r, w, token),
                    host="127.0.0.1", port=0, limit=65536,
                )
                write_endpoint(endpoint_path(), server.sockets[0].getsockname()[1], token)
            else:
                sock.unlink(missing_ok=True)
                server = await asyncio.start_unix_server(
                    lambda r, w: serve_connection(service, r, w), path=str(sock), limit=65536,
                )
                sock.chmod(0o600)
            loop = asyncio.get_running_loop()
            if not is_windows():
                for signum in (signal.SIGTERM, signal.SIGINT):
                    with contextlib.suppress(NotImplementedError, RuntimeError):
                        loop.add_signal_handler(signum, service.stop_requested.set)
            await service.stop_requested.wait()
        finally:
            if server is not None:
                server.close()
                await server.wait_closed()
            await service.stop_background()
            await service.stop()
            # The loopback Web is a separate process.  A normal Hoard
            # shutdown must not leave it orphaned.
            try:
                from .web import stop_web_for_shutdown

                await asyncio.to_thread(stop_web_for_shutdown)
            except Exception:
                pass
            service.store.close()
            if token is not None:
                remove_endpoint_if_owned(endpoint_path(), token)
            else:
                sock.unlink(missing_ok=True)
