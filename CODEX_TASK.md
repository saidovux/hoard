# Maintainer release checklist

Before publishing a Hoard release:

1. Run `python -m compileall -q src tests`, `ruff check .` and `pytest -q`.
2. Exercise the inline interface in a Linux PTY at narrow and wide terminal sizes.
3. Test live Telegram login/download only with the user's authorization and permitted content.
4. Confirm the UI process never creates a Telethon client and closing it leaves the daemon alive.
5. Inspect Git's complete staged file list for sessions, configuration, SQLite, archives, logs,
   environment files and credentials.
6. Never force-push or replace unexpected remote history.
