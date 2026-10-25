# Hoard contributor notes

Local-first Telegram archive and extensible small-tool core, Python >=3.11. Keep a single daemon + user-owned IPC; no network service, Redis, Docker or Electron. The sole HTTP exception is the explicit, short-lived `hoard web` client: it binds only to `127.0.0.1`, proxies through daemon IPC and must never become a LAN or persistent service.

- The only interactive terminal client is the full-screen Textual app in `src/hoard/tui/` with its
  strictly black theme and accent #F79646. `hoard`, `hoard ui` and `hoard tui` all launch it;
  `hoard ui` and `hoard tui` are compatibility aliases. The legacy Rich/InquirerPy UI is gone.
- Maintain existing XDG session/config/database locations and schema. Never commit sessions, API keys, downloaded media, local archives or real profile data.
- Preserve `hoard start`, `stop`, `scan`, `status`, `shutdown`, `add`, `retry`, `telegram` and
  `web` and their noninteractive/JSON output. Service commands must not import Textual.
- The Textual client may use full-screen application mode. Closing or cancelling it must never
  kill the daemon, stop background work or close an already running Web session.
- UI must call daemon through IPC; never instantiate `TelegramClient` in the UI process.
- Telegram Web is a separate loopback process managed from **Telegram → Web**. Its private
  `0600` state file carries a launch identity, PID, control port and control token. The browser
  app must never receive the control token or a stop route. Closing the TUI leaves Web running;
  only an explicit TUI stop, daemon shutdown or a crash ends a manually managed Web. Bootstrap
  tokens are short-lived, one-use and issued through the authenticated control channel.
- Web updates use the existing `catalog_dirty`/atomic HTML publication. Do not treat a database
  revision as published readiness and do not rebuild the catalog from the version-check endpoint.
- Keep unimplemented integrations hidden from UI. Do not expand personal data gathering without explicit consent.
- Recheck Python syntax, tests, CLI, IPC and `pytest -q` after changes. Live Telegram tests require user's authorization and should be run against permitted content.
- Every completed and locally verified Hoard round must be published automatically to
  `origin/main`: build and inspect the wheel, audit staged files, commit, fetch, verify a
  fast-forward, push normally and verify the remote HEAD. Passing local syntax, lint, tests,
  wheel build and clean-install checks is sufficient to close a round. GitHub Actions is manual
  only and must not be started, restarted or inspected without the user's separate permission.
  Never push with failing local checks and never force-push. Report `PUSH BLOCKED` when access or
  history prevents safe publication.
