# Maintainer release checklist

Before publishing a Hoard release or completed implementation round:

1. Run `python -m compileall -q src tests`, `ruff check .` and `pytest -q`.
2. Exercise the affected interface in a Linux PTY at narrow and wide terminal sizes. For the
   Textual client, also run the headless size, theme, navigation and polling tests.
3. Test live Telegram login/download only with the user's authorization and permitted content.
4. Confirm the UI process never creates a Telethon client and closing it leaves the daemon alive.
5. Inspect Git's complete staged file list for sessions, configuration, SQLite, archives, logs,
   environment files and credentials.
6. Build a wheel, install it in a clean environment and verify packaged UI resources.
7. Never force-push or replace unexpected remote history.
8. After all checks pass, create the requested commit, run `git fetch origin`, verify the local
   commit is a fast-forward of `origin/main`, and automatically push to `origin/main`.
9. Verify `git ls-remote origin refs/heads/main` matches local HEAD. Successful local syntax,
   lint, tests, wheel inspection and clean-install smoke checks are sufficient to close a round.
   GitHub Actions is manual-only: do not start, restart or inspect it without separate user
   permission. If access or remote history blocks safe publication, report `PUSH BLOCKED`.
