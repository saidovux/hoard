"""Widgets that render the existing daemon status response."""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from ..state import progress_fraction, progress_lines, value


class StatisticsView(Static):
    def __init__(self) -> None:
        super().__init__(id="statistics")

    def show_status(self, status: dict) -> None:
        stats = status.get("stats") or {}
        queue = status.get("queue") or {}
        text = Text()
        text.append("АРХИВ\n", style="bold #F79646")
        text.append(f"Источники  {value(stats, 'sources')}\n", style="#FFFFFF")
        text.append(f"Профили    {value(stats, 'authors')}\n", style="#FFFFFF")
        text.append(f"Кружки     {value(stats, 'downloads')}\n\n", style="#FFFFFF")
        text.append("ОЧЕРЕДЬ\n", style="bold #F79646")
        text.append(f"Ожидают       {value(queue, 'queued')}\n", style="#FFFFFF")
        text.append(f"Выполняются   {value(queue, 'running')}\n", style="#FFFFFF")
        text.append(f"Ошибки        {value(queue, 'failed')}", style="#FFFFFF")
        self.update(text)


class EventsView(Static):
    def __init__(self, *, limit: int = 8) -> None:
        super().__init__(id="events")
        self.limit = limit

    def show_status(self, status: dict) -> None:
        events = status.get("events")
        if not events:
            self.update(Text("Событий пока нет", style="#888888"))
            return
        text = Text()
        for event in events[: self.limit]:
            kind = str(event.get("kind") or "info")
            marker = "!" if kind == "error" else "·"
            marker_style = "bold #F79646" if kind == "error" else "#888888"
            created = str(event.get("created_at") or "")
            message = str(event.get("text") or "")
            text.append(f"{marker} ", style=marker_style)
            if created:
                text.append(f"{created}  ", style="#888888")
            text.append(message or "—", style="#FFFFFF")
            text.append("\n")
        self.update(text)


class ProgressView(Static):
    def __init__(self) -> None:
        super().__init__(id="progress")

    def show_status(self, status: dict) -> None:
        text = Text()
        for line in progress_lines(status):
            text.append(line + "\n", style="#FFFFFF")
        fraction = progress_fraction(status)
        if fraction is not None:
            current, total = fraction
            width = 28
            filled = round(width * current / total)
            text.append("█" * filled, style="#F79646")
            text.append("─" * (width - filled), style="#262626")
            text.append(f"  {current * 100 // total}%", style="#F79646")
        self.update(text)


def current_action(status: dict) -> str:
    progress = status.get("progress") or {}
    if progress.get("file"):
        return f"Скачивание: {progress['file']}"
    if progress.get("source"):
        return f"Проверка: {progress['source']}"
    queue = status.get("queue") or {}
    if queue.get("running"):
        return "Выполняется задание"
    return "Нет активной работы"
