"""Command dispatcher behavior without a daemon, TTY, or Textual import."""

from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

import hoard.cli as cli
import hoard.tui


def test_default_ui_and_aliases_launch_textual(monkeypatch):
    launched: list[str] = []
    monkeypatch.setattr(hoard.tui, "launch", lambda: launched.append("textual"))
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(cli.sys, "stdout", SimpleNamespace(isatty=lambda: True))

    for argv in (["hoard"], ["hoard", "ui"], ["hoard", "tui"]):
        monkeypatch.setattr(cli.sys, "argv", argv)
        cli.main()

    assert launched == ["textual", "textual", "textual"]


def test_default_ui_without_tty_is_actionable(monkeypatch, capsys):
    launched: list[str] = []
    monkeypatch.setattr(hoard.tui, "launch", lambda: launched.append("textual"))
    monkeypatch.setattr(cli.sys, "argv", ["hoard"])
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(cli.sys, "stdout", SimpleNamespace(isatty=lambda: True))

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    assert "TUI" in capsys.readouterr().err
    assert launched == []


def test_help_does_not_import_textual_or_start_daemon():
    script = """
import sys
import hoard.cli as cli

started = []
async def fake_start():
    started.append(True)

cli.ensure_daemon = fake_start
sys.argv = ["hoard", "--help"]
try:
    cli.main()
except SystemExit as exc:
    print("CODE", exc.code)
print("TEXTUAL", any(
    name == "textual" or name.startswith("textual.") for name in sys.modules
))
print("STARTED", bool(started))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "CODE 0" in result.stdout
    assert "TEXTUAL False" in result.stdout
    assert "STARTED False" in result.stdout


def test_parent_and_child_json_flags_keep_status_contract(monkeypatch, capsys):
    async def fake_request(method, args=None, timeout=12):
        assert method == "status"
        return {
            "version": "0.7.1.10",
            "protocol": 1,
            "running": False,
        }

    monkeypatch.setattr(cli, "request", fake_request)
    monkeypatch.setattr(cli, "validate_daemon_status", lambda _status: None)

    for argv in (["hoard", "--json", "status"], ["hoard", "status", "--json"]):
        monkeypatch.setattr(cli.sys, "argv", argv)
        cli.main()
        value = json.loads(capsys.readouterr().out)
        assert value["running"] is False
        assert value["version"] == "0.7.1.10"
