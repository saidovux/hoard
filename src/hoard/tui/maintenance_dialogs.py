"""Read-only detail dialogs for Telegram maintenance catalogs."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Static

from .colors import MUTED, TEXT
from .state import format_bytes


def detail_text(rows: tuple[tuple[str, object], ...]) -> Text:
    text = Text()
    for index, (label, value) in enumerate(rows):
        if index:
            text.append("\n")
        text.append(label.ljust(18), style=MUTED)
        text.append(str(value if value not in {None, ""} else "—"), style=TEXT)
    return text


class MaintenanceDetailDialog(ModalScreen[None]):
    BINDINGS = [Binding("escape", "close", "Закрыть")]

    def __init__(self, title: str, content: Text) -> None:
        super().__init__()
        self.title_text = title
        self.content = content

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog maintenance-detail-dialog"):
            yield Static(self.title_text, classes="dialog-title")
            yield Static(self.content, id="maintenance-detail-content")
            yield Button("Закрыть", id="maintenance-detail-close", variant="primary")

    def action_close(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "maintenance-detail-close":
            self.action_close()


class ProfileDetailDialog(ModalScreen[dict[str, Any] | None]):
    """Show local identity, photos, source relations and saved PSL references."""

    BINDINGS = [Binding("escape", "close", "Закрыть")]

    def __init__(self, profile: dict[str, Any], analyses: list[dict[str, Any]]) -> None:
        super().__init__()
        self.profile = profile
        self.analyses = analyses
        self.photos = list(profile.get("photos") or [])

    def compose(self) -> ComposeResult:
        history = ", ".join(
            "@" + str(item.get("username") or "").lstrip("@")
            for item in self.profile.get("username_history") or []
        ) or "—"
        sources = ", ".join(
            f"@{item.get('username') or '—'}/{item.get('post_id') or '—'}"
            for item in self.profile.get("sources") or []
        ) or "legacy / без связи"
        summary = detail_text(
            (
                ("Telegram ID", self.profile.get("author_id")),
                ("Имя", self.profile.get("display_name")),
                ("Username", self.profile.get("current_username")),
                ("История", history),
                ("Первое наблюдение", self.profile.get("first_seen")),
                ("Последнее", self.profile.get("last_seen")),
                ("Публикации", sources),
                ("Кружки", self.profile.get("video_count", 0)),
                ("Наборы кадров", self.profile.get("frame_set_count", 0)),
            )
        )
        with Vertical(classes="dialog profile-detail-dialog"):
            yield Static("Локальный Telegram-профиль", classes="dialog-title")
            yield Static(summary, id="profile-detail-summary")
            yield Static("Сохранённые фотографии", classes="section-heading")
            yield DataTable(id="profile-detail-photos", cursor_type="row")
            yield Static(id="profile-detail-analyses")
            with Horizontal(classes="dialog-actions"):
                yield Button("Открыть фото", id="profile-detail-open", variant="primary")
                yield Button("Закрыть", id="profile-detail-close")

    def on_mount(self) -> None:
        table = self.query_one("#profile-detail-photos", DataTable)
        for column in ("Photo ID", "Дата", "Размер", "Файл"):
            table.add_column(column)
        for photo in self.photos:
            table.add_row(
                str(photo.get("photo_id") or "—"),
                str(photo.get("photo_date") or "—")[:16],
                format_bytes(photo.get("size")),
                "доступен" if photo.get("available") else "недоступен",
            )
        if not self.photos:
            table.add_row("—", "—", "—", "Фотографий нет")
        analysis_lines = []
        for item in self.analyses[:20]:
            state = str(item.get("attempt_state") or item.get("result_status") or "—")
            analysis_lines.append(
                f"{str(item.get('id') or '—')[:12]} · {item.get('image_mode') or 'photos'} · "
                f"{state} · {str(item.get('created_at') or '—')[:16]}"
            )
        self.query_one("#profile-detail-analyses", Static).update(
            "Сохранённые PSL\n" + ("\n".join(analysis_lines) if analysis_lines else "—")
        )
        self.query_one("#profile-detail-open", Button).disabled = not bool(self.photos)

    def action_close(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "profile-detail-close":
            self.action_close()
        elif event.button.id == "profile-detail-open":
            row = self.query_one("#profile-detail-photos", DataTable).cursor_row
            if 0 <= row < len(self.photos):
                photo = self.photos[row]
                if photo.get("available"):
                    self.dismiss({"photo_id": str(photo.get("photo_id") or "")})
