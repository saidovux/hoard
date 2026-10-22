"""Local supervisor and IPC; no HTTP server and no Telegram client in the UI."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import shutil
import signal
from functools import partial

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
from .ipc import IPC_MAX_BYTES, encode_ipc_message
from .jobs import JobRunner
from .models import parse_post_url
from .paths import db_path, endpoint_path, ensure_private, socket_path, state_dir
from .platform_support import is_windows, remove_endpoint_if_owned, single_instance, write_endpoint
from .storage import Store
from .telegram import AuthFlowError, AuthState, TelegramEngine


class Service:
    def __init__(
        self,
        store: Store | None = None,
        engine: TelegramEngine | None = None,
        ai_core: AICore | None = None,
    ):
        self.store = store or Store(db_path())
        self.progress: dict = {}
        self._telegram_lock = asyncio.Lock()
        self._report_lock = asyncio.Lock()
        self.engine = engine or TelegramEngine(self.store, self._progress)
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
        return next((item for item in self.store.sources() if item["id"] == source_id), None)

    def _queue_report(self) -> int:
        return self.store.enqueue(
            "telegram.report", {}, unique_key="telegram-report"
        )

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

    async def _ai_request(self, payload: dict) -> str:
        try:
            result = await self.ai_service.execute_legacy(payload)
        except AIAuthenticationError:
            self._ai_key_state = "authorization_error"
            raise
        else:
            self._ai_key_state = "verified"
            return result

    async def _ai_chat(self, payload: dict) -> str:
        try:
            result = await self.ai_service.execute_attempt(str(payload.get("attempt_id") or ""))
        except AIAuthenticationError:
            self._ai_key_state = "authorization_error"
            raise
        else:
            self._ai_key_state = "verified"
            return result

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
            async with self._telegram_lock:
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
            async with self._telegram_lock:
                return await self.engine.enrich_source(source)
        finally:
            if self.store.telegram.catalog_dirty():
                self._queue_report()

    async def _telegram_post_media(self, payload: dict) -> str:
        source = self._source(int(payload["source_id"]))
        if source is None:
            return "Источник не найден"
        try:
            async with self._telegram_lock:
                return await self.engine.download_post_media(source)
        finally:
            if self.store.telegram.catalog_dirty():
                self._queue_report()

    async def _telegram_profile_photos(self, payload: dict) -> str:
        author_id = int(payload["author_id"])
        try:
            async with self._telegram_lock:
                return await self.engine.refresh_profile_photos(author_id)
        finally:
            if self.store.telegram.catalog_dirty():
                self._queue_report()

    async def _telegram_report(self, payload: dict) -> str:
        async with self._report_lock:
            preferred = payload.get("archive_root")
            report = None
            # A Telegram job may write while the snapshot is being built. The dirty
            # marker makes the build converge without regenerating after each message.
            for _ in range(3):
                report = await asyncio.to_thread(
                    partial(
                        build_catalog_from_db,
                        self.store.filename,
                        archive_root=preferred,
                    )
                )
                if not self.store.telegram.catalog_dirty():
                    break
            assert report is not None
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
        if self.running:
            return "Обработчик задач уже работает"
        await self.runner.start()
        was_paused = self.collector_state == "paused"
        self.collector_state = "running"
        return "Сборщик продолжил работу" if was_paused else "Сборщик запущен"

    async def start_background(self) -> None:
        """Start non-Telegram work owned by the single Hoard daemon."""
        if not self.ai_runner.running:
            self.ai_service.register_interrupted_legacy_attempts()
            self.store.ai.recover_interrupted_attempts()
        await self.ai_runner.start()

    async def stop_background(self) -> None:
        await self.ai_runner.stop()

    async def stop(self, *, paused: bool = False) -> str:
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
            return await operation
        except AuthFlowError as exc:
            return {
                "state": str(exc.state),
                "authorized": False,
                "error": str(exc),
                "error_code": exc.code,
                "retryable": exc.retryable,
            }

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
            if not self._injected_ai_core:
                self.ai_core = create_default_core(config)
                self.ai_service.core = self.ai_core
            self._ai_key_state = "key_saved"
            return {
                "message": "Ключ сохранён; он будет проверен при первом AI-запросе",
                "provider": config.provider,
                "model": config.model,
                "configured": True,
                "key_status": self._ai_key_state,
            }
        if method.startswith("ai_"):
            return await self.ai_ipc.handle(method, args)
        if method == "status":
            configured = read_credentials() is not None
            auth_state = self.engine.authorization_step
            if not configured:
                auth_state = AuthState.API_REQUIRED
            archive_root = self.store.telegram.archive_root()
            return {
                "running": self.running,
                "collector_state": self.collector_state,
                "telegram": str(auth_state),
                "configured": configured,
                "progress": self.progress.copy(),
                "stats": self.store.stats(),
                "queue": self.store.queue_stats(),
                "sources": self.store.sources(),
                "downloads": self.store.recent_downloads(),
                "profiles": self.store.telegram.recent_profiles(),
                "jobs": self.store.recent_jobs(),
                "events": self.store.recent_events(),
                "archive_root": str(archive_root),
                "catalog": str(telegram_catalog_file(archive_root)),
                "integrations": {
                    "telegram": True,
                    "rclone": shutil.which("rclone") is not None,
                    "yt_dlp": shutil.which("yt-dlp") is not None,
                    "browser": False,
                    "ai": self.ai_core.is_configured(),
                },
            }
        if method == "credentials":
            api_id = int(args.get("api_id", 0))
            api_hash = str(args.get("api_hash", ""))
            if api_id <= 0 or not api_hash.strip():
                raise ValueError("Укажи корректные API ID и API Hash")
            async with self._telegram_lock:
                await self.engine.disconnect()
                write_credentials(api_id, api_hash)
            return {"message": "Параметры API сохранены в закрытом локальном файле"}
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
            async with self._telegram_lock:
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
            async with self._telegram_lock:
                return await self.engine.validate_post(post)
        if method == "source_add":
            post = parse_post_url(str(args.get("url", "")))
            source_id = self.store.add_source(
                post,
                str(args.get("output", "~/Videos/hoard")),
                int(args.get("interval", 120)),
                title=str(args.get("title", "")),
            )
            self.runner.wakeup()
            return {"message": f"Добавлен источник #{source_id}: {post.link}"}
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
            return {"message": result, "catalog": str(telegram_catalog_file(root))}
        if method == "telegram_enrich":
            jobs = 0
            for source in self.store.sources():
                self.store.enqueue(
                    "telegram.enrich_source",
                    {"source_id": source["id"]},
                    unique_key=f"telegram-enrich:{source['id']}",
                )
                jobs += 1
            for author_id in self.store.telegram.profile_ids():
                if self.store.telegram.profile_photo_due(author_id):
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
            self.store.retry_job(int(args["id"]))
            self.runner.wakeup()
            return {"message": "Задание возвращено в очередь"}
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
            service.store.close()
            if token is not None:
                remove_endpoint_if_owned(endpoint_path(), token)
            else:
                sock.unlink(missing_ok=True)
