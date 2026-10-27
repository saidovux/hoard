"""Compact Home monitor for the current Telegram scan and its active transfers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from rich.text import Text
from textual.events import Resize
from textual.widgets import Static

from ..colors import ACCENT, DANGER, GOOD, HAIRLINE, MUTED, TEXT, WARNING

TransferKey = tuple[int, int]
IndicatorState = Literal["green", "yellow", "red", "neutral"]
INDICATOR_COLORS: dict[IndicatorState, str] = {
    "green": GOOD,
    "yellow": WARNING,
    "red": DANGER,
    "neutral": MUTED,
}


@dataclass(frozen=True, slots=True)
class TransferSnapshot:
    key: TransferKey
    received: int
    total: int | None

    @property
    def fraction(self) -> float | None:
        if self.total is None or self.total <= 0:
            return None
        return max(0.0, min(1.0, self.received / self.total))


def telegram_indicator(status: dict[str, Any] | None, *, available: bool = True) -> IndicatorState:
    """Return only a confirmed collector/account state; never infer green from one flag."""
    if not available or status is None:
        return "neutral"
    telegram = str(status.get("telegram") or "")
    account = status.get("telegram_account") or {}
    confirmed = (
        telegram == "authorized"
        and account.get("connected") is True
        and account.get("verified") is True
        and str(account.get("state") or "") == "connected"
    )
    collector_state = str(status.get("collector_state") or "")
    running = status.get("running")
    if confirmed:
        if collector_state == "running" and running is True:
            return "green"
        if collector_state in {"paused", "stopped"} or running is False:
            return "yellow"
        return "neutral"
    if telegram in {
        "disconnected",
        "api_required",
        "phone_required",
        "code_required",
        "password_required",
    }:
        return "red"
    if account.get("verified") is False or str(account.get("state") or "") in {
        "saved",
        "login_required",
        "error",
    }:
        return "red"
    return "neutral"


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return max(0, number)


def transfer_snapshots(progress: dict[str, Any]) -> list[TransferSnapshot]:
    """Normalize bounded daemon telemetry without inventing missing totals."""
    raw_transfers = progress.get("transfers")
    if not isinstance(raw_transfers, list):
        return []
    snapshots: list[TransferSnapshot] = []
    seen: set[TransferKey] = set()
    for raw in raw_transfers[:4]:
        if not isinstance(raw, dict):
            continue
        source_id = _non_negative_int(raw.get("source_id"))
        message_id = _non_negative_int(raw.get("message_id"))
        received = _non_negative_int(raw.get("bytes"))
        total = _non_negative_int(raw.get("total"))
        if source_id is None or message_id is None or received is None:
            continue
        key = (source_id, message_id)
        if key in seen:
            continue
        seen.add(key)
        snapshots.append(
            TransferSnapshot(key=key, received=received, total=total if total else None)
        )
    return snapshots


def _shown_count(value: object) -> str:
    number = _non_negative_int(value)
    return "—" if number is None else f"{number:,}".replace(",", " ")


class TelegramMonitor(Static):
    """Render scan counters and one stable, three-segment transfer strip."""

    SLOT_COUNT = 3

    def __init__(self) -> None:
        super().__init__(id="telegram-monitor")
        self._status: dict[str, Any] | None = None
        self._available = True
        self._slots: list[TransferKey | None] = [None] * self.SLOT_COUNT
        self._render_key: object = None
        self._indicator_state: IndicatorState | None = None

    @property
    def slot_keys(self) -> tuple[TransferKey | None, ...]:
        """Expose immutable slot identity for deterministic UI tests."""
        return tuple(self._slots)

    def show_status(self, status: dict[str, Any], *, available: bool = True) -> None:
        self._status = status
        self._available = available
        self._paint()

    def on_resize(self, event: Resize) -> None:
        self._paint()

    def _collector_is_running(self) -> bool:
        status = self._status or {}
        return (
            self._available
            and status.get("running") is True
            and str(status.get("collector_state") or "") == "running"
        )

    def _sync_slots(self, transfers: list[TransferSnapshot]) -> dict[TransferKey, TransferSnapshot]:
        active = {transfer.key: transfer for transfer in transfers}
        for index, key in enumerate(self._slots):
            if key not in active:
                self._slots[index] = None
        assigned = {key for key in self._slots if key is not None}
        for transfer in transfers:
            if transfer.key in assigned:
                continue
            try:
                slot = self._slots.index(None)
            except ValueError:
                break
            self._slots[slot] = transfer.key
            assigned.add(transfer.key)
        return active

    def _segment_widths(self) -> tuple[list[int], str]:
        width = self.content_size.width or 54
        separator = " │ " if width >= 20 else "│"
        available = max(self.SLOT_COUNT, width - (self.SLOT_COUNT - 1) * len(separator))
        base, extra = divmod(available, self.SLOT_COUNT)
        return [base + (1 if index < extra else 0) for index in range(self.SLOT_COUNT)], separator

    @staticmethod
    def _segment(width: int, transfer: TransferSnapshot | None) -> Text:
        if transfer is None:
            return Text(" " * width, style=f"on {HAIRLINE}")
        fraction = transfer.fraction
        if fraction is None:
            segment = Text(" " * width, style=f"on {HAIRLINE}")
            if width:
                center = width // 2
                segment.plain = segment.plain[:center] + "•" + segment.plain[center + 1 :]
                segment.stylize(f"bold {ACCENT}", center, center + 1)
            return segment
        filled = max(0, min(width, round(width * fraction)))
        segment = Text()
        segment.append(" " * filled, style=f"on {ACCENT}")
        segment.append(" " * (width - filled), style=f"on {HAIRLINE}")
        return segment

    def _bar(self, active: dict[TransferKey, TransferSnapshot]) -> Text:
        widths, separator = self._segment_widths()
        line = Text(no_wrap=True, overflow="crop")
        for index, width in enumerate(widths):
            if index:
                line.append(separator, style=MUTED)
            key = self._slots[index]
            line.append_text(self._segment(width, active.get(key) if key is not None else None))
        return line

    def _set_title(self, indicator: IndicatorState) -> None:
        if self.parent is None or indicator == self._indicator_state:
            return
        self._indicator_state = indicator
        self.parent.border_title = f"Telegram [{INDICATOR_COLORS[indicator]}]●[/]"

    def _paint(self) -> None:
        status = self._status or {}
        progress = status.get("progress") or {}
        running = self._collector_is_running()
        transfers = transfer_snapshots(progress) if running else []
        active = self._sync_slots(transfers)
        indicator = telegram_indicator(self._status, available=self._available)
        self._set_title(indicator)
        compact = (self.content_size.width and self.content_size.width < 52) or (
            self.app.size.height and self.app.size.height < 22
        )
        render_key = (
            self._available,
            indicator,
            compact,
            self.content_size.width,
            progress.get("source"),
            progress.get("checked"),
            progress.get("downloaded"),
            progress.get("remaining"),
            progress.get("active_downloads"),
            tuple((item.key, item.received, item.total) for item in transfers),
            self.slot_keys,
        )
        if render_key == self._render_key:
            return
        self._render_key = render_key

        text = Text(no_wrap=True, overflow="ellipsis")
        if not self._available:
            text.append("Данные Telegram недоступны", style=MUTED)
            text.append("\n")
            text.append_text(self._bar({}))
            self.update(text)
            return

        source = str(progress.get("source") or "—")
        checked = _shown_count(progress.get("checked"))
        downloaded = _shown_count(progress.get("downloaded"))
        remaining = _shown_count(progress.get("remaining"))
        active_count = _non_negative_int(progress.get("active_downloads")) if running else 0
        active_count = len(transfers) if active_count is None else active_count
        if compact:
            text.append("Источник  ", style=MUTED)
            text.append(source, style=TEXT)
            text.append("\n")
            if self.content_size.width >= 40:
                activity = (
                    f"{checked} проверено · {downloaded} скачано · {active_count} загрузок"
                )
            else:
                activity = f"{checked} пров. · {downloaded} скач. · {active_count} загр."
            text.append(activity, style=TEXT)
        else:
            for label, value in (
                ("Источник", source),
                ("Проверено", f"{checked} сообщений" if checked != "—" else "—"),
                ("Скачано", f"{downloaded} новых кружков" if downloaded != "—" else "—"),
                ("Осталось", remaining),
                ("Активно", f"{active_count} загрузок"),
            ):
                if text:
                    text.append("\n")
                text.append(label.ljust(12), style=MUTED)
                text.append(value, style=TEXT)
        text.append("\n")
        text.append_text(self._bar(active))
        self.update(text)


__all__ = [
    "IndicatorState",
    "TelegramMonitor",
    "TransferSnapshot",
    "telegram_indicator",
    "transfer_snapshots",
]
