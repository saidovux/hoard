"""The Home page masthead: the program name in block letters, with the system state beside it.

Two sizes of the same wordmark are kept and the stylesheet picks one by terminal size (see the
``hero-*`` rules in ``theme.tcss``): a six-row block logo for roomy terminals and a three-row
one for everything smaller. Below 22 rows neither is shown; the header already carries the name.
"""

from __future__ import annotations

from itertools import groupby

from rich.text import Text
from textual.events import Resize
from textual.widgets import Static

from hoard import __version__

from ..colors import ACCENT, FRAME, MUTED, TEXT
from ..state import collector_label

# "ANSI Shadow" H-O-A-R-D. The solid blocks are the letters, the line characters their shadow.
LOGO_BIG = (
    "██╗  ██╗ ██████╗  █████╗ ██████╗ ██████╗ ",
    "██║  ██║██╔═══██╗██╔══██╗██╔══██╗██╔══██╗",
    "███████║██║   ██║███████║██████╔╝██║  ██║",
    "██╔══██║██║   ██║██╔══██║██╔══██╗██║  ██║",
    "██║  ██║╚██████╔╝██║  ██║██║  ██║██████╔╝",
    "╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝╚═════╝ ",
)

# "Calvin S" H-O-A-R-D: the same name in three rows of double-line box drawing.
LOGO_SMALL = (
    "╦ ╦╔═╗╔═╗╦═╗╔╦╗",
    "╠═╣║ ║╠═╣╠╦╝ ║║",
    "╩ ╩╚═╝╩ ╩╩╚══╩╝",
)

#: Blank columns between the wordmark and the text beside it.
GAP = 4
TAGLINE = "локальный архив"
SUBLINE = "всё хранится на этом компьютере"


def logo_text(rows: tuple[str, ...], *, shadow: bool) -> list[Text]:
    """Colour a wordmark: solid blocks in the accent, the shadow in its dim shade.

    A solid block is painted as a background-coloured space rather than the ``█`` glyph, so the
    letters stay seamless in terminals that add line spacing or lack that glyph.
    """
    lines: list[Text] = []
    for row in rows:
        line = Text(no_wrap=True)
        for solid, chunk in groupby(row, key=lambda char: char == "█" or not shadow):
            run = "".join(chunk)
            if shadow and solid:
                line.append(" " * len(run), style=f"on {ACCENT}")
            else:
                line.append(run, style=f"bold {ACCENT}" if solid else FRAME)
        lines.append(line)
    return lines


class Hero(Static):
    """One wordmark variant plus the lines of system state next to it."""

    def __init__(self, variant: str) -> None:
        super().__init__(id=f"hero-{variant}", classes=f"hero hero-{variant}")
        self.big = variant == "big"
        self._status: dict | None = None
        self._painted_width = -1

    def on_mount(self) -> None:
        self._paint()

    def show_status(self, status: dict | None) -> None:
        self._status = status
        self._paint()

    def on_resize(self, event: Resize) -> None:
        if self.content_size.width != self._painted_width:
            self._paint()

    def _info_variants(self) -> list[list[Text]]:
        """Text columns from richest to leanest; the first that fits next to the logo wins."""
        title = Text(no_wrap=True)
        title.append(TAGLINE, style=f"bold {TEXT}")
        title.append(f"   v{__version__}", style=MUTED)
        subline = Text(SUBLINE, style=MUTED, no_wrap=True)
        if self._status is None:
            return [[title, subline], [title]]
        label = collector_label(self._status)
        running = label == "работает"
        state = Text(no_wrap=True)
        state.append("● " if running else "○ ", style=ACCENT if running else MUTED)
        state.append(f"сборщик {label}", style=TEXT if running else MUTED)
        if self.big:
            return [[title, subline, Text(), state], [title, Text(), state], [title]]
        return [[title, subline, state], [title, state], [title]]

    def _paint(self) -> None:
        rows = LOGO_BIG if self.big else LOGO_SMALL
        art = logo_text(rows, shadow=self.big)
        art_width = max(len(row) for row in rows)
        width = self._painted_width = self.content_size.width
        room = width - art_width - GAP if width > 0 else 10_000
        info: list[Text] = []
        for variant in self._info_variants():
            if max(line.cell_len for line in variant) <= room:
                info = variant
                break
        # The text hangs from the second row of a tall wordmark and from the first of a short one.
        first = 1 if self.big else 0
        out = Text(no_wrap=True, overflow="crop")
        for index, line in enumerate(art):
            if index:
                out.append("\n")
            out.append_text(line)
            position = index - first
            if 0 <= position < len(info):
                out.append(" " * (art_width - len(rows[index]) + GAP))
                out.append_text(info[position])
        self.update(out)


__all__ = ["GAP", "LOGO_BIG", "LOGO_SMALL", "Hero", "logo_text"]
