"""Local supervisor and IPC; no browser or Telegram logic in the control plane."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import secrets

from .config import read_credentials, write_credentials
from .integrations.media import download, validate_media
from .integrations.rclone import upload, validate_upload
from .jobs import JobRunner
from .models import parse_post_url
from .paths import db_path, endpoint_path, ensure_private, socket_path, state_dir
from .platform_support import is_windows, remove_endpoint_if_owned, single_instance, write_endpoint
from .storage import Store
from .telegram import AuthFlowError, AuthState, TelegramEngine


class Service:
    def __init__(self, store: Store | None = None, engine: TelegramEngine | None = None):
        self.store = store or Store(db_path())
        self.progress: dict = {}
        self._telegram_lock = asyncio.Lock()
        self.engine = engine or TelegramEngine(self.store, self._progress)
        self.runner = JobRunner(self.store, {
            "telegram.scan": self._telegram_scan,
            "cloud.copy": self._cloud_copy,
            "media.download": self._media_download,
        }, parallel=int(os.environ.get("HOARD_MAX_JOBS", "3")))
        self.stop_requested = asyncio.Event()
        self.collector_state = "stopped"

    @property
    def running(self) -> bool:
        return self.runner.running

    def _progress(self, value: dict) -> None:
        self.progress = value

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
        source = next((s for s in self.store.sources() if s["id"] == source_id), None)
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
        if method == "status":
            configured = read_credentials() is not None
            auth_state = self.engine.authorization_step
            if not configured:
                auth_state = AuthState.API_REQUIRED
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
                "jobs": self.store.recent_jobs(),
                "events": self.store.recent_events(),
                "integrations": {
                    "telegram": True,
                    "rclone": shutil.which("rclone") is not None,
                    "yt_dlp": shutil.which("yt-dlp") is not None,
                    "browser": False,
                    "ai": False,
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
        if method == "shutdown":
            await self.stop()
            self.stop_requested.set()
            return {"message": "Служба завершает работу"}
        raise ValueError(f"Неизвестная команда: {method}")


async def serve_connection(service: Service, reader: asyncio.StreamReader,
                           writer: asyncio.StreamWriter, auth_token: str | None = None):
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=20)
        if len(line) > 64 * 1024:
            raise ValueError("Слишком длинный запрос")
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
        writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode())
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
            await service.stop()
            service.store.close()
            if token is not None:
                remove_endpoint_if_owned(endpoint_path(), token)
            else:
                sock.unlink(missing_ok=True)
