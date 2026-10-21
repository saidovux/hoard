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

from rich.cells import cell_len, set_cell_size
from rich.console import Console, Group
from rich.live import Live
from rich.padding import Padding
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table
from rich.text import Text

from . import __version__
from .ipc import ensure_daemon, request
from .models import parse_post_url
from .platform_support import is_windows

ORANGE = "#F79646"
WHITE = "#F0F0F0"
GRAY = "#929292"
DIM = "#777777"
SEPARATOR = "#292929"
LAYOUT_MAX_WIDTH = 112
LAYOUT_MIN_WIDTH = 44

SELECT_KEYBINDINGS = {
    "down": [{"key": "down"}, {"key": "c-n"}, {"key": "j"}],
    "up": [{"key": "up"}, {"key": "c-p"}, {"key": "k"}],
    "answer": [{"key": "enter"}, {"key": "l"}],
    "interrupt": [{"key": "c-c"}, {"key": "c-d"}, {"key": "h"}],
}

NETWORK_ACTIVITIES = {
    "auth_begin": "Проверяю сохранённую Telegram-сессию",
    "auth_send_code": "Подключаюсь к Telegram и запрашиваю код",
    "auth_submit_code": "Проверяю код Telegram",
    "auth_submit_password": "Проверяю пароль двухэтапной защиты",
    "source_validate": "Проверяю доступ к Telegram-посту",
}


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


