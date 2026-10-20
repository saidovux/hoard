# Architecture

Hoard is one local Python application split into an on-demand terminal client and one
background service.

## Process boundary

- `console_ui.py` renders the inline Rich/InquirerPy interface. It never imports or creates
  a `TelegramClient`; every operation goes through IPC.
- `ipc.py` starts the daemon detached when required and sends one JSON request per private
  Unix-socket connection.
- `service.py` owns the database, job runner and the sole `TelegramEngine`. An asyncio lock
  serializes login, validation and scans so one Telethon session is never opened by competing
  clients.
- The socket and all XDG state directories are user-only. There is no TCP/HTTP listener.

Closing or cancelling the interface does not signal the service. Collector pause/stop cancels
active jobs through `JobRunner`, which returns interrupted jobs to `queued`; the daemon stays
available for management. Shutdown closes the runner, Telegram client, database and socket.

## Telegram authorization

The daemon exposes an explicit state machine:

```text
api_required → phone_required → code_required → password_required → authorized
                         ↘──────────────────────────────→ authorized
```

`auth_begin` always checks an existing session first. The phone-code hash stays only in daemon
memory, and repeated requests for the same pending code are deduplicated. Expected Telethon
errors are converted to structured `state`, `error_code`, `retryable` responses; secret request
arguments are never recorded in events or logs. `auth_cancel` clears only a pending challenge,
not credentials or an authorized session.

After login, scans reuse the connected client immediately. If the process is restarted during
login, the transient challenge is intentionally lost and a new code must be requested.

## Persistence and scheduling

`storage.py` owns a synchronous SQLite connection inside the daemon event loop. The schema
keeps sources, downloads, durable jobs and a bounded event history. Version 0.3 adds source
title/last-check metadata with additive `ALTER TABLE` operations; existing rows and downloads
are preserved.

Enabled sources become unique `telegram.scan` jobs when due. Telegram scans are serialized,
while the generic queue retains extension points for dormant adapters. A source checkpoint is
advanced after each processed comment. Media records are committed only after an MP4 is fully
written, fsynced, atomically renamed and accompanied by its JSON sidecar. Missing or size-
mismatched files move the scan cursor back to the earliest affected message.

## Progress and errors

Telethon's progress callback supplies current and total bytes. Hoard measures transfer speed
from callback deltas and exposes it over IPC together with queue counts. The UI shows a percent
only when the total is non-zero and never invents an ETA. Routine Telegram failures are stored
as actionable messages; exception class details are returned only when local `HOARD_DEBUG=1`
is explicitly enabled.

## Current scope

The interactive product is the Telegram video-note archiver. Optional media/rclone adapters
remain dormant code paths and are not presented as user-facing tools. Browser, AI, web UI,
multi-user infrastructure and additional personal-data collection are outside the current
scope.
