"""Input validation and safe names used by download storage."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

_USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


@dataclass(frozen=True)
class Post:
    username: str
    message_id: int

    @property
    def link(self) -> str:
        return f"https://t.me/{self.username}/{self.message_id}"

    @property
    def name(self) -> str:
        return f"{self.username}/{self.message_id}"


def parse_post_url(value: str) -> Post:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"https", "http"} or parsed.netloc.lower() not in {
        "t.me", "www.t.me", "telegram.me", "www.telegram.me",
    }:
        raise ValueError("Нужна публичная ссылка вида https://t.me/channel/123")
    if parsed.fragment:
        raise ValueError("Ссылка не должна содержать #фрагмент")
    parts = parsed.path.strip("/").split("/")
    if len(parts) != 2 or not _USERNAME.fullmatch(parts[0]) or not parts[1].isdigit():
        raise ValueError("Нужна публичная ссылка вида https://t.me/channel/123")
    msg_id = int(parts[1])
    if msg_id <= 0:
        raise ValueError("ID сообщения должен быть положительным")
    return Post(parts[0].lower(), msg_id)


def post_comment_link(post: Post, comment_id: int) -> str:
    # Telegram may require membership in the linked discussion group to open it.
    return f"{post.link}?comment={comment_id}"


def iso_date(value: datetime | None) -> str:
    return value.isoformat() if value else ""
