"""Framed panels: the basic building block of every page.

A panel is a rounded frame with its title set into the top border and, optionally, the shortcuts
that apply inside it set into the bottom one. At rest the frame is a quiet dim orange; as soon
as something inside it has focus the frame lights up, so the eye always knows where the keyboard
is without any extra chrome.
"""

from __future__ import annotations

from collections.abc import Iterable

from rich.text import Text
from textual.containers import Vertical
from textual.widget import Widget

from ..colors import ACCENT, MUTED
from .chrome import Hint


def hints_text(hints: Iterable[Hint]) -> Text:
    """``key label`` pairs for a frame border: the key in orange, the label in grey."""
    text = Text(no_wrap=True)
    for position, (key, label) in enumerate(hints):
        if position:
            text.append("   ")
        text.append(key, style=f"bold {ACCENT}")
        text.append(" " + label, style=MUTED)
    return text


class Panel(Vertical):
    """A titled frame around related widgets."""

    DEFAULT_CLASSES = "panel"

    def __init__(
        self,
        *children: Widget,
        title: str,
        hints: Iterable[Hint] = (),
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(*children, id=id, classes=classes)
        self.border_title = title
        self.set_hints(hints)

    def set_hints(self, hints: Iterable[Hint]) -> None:
        hints = tuple(hints)
        self.border_subtitle = hints_text(hints) if hints else None


__all__ = ["Panel", "hints_text"]
