# Hoard 0.3.0 review notes

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
