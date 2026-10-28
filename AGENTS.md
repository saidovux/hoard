# Hoard contributor notes

Local-first Telegram archive and extensible small-tool core, Python >=3.11. Keep a single daemon + user-owned IPC; no network service, Redis, Docker or Electron. The sole HTTP exception is the explicit, short-lived `hoard web` client: it binds only to `127.0.0.1`, proxies through daemon IPC and must never become a LAN or persistent service.

- The only interactive terminal client is the full-screen Textual app in `src/hoard/tui/` with its
  strictly black theme and accent #F79646. `hoard`, `hoard ui` and `hoard tui` all launch it;
  `hoard ui` and `hoard tui` are compatibility aliases. The legacy Rich/InquirerPy UI is gone.
- Keep the Noir terminal-native visual language: black surfaces, `#FFFFFF` text, `#888888` hints,
  `#7B4B23` frames at rest, `#262626` hairlines and `#F79646` focus/meaning. Pages are framed
  `Panel`s with titles in the border, shortcuts as key chips and orange block selection. `Home`,
  `Archive`, `Telegram` and `AI` remain the four global entries. Telegram replaces the
  global menu with its `Status`, `Sources`, bottom-pinned `Web Viewer` and `Login` workspace;
  `overview`/`activity`/`sources` remain navigation aliases, with `sources` opening the nested
  Sources page. New controls for existing features must fit this language instead of adding a new
  visual system or unmounted section.
- Home stays a global activity surface: the wordmark, one compact Telegram monitor and quiet
  queue/archive totals. Its three stable horizontal transfer slots are fed only by bounded
  `status_brief.progress.transfers`; four downloads may keep running while only three are shown.
  Home never opens SQLite or a Telegram client and retains the single 1.5-second polling loop.
- Telegram Status has two independent refresh lifecycles: current work uses bounded
  `status_brief` only while visible, while archive/FFmpeg/PSL aggregates use the read-only
  `telegram_statistics_get` IPC only on entry or explicit refresh. Preserve the last successful
  aggregate snapshot on errors; never make this page start Telegram, FFmpeg, Web or AI work.
- Telegram Sources uses the bounded, paginated `telegram_sources_overview` IPC rather than
  compatibility `status.sources`. Keep search/filter/sort in SQLite, preserve stable source-ID
  selection and do not add polling. Source edits use `source_update`, keep the post identity,
  enabled state, checkpoint and schedule, and never discard archive relations or media.
- The contextual `telegram_publication` route belongs to Telegram Sources but is not a fifth
  sidebar or palette entry. Enter opens it, `e` edits, and back restores the existing Sources
  screen state. Its four data tabs remain read-only, source-scoped and independently paginated;
  reuse `ai_telegram_profile_list` for profiles, never deep-check every frame row, and never
  attribute PSL by author membership alone. The separate operation panel may explicitly start
  source-validated selected/publication FFmpeg sessions and existing PSL preview/batches through
  IPC. Stable selections are capped at 100 and cleared on source changes; work polling exists only
  while this screen is visible. Media opening must use stable IDs through daemon validation of
  ownership, resolved archive-root containment, regular-file type and size, never a TUI path.
- `sources.collection_mode` defaults to `full`. `minimal` still records message identity,
  author/source relations and video notes, but must not resolve a missing sender or schedule a new
  automatic profile-photo job on that source's behalf. A shared author remains eligible through
  any linked full source; legacy profiles without links keep enrichment compatibility.
- Maintain existing XDG session/config/database locations and schema. Never commit sessions, API keys, downloaded media, local archives or real profile data.
- Preserve `hoard start`, `stop`, `scan`, `status`, `shutdown`, `add`, `retry`, `telegram` and
  `web` and their noninteractive/JSON output. Service commands must not import Textual.
- The Textual client may use full-screen application mode. Closing or cancelling it must never
  kill the daemon, stop background work or close an already running Web session.
- UI must call daemon through IPC; never instantiate `TelegramClient` in the UI process.
- Telegram Web is a separate loopback process managed from **Telegram → Web Viewer**. Its private
  `0600` state file carries a launch identity, PID, control port and control token. The browser
  app must never receive the control token or a stop route. Closing the TUI leaves Web running;
  only an explicit TUI stop, daemon shutdown or a crash ends a manually managed Web. Bootstrap
  tokens are short-lived, one-use and issued through the authenticated control channel.
- Web updates use the existing `catalog_dirty`/atomic HTML publication. Do not treat a database
  revision as published readiness and do not rebuild the catalog from the version-check endpoint.
- Video-note frame extraction is local and manual. A download may register durable waiting work,
  but daemon/TUI startup, download completion, restart and generic job retry must never authorize or
  start FFmpeg/FFprobe. Permission is process-local to one explicit selected or confirmed bulk action
  and is always revoked on restart. Keep the dedicated media runner at one FFmpeg process. Store
  frames under the archive next to the MP4, never in profile-photo rows, and never fabricate a third
  image from a duplicate frame. FFmpeg/FFprobe are optional runtime tools; their absence must not
  break installation, downloads, TUI, Web or photo-only AI.
- Frames enter AI only after an explicit user action. Send the three frames of one video in one
  request (and at most one same-author profile photo in combined mode); never mix authors, never send
  separate paid requests per frame, and keep preview fingerprints and operation-key idempotency.
- Existing MP4s are not mass-processed on upgrade. The explicit old-archive action must remain
  confirmed, resumable and bounded. It may authorize only free slots in the eight-job frame queue;
  never append a fixed-size batch past that limit. Advance the durable cursor only over scanned
  records and stop before unscanned work when capacity is exhausted. Stop is hard: revoke permission,
  cancel and reap the current FFmpeg/FFprobe process, remove unpublished temporary files, return
  unfinished jobs to waiting and preserve the cursor. Closing the TUI alone leaves an explicitly
  started daemon-side frame session running.
- Publication frame sessions must keep a process-local allowlist of their durable job IDs. Selected
  runs may contain at most 100 unique source-owned message IDs; publication runs fill only free
  slots in the existing eight-job budget and retain a source-scoped durable cursor. Neither may
  authorize a waiting frame job from another publication. Source-scoped retry handles only that
  publication's failed/stale sets.
- Telegram collection and FFmpeg are mutually exclusive at the service boundary. A frame action
  conflicts while Telegram is active; an explicit Telegram start has priority, first hard-stops and
  awaits frame processing, then starts the existing concurrent Telegram downloads. Protect both
  transitions against simultaneous IPC requests.
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
