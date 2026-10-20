# Hoard contributor notes

Local-first Telegram archive and extensible small-tool core, Python >=3.11. Keep a single daemon + user-owned IPC; no network service, Redis, Docker, Electron or web UI in current scope.

- Interactive CLI uses `src/hoard/console_ui.py`: Rich + InquirerPy, black terminal background supplied by terminal, accent #F79646.
- Maintain existing XDG session/config/database locations and schema. Never commit sessions, API keys, downloaded media, local archives or real profile data.
- Preserve `hoard start`, `stop`, `scan`, `status`, `shutdown`, `add` and noninteractive output.
- TUI must be **inline**, no alternate-screen, no unnecessary rich panels/tables, no idle refresh when closed. Prompt cancellation must never kill the daemon.
- UI must call daemon through IPC; never instantiate `TelegramClient` in the UI process.
- Keep unimplemented integrations hidden from UI. Do not expand personal data gathering without explicit consent.
- Recheck Python syntax, tests, CLI, IPC and `pytest -q` after changes. Live Telegram tests require user's authorization and should be run against permitted content.
