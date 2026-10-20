"""Minimal local terminal interface: Rich rendering + InquirerPy questions.

No alternate screen, no browser, no Telegram client in the UI process.
All mutations go through the existing daemon's private UNIX socket.
"""
from __future__ import annotations

import asyncio
import os
import select
import sys
import time
from pathlib import Path
from typing import Any

from rich.console import Console, Group
from rich.live import Live
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table
from rich.text import Text

from .ipc import ensure_daemon, request
from .models import parse_post_url
from .platform_support import is_windows

ORANGE = "#F79646"
WHITE = "#F0F0F0"
GRAY = "#929292"
DIM = "#777777"
SEPARATOR = "#292929"


def fmt_bytes(num: int | float | None) -> str:
    if num is None:
        return "—"
    n = float(num)
    for suffix in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or suffix == "TiB":
            return f"{n:.0f} {suffix}" if suffix == "B" else f"{n:.1f} {suffix}"
        n /= 1024
    return "—"


def source_display(source: dict) -> str:
    return f"{source['username']}/{source['post_id']}"


def progress_text(status: dict) -> str:
    """Always derive progress from actual telemetry, never synthesize totals."""
    p = status.get("progress") or {}
    if not p:
        return "Нет активного скачивания"
    checked = p.get("checked", 0)
    saved = p.get("downloaded", 0)
    label = f"{p.get('source', 'Telegram')} · комментариев: {checked} · новых кружков: {saved}"
    if p.get("file"):
        current, total = p.get("bytes"), p.get("total")
        amount = fmt_bytes(current)
        if total:
            amount += f" / {fmt_bytes(total)}"
        speed = p.get("speed_bps")
        return f"{label}\n{p['file']} · {amount}" + (f" · {fmt_bytes(speed)}/s" if speed else "")
    return label


def collector_label(status: dict) -> str:
    state = status.get("collector_state")
    if state == "paused":
        return "приостановлен"
    if status.get("running"):
        return "работает"
    return "остановлен"


def progress_view(status: dict) -> Group:
    progress = status.get("progress") or {}
    stats = status.get("stats") or {}
    queue = status.get("queue") or {}
    header = Text("hoard / downloads", style=f"bold {ORANGE}")
    state = Text(f"Сборщик: {collector_label(status)}", style=WHITE)
    pieces: list[Any] = [header, Text(""), state]
    if progress:
        pieces.extend([Text(""), Text(progress_text(status), style=WHITE)])
        total = progress.get("total")
        if progress.get("file") and total:
            bar = Progress(
                TextColumn(""),
                BarColumn(bar_width=32, style=SEPARATOR, complete_style=ORANGE,
                          finished_style=ORANGE),
                TextColumn("{task.percentage:>3.0f}%", style=ORANGE),
                expand=False, transient=False,
            )
            bar.add_task(
                "file",
                total=int(total),
                completed=min(int(progress.get("bytes") or 0), int(total)),
            )
            pieces.append(bar)
    else:
        pieces.extend([Text(""), Text("Нет активной загрузки", style=GRAY)])
    pieces.extend([
        Text(""),
        Text(f"Сохранено: {stats.get('downloads', 0)}    "
             f"Авторы: {stats.get('authors', 0)}    "
             f"Источники: {stats.get('sources', 0)}", style=GRAY),
        Text(
            f"Очередь: {queue.get('queued', 0)}    "
            f"В работе: {queue.get('running', 0)}    "
            f"Ошибки: {queue.get('failed', 0)}",
            style=GRAY,
        ),
        Text(""),
    ])
    if status.get("running"):
        pieces.append(Text("q — назад   p — пауза   s — остановить", style=DIM))
    elif status.get("collector_state") == "paused":
        pieces.append(Text("q — назад   r — продолжить   s — остановить", style=DIM))
    else:
        pieces.append(Text("q — назад   r — запустить", style=DIM))
    error = next(
        (item.get("text", "") for item in status.get("events", []) if item.get("kind") == "error"),
        "",
    )
    if error:
        pieces.extend([Text(""), Text(f"Последняя ошибка: {error}", style=ORANGE)])
    return Group(*pieces)


