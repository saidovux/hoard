"""Telegram-specific SQLite schema and non-destructive legacy migration."""
from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .archive_paths import default_archive_root, path_is_within
from .models import parse_post_url

TELEGRAM_SCHEMA_VERSION = 3

MEDIA_RETRY_DELAYS = (60, 300, 1800, 7200, 21600, 86400)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class TelegramStorage:
    """Telegram data layer sharing Hoard's existing SQLite connection."""

    def __init__(self, db, *, migrate_legacy: bool = True):
        self.db = db
        self._migrate_schema()
        if migrate_legacy:
            self.migrate_legacy_downloads()
        self._adopt_legacy_archive_root()

    def _migrate_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_versions (
                module TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS tg_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tg_archive_roots (
                path TEXT PRIMARY KEY,
                first_used_at TEXT NOT NULL DEFAULT (datetime('now')),
                last_used_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS tg_profiles (
                author_id INTEGER PRIMARY KEY,
                current_username TEXT NOT NULL DEFAULT '',
                display_name TEXT NOT NULL DEFAULT '',
                first_seen TEXT NOT NULL DEFAULT '',
                last_seen TEXT NOT NULL DEFAULT '',
                photos_checked_at TEXT NOT NULL DEFAULT '',
                photo_error TEXT NOT NULL DEFAULT '',
                photos_retry_at REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS tg_username_history (
                author_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                first_seen TEXT NOT NULL DEFAULT '',
                last_seen TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(author_id, username)
            );
            CREATE TABLE IF NOT EXISTS tg_profile_sources (
                author_id INTEGER NOT NULL,
                source_id INTEGER NOT NULL,
                first_seen TEXT NOT NULL DEFAULT '',
                last_seen TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(author_id, source_id)
            );
            CREATE TABLE IF NOT EXISTS tg_profile_photos (
                author_id INTEGER NOT NULL,
                photo_id TEXT NOT NULL,
                file_path TEXT NOT NULL,
                size INTEGER NOT NULL DEFAULT 0,
                photo_date TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'saved',
                error TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(author_id, photo_id)
            );
            CREATE TABLE IF NOT EXISTS tg_post_metadata (
                source_id INTEGER PRIMARY KEY,
                channel_title TEXT NOT NULL DEFAULT '',
                post_text TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL DEFAULT '',
                fetched_at TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'partial',
                error TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS tg_post_media (
                source_id INTEGER NOT NULL,
                media_id TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'file',
                file_path TEXT NOT NULL,
                size INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'saved',
                error TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(source_id, media_id)
            );
            CREATE TABLE IF NOT EXISTS tg_media_failures (
                source_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                retry_at REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(source_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS tg_profiles_username_idx
                ON tg_profiles(current_username COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS tg_profiles_name_idx
                ON tg_profiles(display_name COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS tg_profiles_seen_idx
                ON tg_profiles(last_seen);
            CREATE INDEX IF NOT EXISTS tg_username_history_username_idx
                ON tg_username_history(username COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS tg_profile_sources_source_idx
                ON tg_profile_sources(source_id, author_id);
            CREATE INDEX IF NOT EXISTS tg_profile_photos_author_idx
                ON tg_profile_photos(author_id, photo_date DESC);
            CREATE INDEX IF NOT EXISTS tg_media_failures_retry_idx
                ON tg_media_failures(retry_at, source_id, message_id);
            """
        )
        download_columns = {
            str(row["name"]) for row in self.db.execute("PRAGMA table_info(downloads)")
        }
        additions = {
            "state": "TEXT NOT NULL DEFAULT 'saved'",
            "observed_at": "TEXT NOT NULL DEFAULT ''",
        }
        for name, declaration in additions.items():
            if name not in download_columns:
                self.db.execute(f"ALTER TABLE downloads ADD COLUMN {name} {declaration}")
        profile_columns = {
            str(row["name"]) for row in self.db.execute("PRAGMA table_info(tg_profiles)")
        }
        if "photo_error_code" not in profile_columns:
            self.db.execute(
                "ALTER TABLE tg_profiles ADD COLUMN photo_error_code TEXT NOT NULL DEFAULT ''"
            )
        self.db.execute(
            "INSERT OR IGNORE INTO tg_settings(key,value) VALUES('catalog_dirty','0')"
        )
        self.db.execute(
            "INSERT OR IGNORE INTO tg_settings(key,value) VALUES('catalog_revision','0')"
        )
        self.db.execute(
            """INSERT INTO schema_versions(module,version) VALUES('telegram',?)
            ON CONFLICT(module) DO UPDATE SET
            version=MAX(schema_versions.version,excluded.version),
            updated_at=datetime('now')""",
            (TELEGRAM_SCHEMA_VERSION,),
        )
        self.db.commit()

    def _adopt_legacy_archive_root(self) -> None:
        configured = self.get_setting("archive_root")
        if configured:
            self._remember_archive_root(Path(configured))
            return
        row = self.db.execute("SELECT output FROM sources ORDER BY id LIMIT 1").fetchone()
        if row is not None and str(row["output"] or "").strip():
            self._persist_archive_root(
                Path(str(row["output"])).expanduser().resolve()
            )

    def _remember_archive_root(self, root: Path) -> None:
        with self.db:
            self.db.execute(
                """INSERT INTO tg_archive_roots(path) VALUES(?)
                ON CONFLICT(path) DO UPDATE SET last_used_at=datetime('now')""",
                (str(root.expanduser().resolve()),),
            )

    def _persist_archive_root(self, root: Path) -> None:
        resolved = root.expanduser().resolve()
        with self.db:
            self.db.execute(
                """INSERT INTO tg_settings(key,value) VALUES('archive_root',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (str(resolved),),
            )
            self.db.execute(
                """INSERT INTO tg_archive_roots(path) VALUES(?)
                ON CONFLICT(path) DO UPDATE SET last_used_at=datetime('now')""",
                (str(resolved),),
            )

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM tg_settings WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row is not None else default

    def set_setting(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute(
                """INSERT INTO tg_settings(key,value) VALUES(?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (key, value),
            )

    def set_catalog_dirty(self, dirty: bool = True) -> None:
        with self.db:
            if dirty:
                self._set_dirty_sql()
            else:
                self.db.execute(
                    "UPDATE tg_settings SET value='0' WHERE key='catalog_dirty'"
                )

    def catalog_dirty(self) -> bool:
        return self.get_setting("catalog_dirty", "0") == "1"

    def catalog_revision(self) -> int:
        try:
            return int(self.get_setting("catalog_revision", "0"))
        except ValueError:
            return 0

    def clear_catalog_dirty(self, revision: int) -> bool:
        """Clear dirty only when no archive mutation followed the rendered snapshot."""
        with self.db:
            cursor = self.db.execute(
                """UPDATE tg_settings SET value='0' WHERE key='catalog_dirty'
                AND CAST((SELECT value FROM tg_settings
                          WHERE key='catalog_revision') AS INTEGER)=?""",
                (int(revision),),
            )
        return cursor.rowcount == 1

    def archive_root(self, preferred: str | Path | None = None, *, persist: bool = False) -> Path:
        if preferred is not None:
            root = Path(preferred).expanduser().resolve()
            if root.exists() and not root.is_dir():
                raise ValueError("Корень архива должен быть каталогом")
            if persist:
                self._persist_archive_root(root)
                self.set_catalog_dirty()
            return root
        configured = self.get_setting("archive_root")
        if configured:
            return Path(configured).expanduser().resolve()
        row = self.db.execute("SELECT output FROM sources ORDER BY id LIMIT 1").fetchone()
        if row is not None and str(row["output"]).strip():
            root = Path(str(row["output"])).expanduser().resolve()
        else:
            root = default_archive_root()
        if persist:
            self._persist_archive_root(root)
        return root

    def ensure_archive_root(self, preferred: str | Path) -> Path:
        configured = self.get_setting("archive_root")
        return self.archive_root(configured or preferred, persist=not bool(configured))

    def media_download_due(self, source_id: int, message_id: int) -> bool:
        row = self.db.execute(
            "SELECT retry_at FROM tg_media_failures WHERE source_id=? AND message_id=?",
            (int(source_id), int(message_id)),
        ).fetchone()
        return row is None or float(row["retry_at"] or 0) <= time.time()

    def record_media_failure(
        self, source_id: int, message_id: int, *, error: str
    ) -> dict[str, Any]:
        current = self.db.execute(
            "SELECT attempts FROM tg_media_failures WHERE source_id=? AND message_id=?",
            (int(source_id), int(message_id)),
        ).fetchone()
        attempts = int(current["attempts"] if current is not None else 0) + 1
        delay = MEDIA_RETRY_DELAYS[min(attempts - 1, len(MEDIA_RETRY_DELAYS) - 1)]
        retry_at = time.time() + delay
        with self.db:
            self.db.execute(
                """INSERT INTO tg_media_failures(
                source_id,message_id,attempts,error,retry_at,updated_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(source_id,message_id) DO UPDATE SET
                attempts=excluded.attempts,error=excluded.error,
                retry_at=excluded.retry_at,updated_at=excluded.updated_at""",
                (
                    int(source_id),
                    int(message_id),
                    attempts,
                    error[:500],
                    retry_at,
                    utc_now(),
                ),
            )
        return {"attempts": attempts, "retry_at": retry_at, "delay": delay}

    def clear_media_failure(self, source_id: int, message_id: int) -> None:
        key = (int(source_id), int(message_id))
        existing = self.db.execute(
            "SELECT 1 FROM tg_media_failures WHERE source_id=? AND message_id=?",
            key,
        ).fetchone()
        if existing is None:
            return
        with self.db:
            self.db.execute(
                "DELETE FROM tg_media_failures WHERE source_id=? AND message_id=?",
                key,
            )

    def allowed_media_roots(self, archive_root: Path) -> list[Path]:
        roots = [archive_root.expanduser().resolve()]
        for row in self.db.execute("SELECT path FROM tg_archive_roots"):
            value = str(row["path"] or "").strip()
            if value:
                candidate = Path(value).expanduser().resolve()
                if candidate not in roots:
                    roots.append(candidate)
        for row in self.db.execute("SELECT DISTINCT output FROM sources"):
            value = str(row["output"] or "").strip()
            if value:
                candidate = Path(value).expanduser().resolve()
                if candidate not in roots:
                    roots.append(candidate)
        return roots

    def _observe_profile(
        self,
        *,
        author_id: int,
        username: str | None,
        display_name: str,
        observed_at: str,
        source_id: int | None,
    ) -> None:
        profile = {
            "author_id": int(author_id),
            "username": username or "",
            "username_known": int(username is not None),
            "display_name": display_name,
            "observed_at": observed_at,
        }
        self.db.execute(
            """INSERT INTO tg_profiles(author_id,current_username,display_name,
            first_seen,last_seen) VALUES(
            :author_id,:username,:display_name,:observed_at,:observed_at)
            ON CONFLICT(author_id) DO UPDATE SET
            current_username=CASE
              WHEN (tg_profiles.last_seen='' OR :observed_at=''
                    OR :observed_at>=tg_profiles.last_seen) AND :username_known=1
              THEN excluded.current_username ELSE tg_profiles.current_username END,
            display_name=CASE
              WHEN (tg_profiles.last_seen='' OR :observed_at=''
                    OR :observed_at>=tg_profiles.last_seen) AND excluded.display_name<>''
              THEN excluded.display_name ELSE tg_profiles.display_name END,
            first_seen=CASE
              WHEN tg_profiles.first_seen='' THEN excluded.first_seen
              WHEN excluded.first_seen='' THEN tg_profiles.first_seen
              WHEN excluded.first_seen<tg_profiles.first_seen THEN excluded.first_seen
              ELSE tg_profiles.first_seen END,
            last_seen=CASE
              WHEN tg_profiles.last_seen='' THEN excluded.last_seen
              WHEN excluded.last_seen='' THEN tg_profiles.last_seen
              WHEN excluded.last_seen>tg_profiles.last_seen THEN excluded.last_seen
              ELSE tg_profiles.last_seen END""",
            profile,
        )
        if username:
            self.db.execute(
                """INSERT INTO tg_username_history(
                author_id,username,first_seen,last_seen) VALUES(?,?,?,?)
                ON CONFLICT(author_id,username) DO UPDATE SET
                first_seen=CASE
                  WHEN tg_username_history.first_seen='' THEN excluded.first_seen
                  WHEN excluded.first_seen='' THEN tg_username_history.first_seen
                  WHEN excluded.first_seen<tg_username_history.first_seen
                  THEN excluded.first_seen ELSE tg_username_history.first_seen END,
                last_seen=CASE
                  WHEN tg_username_history.last_seen='' THEN excluded.last_seen
                  WHEN excluded.last_seen='' THEN tg_username_history.last_seen
                  WHEN excluded.last_seen>tg_username_history.last_seen
                  THEN excluded.last_seen ELSE tg_username_history.last_seen END""",
                (author_id, username, observed_at, observed_at),
            )
        if source_id is not None:
            self.db.execute(
                """INSERT INTO tg_profile_sources(
                author_id,source_id,first_seen,last_seen) VALUES(?,?,?,?)
                ON CONFLICT(author_id,source_id) DO UPDATE SET
                first_seen=CASE
                  WHEN tg_profile_sources.first_seen='' THEN excluded.first_seen
                  WHEN excluded.first_seen='' THEN tg_profile_sources.first_seen
                  WHEN excluded.first_seen<tg_profile_sources.first_seen
                  THEN excluded.first_seen ELSE tg_profile_sources.first_seen END,
                last_seen=CASE
                  WHEN tg_profile_sources.last_seen='' THEN excluded.last_seen
                  WHEN excluded.last_seen='' THEN tg_profile_sources.last_seen
                  WHEN excluded.last_seen>tg_profile_sources.last_seen
                  THEN excluded.last_seen ELSE tg_profile_sources.last_seen END""",
                (author_id, source_id, observed_at, observed_at),
            )

    def observe_profile(
        self,
        *,
        author_id: int,
        username: str | None,
        display_name: str,
        observed_at: str | None = None,
        source_id: int | None = None,
    ) -> None:
        with self.db:
            self._observe_profile(
                author_id=int(author_id),
                username=username,
                display_name=display_name[:300],
                observed_at=observed_at or utc_now(),
                source_id=source_id,
            )
            self._set_dirty_sql()

    def _set_dirty_sql(self) -> None:
        self.db.execute(
            """INSERT INTO tg_settings(key,value) VALUES('catalog_dirty','1')
            ON CONFLICT(key) DO UPDATE SET value='1'"""
        )
        self.db.execute(
            """INSERT INTO tg_settings(key,value) VALUES('catalog_revision','1')
            ON CONFLICT(key) DO UPDATE SET
            value=CAST(CAST(value AS INTEGER)+1 AS TEXT)"""
        )

    def migrate_legacy_downloads(self) -> None:
        if self.get_setting("legacy_downloads_v1") == "done":
            return
        rows = list(
            self.db.execute(
                """SELECT downloads.*, sources.output, sources.last_checked_at
                FROM downloads JOIN sources ON sources.id=downloads.source_id
                ORDER BY downloads.date,downloads.source_id,downloads.message_id"""
            )
        )
        with self.db:
            for raw in rows:
                item = dict(raw)
                sidecar = self._legacy_sidecar(item)
                author = sidecar.get("author") if isinstance(sidecar.get("author"), dict) else {}
                author_id = item.get("author_id")
                if author_id is None and isinstance(author.get("id"), int):
                    author_id = int(author["id"])
                username = str(item.get("author_username") or author.get("username") or "")
                name = str(item.get("author_name") or author.get("name") or "")
                observed_at = str(item.get("observed_at") or item.get("last_checked_at") or "")
                if not observed_at:
                    observed_at = self._sidecar_mtime(item)
                state = self._legacy_file_state(item)
                self.db.execute(
                    """UPDATE downloads SET author_id=COALESCE(author_id,?),
                    author_username=CASE WHEN author_username='' THEN ? ELSE author_username END,
                    author_name=CASE WHEN author_name='' THEN ? ELSE author_name END,
                    observed_at=CASE WHEN observed_at='' THEN ? ELSE observed_at END,
                    state=? WHERE source_id=? AND message_id=?""",
                    (
                        author_id,
                        username,
                        name,
                        observed_at,
                        state,
                        item["source_id"],
                        item["message_id"],
                    ),
                )
                if author_id is not None:
                    self._observe_profile(
                        author_id=int(author_id),
                        username=username or None,
                        display_name=name,
                        observed_at=observed_at,
                        source_id=int(item["source_id"]),
                    )
            if rows:
                self._set_dirty_sql()
            self.db.execute(
                """INSERT INTO tg_settings(key,value) VALUES('legacy_downloads_v1','done')
                ON CONFLICT(key) DO UPDATE SET value='done'"""
            )

    @staticmethod
    def _sidecar_mtime(item: dict[str, Any]) -> str:
        try:
            stamp = Path(str(item["file_path"])).with_suffix(".json").stat().st_mtime
        except (OSError, ValueError):
            return ""
        return datetime.fromtimestamp(stamp, UTC).isoformat()

    @staticmethod
    def _legacy_file_state(item: dict[str, Any]) -> str:
        try:
            path = Path(str(item["file_path"]))
            expected = int(item.get("size") or 0)
            if path.is_file() and path.stat().st_size > 0:
                return "saved" if not expected or path.stat().st_size == expected else "corrupt"
        except (OSError, TypeError, ValueError):
            pass
        return "missing"

    @staticmethod
    def _legacy_sidecar(item: dict[str, Any]) -> dict[str, Any]:
        try:
            source_root = Path(str(item["output"])).expanduser().resolve()
            media = Path(str(item["file_path"])).expanduser().resolve()
            if not path_is_within(media, source_root):
                return {}
            sidecar = media.with_suffix(".json")
            if not sidecar.is_file() or sidecar.stat().st_size > 1024 * 1024:
                return {}
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def record_post_metadata(
        self,
        source_id: int,
        *,
        channel_title: str,
        post_text: str,
        published_at: str,
        status: str = "ready",
        error: str = "",
    ) -> None:
        with self.db:
            self.db.execute(
                """INSERT INTO tg_post_metadata(source_id,channel_title,post_text,
                published_at,fetched_at,status,error) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(source_id) DO UPDATE SET
                channel_title=excluded.channel_title,post_text=excluded.post_text,
                published_at=excluded.published_at,fetched_at=excluded.fetched_at,
                status=excluded.status,error=excluded.error""",
                (
                    int(source_id),
                    channel_title[:300],
                    post_text[:20000],
                    published_at,
                    utc_now(),
                    status,
                    error[:500],
                ),
            )
            self._set_dirty_sql()

    def post_media(self, source_id: int, media_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM tg_post_media WHERE source_id=? AND media_id=?",
            (int(source_id), str(media_id)),
        ).fetchone()
        return dict(row) if row is not None else None

    def record_post_media(
        self,
        source_id: int,
        media_id: str,
        *,
        kind: str,
        file_path: str,
        size: int,
        status: str = "saved",
        error: str = "",
    ) -> None:
        with self.db:
            self.db.execute(
                """INSERT INTO tg_post_media(source_id,media_id,kind,file_path,size,status,error)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(source_id,media_id) DO UPDATE SET
                kind=excluded.kind,file_path=excluded.file_path,size=excluded.size,
                status=excluded.status,error=excluded.error""",
                (int(source_id), str(media_id), kind, file_path, int(size), status, error[:500]),
            )
            self._set_dirty_sql()

    def profile_ids(self) -> list[int]:
        return [int(row[0]) for row in self.db.execute("SELECT author_id FROM tg_profiles")]

    def get_profile(self, author_id: int) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM tg_profiles WHERE author_id=?", (int(author_id),)
        ).fetchone()
        if row is None:
            raise ValueError("Telegram-профиль не найден в локальном архиве")
        result = dict(row)
        result["photos"] = self.profile_photos(author_id)
        return result

    def get_profile_identity(self, author_id: int) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM tg_profiles WHERE author_id=?", (int(author_id),)
        ).fetchone()
        if row is None:
            raise ValueError("Telegram-профиль не найден в локальном архиве")
        return dict(row)

    def profile_photos(
        self, author_id: int, *, include_path: bool = False
    ) -> list[dict[str, Any]]:
        profile = self.db.execute(
            "SELECT 1 FROM tg_profiles WHERE author_id=?", (int(author_id),)
        ).fetchone()
        if profile is None:
            raise ValueError("Telegram-профиль не найден в локальном архиве")
        roots = self.allowed_media_roots(self.archive_root())
        result: list[dict[str, Any]] = []
        for row in self.db.execute(
            """SELECT * FROM tg_profile_photos WHERE author_id=? AND status='saved'
            ORDER BY photo_date DESC,photo_id""",
            (int(author_id),),
        ):
            item = dict(row)
            try:
                path = Path(str(item["file_path"])).expanduser().resolve(strict=True)
                valid = path.is_file() and any(path_is_within(path, root) for root in roots)
                if valid and int(item.get("size") or 0) > 0:
                    valid = path.stat().st_size == int(item["size"])
            except (OSError, RuntimeError, ValueError):
                valid = False
                path = Path(str(item["file_path"]))
            item["available"] = bool(valid)
            if include_path and valid:
                item["file_path"] = str(path)
            else:
                item.pop("file_path", None)
            result.append(item)
        return result

    def selected_profile_photos(
        self, author_id: int, photo_ids: list[Any]
    ) -> list[dict[str, Any]]:
        identifiers = [str(value).strip() for value in photo_ids]
        if not 1 <= len(identifiers) <= 4 or len(identifiers) != len(set(identifiers)):
            raise ValueError("Выбери от 1 до 4 уникальных локальных фотографий")
        photos = {
            str(item["photo_id"]): item
            for item in self.profile_photos(author_id, include_path=True)
        }
        selected: list[dict[str, Any]] = []
        for photo_id in identifiers:
            item = photos.get(photo_id)
            if item is None or not item.get("available") or not item.get("file_path"):
                raise ValueError("Выбранное фото не принадлежит профилю или недоступно локально")
            selected.append(item)
        return selected

    def recent_profiles(self, count: int = 80) -> list[dict[str, Any]]:
        """Return presentation-safe profile summaries for the daemon status response."""
        result = []
        for row in self.db.execute(
                """SELECT profiles.*,
                COUNT(DISTINCT photos.photo_id) AS photo_count,
                COUNT(DISTINCT links.source_id) AS source_count,
                GROUP_CONCAT(DISTINCT links.source_id) AS source_ids_text
                FROM tg_profiles AS profiles
                LEFT JOIN tg_profile_photos AS photos
                  ON photos.author_id=profiles.author_id
                LEFT JOIN tg_profile_sources AS links
                  ON links.author_id=profiles.author_id
                GROUP BY profiles.author_id
                ORDER BY profiles.last_seen DESC, profiles.author_id
                LIMIT ?""",
                (max(1, int(count)),),
            ):
            item = dict(row)
            item["source_ids"] = [
                int(value)
                for value in str(item.pop("source_ids_text") or "").split(",")
                if value
            ]
            result.append(item)
        return result

    def resolve_source_id(self, value: Any) -> int:
        """Resolve only an already saved source id or post URL; never contact Telegram."""
        raw = str(value or "").strip()
        if not raw:
            raise ValueError("Выбери сохранённую публикацию")
        try:
            source_id = int(raw)
        except ValueError:
            post = parse_post_url(raw)
            row = self.db.execute(
                "SELECT id FROM sources WHERE lower(username)=? AND post_id=?",
                (post.username, post.message_id),
            ).fetchone()
        else:
            row = self.db.execute("SELECT id FROM sources WHERE id=?", (source_id,)).fetchone()
        if row is None:
            raise ValueError(
                "Публикация не найдена в локальном архиве; анализ не запускает новый сбор"
            )
        return int(row["id"])

    def source_summary(self, source_id: int) -> dict[str, Any]:
        row = self.db.execute(
            """SELECT s.id,s.username,s.post_id,s.url,s.title,
            COALESCE(m.channel_title,'') AS channel_title,
            COUNT(DISTINCT links.author_id) AS profile_count,
            COUNT(DISTINCT CASE WHEN photos.status='saved' THEN photos.photo_id END)
                AS photo_count
            FROM sources s
            LEFT JOIN tg_post_metadata m ON m.source_id=s.id
            LEFT JOIN tg_profile_sources links ON links.source_id=s.id
            LEFT JOIN tg_profile_photos photos ON photos.author_id=links.author_id
            WHERE s.id=? GROUP BY s.id""",
            (int(source_id),),
        ).fetchone()
        if row is None:
            raise ValueError("Публикация не найдена в локальном архиве")
        return dict(row)

    def source_profiles(self, source_id: int) -> list[int]:
        """Return the complete local relation, intentionally without the status 80-row cap."""
        self.source_summary(source_id)
        return [
            int(row["author_id"])
            for row in self.db.execute(
                """SELECT author_id FROM tg_profile_sources WHERE source_id=?
                ORDER BY last_seen DESC,author_id""",
                (int(source_id),),
            )
        ]

    def profile_summaries(
        self,
        *,
        author_ids: list[int] | None = None,
        source_id: int | None = None,
        query: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Paginated presentation-safe local profiles for analysis pickers."""
        page_limit = max(1, min(100, int(limit)))
        page_offset = max(0, int(offset))
        clauses = ["1=1"]
        parameters: list[Any] = []
        join = ""
        if source_id is not None:
            selected_source = self.resolve_source_id(source_id)
            join = "JOIN tg_profile_sources selected ON selected.author_id=profiles.author_id"
            clauses.append("selected.source_id=?")
            parameters.append(selected_source)
        if author_ids is not None:
            identifiers: list[int] = []
            for item in author_ids:
                try:
                    author_id = int(item)
                except (TypeError, ValueError) as exc:
                    raise ValueError("author_ids содержит некорректный Telegram ID") from exc
                if author_id > 0 and author_id not in identifiers:
                    identifiers.append(author_id)
            if not identifiers:
                return {"items": [], "total": 0, "limit": page_limit, "offset": page_offset,
                        "has_more": False}
            placeholders = ",".join("?" for _ in identifiers)
            clauses.append(f"profiles.author_id IN ({placeholders})")
            parameters.extend(identifiers)
        needle = str(query or "").strip().lstrip("@").casefold()
        if needle:
            clauses.append(
                """(CAST(profiles.author_id AS TEXT) LIKE ? OR
                lower(profiles.display_name) LIKE ? OR lower(profiles.current_username) LIKE ? OR
                EXISTS (SELECT 1 FROM tg_username_history history
                        WHERE history.author_id=profiles.author_id
                          AND lower(history.username) LIKE ?))"""
            )
            pattern = f"%{needle}%"
            parameters.extend([pattern, pattern, pattern, pattern])
        where = " AND ".join(clauses)
        total = int(
            self.db.execute(
                f"SELECT COUNT(DISTINCT profiles.author_id) FROM tg_profiles profiles {join} "
                f"WHERE {where}",
                parameters,
            ).fetchone()[0]
        )
        rows = [
            dict(row)
            for row in self.db.execute(
                f"""SELECT profiles.author_id,profiles.current_username,
                profiles.display_name,profiles.first_seen,profiles.last_seen,
                profiles.photo_error,profiles.photo_error_code,
                COUNT(DISTINCT CASE WHEN photos.status='saved' THEN photos.photo_id END)
                    AS photo_count,
                COUNT(DISTINCT links.source_id) AS source_count
                FROM tg_profiles profiles {join}
                LEFT JOIN tg_profile_photos photos ON photos.author_id=profiles.author_id
                LEFT JOIN tg_profile_sources links ON links.author_id=profiles.author_id
                WHERE {where} GROUP BY profiles.author_id
                ORDER BY profiles.last_seen DESC,profiles.author_id LIMIT ? OFFSET ?""",
                (*parameters, page_limit, page_offset),
            )
        ]
        return {
            "items": rows,
            "total": total,
            "limit": page_limit,
            "offset": page_offset,
            "has_more": page_offset + len(rows) < total,
        }

    def due_profile_photo_ids(self, limit: int = 100) -> list[int]:
        """Return profiles whose visible photos need an initial check or retry."""
        return [
            int(row["author_id"])
            for row in self.db.execute(
                """SELECT author_id FROM tg_profiles
                WHERE photos_retry_at<=? ORDER BY photos_retry_at,author_id LIMIT ?""",
                (time.time(), max(1, int(limit))),
            )
        ]

    def profile_photo_due(self, author_id: int) -> bool:
        row = self.db.execute(
            "SELECT photos_retry_at FROM tg_profiles WHERE author_id=?", (int(author_id),)
        ).fetchone()
        return row is not None and float(row["photos_retry_at"] or 0) <= time.time()

    def profile_photo(self, author_id: int, photo_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM tg_profile_photos WHERE author_id=? AND photo_id=?",
            (int(author_id), str(photo_id)),
        ).fetchone()
        return dict(row) if row is not None else None

    def record_profile_photo(
        self,
        author_id: int,
        photo_id: str,
        *,
        file_path: str,
        size: int,
        photo_date: str,
        status: str = "saved",
        error: str = "",
    ) -> None:
        with self.db:
            self.db.execute(
                """INSERT INTO tg_profile_photos(author_id,photo_id,file_path,size,
                photo_date,status,error) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(author_id,photo_id) DO UPDATE SET
                file_path=excluded.file_path,size=excluded.size,
                photo_date=excluded.photo_date,status=excluded.status,error=excluded.error""",
                (
                    int(author_id),
                    str(photo_id),
                    file_path,
                    int(size),
                    photo_date,
                    status,
                    error[:500],
                ),
            )
            self._set_dirty_sql()

    def finish_profile_photo_check(
        self,
        author_id: int,
        *,
        error: str = "",
        error_code: str = "",
        retry_after: int = 6 * 60 * 60,
    ) -> None:
        retry_at = time.time() + retry_after if error else time.time() + 24 * 60 * 60
        with self.db:
            self.db.execute(
                """UPDATE tg_profiles SET photos_checked_at=?,photo_error=?,photo_error_code=?,
                photos_retry_at=?
                WHERE author_id=?""",
                (utc_now(), error[:500], error_code[:80], retry_at, int(author_id)),
            )
            self._set_dirty_sql()

    def catalog_rows(self) -> dict[str, list[dict[str, Any]]]:
        queries = {
            "sources": "SELECT * FROM sources ORDER BY id",
            "downloads": "SELECT * FROM downloads ORDER BY source_id,message_id",
            "profiles": "SELECT * FROM tg_profiles ORDER BY author_id",
            "username_history": (
                "SELECT * FROM tg_username_history ORDER BY author_id,first_seen,username"
            ),
            "profile_sources": (
                "SELECT * FROM tg_profile_sources ORDER BY source_id,author_id"
            ),
            "profile_photos": (
                "SELECT * FROM tg_profile_photos ORDER BY author_id,photo_date DESC,photo_id"
            ),
            "post_metadata": "SELECT * FROM tg_post_metadata ORDER BY source_id",
            "post_media": "SELECT * FROM tg_post_media ORDER BY source_id,media_id",
        }
        return {
            name: [dict(row) for row in self.db.execute(query)]
            for name, query in queries.items()
        }
