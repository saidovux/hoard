"""Widgets that render the existing daemon status response."""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual.events import Resize
from textual.widgets import Static

from ..colors import ACCENT, HAIRLINE, MUTED, TEXT
from ..state import progress_fraction, progress_lines


@dataclass(frozen=True, slots=True)
class Tile:
    """One figure of the status response: where to find it, what to call it, whether it nags."""

    label: str
    group: str
    key: str
    #: Hot figures turn orange as soon as they are not zero.
    hot: bool = False


ARCHIVE_TILES = (
    Tile("источников", "stats", "sources"),
    Tile("профилей", "stats", "authors"),
    Tile("кружков", "stats", "downloads"),
)
QUEUE_TILES = (
    Tile("ожидают", "queue", "queued"),
    Tile("в работе", "queue", "running", hot=True),
    Tile("ошибки", "queue", "failed", hot=True),
)


def _number(raw: object, *, hot: bool = False) -> tuple[str, str]:
    shown = "—" if raw is None else str(raw)
    if shown in {"—", "0"}:
        return shown, MUTED
    return shown, f"bold {ACCENT}" if hot else f"bold {TEXT}"


class StatisticsView(Static):
    """A row of figures with their labels underneath; zeros fade out and rows wrap by width."""

    def __init__(self, tiles: tuple[Tile, ...] = ARCHIVE_TILES, *, id: str = "statistics") -> None:
        super().__init__(id=id)
        self.tiles = tiles
        self._status: dict | None = None
        self._painted_width = -1

    def show_status(self, status: dict) -> None:
        self._status = status
        self._paint()

    def on_resize(self, event: Resize) -> None:
        if self._status is not None and self.content_size.width != self._painted_width:
            self._paint()

    def _inline(self) -> bool:
        """On short terminals every figure sits on one line: ``4 ожидают   1 в работе``."""
        return self.app.size.height < 30 and self.app.size.height > 0

    def _tile_width(self) -> int:
        """The longest label plus a gap, tightened by one column when that lets every tile fit."""
        longest = max(len(tile.label) for tile in self.tiles)
        width = self.content_size.width
        if width > 0 and len(self.tiles) * (longest + 2) > width:
            return longest + 1
        return longest + 2

    def _columns(self) -> int:
        width = self.content_size.width
        if width <= 0:
            return len(self.tiles)
        return max(1, min(len(self.tiles), width // self._tile_width()))

    def _paint(self) -> None:
        status = self._status or {}
        columns = self._columns()
        tile_width = self._tile_width()
        self._painted_width = self.content_size.width
        figures = [
            (tile.label, *_number((status.get(tile.group) or {}).get(tile.key), hot=tile.hot))
            for tile in self.tiles
        ]
        text = Text(no_wrap=True, overflow="crop")
        if self._inline():
            for position, (label, number, style) in enumerate(figures):
                if position:
                    text.append("  ")
                text.append(number, style=style)
                text.append(" " + label, style=MUTED)
            if self.content_size.width <= 0 or text.cell_len <= self.content_size.width:
                self.update(text)
                return
            text = Text(no_wrap=True, overflow="crop")
        for start in range(0, len(figures), columns):
            chunk = figures[start : start + columns]
            if start:
                text.append("\n\n")
            for _label, number, style in chunk:
                text.append(number.ljust(tile_width), style=style)
            text.append("\n")
            for label, _number_text, _style in chunk:
                text.append(label.ljust(tile_width), style=MUTED)
        self.update(text)


class EventsView(Static):
    def __init__(self, *, limit: int = 8) -> None:
        super().__init__(id="events")
        self.limit = limit

    def show_status(self, status: dict) -> None:
        events = status.get("events")
        if not events:
            self.update(Text("Событий пока нет", style=MUTED))
            return
        text = Text(no_wrap=True, overflow="ellipsis")
        for position, event in enumerate(events[: self.limit]):
            if position:
                text.append("\n")
            failed = str(event.get("kind") or "info") == "error"
            created = str(event.get("created_at") or "")
            message = str(event.get("text") or "")
            text.append("! " if failed else "· ", style=f"bold {ACCENT}" if failed else MUTED)
            if created:
                text.append(f"{created}  ", style=MUTED)
            text.append(message or "—", style=ACCENT if failed else TEXT)
        self.update(text)


class ProgressView(Static):
    """What the current job reports, plus a block bar when it reports a size."""

    def __init__(self) -> None:
        super().__init__(id="progress")
        self._status: dict | None = None
        self._bar_width = 0

    def show_status(self, status: dict) -> None:
        self._status = status
        self._paint()

    def on_resize(self, event: Resize) -> None:
        if self._status is not None and self._width() != self._bar_width:
            self._paint()

    def _width(self) -> int:
        available = self.content_size.width
        return 28 if available <= 0 else max(10, min(56, available - 7))

    def _paint(self) -> None:
        status = self._status or {}
        text = Text(no_wrap=True, overflow="ellipsis")
        active = bool(status.get("progress") or {})
        self.display = active
        if active:
            text.append("\n".join(progress_lines(status)), style=TEXT)
        fraction = progress_fraction(status)
        if fraction is not None:
            current, total = fraction
            width = self._bar_width = self._width()
            filled = round(width * current / total)
            text.append("\n")
            text.append(" " * filled, style=f"on {ACCENT}")
            text.append(" " * (width - filled), style=f"on {HAIRLINE}")
            text.append(f"  {current * 100 // total}%", style=f"bold {ACCENT}")
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