class HoardCLI:
    def __init__(self, *, console: Console | None = None, prompts: Any = None):
        self.console = console or Console(highlight=False, color_system="auto")
        self._prompts = prompts
        self._style = None

    def _ensure_prompts(self):
        if self._prompts is None:
            from InquirerPy import inquirer
            from InquirerPy.utils import get_style
            self._prompts = inquirer
            self._style = get_style({
                "questionmark": ORANGE,
                "answermark": ORANGE,
                "answer": WHITE,
                "input": WHITE,
                "question": WHITE,
                "answered_question": GRAY,
                "instruction": DIM,
                "long_instruction": DIM,
                "pointer": ORANGE,
                "checkbox": ORANGE,
                "separator": DIM,
                "skipped": DIM,
                "validator": ORANGE,
                "marker": ORANGE,
            }, style_override=True)
        return self._prompts

    def select(self, message: str, choices: list[dict[str, str]]) -> str:
        prompts = self._ensure_prompts()
        return prompts.select(
            message=message, choices=choices, style=self._style,
            pointer="❯", qmark="", amark="", border=False,
            instruction="(↑↓ выбор · Enter продолжить)",
            cycle=False,
        ).execute()

    def text(self, message: str, default: str = "") -> str:
        return str(self._ensure_prompts().text(
            message=message, default=default, style=self._style,
            qmark="", amark="",
        ).execute()).strip()

    def secret(self, message: str) -> str:
        return str(self._ensure_prompts().secret(
            message=message,
            style=self._style,
            qmark="",
            amark="",
            transformer=lambda _: "[скрыто]",
        ).execute())

    def confirm(self, message: str, default: bool = False) -> bool:
        return bool(self._ensure_prompts().confirm(
            message=message, default=default, style=self._style, qmark="", amark="",
        ).execute())

    @staticmethod
    def api(method: str, args: dict | None = None, timeout: float = 30) -> dict:
        return asyncio.run(request(method, args, timeout=timeout))

    def call(self, method: str, args: dict | None = None, timeout: float = 30) -> dict | None:
        try:
            result = self.api(method, args, timeout=timeout)
        except Exception as exc:
            message = str(exc)
            for prefix in ("RuntimeError: ", "ValueError: ", "OSError: "):
                if message.startswith(prefix):
                    message = message[len(prefix):]
                    break
            self.console.print(Text(f"Ошибка: {message}", style=ORANGE))
            return None
        message = result.get("message")
        if message:
            self.console.print(Text(message, style=GRAY))
        return result

    def get_status(self) -> dict:
        return self.api("status", timeout=4)

    def print_header(self, status: dict) -> None:
        self.console.print(
            Text("hoard.", style=f"bold {ORANGE}"),
            Text(" v0.4.0", style=DIM),
            sep="",
        )
        stats = status.get("stats") or {}
        auth_labels = {
            "authorized": "подключён",
            "api_required": "нужны API-ключи",
            "phone_required": "нужен вход",
            "code_required": "ожидается код",
            "password_required": "ожидается 2FA",
            "disconnected": "сессия не проверена",
        }
        telegram = auth_labels.get(status.get("telegram"), "сессия не проверена")
        self.console.print(Text(
            f"Telegram: {telegram}  ·  Сборщик: {collector_label(status)}  ·  "
            f"Источники: {stats.get('sources', 0)}  ·  Кружки: {stats.get('downloads', 0)}",
            style=GRAY,
        ))
        self.console.print(Text("─" * min(70, self.console.width), style=SEPARATOR))

    def print_sources(self, sources: list[dict]) -> None:
        if not sources:
            self.console.print(Text("Источников пока нет", style=GRAY))
            return
        for s in sources:
            marker = "●" if s["enabled"] else "○"
            title = s.get("title") or s["username"]
            self.console.print(
                Text(f"{marker} {title}", style=WHITE),
                Text(f"  {source_display(s)}  ·  #{s['id']}", style=GRAY),
                sep="",
            )
            result = s.get("last_result") or "Ещё не проверялся"
            checked = s.get("last_checked_at") or "—"
            self.console.print(
                Text(
                    f"  Кружки: {s.get('download_count', 0)}  ·  "
                    f"Последняя проверка: {checked}  ·  {result}",
                    style=GRAY,
                )
            )
            if s.get("last_error"):
                self.console.print(Text(f"  Ошибка: {s['last_error']}", style=ORANGE))

    def _auth_error(self, result: dict) -> None:
        if result.get("error"):
            self.console.print(Text(str(result["error"]), style=ORANGE))

    def connect_telegram(self, *, quiet_if_authorized: bool = False) -> bool:
        """Drive daemon-owned Telegram login as one cancellable conversation."""
        phone = ""
        last_code = ""
        last_password = ""
        try:
            result = self.call("auth_begin", timeout=75)
            while result:
                state = result.get("state")
                self._auth_error(result)
                if result.get("error") and not result.get("retryable", True):
                    return False
                if state == "authorized" or result.get("authorized"):
                    if not quiet_if_authorized and not result.get("message"):
                        self.console.print(Text("Telegram подключён.", style=WHITE))
                    return True
                if state == "api_required":
                    raw_id = self.text("API ID с my.telegram.org/apps")
                    try:
                        api_id = int(raw_id)
                    except ValueError:
                        self.console.print(
                            Text("API ID должен быть положительным числом.", style=ORANGE)
                        )
                        continue
                    api_hash = self.secret("API Hash")
                    if not api_hash:
                        self.console.print(Text("API Hash не должен быть пустым.", style=ORANGE))
                        continue
                    if self.call(
                        "credentials", {"api_id": api_id, "api_hash": api_hash}
                    ) is None:
                        return False
                    result = self.call("auth_begin", timeout=75)
                    continue
                if state == "phone_required":
                    force = False
                    if phone and result.get("error_code") in {"code_expired", "network"}:
                        if not self.confirm("Запросить новый код на тот же номер?", True):
                            self.call("auth_cancel")
                            return False
                        force = True
                    elif phone and not result.get("error"):
                        # A valid phone survives correction of rejected API credentials.
                        force = False
                    else:
                        phone = self.text("Номер телефона (+79990000000)")
                    result = self.call(
                        "auth_send_code", {"phone": phone, "force": force}, timeout=90
                    )
                    continue
                if state == "code_required":
                    if last_code and result.get("error_code") == "network":
                        reuse = self.confirm("Повторить проверку того же кода?", True)
                        code = last_code if reuse else self.secret("Код из Telegram")
                    else:
                        code = self.secret("Код из Telegram")
                    last_code = code
                    result = self.call("auth_submit_code", {"code": code}, timeout=90)
                    if result and result.get("error_code") == "code_invalid":
                        last_code = ""
                    continue
                if state == "password_required":
                    if last_password and result.get("error_code") == "network":
                        reuse = self.confirm("Повторить проверку того же пароля?", True)
                        password = (
                            last_password
                            if reuse
                            else self.secret("Пароль двухэтапной проверки")
                        )
                    else:
                        password = self.secret("Пароль двухэтапной проверки")
                    last_password = password
                    result = self.call(
                        "auth_submit_password", {"password": password}, timeout=90
                    )
                    if result and result.get("error_code") == "password_invalid":
                        last_password = ""
                    continue
                if state == "disconnected" and result.get("error_code") == "network":
                    if not self.confirm("Повторить подключение к Telegram?", True):
                        return False
                    result = self.call("auth_begin", timeout=75)
                    continue
                self.console.print(
                    Text("Неизвестное состояние подключения Telegram.", style=ORANGE)
                )
                return False
            return False
        except (KeyboardInterrupt, EOFError):
            cancelled = self.call("auth_cancel")
            if cancelled is None:
                self.console.print(
                    Text("Подключение отменено; служба продолжает работу.", style=GRAY)
                )
            return False

    def add_source(self) -> None:
        self.console.print(Text("Новый Telegram-пост", style=f"bold {ORANGE}"))
        raw = self.text("Ссылка на пост")
        try:
            post = parse_post_url(raw)
        except ValueError as exc:
            self.console.print(Text(str(exc), style=ORANGE))
            return
        if not self.connect_telegram(quiet_if_authorized=True):
            self.console.print(
                Text("Пост не добавлен: подключение Telegram не завершено.", style=GRAY)
            )
            return
        checked = self.call("source_validate", {"url": post.link}, timeout=75)
        if not checked:
            return
        self.console.print(
            Text("Найден пост: ", style=GRAY),
            Text(str(checked.get("title") or post.username), style=WHITE),
            sep="",
        )
        status = self.get_status()
        target = self.text(
            "Каталог", default=str(status.get("archive_root") or "~/Videos/hoard")
        )
        if not target:
            self.console.print(Text("Каталог не должен быть пустым", style=ORANGE))
            return
        interval = 120
        if not self.confirm("Проверять каждые 120 секунд?", default=True):
            interval_str = self.text("Интервал проверки в секундах", "120")
            try:
                interval = int(interval_str)
                if interval < 30 or interval > 86400:
                    raise ValueError
            except ValueError:
                self.console.print(Text("Интервал: от 30 до 86400 секунд", style=ORANGE))
                return
        self.console.print(Text(f"{post.link}  →  {Path(target).expanduser()}", style=GRAY))
        if not self.confirm("Добавить источник?", default=True):
            return
        added = self.call(
            "source_add",
            {
                "url": post.link,
                "output": target,
                "interval": interval,
                "title": checked.get("title", ""),
            },
        )
        if added and self.confirm("Сразу проверить новые комментарии?", default=True):
            status = self.get_status()
            if not status.get("running"):
                self.call("start")
            self.call("scan")

    def sources_menu(self) -> None:
        while True:
            status = self.get_status()
            self.console.print()
            self.console.print(Text("hoard / sources", style=f"bold {ORANGE}"))
            self.print_sources(status.get("sources", []))
            action = self.select("Источники", [
                {"name": "Добавить пост", "value": "add"},
                {"name": "Включить / отключить пост", "value": "toggle"},
                {"name": "Назад", "value": "back"},
            ])
            if action == "back":
                return
            if action == "add":
                self.add_source()
            if action == "toggle":
                sources = status.get("sources", [])
                if not sources:
                    self.console.print(Text("Нет источников", style=GRAY))
                    continue
                chosen = self.select("Выбери пост", [
                    {"name": f"{'●' if s['enabled'] else '○'} {source_display(s)} · #{s['id']}",
                     "value": str(s["id"])} for s in sources
                ])
                source = next(s for s in sources if s["id"] == int(chosen))
                self.call(
                    "source_enable",
                    {"id": source["id"], "enabled": not bool(source["enabled"])},
                )

    def telegram_menu(self) -> None:
        self.console.print(Text("hoard / telegram", style=f"bold {ORANGE}"))
        self.connect_telegram()

    def show_recent(self) -> None:
        status = self.get_status()
        self.console.print(Text("hoard / downloads", style=f"bold {ORANGE}"))
        items = status.get("downloads", [])[:15]
        if not items:
            self.console.print(Text("Пока нет сохранённых кружков", style=GRAY))
            return
        table = Table(box=None, show_header=True, padding=(0, 2), pad_edge=False)
        for label in ("Комментарий", "Автор", "Размер", "Файл"):
            table.add_column(label, style=GRAY)
        for item in items:
            table.add_row(
                str(item["message_id"]),
                Text(item.get("author_username") or item.get("author_name") or "—", style=WHITE),
                fmt_bytes(item.get("size")),
                Text(str(item.get("file_path") or "").rsplit("/", 1)[-1], style=WHITE),
            )
        self.console.print(table)

    def show_live(self) -> None:
        """Inline live view; no alt screen; keyboard shortcuts only while viewing."""
        if not sys.stdin.isatty():
            self.console.print(Text("Для просмотра нужен интерактивный терминал", style=GRAY))
            return
        if is_windows():
            import msvcrt

            def read_key() -> str | None:
                if msvcrt.kbhit():
                    char = msvcrt.getwch()
                    # Ignore special-key scan codes (arrows, function keys).
                    if char in ("\x00", "\xe0"):
                        msvcrt.getwch()
                        return None
                    return char.lower()
                return None

            def key_wait() -> None:
                time.sleep(0.1)

            def restore() -> None:
                pass
        else:
            import termios
            import tty

            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            tty.setcbreak(fd)

            def read_key() -> str | None:
                ready, _, _ = select.select([fd], [], [], 0.15)
                if not ready:
                    return None
                return os.read(fd, 1).decode("utf-8", errors="ignore").lower()

            def key_wait() -> None:
                pass

            def restore() -> None:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

        try:
            with Live(progress_view(self.get_status()), console=self.console,
                      screen=False, auto_refresh=False, transient=True) as live:
                next_poll = 0.0
                while True:
                    if time.monotonic() >= next_poll:
                        try:
                            status = self.get_status()
                            live.update(progress_view(status), refresh=True)
                        except Exception as exc:
                            live.update(
                                Text(f"Служба недоступна: {exc}", style=ORANGE),
                                refresh=True,
                            )
                        next_poll = time.monotonic() + 0.6
                    char = read_key()
                    if char is None:
                        key_wait()
                        continue
                    if char in ("q", "\x1b", "\x03", "\x04"):
                        break
                    if char in ("p", "s", "r"):
                        command = {"p": "pause", "s": "stop", "r": "start"}[char]
                        self.call(command)
                        next_poll = 0.0
        finally:
            restore()

    def control_menu(self, status: dict) -> None:
        state = status.get("collector_state")
        if status.get("running"):
            choices = [
                {"name": "Приостановить", "value": "pause"},
                {"name": "Остановить", "value": "stop"},
            ]
        elif state == "paused":
            choices = [
                {"name": "Продолжить", "value": "start"},
                {"name": "Остановить", "value": "stop"},
            ]
        else:
            choices = [{"name": "Запустить", "value": "start"}]
        choices.append({"name": "Назад", "value": "back"})
        action = self.select("Сборщик", choices)
        if action == "back":
            return
        if action == "start" and not self.connect_telegram(quiet_if_authorized=True):
            return
        if (
            status.get("running")
            and action in {"stop", "pause"}
            and not self.confirm("Прервать активную загрузку и сохранить её в очереди?", False)
        ):
            return
        self.call(action)

    def run(self) -> None:
        try:
            asyncio.run(ensure_daemon())
        except (TimeoutError, RuntimeError, OSError) as exc:
            self.console.print(Text(f"Не удалось запустить локальную службу: {exc}", style=ORANGE))
            return
        while True:
            try:
                status = self.get_status()
                self.console.print()
                self.print_header(status)
                control = {
                    "running": "Управление сборщиком",
                    "paused": "Продолжить или остановить сборщик",
                    "stopped": "Запустить сборщик",
                }.get(status.get("collector_state"), "Управление сборщиком")
                telegram = (
                    "Проверить подключение Telegram"
                    if status.get("telegram") == "authorized"
                    else "Подключить Telegram"
                )
                choices = [
                    {"name": "Источники и посты", "value": "sources"},
                    {"name": "Загрузки и прогресс", "value": "downloads"},
                    {"name": "Обновить HTML-каталог", "value": "catalog"},
                ]
                if (status.get("stats") or {}).get("sources", 0):
                    choices.extend([
                        {"name": "Проверить новые комментарии", "value": "scan"},
                        {"name": "Дополнить данные из Telegram", "value": "enrich"},
                        {"name": control, "value": "control"},
                    ])
                choices.extend([
                    {"name": telegram, "value": "telegram"},
                    {"name": "Выход", "value": "exit"},
                ])
                action = self.select("Выбери действие", choices)
                if action == "exit":
                    self.console.print(
                        Text(
                            "Интерфейс закрыт. Фоновая служба продолжает работу.",
                            style=DIM,
                        )
                    )
                    break
                if action == "sources":
                    self.sources_menu()
                elif action == "downloads":
                    sub = self.select("Загрузки", [
                        {"name": "Живой прогресс", "value": "live"},
                        {"name": "Последние скачанные кружки", "value": "recent"},
                        {"name": "Назад", "value": "back"},
                    ])
                    if sub == "live":
                        self.show_live()
                    elif sub == "recent":
                        self.show_recent()
                elif action == "scan":
                    if self.connect_telegram(quiet_if_authorized=True):
                        if not status.get("running"):
                            if not self.confirm("Сборщик остановлен. Запустить и проверить?", True):
                                continue
                            self.call("start")
                        self.call("scan")
                elif action == "catalog":
                    result = self.call("telegram_report", timeout=90)
                    if result:
                        self.console.print(
                            Text(str(status.get("catalog") or ""), style=WHITE)
                        )
                elif action == "enrich":
                    if self.connect_telegram(quiet_if_authorized=True):
                        if not status.get("running"):
                            self.call("start")
                        self.call("telegram_enrich")
                elif action == "control":
                    self.control_menu(status)
                elif action == "telegram":
                    self.telegram_menu()
            except (KeyboardInterrupt, EOFError):
                self.console.print(
                    Text(
                        "\nИнтерфейс закрыт. Фоновая служба продолжает работу.",
                        style=DIM,
                    )
                )
                break
            except (ConnectionError, RuntimeError, OSError, ValueError) as exc:
                self.console.print(Text(f"Ошибка: {exc}", style=ORANGE))
                break


def launch() -> None:
    HoardCLI().run()
