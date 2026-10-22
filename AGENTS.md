# Hoard contributor notes

Local-first Telegram archive and extensible small-tool core, Python >=3.11. Keep a single daemon + user-owned IPC; no network service, Redis, Docker or Electron. The sole HTTP exception is the explicit, short-lived `hoard web` client: it binds only to `127.0.0.1`, proxies through daemon IPC and must never become a LAN or persistent service.

- The legacy `hoard` / `hoard ui` client uses `src/hoard/console_ui.py`: Rich + InquirerPy,
  black terminal background supplied by the terminal, accent #F79646. The optional `hoard tui`
  client is a full-screen Textual application with its own strictly black theme.
- Maintain existing XDG session/config/database locations and schema. Never commit sessions, API keys, downloaded media, local archives or real profile data.
- Preserve `hoard start`, `stop`, `scan`, `status`, `shutdown`, `add` and noninteractive output.
- The legacy Rich client must remain **inline**, with no alternate-screen, unnecessary panels or
  idle refresh. The Textual client may use full-screen application mode. Closing or cancelling
  either client must never kill the daemon.
- UI must call daemon through IPC; never instantiate `TelegramClient` in the UI process.
- Keep unimplemented integrations hidden from UI. Do not expand personal data gathering without explicit consent.
- Recheck Python syntax, tests, CLI, IPC and `pytest -q` after changes. Live Telegram tests require user's authorization and should be run against permitted content.
- Every completed and locally verified Hoard round must be published automatically to
  `origin/main`: build and inspect the wheel, audit staged files, commit, fetch, verify a
  fast-forward, push normally and verify the remote HEAD. Passing local syntax, lint, tests,
  wheel build and clean-install checks is sufficient to close a round. GitHub Actions is manual
  only and must not be started, restarted or inspected without the user's separate permission.
  Never push with failing local checks and never force-push. Report `PUSH BLOCKED` when access or
  history prevents safe publication.