def progress_view(status: dict, width: int = 72) -> Group:
    progress = status.get("progress") or {}
    stats = status.get("stats") or {}
    queue = status.get("queue") or {}
    width = max(24, width)
    heading = Table.grid(padding=0)
    heading.width = width
    heading.add_column(ratio=1)
    heading.add_column(ratio=1, justify="right")
    heading.add_row(
        Text("ЗАГРУЗКИ", style=f"bold {ORANGE}"),
        Text(f"Сборщик  {collector_label(status)}", style=WHITE),
    )
    pieces: list[Any] = [heading, Text("─" * width, style=SEPARATOR), Text("")]
    if progress:
        pieces.append(Text(progress_text(status), style=WHITE))
        total = progress.get("total")
        if progress.get("file") and total:
            bar = Progress(
                TextColumn(""),
                BarColumn(
                    bar_width=max(10, width - 8),
                    style=SEPARATOR,
                    complete_style=ORANGE,
                    finished_style=ORANGE,
                ),
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
        pieces.append(Text("Нет активной загрузки", style=GRAY))
    counters = Table.grid(padding=0)
    counters.width = width
    archive_stats = Text(
        f"{stats.get('downloads', 0)} кружков  ·  "
        f"{stats.get('authors', 0)} профилей  ·  "
        f"{stats.get('sources', 0)} источников",
        style=GRAY,
    )
    queue_stats = Text(
        f"Очередь: {queue.get('queued', 0)}    "
        f"В работе: {queue.get('running', 0)}    "
        f"Ошибки: {queue.get('failed', 0)}",
        style=GRAY,
    )
    if width < 84:
        counters.add_column()
        counters.add_row(archive_stats)
        counters.add_row(queue_stats)
    else:
        counters.add_column(ratio=1)
        counters.add_column(ratio=1, justify="right")
        counters.add_row(archive_stats, queue_stats)
    pieces.extend([Text(""), counters, Text("")])
    if status.get("running"):
        pieces.append(Text("h/q — назад   p — пауза   s — остановить", style=DIM))
    elif status.get("collector_state") == "paused":
        pieces.append(Text("h/q — назад   r — продолжить   s — остановить", style=DIM))
    else:
        pieces.append(Text("h/q — назад   r — запустить", style=DIM))
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
        self._notice: tuple[str, bool] | None = None

    @property
    def content_width(self) -> int:
        available = max(1, self.console.width - 2)
        if available < LAYOUT_MIN_WIDTH:
            return available
        return min(LAYOUT_MAX_WIDTH, available)

    @property
    def content_margin(self) -> int:
        return max(0, (self.console.width - self.content_width) // 2)

    def clear_view(self) -> None:
        """Clear the primary screen between views without using an alternate screen."""
        if self.console.is_terminal:
            self.console.clear(home=True)

    def print_content(self, renderable: Any = "") -> None:
        margin = self.content_margin
        self.console.print(
            Padding(renderable, (0, margin, 0, margin), expand=False),
            soft_wrap=False,
        )

    @staticmethod
    def _fit(text: str, width: int) -> str:
        if width <= 0:
            return ""
        if cell_len(text) <= width:
            return text
        if width == 1:
            return "…"
        return set_cell_size(text, width - 1).rstrip() + "…"

    def _format_choice(self, title: str, hint: str = "") -> str:
        width = max(1, self.content_width - 3)
        if not hint:
            return self._fit(title, width)
        title_width = cell_len(title)
        hint_width = cell_len(hint)
        if title_width + hint_width + 3 <= width:
            return f"{title}{' ' * (width - title_width - hint_width)}{hint}"
        remaining = width - title_width - 3
        if remaining >= 10:
            return f"{title} · {self._fit(hint, remaining)}"
        return self._fit(title, width)

    def feedback(self, message: str, *, error: bool = False) -> None:
        self._notice = (message, error)
        self.print_content(Text(message, style=ORANGE if error else GRAY))

    def render_page(self, status: dict, name: str, description: str = "") -> None:
        self.clear_view()
        self.print_header(status)
        self.print_section(name, description)

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
        margin = " " * self.content_margin
        display_choices = []
        for choice in choices:
            display = dict(choice)
            display["name"] = self._format_choice(
                str(choice.get("name", "")), str(choice.get("hint", ""))
            )
            display.pop("hint", None)
            display_choices.append(display)
        try:
            result = prompts.select(
                message=f"{margin}{message}",
                choices=display_choices,
                style=self._style,
                pointer=f"{margin}›",
                qmark="",
                amark="",
                border=False,
                instruction="  j/k перемещение · l/Enter открыть · h назад",
                keybindings=SELECT_KEYBINDINGS,
                cycle=False,
                show_cursor=False,
            ).execute()
            return str(result)
        except KeyboardInterrupt:
            values = {str(choice.get("value", "")) for choice in choices}
            if "back" in values:
                return "back"
            if "exit" in values:
                return "exit"
            raise
        finally:
            self.clear_view()

    def text(self, message: str, default: str = "") -> str:
        try:
            return str(self._ensure_prompts().text(
                message=f"{' ' * self.content_margin}{message}",
                default=default,
                style=self._style,
                qmark="",
                amark="",
            ).execute()).strip()
        finally:
            self.clear_view()

    def secret(self, message: str) -> str:
        try:
            return str(self._ensure_prompts().secret(
                message=f"{' ' * self.content_margin}{message}",
                style=self._style,
                qmark="",
                amark="",
                transformer=lambda _: "[скрыто]",
            ).execute())
        finally:
            self.clear_view()

    def confirm(self, message: str, default: bool = False) -> bool:
        try:
            return bool(self._ensure_prompts().confirm(
                message=f"{' ' * self.content_margin}{message}",
                default=default,
                style=self._style,
                qmark="",
                amark="",
            ).execute())
        finally:
            self.clear_view()

    @staticmethod
    def api(method: str, args: dict | None = None, timeout: float = 30) -> dict:
        return asyncio.run(request(method, args, timeout=timeout))

    async def _api_with_activity_async(
        self, method: str, args: dict | None, timeout: float, activity: str
    ) -> dict:
        task = asyncio.create_task(request(method, args, timeout=timeout))
        started = time.monotonic()
        hint = "проверь сеть/VPN, если ожидание затянулось · Ctrl+C — отмена"
        try:
            with self.console.status(
                Text(f"{activity} · 0 с · {hint}", style=GRAY),
                spinner="dots",
                spinner_style=ORANGE,
                refresh_per_second=8,
            ) as status:
                while not task.done():
                    elapsed = int(time.monotonic() - started)
                    status.update(Text(f"{activity} · {elapsed} с · {hint}", style=GRAY))
                    await asyncio.wait({task}, timeout=0.2)
            return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def api_with_activity(
        self, method: str, args: dict | None, timeout: float, activity: str
    ) -> dict:
        return asyncio.run(self._api_with_activity_async(method, args, timeout, activity))

    def call(
        self,
        method: str,
        args: dict | None = None,
        timeout: float = 30,
        *,
        display: bool = True,
    ) -> dict | None:
        try:
            activity = NETWORK_ACTIVITIES.get(method)
            result = (
                self.api_with_activity(method, args, timeout, activity)
                if activity
                else self.api(method, args, timeout=timeout)
            )
        except Exception as exc:
            if isinstance(exc, TimeoutError) and method in NETWORK_ACTIVITIES:
                message = (
                    "Telegram не ответил вовремя. Проверь интернет и VPN, "
                    "затем повтори действие."
                )
            else:
                message = str(exc) or "Операция не завершилась"
            for prefix in ("RuntimeError: ", "ValueError: ", "OSError: "):
                if message.startswith(prefix):
                    message = message[len(prefix):]
                    break
            if display:
                self.feedback(f"Ошибка: {message}", error=True)
            else:
                self._notice = (f"Ошибка: {message}", True)
            return None
        message = result.get("message")
        if message:
            if display:
                self.feedback(str(message))
            else:
                self._notice = (str(message), False)
        return result

    def get_status(self) -> dict:
        return self.api("status", timeout=4)

    def print_header(self, status: dict) -> None:
        stats = status.get("stats") or {}
        queue = status.get("queue") or {}
        auth_labels = {
            "authorized": "подключён",
            "api_required": "нужны API-ключи",
            "phone_required": "нужен вход",
            "code_required": "ожидается код",
            "password_required": "ожидается 2FA",
            "disconnected": "сессия не проверена",
        }
        telegram = auth_labels.get(status.get("telegram"), "сессия не проверена")
        telegram_marker = "●" if status.get("telegram") == "authorized" else "○"
        collector_marker = "●" if status.get("running") else "○"
        brand = Text()
        brand.append("hoard", style=f"bold {ORANGE}")
        brand.append("  local telegram archive", style=DIM)
        version = Text(f"v{__version__}", style=DIM)

        telegram_state = Text(f"{telegram_marker} ", style=ORANGE)
        telegram_state.append("Telegram  ", style=GRAY)
        telegram_state.append(telegram, style=WHITE)
        collector_state = Text(f"{collector_marker} ", style=ORANGE)
        collector_state.append("Сборщик  ", style=GRAY)
        collector_state.append(collector_label(status), style=WHITE)

        archive_stats = Text(
            f"источники {stats.get('sources', 0)}  ·  "
            f"профили {stats.get('authors', 0)}  ·  "
            f"кружки {stats.get('downloads', 0)}",
            style=GRAY,
        )
        queue_stats = Text(
            f"очередь {queue.get('queued', 0)}  ·  "
            f"в работе {queue.get('running', 0)}  ·  "
            f"ошибки {queue.get('failed', 0)}",
            style=GRAY,
        )
        identity = Table.grid(padding=0)
        identity.width = self.content_width
        identity.add_column(ratio=3, no_wrap=True)
        identity.add_column(ratio=1, justify="right", no_wrap=True)
        identity.add_row(brand, version)

        states = Table.grid(padding=0)
        states.width = self.content_width
        states.add_column(ratio=1, no_wrap=True)
        states.add_column(ratio=1, justify="right", no_wrap=True)
        states.add_row(telegram_state, collector_state)

        metrics = Table.grid(padding=0)
        metrics.width = self.content_width
        if self.content_width < 84:
            metrics.add_column()
            metrics.add_row(archive_stats)
            metrics.add_row(queue_stats)
        else:
            metrics.add_column(ratio=1, no_wrap=True)
            metrics.add_column(ratio=1, justify="right", no_wrap=True)
            metrics.add_row(archive_stats, queue_stats)
        self.print_content(Group(identity, states, metrics))
        self.print_content(Text("─" * self.content_width, style=SEPARATOR))
        if self._notice:
            message, is_error = self._notice
            self.print_content(Text(f"  {message}", style=ORANGE if is_error else WHITE))
            self._notice = None

    def print_section(self, name: str, description: str = "") -> None:
        section = Table.grid(padding=0)
        section.width = self.content_width
        section.add_column(ratio=1, no_wrap=True)
        section.add_column(ratio=2, justify="right")
        section.add_row(
            Text(name.upper(), style=f"bold {ORANGE}"),
            Text(description, style=GRAY),
        )
        self.print_content("")
        self.print_content(section)
        self.print_content("")

    def print_sources(self, sources: list[dict]) -> None:
        if not sources:
            self.print_content(Text("Источников пока нет", style=GRAY))
            return
        table = Table.grid(padding=(0, 1))
        table.width = self.content_width
        table.add_column(ratio=3)
        table.add_column(ratio=2, justify="right")
        for s in sources:
            marker = "●" if s["enabled"] else "○"
            title = s.get("title") or s["username"]
            table.add_row(
                Text(f"{marker}  {title}", style=WHITE),
                Text(f"{source_display(s)}  ·  #{s['id']}", style=GRAY),
            )
            result = s.get("last_result") or "Ещё не проверялся"
            checked = s.get("last_checked_at") or "—"
            table.add_row(
                Text(
                    f"   {s.get('download_count', 0)} кружков  ·  {result}",
                    style=GRAY,
                ),
                Text(f"проверка {checked}", style=GRAY),
            )
            if s.get("last_error"):
                table.add_row(
                    Text(f"   {s['last_error']}", style=ORANGE),
                    Text("ошибка", style=ORANGE),
                )
            table.add_row("", "")
        self.print_content(table)

    def _auth_error(self, result: dict) -> None:
        if result.get("error"):
            self.feedback(str(result["error"]), error=True)

    def connect_telegram(self, *, quiet_if_authorized: bool = False) -> bool:
        """Drive daemon-owned Telegram login as one cancellable conversation."""
        phone = ""
        last_code = ""
        last_password = ""
        try:
            if not quiet_if_authorized:
                self.print_content(
                    Text(
                        "Вход выполняет фоновая служба. Во время сетевых шагов "
                        "будут видны ожидание и таймер.",
                        style=GRAY,
                    )
                )
            result = self.call("auth_begin", timeout=75)
            while result:
                state = result.get("state")
                self._auth_error(result)
                if result.get("error") and not result.get("retryable", True):
                    return False
                if state == "authorized" or result.get("authorized"):
                    if not quiet_if_authorized and not result.get("message"):
                        self.feedback("Telegram подключён.")
                    return True
                if state == "api_required":
                    raw_id = self.text("API ID с my.telegram.org/apps")
                    try:
                        api_id = int(raw_id)
                    except ValueError:
                        self.feedback("API ID должен быть положительным числом.", error=True)
                        continue
                    api_hash = self.secret("API Hash")
                    if not api_hash:
                        self.feedback("API Hash не должен быть пустым.", error=True)
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
                    self.print_content(
                        Text(
                            "Запрашиваю код. Если Telegram недоступен в текущей сети, "
                            "включи VPN; Ctrl+C безопасно отменит вход.",
                            style=GRAY,
                        )
                    )
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
                self.feedback("Неизвестное состояние подключения Telegram.", error=True)
                return False
            return False
        except (KeyboardInterrupt, EOFError):
            cancelled = self.call("auth_cancel")
            if cancelled is None:
                self.feedback("Подключение отменено; служба продолжает работу.")
            return False

    def add_source(self) -> None:
        self.render_page(
            self.get_status(),
            "новый пост", "Ссылка → проверка доступа → каталог → запуск сканирования"
        )
        raw = self.text("Ссылка на пост")
        try:
            post = parse_post_url(raw)
        except ValueError as exc:
            self.feedback(str(exc), error=True)
            return
        if not self.connect_telegram(quiet_if_authorized=True):
            self.feedback("Пост не добавлен: подключение Telegram не завершено.")
            return
        checked = self.call("source_validate", {"url": post.link}, timeout=75)
        if not checked:
            return
        self.print_content(
            Text("Найден пост: ", style=GRAY),
        )
        self.print_content(Text(str(checked.get("title") or post.username), style=WHITE))
        status = self.get_status()
        target = self.text(
            "Каталог", default=str(status.get("archive_root") or "~/Videos/hoard")
        )
        if not target:
            self.feedback("Каталог не должен быть пустым", error=True)
            return
        interval = 120
        if not self.confirm("Проверять каждые 120 секунд?", default=True):
            interval_str = self.text("Интервал проверки в секундах", "120")
            try:
                interval = int(interval_str)
                if interval < 30 or interval > 86400:
                    raise ValueError
            except ValueError:
                self.feedback("Интервал: от 30 до 86400 секунд", error=True)
                return
        self.print_content(Text(f"{post.link}  →  {Path(target).expanduser()}", style=GRAY))
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
            self.render_page(status, "источники", "Посты, которые Hoard проверяет в фоне")
            self.print_sources(status.get("sources", []))
            action = self.select("Источники", [
                {"name": "Добавить пост", "hint": "новый источник", "value": "add"},
                {"name": "Включить / отключить", "hint": "управление", "value": "toggle"},
                {"name": "Назад", "value": "back"},
            ])
            if action == "back":
                return
            if action == "add":
                self.add_source()
            if action == "toggle":
                sources = status.get("sources", [])
                if not sources:
                    self.feedback("Нет источников")
                    continue
                source_choices = [
                    {
                        "name": (
                            f"{'●' if source['enabled'] else '○'} "
                            f"{source_display(source)} · #{source['id']}"
                        ),
                        "value": str(source["id"]),
                    }
                    for source in sources
                ]
                source_choices.append({"name": "Назад", "value": "back"})
                chosen = self.select("Выбери пост", source_choices)
                if chosen == "back":
                    continue
                source = next(s for s in sources if s["id"] == int(chosen))
                self.call(
                    "source_enable",
                    {"id": source["id"], "enabled": not bool(source["enabled"])},
                )

    def telegram_menu(self) -> None:
        self.render_page(
            self.get_status(), "telegram", "Проверка сессии и безопасный вход через daemon"
        )
        self.connect_telegram()

    def show_recent(self) -> None:
        status = self.get_status()
        self.render_page(status, "загрузки", "Последние сохранённые видеокружки")
        items = status.get("downloads", [])[:15]
        if not items:
            self.print_content(Text("Пока нет сохранённых кружков", style=GRAY))
            self.select("", [{"name": "Назад", "value": "back"}])
            return
        table = Table(
            box=None,
            show_header=True,
            padding=(0, 1),
            pad_edge=False,
            width=self.content_width,
            expand=False,
        )
        for label in ("Комментарий", "Автор", "Размер", "Файл"):
            table.add_column(label, style=GRAY)
        for item in items:
            table.add_row(
                str(item["message_id"]),
                Text(item.get("author_username") or item.get("author_name") or "—", style=WHITE),
                fmt_bytes(item.get("size")),
                Text(str(item.get("file_path") or "").rsplit("/", 1)[-1], style=WHITE),
            )
        self.print_content(table)
        self.select("", [{"name": "Назад", "value": "back"}])

    def show_live(self) -> None:
        """Inline live view; no alt screen; keyboard shortcuts only while viewing."""
        if not sys.stdin.isatty():
            self.feedback("Для просмотра нужен интерактивный терминал")
            return
        self.clear_view()
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
            def view(data: dict) -> Padding:
                return Padding(
                    progress_view(data, self.content_width),
                    (0, self.content_margin),
                    expand=False,
                )

            with Live(
                view(self.get_status()),
                console=self.console,
                screen=False,
                auto_refresh=False,
                transient=True,
            ) as live:
                next_poll = 0.0
                while True:
                    if time.monotonic() >= next_poll:
                        try:
                            status = self.get_status()
                            live.update(view(status), refresh=True)
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
                    if char in ("h", "q", "\x1b", "\x03", "\x04"):
                        break
                    if char in ("p", "s", "r"):
                        command = {"p": "pause", "s": "stop", "r": "start"}[char]
                        self.call(command, display=False)
                        next_poll = 0.0
        finally:
            restore()

    def control_menu(self, status: dict) -> None:
        self.render_page(status, "сборщик", "Запуск, пауза и безопасная остановка")
        state = status.get("collector_state")
        if status.get("running"):
            choices = [
                {"name": "Приостановить", "hint": "сохранить очередь", "value": "pause"},
                {"name": "Остановить", "hint": "завершить сбор", "value": "stop"},
            ]
        elif state == "paused":
            choices = [
                {"name": "Продолжить", "hint": "возобновить очередь", "value": "start"},
                {"name": "Остановить", "hint": "завершить сбор", "value": "stop"},
            ]
        else:
            choices = [{"name": "Запустить", "hint": "обработать очередь", "value": "start"}]
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

    def catalog_menu(self, status: dict) -> None:
        catalog = Path(str(status.get("catalog") or "")).expanduser()
        self.render_page(status, "html-каталог", "Один автономный файл для просмотра архива")
        choices = []
        if catalog.is_file():
            choices.append(
                {"name": "Открыть в браузере", "hint": catalog.name, "value": "open"}
            )
        choices.extend(
            [
                {"name": "Обновить", "hint": "пересобрать из SQLite", "value": "rebuild"},
                {"name": "Показать путь", "hint": "скопировать вручную", "value": "path"},
                {"name": "Назад", "value": "back"},
            ]
        )
        action = self.select("HTML-каталог", choices)
        if action == "back":
            return
        if action == "rebuild":
            result = self.call("telegram_report", timeout=90)
            if result:
                self._notice = (f"Каталог обновлён: {catalog}", False)
            return
        if action == "path":
            self._notice = (str(catalog), False)
            return
        if action == "open":
            import webbrowser

            try:
                opened = webbrowser.open(catalog.resolve().as_uri(), new=2)
            except (OSError, ValueError) as exc:
                self.feedback(f"Не удалось открыть каталог: {exc}", error=True)
                return
            if opened:
                self.feedback("Каталог открыт в браузере.")
            else:
                self._notice = (
                    f"Браузер не найден. Открой файл вручную: {catalog}",
                    True,
                )

    def run(self) -> None:
        try:
            asyncio.run(ensure_daemon())
        except (TimeoutError, RuntimeError, OSError) as exc:
            self.print_content(
                Text(f"Не удалось запустить локальную службу: {exc}", style=ORANGE)
            )
            return
        while True:
            try:
                status = self.get_status()
                self.render_page(status, "главная", "Локальный архив Telegram")
                control_hint = {
                    "running": "работает — пауза или стоп",
                    "paused": "приостановлен — продолжить",
                    "stopped": "остановлен — запустить",
                }.get(status.get("collector_state"), "Управление сборщиком")
                telegram_hint = (
                    "сессия подключена — проверить"
                    if status.get("telegram") == "authorized"
                    else "подключить аккаунт"
                )
                choices = [
                    {
                        "name": "Источники",
                        "hint": "посты и расписание",
                        "value": "sources",
                    },
                    {
                        "name": "Загрузки",
                        "hint": "прогресс и последние файлы",
                        "value": "downloads",
                    },
                    {
                        "name": "HTML-каталог",
                        "hint": "открыть или обновить",
                        "value": "catalog",
                    },
                ]
                if (status.get("stats") or {}).get("sources", 0):
                    choices.extend([
                        {
                            "name": "Проверить сейчас",
                            "hint": "новые комментарии",
                            "value": "scan",
                        },
                        {
                            "name": "Дополнить данные",
                            "hint": "посты и аватарки",
                            "value": "enrich",
                        },
                        {"name": "Сборщик", "hint": control_hint, "value": "control"},
                    ])
                choices.extend([
                    {"name": "Telegram", "hint": telegram_hint, "value": "telegram"},
                    {"name": "Выход", "value": "exit"},
                ])
                action = self.select("Действие", choices)
                if action == "exit":
                    self.print_content(
                        Text(
                            "Интерфейс закрыт. Фоновая служба продолжает работу.",
                            style=DIM,
                        )
                    )
                    break
                if action == "sources":
                    self.sources_menu()
                elif action == "downloads":
                    self.render_page(
                        self.get_status(), "загрузки", "Прогресс и последние сохранённые файлы"
                    )
                    sub = self.select(
                        "Загрузки",
                        [
                            {
                                "name": "Живой прогресс",
                                "hint": "обновляется в реальном времени",
                                "value": "live",
                            },
                            {
                                "name": "Последние файлы",
                                "hint": "15 видеокружков",
                                "value": "recent",
                            },
                            {"name": "Назад", "value": "back"},
                        ],
                    )
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
                    self.catalog_menu(status)
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
                self.clear_view()
                self.print_content(
                    Text(
                        "Интерфейс закрыт. Фоновая служба продолжает работу.",
                        style=DIM,
                    )
                )
                break
            except (ConnectionError, RuntimeError, OSError, ValueError) as exc:
                self.clear_view()
                self.print_content(Text(f"Ошибка: {exc}", style=ORANGE))
                break


def launch() -> None:
    HoardCLI().run()
