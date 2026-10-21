"""Formatting of daemon status for the TUI, without synthetic telemetry."""

from __future__ import annotations

from typing import Any


def value(data: dict, key: str) -> str:
    item = data.get(key)
    return "—" if item is None else str(item)


def format_bytes(number: Any) -> str:
    if number is None:
        return "—"
    try:
        amount = float(number)
    except (TypeError, ValueError):
        return "—"
    for suffix in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(amount) < 1024 or suffix == "TiB":
            return f"{amount:.0f} {suffix}" if suffix == "B" else f"{amount:.1f} {suffix}"
        amount /= 1024
    return "—"


def collector_label(status: dict) -> str:
    state = status.get("collector_state")
    if state == "paused":
        return "пауза"
    if state == "running" or status.get("running"):
        return "работает"
    if state == "stopped" or status.get("running") is False:
        return "остановлен"
    return "состояние неизвестно"


def telegram_label(status: dict) -> str:
    state = status.get("telegram")
    if state is None:
        return "данные недоступны"
    labels = {
        "authorized": "подключён",
        "disconnected": "отключён",
        "api_required": "нужны API credentials",
        "phone_required": "нужен номер телефона",
        "code_required": "нужен код",
        "password_required": "нужен пароль 2FA",
    }
    return labels.get(str(state), str(state).replace("_", " "))


def progress_lines(status: dict) -> list[str]:
    """Describe only telemetry actually present in the daemon response."""
    progress = status.get("progress") or {}
    if not progress:
        return ["Нет активной работы"]

    lines: list[str] = []
    source = progress.get("source")
    if source:
        lines.append(f"Источник: {source}")
    checked = progress.get("checked")
    downloaded = progress.get("downloaded")
    if checked is not None or downloaded is not None:
        parts = []
        if checked is not None:
            parts.append(f"проверено {checked}")
        if downloaded is not None:
            parts.append(f"сохранено {downloaded}")
        lines.append(" · ".join(parts))

    filename = progress.get("file")
    current = progress.get("bytes")
    total = progress.get("total")
    speed = progress.get("speed_bps")
    if filename:
        lines.append(str(filename))
    if current is not None:
        amount = format_bytes(current)
        if total:
            amount += f" / {format_bytes(total)}"
        if speed:
            amount += f" · {format_bytes(speed)}/s"
        lines.append(amount)
    return lines or ["Выполняется фоновая операция"]


def progress_fraction(status: dict) -> tuple[int, int] | None:
    progress = status.get("progress") or {}
    current, total = progress.get("bytes"), progress.get("total")
    try:
        current_int, total_int = int(current), int(total)
    except (TypeError, ValueError):
        return None
    if total_int <= 0:
        return None
    return max(0, min(current_int, total_int)), total_int
