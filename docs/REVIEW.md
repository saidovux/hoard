# Hoard review notes

## 0.7.4.5 Archive retirement gate

Round 7 removes the old global `ArchiveScreen` only after moving every reachable workflow to an
owned contextual route. The global navigation is now exactly Home / Telegram / AI; the legacy
`archive` key remains a route alias for **Telegram → Status → Maintenance**, not a sidebar item.

| Former Archive scenario | Current owner | Bounded daemon contract | Regression coverage |
| --- | --- | --- | --- |
| Downloaded video-note list and details | Maintenance → Media | `telegram_media_list/get/open` with opaque stable IDs | Round 7 backend media pagination/validation and TUI media viewer tests |
| Global and legacy/unlinked profiles | Maintenance → Profiles | paginated `ai_telegram_profile_list`, existing saved-photo preview contracts | global multi-profile TUI test and backend legacy-profile test |
| Job list, details and safe retry | Maintenance → Jobs | `jobs_list`, `job_get`, `job_retry`; paid AI is never generically retried | Round 7 jobs pagination/idempotency tests and explicit TUI retry test |
| Build and open offline catalog | Maintenance → Operations | `telegram_report` and validated `catalog_open` | explicit-operation TUI test and catalog security suites |
| Profile/publication enrichment | Maintenance → Operations | existing explicit `telegram_enrich` mutation | confirmation/no-automatic-work TUI test |
| Selected/publication frame extraction | Telegram Sources → Publication | source-owned selected/publication FFmpeg IPC | Round 6 publication and manual-frame policy suites |
| Whole-archive frame backfill, retry and hard stop | Maintenance → Operations | bounded backfill/retry/status and session-ID/scope-checked stop | Round 7 stop-race test and manual-frame policy suites |
| Single and multi-profile PSL | Maintenance → Profiles or Publication | existing preview/start APIs with saved-photo/frame validation | profile selector and publication PSL TUI suites |
| Active and historical AI groups | AI, Publication or Maintenance → batch history | paginated `ai_telegram_batch_catalog/get` and participant pages | source-scoped batch-history TUI test and aggregate-status backend test |

No separate export workflow existed in `ArchiveScreen`; the autonomous HTML catalog remains the
portable export. The equivalence gate also verifies stable selection, empty/error states, short
terminal layouts, contextual back navigation and absence of automatic Telegram, FFmpeg, Web or
paid-AI work. `src/hoard/tui/screens/archive.py` and all imports were deleted only after these routes
and tests were in place.

The frame-session summary now distinguishes `completed` (all terminal outcomes) from `successful`
and `errors`. A stop request carries the session identity and optional publication scope; the daemon
checks both under the transition lock, so a stale publication cannot stop a replacement or global
session. AI group history returns the complete paginated catalog, while PSL result fields are
displayed only for completed, eligible adult results.

The final local synthetic SQLite check used compact page size 50 (batch size 20) and measured one
warm-process query plus IPC encoding. Results were: media 10 — 2.34 ms / 3,164 bytes; media 1,000 —
13.66 ms / 15,608 bytes; media 10,000 — 73.30 ms / 15,610 bytes; profiles 1,000 — 31.61 ms /
11,665 bytes; jobs 5,000 — 10.87 ms / 8,927 bytes; AI batches 2,000 with 10,000 participant rows —
31.79 ms / 5,016 bytes. The temporary database contained synthetic metadata only; the media list
regression separately forbids filesystem `stat()` calls while paging.

## 0.3.0 review notes

## UX and reliability changes

- Telegram login is one daemon-owned flow instead of separate API/phone/code/2FA menu items.
- Existing sessions are checked first; pending code requests are reused rather than resent.
- Expected invalid/expired code, 2FA, flood-limit and network outcomes have actionable states.
- Adding a post verifies access before asking for storage settings, uses the default interval
  without an extra numeric prompt, and can start an immediate scan in the same flow.
- Source summaries include title, post, enabled state, download count, last check/result and
  last error.
- Collector menus are state-aware and distinguish paused from stopped work.
- Download progress uses only real telemetry and includes queue and error state.
- Existing SQLite rows are retained through an additive migration.

## Verification boundary

Automated tests use fake Telethon clients for saved-session, code, 2FA, retry, expiry,
cancellation, scan, download, deduplication and recovery behavior. They do not prove that a
particular Telegram account can access a particular post. Live MTProto login and real-media
checks must be performed locally with the user's own authorization and permitted content; no
credentials or live codes belong in the test suite or repository.
