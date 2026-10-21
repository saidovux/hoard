"""Full-screen Textual interface for Hoard."""

from __future__ import annotations


def launch() -> None:
    """Start the full-screen UI without importing it from ordinary CLI commands."""
    from .app import HoardApp

    HoardApp().run()


__all__ = ["launch"]
