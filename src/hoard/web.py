"""Explicit, short-lived localhost browser client backed only by daemon IPC."""
from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import mimetypes
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from .archive_paths import path_is_within
from .ipc import ensure_daemon, request
from .paths import ensure_private, state_dir
from .platform_support import is_windows

RequestFunction = Callable[[str, dict[str, Any], float], Awaitable[dict[str, Any]]]
_ID = re.compile(r"^[a-f0-9]{32}$")
_READY_FILE = "web.ready.json"
_LAUNCH_LOCK_FILE = "web.launch.lock"
_SERVER_LOCK_FILE = "web.server.lock"
_WEB_LAUNCH_TIMEOUT = 240.0
_WEB_POLL_INTERVAL = 0.1


async def _daemon_request(
    method: str, args: dict[str, Any], timeout: float
) -> dict[str, Any]:
    return await request(method, args, timeout=timeout)


@dataclass
class WebState:
    catalog_path: Path
    allowed_media_roots: tuple[Path, ...] = ()
    requester: RequestFunction = _daemon_request
    session_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    bootstrap_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    csrf_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    nonce: str = field(default_factory=lambda: secrets.token_urlsafe(18))
    lifetime: int = 1800
    host: str = "127.0.0.1"
    port: int = 0
    last_seen: float = field(default_factory=time.monotonic)
    refresh_lock: threading.Lock = field(default_factory=threading.Lock)
    media_files: dict[str, tuple[Path, int]] = field(default_factory=dict)
    catalog_payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.catalog_path = self.catalog_path.expanduser().resolve(strict=True)
        roots = self.allowed_media_roots or (self.catalog_path.parent,)
        self.allowed_media_roots = tuple(
            dict.fromkeys(Path(root).expanduser().resolve(strict=False) for root in roots)
        )
        self.reload_media_allowlist()

    @property
    def origin(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def host_header(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def start_url(self) -> str:
        return f"{self.origin}/start/{self.bootstrap_token}"

    def call(self, method: str, args: dict[str, Any], *, timeout: float = 20) -> dict[str, Any]:
        return asyncio.run(self.requester(method, args, timeout))

    def refresh_catalog(self) -> dict[str, Any]:
        with self.refresh_lock:
            result = self.call("telegram_report", {}, timeout=180)
            path = Path(str(result.get("catalog") or "")).expanduser().resolve(strict=True)
            if not path.is_file() or path.name != "telegram_archive.html":
                raise RuntimeError("Daemon вернул недопустимый путь HTML-каталога")
            self.catalog_path = path
            roots = result.get("media_roots") or [path.parent]
            self.allowed_media_roots = tuple(
                dict.fromkeys(
                    Path(str(root)).expanduser().resolve(strict=False) for root in roots
                )
            )
            self.reload_media_allowlist()
            return result

    def reload_media_allowlist(self) -> None:
        """Allow only files explicitly validated into the current catalog snapshot."""
        source = self.catalog_path.read_text(encoding="utf-8")
        marker = "window.HOARD_CATALOG="
        start = source.find(marker)
        if start < 0:
            raise RuntimeError("HTML-каталог не содержит локальный снимок")
        try:
            payload, _end = json.JSONDecoder().raw_decode(source, start + len(marker))
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError("Не удалось прочитать снимок HTML-каталога") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Снимок HTML-каталога имеет неверный формат")
        media: list[dict[str, Any]] = list(payload.get("videos") or [])
        for post in payload.get("posts") or []:
            media.extend(post.get("media") or [])
        for profile in payload.get("profiles") or []:
            media.extend(profile.get("photos") or [])
        catalog_root = self.catalog_path.parent.resolve(strict=False)
        allowed: dict[str, tuple[Path, int]] = {}
        for item in media:
            if not isinstance(item, dict) or not item.get("available"):
                continue
            raw_url = str(item.get("url") or "")
            parsed = urlsplit(raw_url)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            relative = unquote(parsed.path)
            if relative.startswith("/") or "\\" in relative:
                continue
            try:
                target = (catalog_root / relative).resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            root_index = next(
                (
                    index
                    for index, root in enumerate(self.allowed_media_roots)
                    if path_is_within(target, root)
                ),
                None,
            )
            if root_index is None or not target.is_file():
                continue
            expected = int(item.get("size") or 0)
            if expected > 0 and target.stat().st_size != expected:
                continue
            selected_root = self.allowed_media_roots[root_index]
            try:
                rooted_path = target.relative_to(selected_root).as_posix()
            except ValueError:
                continue
            web_url = f"/media/r{root_index}/{quote(rooted_path, safe='/._-~')}"
            allowed[web_url] = (target, expected)
            item["url"] = web_url
        self.media_files = allowed
        self.catalog_payload = payload


class HoardWebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], state: WebState):
        self.state = state
        super().__init__(address, HoardWebHandler)
        state.port = int(self.server_address[1])


