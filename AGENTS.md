# Hoard contributor notes

Local-first Telegram archive and extensible small-tool core, Python >=3.11. Keep a single daemon + user-owned IPC; no network service, Redis, Docker or Electron. The sole HTTP exception is the explicit, short-lived `hoard web` client: it binds only to `127.0.0.1`, proxies through daemon IPC and must never become a LAN or persistent service.

- The only interactive terminal client is the full-screen Textual app in `src/hoard/tui/` with its
  strictly black theme and accent #F79646. `hoard`, `hoard ui` and `hoard tui` all launch it;
  `hoard ui` and `hoard tui` are compatibility aliases. The legacy Rich/InquirerPy UI is gone.
- Keep the Noir terminal-native visual language: black surfaces, `#FFFFFF` text, `#888888` hints,
  `#7B4B23` frames at rest, `#262626` hairlines and `#F79646` focus/meaning. Pages are framed
  `Panel`s with titles in the border, shortcuts as key chips and orange block selection. `Home`,
  `Archive`, `Telegram` and `AI` are the four registry pages; `overview`/`activity`/`sources`
  remain navigation aliases. New controls for existing features must fit this language instead of
  adding a new visual system or unmounted section.
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
- Video-note frame extraction is local and automatic after a successful download, but never on the
  Telegram event loop. Keep the dedicated media runner at one FFmpeg process unless measured evidence
  justifies more. Store frames under the archive next to the MP4, never in profile-photo rows, and
  never fabricate a third image from a duplicate frame. FFmpeg/FFprobe are optional runtime tools;
  their absence must not break installation, downloads, TUI, Web or photo-only AI.
- Frames enter AI only after an explicit user action. Send the three frames of one video in one
  request (and at most one same-author profile photo in combined mode); never mix authors, never send
  separate paid requests per frame, and keep preview fingerprints and operation-key idempotency.
- Existing MP4s are not mass-processed on upgrade. The explicit old-archive action must remain
  resumable, bounded and independent of new Telegram jobs. It may fill only free slots in the
  eight-job frame queue; never append a fixed-size batch past that limit. Advance the durable
  cursor only over scanned records and stop before unscanned work when capacity is exhausted.
  Its pause is soft: stop scheduling new backfill jobs, but let the current FFmpeg process and
  already queued jobs finish.
- A publication-scoped Telegram AI analysis must pass that `source_id` into frame selection during
  preview, queueing and durable batch resume. A profile-only analysis may choose frames from any
  of that profile's saved sources. Never fall back to a newer video from another publication.
- Keep unimplemented integrations hidden from UI. Do not expand personal data gathering without explicit consent.
- Recheck Python syntax, tests, CLI, IPC and `pytest -q` after changes. Live Telegram tests require user's authorization and should be run against permitted content.
- Every completed and locally verified Hoard round must be published automatically to
  `origin/main`: build and inspect the wheel, audit staged files, commit, fetch, verify a
  fast-forward, push normally and verify the remote HEAD. Passing local syntax, lint, tests,
  wheel build and clean-install checks is sufficient to close a round. GitHub Actions is manual
  only and must not be started, restarted or inspected without the user's separate permission.
  Never push with failing local checks and never force-push. Report `PUSH BLOCKED` when access or
  history prevents safe publication.