class HoardWebHandler(BaseHTTPRequestHandler):
    server: HoardWebServer
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *args: Any) -> None:
        # Bootstrap/session tokens, profile IDs and API errors never enter common logs.
        return

    @property
    def state(self) -> WebState:
        return self.server.state

    def _security_headers(self, *, content_type: str, length: int | None = None) -> None:
        self.send_header("Content-Type", content_type)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")

    def _host_is_valid(self) -> bool:
        return self.headers.get("Host", "") == self.state.host_header

    def _session_is_valid(self) -> bool:
        cookie = SimpleCookie()
        with contextlib.suppress(Exception):
            cookie.load(self.headers.get("Cookie", ""))
        value = cookie.get("hoard_session")
        valid = bool(
            value
            and secrets.compare_digest(str(value.value), self.state.session_token)
            and time.monotonic() - self.state.last_seen <= self.state.lifetime
        )
        if valid:
            self.state.last_seen = time.monotonic()
        return valid

    def _reject(self, status: int, message: str) -> None:
        payload = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._security_headers(content_type="application/json; charset=utf-8", length=len(payload))
        self.end_headers()
        self.wfile.write(payload)

    def _require_common(self) -> bool:
        if not self._host_is_valid():
            self._reject(HTTPStatus.MISDIRECTED_REQUEST, "Недопустимый Host")
            return False
        if not self._session_is_valid():
            self._reject(HTTPStatus.UNAUTHORIZED, "Локальная веб-сессия недействительна")
            return False
        return True

    def _require_write(self) -> bool:
        if not self._require_common():
            return False
        if self.headers.get("Origin", "") != self.state.origin:
            self._reject(HTTPStatus.FORBIDDEN, "Недопустимый Origin")
            return False
        token = self.headers.get("X-Hoard-CSRF", "")
        if not token or not secrets.compare_digest(token, self.state.csrf_token):
            self._reject(HTTPStatus.FORBIDDEN, "CSRF-проверка не пройдена")
            return False
        return True

    def _json_body(self) -> dict[str, Any]:
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Ожидается application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Некорректный Content-Length") from exc
        if not 0 <= length <= 64 * 1024:
            raise ValueError("HTTP-запрос превышает лимит 64 KiB")
        value = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(value, dict):
            raise ValueError("JSON-запрос должен быть объектом")
        return value

    def _send_json(self, value: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self._security_headers(content_type="application/json; charset=utf-8", length=len(payload))
        self.end_headers()
        self.wfile.write(payload)

    def _bootstrap(self, token: str) -> None:
        if not self._host_is_valid() or not secrets.compare_digest(
            token, self.state.bootstrap_token
        ):
            self._reject(HTTPStatus.NOT_FOUND, "Страница не найдена")
            return
        self.state.bootstrap_token = secrets.token_urlsafe(32)
        self.state.last_seen = time.monotonic()
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.send_header(
            "Set-Cookie",
            f"hoard_session={self.state.session_token}; Path=/; Max-Age={self.state.lifetime}; "
            "HttpOnly; SameSite=Strict",
        )
        self._security_headers(content_type="text/plain; charset=utf-8", length=0)
        self.end_headers()

    def _interactive_html(self) -> bytes:
        source = self.state.catalog_path.read_text(encoding="utf-8")
        marker = "window.HOARD_CATALOG="
        start = source.find(marker)
        if start < 0:
            raise RuntimeError("HTML-каталог не содержит локальный снимок")
        _, end = json.JSONDecoder().raw_decode(source, start + len(marker))
        data = json.dumps(
            self.state.catalog_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        data = (
            data.replace("&", "\\u0026")
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029")
        )
        source = source[: start + len(marker)] + data + source[end:]
        nonce = self.state.nonce
        source = source.replace(
            '<style data-hoard-bundle="style">',
            f'<style nonce="{nonce}" data-hoard-bundle="style">',
            1,
        )
        source = source.replace(
            '<script data-hoard-bundle="data">',
            f'<script nonce="{nonce}" data-hoard-bundle="data">',
            1,
        )
        app_tag = '<script data-hoard-bundle="app">'
        config = json.dumps(
            {"csrf": self.state.csrf_token, "interactive": True},
            ensure_ascii=True,
            separators=(",", ":"),
        )
        bootstrap = f'<script nonce="{nonce}">window.HOARD_INTERACTIVE={config};</script>\n  '
        source = source.replace(
            app_tag,
            bootstrap + f'<script nonce="{nonce}" data-hoard-bundle="app">',
            1,
        )
        return source.encode("utf-8")

    def _serve_html(self) -> None:
        try:
            payload = self._interactive_html()
        except (OSError, UnicodeError) as exc:
            self._reject(HTTPStatus.SERVICE_UNAVAILABLE, f"Каталог недоступен: {exc}")
            return
        self.send_response(HTTPStatus.OK)
        self._security_headers(content_type="text/html; charset=utf-8", length=len(payload))
        nonce = self.state.nonce
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; "
            f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
            "img-src 'self' data: blob: file:; media-src 'self' blob: file:; "
            "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        )
        self.end_headers()
        self.wfile.write(payload)

    def _serve_media(self, request_path: str) -> None:
        selected = self.state.media_files.get(request_path)
        if selected is None:
            self._reject(HTTPStatus.NOT_FOUND, "Файл не найден")
            return
        target, expected_size = selected
        size = target.stat().st_size
        if expected_size > 0 and size != expected_size:
            self._reject(HTTPStatus.NOT_FOUND, "Файл изменился после сборки каталога")
            return
        start, end = 0, max(0, size - 1)
        status = HTTPStatus.OK
        raw_range = self.headers.get("Range", "")
        match = re.fullmatch(r"bytes=(\d+)-(\d*)", raw_range) if raw_range else None
        if match:
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else end
            if start >= size or end < start:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            end = min(end, size - 1)
            status = HTTPStatus.PARTIAL_CONTENT
        length = end - start + 1 if size else 0
        self.send_response(status)
        self._security_headers(
            content_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream",
            length=length,
        )
        self.send_header("Accept-Ranges", "bytes")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with target.open("rb") as stream:
            stream.seek(start)
            remaining = length
            while remaining:
                chunk = stream.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/start/"):
            self._bootstrap(parsed.path.removeprefix("/start/"))
            return
        if not self._require_common():
            return
        if parsed.path in {"/", "/index.html"}:
            self._serve_html()
            return
        if parsed.path == "/api/config":
            try:
                result = self.state.call("ai_config_get", {}, timeout=15)
            except (ValueError, RuntimeError, OSError) as exc:
                self._reject(HTTPStatus.BAD_REQUEST, str(exc) or "Запрос не выполнен")
                return
            self._send_json(result)
            return
        if parsed.path.startswith("/api/batches/"):
            batch_id = parsed.path.removeprefix("/api/batches/")
            if not _ID.fullmatch(batch_id):
                self._reject(HTTPStatus.BAD_REQUEST, "Некорректный ID группы")
                return
            try:
                result = self.state.call(
                    "ai_telegram_batch_get", {"id": batch_id, "limit": 100}, timeout=15
                )
            except (ValueError, RuntimeError, OSError) as exc:
                self._reject(HTTPStatus.BAD_REQUEST, str(exc) or "Запрос не выполнен")
                return
            self._send_json(result)
            return
        if parsed.path in self.state.media_files:
            self._serve_media(parsed.path)
            return
        self._reject(HTTPStatus.NOT_FOUND, "Страница не найдена")

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if not self._require_write():
            return
        parsed = urlsplit(self.path)
        try:
            body = self._json_body()
            if parsed.path == "/api/config":
                key = str(body.get("api_key") or "").strip()
                if not key:
                    raise ValueError("Вставь API-ключ DeepSeek")
                result = self.state.call(
                    "ai_config_set", {"provider": "deepseek", "api_key": key}, timeout=20
                )
            elif parsed.path == "/api/ai/preview":
                result = self.state.call("ai_telegram_batch_preview", body, timeout=30)
            elif parsed.path == "/api/ai/start":
                result = self.state.call("ai_telegram_batch_start", body, timeout=30)
            elif parsed.path == "/api/catalog/refresh":
                result = self.state.refresh_catalog()
            else:
                self._reject(HTTPStatus.NOT_FOUND, "Маршрут не найден")
                return
        except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
            self._reject(HTTPStatus.BAD_REQUEST, str(exc) or "Запрос не выполнен")
            return
        self._send_json(result)


def create_web_server(state: WebState) -> HoardWebServer:
    return HoardWebServer((state.host, 0), state)


def _ready_path() -> Path:
    return state_dir() / _READY_FILE


def _launch_lock_path() -> Path:
    return state_dir() / _LAUNCH_LOCK_FILE


def _server_lock_path() -> Path:
    return state_dir() / _SERVER_LOCK_FILE


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if is_windows():
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            process_query_limited_information, False, int(pid)
        )
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return int(exit_code.value) == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_state() -> dict[str, Any] | None:
    try:
        value = json.loads(_ready_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _write_state(
    status: str,
    *,
    launch_id: str = "",
    pid: int | None = None,
    parent_pid: int = 0,
    port: int = 0,
    url: str = "",
    error: str = "",
) -> None:
    """Publish one complete state file atomically."""
    directory = ensure_private(state_dir())
    payload = json.dumps(
        {
            "launch_id": str(launch_id),
            "pid": int(os.getpid() if pid is None else pid),
            "parent_pid": int(parent_pid),
            "status": status,
            "port": int(port),
            "url": str(url),
            "error": str(error)[:500],
            "updated_at": time.time(),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".web.ready-", suffix=".tmp", dir=directory
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, _ready_path())
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _remove_state(*, launch_id: str = "", pid: int | None = None) -> bool:
    """Remove only a state file owned by the caller's launch/process."""
    value = _read_state()
    if value is None:
        return False
    if launch_id and str(value.get("launch_id") or "") != launch_id:
        return False
    if pid is not None and _state_pid(value) != int(pid):
        return False
    _ready_path().unlink(missing_ok=True)
    return True


def _clear_state_file() -> None:
    """Remove a corrupted/unreadable state while the launch lock is held."""
    _ready_path().unlink(missing_ok=True)


def _state_phase(value: dict[str, Any] | None) -> str:
    if not value:
        return ""
    status = str(value.get("status") or "ready")
    return status if status in {"preparing", "starting", "ready", "error"} else ""


def _state_pid(value: dict[str, Any] | None) -> int:
    try:
        return int(value.get("pid") or 0) if value else 0
    except (TypeError, ValueError):
        return 0


def _state_launch_id(value: dict[str, Any] | None) -> str:
    return str(value.get("launch_id") or "") if value else ""


def _state_port(value: dict[str, Any] | None) -> int:
    try:
        port = int(value.get("port") or 0) if value else 0
    except (TypeError, ValueError):
        return 0
    return port if 0 < port <= 65535 else 0


def _state_url(value: dict[str, Any] | None) -> str:
    return str(value.get("url") or "") if value else ""


def _open_browser(url: str) -> None:
    """Browser launch is best-effort; the printed/returned URL remains usable."""
    with contextlib.suppress(Exception):
        webbrowser.open(url, new=2)


def _web_reachable(port: int) -> bool:
    if port <= 0:
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


@contextmanager
def _file_lock(path: Path, *, blocking: bool) -> Iterator[bool]:
    ensure_private(path.parent)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if is_windows():
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            while True:
                try:
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in {
                        errno.EACCES,
                        errno.EAGAIN,
                        getattr(errno, "EDEADLK", errno.EACCES),
                    }:
                        raise
                    if not blocking:
                        yield False
                        return
                    time.sleep(_WEB_POLL_INTERVAL)
            try:
                yield True
            finally:
                os.lseek(descriptor, 0, os.SEEK_SET)
                with contextlib.suppress(OSError):
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(
                    descriptor,
                    fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB),
                )
            except OSError as exc:
                if not blocking and exc.errno in {errno.EACCES, errno.EAGAIN}:
                    yield False
                    return
                raise
            try:
                yield True
            finally:
                with contextlib.suppress(OSError):
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _wait_for_web(
    pid: int,
    process: subprocess.Popen | None,
    log: Path,
    *,
    launch_id: str,
) -> str:
    deadline = time.monotonic() + _WEB_LAUNCH_TIMEOUT
    while time.monotonic() < deadline:
        value = _read_state()
        if (
            _state_launch_id(value) == launch_id
            and _state_pid(value) == pid
            and value is not None
        ):
            phase = _state_phase(value)
            if phase == "ready":
                if process is not None and process.poll() is not None:
                    raise RuntimeError(
                        f"Веб-интерфейс завершился сразу после запуска; проверь {log}"
                    )
                if not _pid_alive(pid):
                    raise RuntimeError(
                        f"Веб-интерфейс завершился при подготовке; проверь {log}"
                    )
                url = _state_url(value)
                if url and _web_reachable(_state_port(value)):
                    return url
            elif phase == "error":
                detail = str(value.get("error") or "неизвестная ошибка")
                raise RuntimeError(f"Веб-интерфейс не запустился: {detail}")
        if process is not None and process.poll() is not None:
            value = _read_state()
            if (
                _state_launch_id(value) == launch_id
                and _state_pid(value) == pid
                and _state_phase(value) == "error"
            ):
                detail = str(value.get("error") or "неизвестная ошибка")
                raise RuntimeError(f"Веб-интерфейс не запустился: {detail}")
            raise RuntimeError(f"Веб-интерфейс не запустился; проверь {log}")
        if process is None and not _pid_alive(pid):
            raise RuntimeError(f"Веб-интерфейс завершился при подготовке; проверь {log}")
        time.sleep(_WEB_POLL_INTERVAL)
    if process is not None:
        with contextlib.suppress(OSError):
            process.terminate()
        with contextlib.suppress(Exception):
            process.wait(timeout=5)
        _remove_state(launch_id=launch_id)
    raise RuntimeError(
        "Веб-интерфейс не завершил подготовку за "
        f"{int(_WEB_LAUNCH_TIMEOUT)} с; проверь {log}"
    )


def run_web(*, open_browser: bool = True, lifetime: int = 1800) -> str:
    """Run the explicit loopback-only browser session until idle expiry or Ctrl+C."""
    with _file_lock(_server_lock_path(), blocking=False) as acquired:
        if not acquired:
            value = _read_state()
            url = _state_url(value)
            if (
                _state_phase(value) == "ready"
                and url
                and _web_reachable(_state_port(value))
            ):
                print(f"Hoard Web уже запущен: {url}", flush=True)
                if open_browser:
                    _open_browser(url)
                return url
            raise RuntimeError(
                "Веб-интерфейс уже запускается; повтори через несколько секунд"
            )

        launch_id = (
            str(os.environ.get("HOARD_WEB_LAUNCH_ID") or "").strip()
            or secrets.token_hex(16)
        )
        _write_state("preparing", launch_id=launch_id)
        server: HoardWebServer | None = None
        url = ""
        failed = False
        try:
            asyncio.run(ensure_daemon())
            report = asyncio.run(request("telegram_report", timeout=180))
            catalog_value = str(report.get("catalog") or "").strip()
            if not catalog_value:
                raise RuntimeError(
                    "Ответ daemon устарел и не содержит путь каталога. "
                    "Выполни `hoard shutdown` и повтори"
                )
            catalog = Path(catalog_value).expanduser().resolve(strict=True)
            if catalog.name != "telegram_archive.html" or not catalog.is_file():
                raise RuntimeError("Не удалось подготовить локальный HTML-каталог")
            media_roots = tuple(
                Path(str(value)) for value in report.get("media_roots") or [catalog.parent]
            )
            state = WebState(
                catalog,
                allowed_media_roots=media_roots,
                lifetime=max(60, min(int(lifetime), 86_400)),
            )
            server = create_web_server(state)
            server.timeout = 1
            url = state.start_url
            _write_state("ready", launch_id=launch_id, port=state.port, url=url)
            print(f"Hoard Web: {url}", flush=True)
            print("Только 127.0.0.1 · Ctrl+C для закрытия", flush=True)
            if open_browser:
                _open_browser(url)
            while time.monotonic() - state.last_seen <= state.lifetime:
                server.handle_request()
        except KeyboardInterrupt:
            pass
        except Exception as exc:
            failed = True
            _write_state(
                "error",
                launch_id=launch_id,
                error=f"{type(exc).__name__}: {exc}"[:500],
            )
            raise
        finally:
            if server is not None:
                server.server_close()
            if not failed:
                _remove_state(launch_id=launch_id, pid=os.getpid())
        return url


def launch_web_process(*, open_browser: bool = True) -> str:
    """Start or reuse the browser client; it never owns or stops the daemon."""
    ensure_private(state_dir())
    log = state_dir() / "web.log"
    log.touch(mode=0o600, exist_ok=True)
    process: subprocess.Popen | None = None
    pid = 0

    # The launch lock is held until the child publishes its own state.  The
    # parent publishes only the pre-spawn "starting" record, before Popen();
    # after that, only the child writes preparing/ready/error.  A concurrent
    # launcher waits here and reuses the ready server instead of spawning a
    # second one.
    with _file_lock(_launch_lock_path(), blocking=True):
        value = _read_state()
        phase = _state_phase(value)
        existing_pid = _state_pid(value)
        if value is None:
            _clear_state_file()
        if existing_pid and _pid_alive(existing_pid):
            if phase == "ready":
                existing_url = _state_url(value)
                if not existing_url or not _web_reachable(_state_port(value)):
                    raise RuntimeError(
                        "Веб-интерфейс уже запущен, но не отвечает; "
                        "закрой его или дождись завершения"
                    )
                if open_browser:
                    _open_browser(existing_url)
                return existing_url
            if phase in {"preparing", "starting"}:
                return _wait_for_web(
                    existing_pid,
                    None,
                    log,
                    launch_id=_state_launch_id(value),
                )
            if phase == "error":
                detail = str(value.get("error") or "неизвестная ошибка")
                raise RuntimeError(f"Веб-интерфейс не запустился: {detail}")
            _clear_state_file()
        elif existing_pid:
            _clear_state_file()

        launch_id = secrets.token_hex(16)
        _write_state(
            "starting",
            launch_id=launch_id,
            pid=0,
            parent_pid=os.getpid(),
        )
        command = [sys.executable, "-m", "hoard", "web"]
        environment = os.environ.copy()
        environment["HOARD_WEB_LAUNCH_ID"] = launch_id
        options: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "close_fds": True,
            "env": environment,
        }
        if is_windows():
            options["creationflags"] = (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            options["start_new_session"] = True
        try:
            with log.open("ab") as stream:
                options["stdout"] = stream
                options["stderr"] = stream
                process = subprocess.Popen(command, **options)
        except BaseException:
            _remove_state(launch_id=launch_id)
            raise
        pid = process.pid
        return _wait_for_web(pid, process, log, launch_id=launch_id)
