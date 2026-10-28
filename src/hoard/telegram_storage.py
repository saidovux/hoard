"""Telegram-specific SQLite schema and non-destructive legacy migration."""
from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .archive_paths import default_archive_root, path_is_within
from .models import parse_post_url

TELEGRAM_SCHEMA_VERSION = 4

MEDIA_RETRY_DELAYS = (60, 300, 1800, 7200, 21600, 86400)
CATALOG_REPORT_RETRY_DELAYS = (30, 120, 600, 1800, 3600)
CATALOG_REPORT_COOLDOWN = 2.0


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
            CREATE TABLE IF NOT EXISTS tg_video_frame_sets (
                source_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                author_id INTEGER,
                video_path TEXT NOT NULL,
                video_size INTEGER NOT NULL DEFAULT 0,
                video_mtime_ns INTEGER NOT NULL DEFAULT 0,
                frame_set_id TEXT NOT NULL DEFAULT '',
                algorithm_version INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'pending',
                duration REAL NOT NULL DEFAULT 0,
                frame_count INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                error_code TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0,
                retry_at REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(source_id, message_id)
            );
            CREATE TABLE IF NOT EXISTS tg_video_frames (
                source_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                timestamp_seconds REAL NOT NULL DEFAULT 0,
                file_path TEXT NOT NULL,
                size INTEGER NOT NULL DEFAULT 0,
                sha256 TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(source_id, message_id, position)
            );
            CREATE INDEX IF NOT EXISTS tg_video_frame_sets_author_idx
                ON tg_video_frame_sets(author_id, message_id DESC);
            CREATE INDEX IF NOT EXISTS tg_video_frame_sets_status_idx
                ON tg_video_frame_sets(status, source_id, message_id);
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
        for key, value in (
            ("frames_backfill_active", "0"),
            ("frames_backfill_complete", "0"),
            ("frames_backfill_cursor", "0"),
            ("frames_backfill_updated_at", ""),
        ):
            self.db.execute(
                "INSERT OR IGNORE INTO tg_settings(key,value) VALUES(?,?)", (key, value)
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

    def _persist_archive_root(self, root: Path) -> bool:
        resolved = root.expanduser().resolve()
        with self.db:
            changed = self.db.execute(
                """INSERT INTO tg_settings(key,value) VALUES('archive_root',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                WHERE tg_settings.value<>excluded.value""",
                (str(resolved),),
            ).rowcount == 1
            self.db.execute(
                """INSERT INTO tg_archive_roots(path) VALUES(?)
                ON CONFLICT(path) DO UPDATE SET last_used_at=datetime('now')""",
                (str(resolved),),
            )
            if changed:
                self._set_dirty_sql()
        return changed

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM tg_settings WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row is not None else default

    def set_setting(self, key: str, value: str) -> None:
        with self.db:
            self._set_setting_sql(key, value)

    def _set_setting_sql(self, key: str, value: str) -> None:
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

    def catalog_report_due(self, *, now: float | None = None) -> bool:
        try:
            retry_at = float(self.get_setting("catalog_report_retry_at", "0"))
        except ValueError:
            retry_at = 0
        return retry_at <= (time.time() if now is None else float(now))

    def record_catalog_report_failure(self, error: str, *, now: float | None = None) -> int:
        try:
            previous = int(self.get_setting("catalog_report_failures", "0"))
        except ValueError:
            previous = 0
        failures = max(0, previous) + 1
        delay = CATALOG_REPORT_RETRY_DELAYS[
            min(failures - 1, len(CATALOG_REPORT_RETRY_DELAYS) - 1)
        ]
        retry_at = (time.time() if now is None else float(now)) + delay
        with self.db:
            self._set_setting_sql("catalog_report_failures", str(failures))
            self._set_setting_sql("catalog_report_retry_at", str(retry_at))
            self._set_setting_sql("catalog_report_last_error", error[:500])
        return delay

    def record_catalog_report_success(self, *, now: float | None = None) -> None:
        # A short durable cooldown bounds rebuild frequency when mutations never stop.
        retry_at = (time.time() if now is None else float(now)) + CATALOG_REPORT_COOLDOWN
        with self.db:
            self._set_setting_sql("catalog_report_failures", "0")
            self._set_setting_sql("catalog_report_retry_at", str(retry_at))
            # Do not keep a resolved failure visible in diagnostics forever.
            self._set_setting_sql("catalog_report_last_error", "")

    def clear_catalog_report_retry(self) -> None:
        """Let an explicit user retry bypass the automatic failure backoff."""
        with self.db:
            self._set_setting_sql("catalog_report_retry_at", "0")

    # -- video-note frame sets -------------------------------------------------
    # One latest immutable set is kept per source/message.  Frames are never
    # mixed with profile photos in the filesystem or in the AI selection layer.

    def frame_set(self, source_id: int, message_id: int) -> dict[str, Any] | None:
        row = self.db.execute(
            """SELECT sets.*, d.date AS message_date, d.media_url AS comment_url,
            s.username AS source_username, s.post_id AS source_post_id,
            s.title AS source_title
            FROM tg_video_frame_sets sets
            LEFT JOIN downloads d ON d.source_id=sets.source_id
              AND d.message_id=sets.message_id
            LEFT JOIN sources s ON s.id=sets.source_id
            WHERE sets.source_id=? AND sets.message_id=?""",
            (int(source_id), int(message_id)),
        ).fetchone()
        return dict(row) if row is not None else None

    def frame_rows(self, source_id: int, message_id: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.execute(
                """SELECT * FROM tg_video_frames WHERE source_id=? AND message_id=?
                ORDER BY position""",
                (int(source_id), int(message_id)),
            )
        ]

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def register_frame_set_sql(
        self,
        *,
        source_id: int,
        message_id: int,
        author_id: int | None,
        file_path: str,
        size: int,
        mtime_ns: int,
        mark_dirty: bool = True,
    ) -> bool:
        """Create/reset a pending set inside the caller's download transaction.

        Returns True when a frame job should be enqueued.  An unchanged ready
        set is never regenerated just because a scan was repeated.
        """
        source_id = int(source_id)
        message_id = int(message_id)
        current = self.db.execute(
            """SELECT status,video_path,video_size,video_mtime_ns
            FROM tg_video_frame_sets WHERE source_id=? AND message_id=?""",
            (source_id, message_id),
        ).fetchone()
        same_source = bool(
            current is not None
            and str(current["video_path"]) == str(file_path)
            and int(current["video_size"] or 0) == int(size)
            and int(current["video_mtime_ns"] or 0) == int(mtime_ns)
        )
        if current is not None and same_source and current["status"] in {
            "saved",
            "pending",
            "running",
            "failed",
        }:
            return False
        if current is None:
            self.db.execute(
                """INSERT INTO tg_video_frame_sets(
                source_id,message_id,author_id,video_path,video_size,video_mtime_ns,
                status,updated_at) VALUES(?,?,?,?,?,?,'pending',?)""",
                (
                    source_id,
                    message_id,
                    int(author_id) if author_id is not None else None,
                    str(file_path),
                    int(size),
                    int(mtime_ns),
                    utc_now(),
                ),
            )
        else:
            self.db.execute(
                """UPDATE tg_video_frame_sets SET author_id=?,video_path=?,video_size=?,
                video_mtime_ns=?,frame_set_id='',status='pending',duration=0,frame_count=0,
                error='',error_code='',attempts=0,retry_at=0,updated_at=?
                WHERE source_id=? AND message_id=?""",
                (
                    int(author_id) if author_id is not None else None,
                    str(file_path),
                    int(size),
                    int(mtime_ns),
                    utc_now(),
                    source_id,
                    message_id,
                ),
            )
            self.db.execute(
                "DELETE FROM tg_video_frames WHERE source_id=? AND message_id=?",
                (source_id, message_id),
            )
        if mark_dirty:
            self._set_dirty_sql()
        return True

    @staticmethod
    def _frame_job_payload(source_id: int, message_id: int) -> str:
        return json.dumps(
            {"source_id": int(source_id), "message_id": int(message_id)},
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _enqueue_frame_job_sql(
        self,
        source_id: int,
        message_id: int,
        *,
        authorized: bool = False,
    ) -> int | None:
        unique_key = f"video-frames:{int(source_id)}:{int(message_id)}"
        active = self.db.execute(
            """SELECT id,state FROM jobs WHERE unique_key=?
            AND state IN ('waiting','queued','running') ORDER BY id DESC LIMIT 1""",
            (unique_key,),
        ).fetchone()
        if active is not None:
            if authorized and str(active["state"]) == "waiting":
                self.db.execute(
                    """UPDATE jobs SET state='queued',result='',updated_at=datetime('now')
                    WHERE id=? AND state='waiting'""",
                    (int(active["id"]),),
                )
            return int(active["id"])
        cursor = self.db.execute(
            """INSERT OR IGNORE INTO jobs(kind,payload,unique_key,state,result)
            VALUES('telegram.video_frames',?,?,?,?)""",
            (
                self._frame_job_payload(source_id, message_id),
                unique_key,
                "queued" if authorized else "waiting",
                "" if authorized else "Ожидает явного запуска обработки кадров",
            ),
        )
        return int(cursor.lastrowid) if cursor.rowcount else None

    def queue_frame_job(
        self,
        source_id: int,
        message_id: int,
        *,
        reset_failed: bool = False,
        authorized: bool = False,
    ) -> int | None:
        """Durably enqueue extraction for one existing download."""
        row = self.db.execute(
            """SELECT d.source_id,d.message_id,d.author_id,d.file_path,d.size,
            sets.status AS frame_status
            FROM downloads d LEFT JOIN tg_video_frame_sets sets
            ON sets.source_id=d.source_id AND sets.message_id=d.message_id
            WHERE d.source_id=? AND d.message_id=?""",
            (int(source_id), int(message_id)),
        ).fetchone()
        if row is None:
            raise ValueError("Сохранённый видеокружок не найден")
        path = Path(str(row["file_path"]))
        try:
            stat = path.stat()
            if not path.is_file() or stat.st_size <= 0:
                raise OSError
        except OSError as exc:
            raise ValueError("Исходный MP4 недоступен локально") from exc
        with self.db:
            if reset_failed and str(row["frame_status"] or "") == "failed":
                self.db.execute(
                    """UPDATE tg_video_frame_sets SET status='pending',error='',error_code='',
                    retry_at=0,updated_at=? WHERE source_id=? AND message_id=?""",
                    (utc_now(), int(source_id), int(message_id)),
                )
            needs = self.register_frame_set_sql(
                source_id=int(source_id),
                message_id=int(message_id),
                author_id=(
                    int(row["author_id"]) if row["author_id"] is not None else None
                ),
                file_path=str(path.resolve(strict=False)),
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
            )
            frame_status = str(row["frame_status"] or "")
            if not needs and frame_status == "saved":
                return None
            if not needs and frame_status in {"failed", "stale"} and not reset_failed:
                return None
            return self._enqueue_frame_job_sql(
                int(source_id), int(message_id), authorized=authorized
            )

    def mark_frame_running(self, source_id: int, message_id: int) -> None:
        with self.db:
            self.db.execute(
                """UPDATE tg_video_frame_sets SET status='running',attempts=attempts+1,
                error='',error_code='',updated_at=? WHERE source_id=? AND message_id=?""",
                (utc_now(), int(source_id), int(message_id)),
            )
            self._set_dirty_sql()

    def mark_frame_failed(
        self,
        source_id: int,
        message_id: int,
        *,
        error: str,
        error_code: str,
        retry_after: float = 0,
    ) -> None:
        with self.db:
            self.db.execute(
                """UPDATE tg_video_frame_sets SET status='failed',error=?,error_code=?,
                retry_at=?,updated_at=? WHERE source_id=? AND message_id=?""",
                (
                    str(error)[:500],
                    str(error_code)[:80],
                    time.time() + max(0.0, float(retry_after)),
                    utc_now(),
                    int(source_id),
                    int(message_id),
                ),
            )
            self._set_dirty_sql()

    def mark_frame_pending(self, source_id: int, message_id: int, *, error: str = "") -> None:
        with self.db:
            self.db.execute(
                """UPDATE tg_video_frame_sets SET status='pending',error=?,error_code='',
                updated_at=? WHERE source_id=? AND message_id=?""",
                (str(error)[:500], utc_now(), int(source_id), int(message_id)),
            )
            self._set_dirty_sql()

    def save_frame_set(
        self,
        source_id: int,
        message_id: int,
        *,
        frame_set_id: str,
        algorithm_version: int,
        duration: float,
        video_size: int,
        video_mtime_ns: int,
        frames: list[dict[str, Any]],
    ) -> None:
        if len(frames) != 3:
            raise ValueError("Набор кадров должен содержать ровно три изображения")
        positions = [int(item["position"]) for item in frames]
        if positions != [1, 2, 3]:
            raise ValueError("Кадры должны иметь позиции 1, 2 и 3")
        with self.db:
            self.db.execute(
                """UPDATE tg_video_frame_sets SET frame_set_id=?,algorithm_version=?,
                status='saved',duration=?,frame_count=3,error='',error_code='',retry_at=0,
                video_size=?,video_mtime_ns=?,updated_at=?
                WHERE source_id=? AND message_id=?""",
                (
                    str(frame_set_id),
                    int(algorithm_version),
                    float(duration),
                    int(video_size),
                    int(video_mtime_ns),
                    utc_now(),
                    int(source_id),
                    int(message_id),
                ),
            )
            self.db.execute(
                "DELETE FROM tg_video_frames WHERE source_id=? AND message_id=?",
                (int(source_id), int(message_id)),
            )
            for item in frames:
                self.db.execute(
                    """INSERT INTO tg_video_frames(
                    source_id,message_id,position,timestamp_seconds,file_path,size,sha256)
                    VALUES(?,?,?,?,?,?,?)""",
                    (
                        int(source_id),
                        int(message_id),
                        int(item["position"]),
                        float(item["timestamp_seconds"]),
                        str(item["file_path"]),
                        int(item["size"]),
                        str(item["sha256"]),
                    ),
                )
            self.db.execute(
                """UPDATE jobs SET state='done',result='Кадры уже готовы',
                updated_at=datetime('now') WHERE unique_key=? AND state='waiting'""",
                (f"video-frames:{int(source_id)}:{int(message_id)}",),
            )
            self._set_dirty_sql()

    def _frame_set_is_valid(
        self, row: dict[str, Any], frames: list[dict[str, Any]]
    ) -> tuple[bool, str]:
        if str(row.get("status")) != "saved" or not str(row.get("frame_set_id") or ""):
            return False, "Кадры ещё не готовы"
        if int(row.get("frame_count") or 0) != 3 or len(frames) != 3:
            return False, "Набор кадров неполный"
        if [int(item["position"]) for item in frames] != [1, 2, 3]:
            return False, "Порядок кадров повреждён"
        try:
            video = Path(str(row["video_path"])).expanduser().resolve(strict=True)
            video_stat = video.stat()
            if (
                not video.is_file()
                or video_stat.st_size != int(row.get("video_size") or 0)
                or video_stat.st_mtime_ns != int(row.get("video_mtime_ns") or 0)
            ):
                return False, "Исходный MP4 изменился после извлечения кадров"
        except (OSError, RuntimeError, ValueError):
            return False, "Исходный MP4 недоступен"
        for item in frames:
            try:
                path = Path(str(item["file_path"])).expanduser().resolve(strict=True)
                stat = path.stat()
                if not path.is_file() or stat.st_size != int(item["size"] or 0):
                    return False, "Файл кадра отсутствует или изменился"
                if self._file_sha256(path) != str(item["sha256"]):
                    return False, "Содержимое кадра изменилось"
            except (OSError, RuntimeError, ValueError):
                return False, "Файл кадра недоступен"
        return True, ""

    def _mark_frame_stale(self, source_id: int, message_id: int, reason: str) -> None:
        with self.db:
            self.db.execute(
                """UPDATE tg_video_frame_sets SET status='stale',error=?,error_code='stale',
                updated_at=? WHERE source_id=? AND message_id=? AND status='saved'""",
                (str(reason)[:500], utc_now(), int(source_id), int(message_id)),
            )
            self._set_dirty_sql()

    def frame_details(self, source_id: int, message_id: int) -> dict[str, Any] | None:
        row = self.frame_set(source_id, message_id)
        if row is None:
            return None
        frames = self.frame_rows(source_id, message_id)
        valid, reason = self._frame_set_is_valid(row, frames) if row["status"] == "saved" else (
            False,
            str(row.get("error") or ""),
        )
        if row["status"] == "saved" and not valid:
            self._mark_frame_stale(source_id, message_id, reason)
            row = self.frame_set(source_id, message_id) or row
        result = dict(row)
        result["video_id"] = f"{int(source_id)}:{int(message_id)}"
        result["valid"] = bool(valid)
        result["frames"] = [
            {
                **item,
                "image_id": f"frame:{int(source_id)}:{int(message_id)}:{int(item['position'])}",
                "available": valid,
            }
            for item in frames
        ]
        return result

    def video_frame_options(
        self, author_id: int, *, source_id: int | None = None
    ) -> list[dict[str, Any]]:
        scope = ""
        parameters: list[Any] = [int(author_id)]
        if source_id is not None:
            scope = " AND sets.source_id=?"
            parameters.append(int(source_id))
        rows = list(
            self.db.execute(
                f"""SELECT sets.source_id,sets.message_id FROM tg_video_frame_sets sets
                LEFT JOIN downloads d ON d.source_id=sets.source_id
                  AND d.message_id=sets.message_id
                WHERE sets.author_id=? AND sets.status='saved'{scope}
                ORDER BY COALESCE(d.date,'') DESC,sets.message_id DESC""",
                parameters,
            )
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            details = self.frame_details(int(row["source_id"]), int(row["message_id"]))
            if details is not None and details["valid"]:
                result.append(details)
        return result

    def selected_video_frames(
        self,
        author_id: int,
        video_id: str | None = None,
        image_ids: list[Any] | None = None,
        *,
        source_id: int | None = None,
    ) -> dict[str, Any]:
        options = self.video_frame_options(author_id, source_id=source_id)
        if not options:
            raise ValueError(
                "Нет готового набора кадров этого профиля в выбранной публикации"
                if source_id is not None
                else "Нет готового набора кадров этого профиля"
            )
        selected = None
        if video_id:
            value = str(video_id).strip()
            selected = next((item for item in options if item["video_id"] == value), None)
            if selected is None:
                raise ValueError(
                    "Выбранный видеокружок не принадлежит профилю, публикации "
                    "или кадры не готовы"
                )
        else:
            selected = options[0]
        frames = list(selected["frames"])
        if image_ids is not None:
            identifiers = [str(value) for value in image_ids]
            by_id = {str(item["image_id"]): item for item in frames}
            if len(identifiers) != 3 or len(set(identifiers)) != 3:
                raise ValueError("Для анализа нужны ровно три разных кадра")
            ordered = [by_id.get(identifier) for identifier in identifiers]
            if any(item is None for item in ordered):
                raise ValueError("Кадры изменились после предпросмотра")
            positions = [int(item["position"]) for item in ordered]
            if positions != [1, 2, 3]:
                raise ValueError("Кадры изменились после предпросмотра")
            frames = ordered
        return {**selected, "frames": frames}

    def frame_for_image_id(
        self, author_id: int, image_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        parts = str(image_id).split(":")
        if len(parts) != 4 or parts[0] != "frame":
            raise ValueError("Некорректный идентификатор кадра")
        try:
            source_id, message_id, position = (int(parts[1]), int(parts[2]), int(parts[3]))
        except ValueError as exc:
            raise ValueError("Некорректный идентификатор кадра") from exc
        details = self.frame_details(source_id, message_id)
        if details is None or not details["valid"] or int(details.get("author_id") or 0) != int(
            author_id
        ):
            raise ValueError("Кадры не принадлежат выбранному профилю или недоступны")
        frame = next(
            (item for item in details["frames"] if int(item["position"]) == position),
            None,
        )
        if frame is None:
            raise ValueError("Кадр не найден")
        return details, frame

    def frame_stats(self) -> dict[str, Any]:
        row = self.db.execute(
            """WITH download_counts AS (
                SELECT COUNT(*) AS total FROM downloads
            ), frame_counts AS (
                SELECT COUNT(*) AS amount,
                  COALESCE(SUM(status='saved'),0) AS saved,
                  COALESCE(SUM(status='failed'),0) AS failed,
                  COALESCE(SUM(status='stale'),0) AS stale,
                  COALESCE(SUM(status='pending'),0) AS pending,
                  COALESCE(SUM(status='running'),0) AS running
                FROM tg_video_frame_sets
            ), job_counts AS (
                SELECT COALESCE(SUM(state='waiting'),0) AS waiting,
                  COALESCE(SUM(state='queued'),0) AS queued,
                  COALESCE(SUM(state='running'),0) AS processing
                FROM jobs WHERE kind='telegram.video_frames'
            ), setting_values AS (
                SELECT MAX(CASE WHEN key='frames_backfill_active' THEN value END) AS active,
                  MAX(CASE WHEN key='frames_backfill_complete' THEN value END) AS complete,
                  MAX(CASE WHEN key='frames_backfill_cursor' THEN value END) AS cursor
                FROM tg_settings WHERE key IN (
                  'frames_backfill_active','frames_backfill_complete','frames_backfill_cursor'
                )
            )
            SELECT download_counts.total AS total,
              MAX(download_counts.total-frame_counts.amount,0) AS missing,
              frame_counts.*,job_counts.*,setting_values.*
            FROM download_counts,frame_counts,job_counts,setting_values"""
        ).fetchone()
        assert row is not None
        counts = {
            "saved": int(row["saved"] or 0),
            "failed": int(row["failed"] or 0),
            "stale": int(row["stale"] or 0),
            "pending": int(row["pending"] or 0),
            "running": int(row["running"] or 0),
        }
        waiting = int(row["waiting"] or 0)
        missing = int(row["missing"] or 0)
        return {
            "total": int(row["total"] or 0),
            "saved": counts.get("saved", 0),
            "failed": counts.get("failed", 0) + counts.get("stale", 0),
            "pending": counts.get("pending", 0) + counts.get("running", 0),
            "missing": missing,
            "waiting": waiting,
            "queued": int(row["queued"] or 0) + int(row["processing"] or 0),
            "processing": int(row["processing"] or 0),
            "actionable": waiting + missing,
            "statuses": counts,
            "backfill_active": str(row["active"] or "0") == "1",
            "backfill_complete": str(row["complete"] or "0") == "1",
            "backfill_cursor": int(row["cursor"] or 0),
        }

    def archive_frame_statistics(self) -> dict[str, int]:
        """Return exact frame-set/job aggregates without inspecting archive files."""
        row = self.db.execute(
            """WITH set_counts AS (
                SELECT COUNT(*) AS total,
                  COALESCE(SUM(status='saved'),0) AS saved,
                  COALESCE(SUM(status='failed'),0) AS failed,
                  COALESCE(SUM(status='stale'),0) AS stale
                FROM tg_video_frame_sets
            ), job_counts AS (
                SELECT COALESCE(SUM(state='waiting'),0) AS waiting,
                  COALESCE(SUM(state='queued'),0) AS queued,
                  COALESCE(SUM(state='running'),0) AS processing
                FROM jobs WHERE kind='telegram.video_frames'
            ), saved_frames AS (
                SELECT COUNT(*) AS amount
                FROM tg_video_frames AS frame
                JOIN tg_video_frame_sets AS frame_set
                  ON frame_set.source_id=frame.source_id
                 AND frame_set.message_id=frame.message_id
                WHERE frame_set.status='saved'
            ), download_counts AS (
                SELECT COUNT(*) AS total FROM downloads
            )
            SELECT set_counts.*,job_counts.*,saved_frames.amount AS created_frames,
              MAX(download_counts.total-set_counts.total,0) AS missing
            FROM set_counts,job_counts,saved_frames,download_counts"""
        ).fetchone()
        assert row is not None
        return {
            "processed_videos": int(row["saved"] or 0),
            "created_frames": int(row["created_frames"] or 0),
            "waiting": int(row["waiting"] or 0),
            "queued": int(row["queued"] or 0),
            "processing": int(row["processing"] or 0),
            "missing": int(row["missing"] or 0),
            "failed": int(row["failed"] or 0),
            "stale": int(row["stale"] or 0),
        }

    def frame_backfill_active(self) -> bool:
        return self.get_setting("frames_backfill_active", "0") == "1"

    def frame_work_state(self) -> dict[str, int | bool]:
        row = self.db.execute(
            """SELECT
            COALESCE(SUM(state='waiting'),0) AS waiting,
            COALESCE(SUM(state='queued'),0) AS queued,
            COALESCE(SUM(state='running'),0) AS running,
            (SELECT value FROM tg_settings WHERE key='frames_backfill_complete') AS complete
            FROM jobs WHERE kind='telegram.video_frames'"""
        ).fetchone()
        assert row is not None
        return {
            "waiting": int(row["waiting"] or 0),
            "queued": int(row["queued"] or 0),
            "running": int(row["running"] or 0),
            "backfill_complete": str(row["complete"] or "0") == "1",
        }

    def authorize_waiting_frame_jobs(self, limit: int) -> int:
        identifiers = [
            int(row["id"])
            for row in self.db.execute(
                """SELECT id FROM jobs WHERE kind='telegram.video_frames'
                AND state='waiting' ORDER BY id LIMIT ?""",
                (max(0, int(limit)),),
            )
        ]
        if not identifiers:
            return 0
        placeholders = ",".join("?" for _ in identifiers)
        with self.db:
            cursor = self.db.execute(
                f"""UPDATE jobs SET state='queued',result='',updated_at=datetime('now')
                WHERE state='waiting' AND id IN ({placeholders})""",
                identifiers,
            )
        return int(cursor.rowcount)

    def disable_frame_processing(self) -> None:
        """Revoke process-local execution permission while retaining durable work."""
        with self.db:
            self._set_setting_sql("frames_backfill_active", "0")
            self._set_setting_sql("frames_backfill_updated_at", utc_now())
            self.db.execute(
                """UPDATE jobs SET state='waiting',
                result='Ожидает явного запуска обработки кадров',updated_at=datetime('now')
                WHERE kind='telegram.video_frames' AND state IN ('queued','running')"""
            )
            changed = self.db.execute(
                """UPDATE tg_video_frame_sets SET status='pending',
                error='Обработка остановлена; требуется ручной запуск',error_code='',updated_at=?
                WHERE status='running'""",
                (utc_now(),),
            ).rowcount
            if changed:
                self._set_dirty_sql()

    def start_frame_backfill(self) -> dict[str, Any]:
        complete = self.get_setting("frames_backfill_complete", "0") == "1"
        with self.db:
            if complete:
                self._set_setting_sql("frames_backfill_cursor", "0")
            self._set_setting_sql("frames_backfill_active", "1")
            self._set_setting_sql("frames_backfill_complete", "0")
            self._set_setting_sql("frames_backfill_updated_at", utc_now())
        return self.frame_stats()

    def pause_frame_backfill(self) -> dict[str, Any]:
        with self.db:
            self._set_setting_sql("frames_backfill_active", "0")
            self._set_setting_sql("frames_backfill_updated_at", utc_now())
        return self.frame_stats()

    def next_frame_backfill_downloads(self, cursor: int, limit: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.execute(
                """SELECT d.rowid AS cursor_rowid,d.source_id,d.message_id,d.author_id,
                d.file_path,d.size,sets.status AS frame_status
                FROM downloads d LEFT JOIN tg_video_frame_sets sets
                ON sets.source_id=d.source_id AND sets.message_id=d.message_id
                WHERE d.rowid>? ORDER BY d.rowid LIMIT ?""",
                (int(cursor), max(1, int(limit))),
            )
        ]

    def set_frame_backfill_cursor(self, cursor: int, *, complete: bool = False) -> None:
        with self.db:
            self._set_setting_sql("frames_backfill_cursor", str(max(0, int(cursor))))
            self._set_setting_sql("frames_backfill_updated_at", utc_now())
            if complete:
                self._set_setting_sql("frames_backfill_active", "0")
                self._set_setting_sql("frames_backfill_complete", "1")

    def retry_failed_frames(
        self, *, limit: int = 100, authorized: bool = False
    ) -> dict[str, Any]:
        rows = list(
            self.db.execute(
                """SELECT source_id,message_id FROM tg_video_frame_sets
                WHERE status IN ('failed','stale') ORDER BY source_id,message_id LIMIT ?""",
                (max(1, min(1000, int(limit))),),
            )
        )
        queued = 0
        for row in rows:
            try:
                if self.queue_frame_job(
                    int(row["source_id"]),
                    int(row["message_id"]),
                    reset_failed=True,
                    authorized=authorized,
                ) is not None:
                    queued += 1
            except ValueError:
                continue
        return {"queued": queued, **self.frame_stats()}

    def archive_root(self, preferred: str | Path | None = None, *, persist: bool = False) -> Path:
        if preferred is not None:
            root = Path(preferred).expanduser().resolve()
            if root.exists() and not root.is_dir():
                raise ValueError("Корень архива должен быть каталогом")
            if persist:
                self._persist_archive_root(root)
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
        values = (
            channel_title[:300],
            post_text[:20000],
            published_at,
            status,
            error[:500],
        )
        with self.db:
            previous = self.db.execute(
                """SELECT channel_title,post_text,published_at,status,error
                FROM tg_post_metadata WHERE source_id=?""",
                (int(source_id),),
            ).fetchone()
            changed = previous is None or tuple(previous) != values
            self.db.execute(
                """INSERT INTO tg_post_metadata(source_id,channel_title,post_text,
                published_at,fetched_at,status,error) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(source_id) DO UPDATE SET
                channel_title=excluded.channel_title,post_text=excluded.post_text,
                published_at=excluded.published_at,fetched_at=excluded.fetched_at,
                status=excluded.status,error=excluded.error""",
                (
                    int(source_id),
                    *values[:3],
                    utc_now(),
                    *values[3:],
                ),
            )
            if changed:
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
        self,
        author_id: int,
        *,
        include_path: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        profile = self.db.execute(
            "SELECT 1 FROM tg_profiles WHERE author_id=?", (int(author_id),)
        ).fetchone()
        if profile is None:
            raise ValueError("Telegram-профиль не найден в локальном архиве")
        roots = self.allowed_media_roots(self.archive_root())
        result: list[dict[str, Any]] = []
        query = """SELECT * FROM tg_profile_photos WHERE author_id=? AND status='saved'
            ORDER BY photo_date DESC,photo_id"""
        parameters: tuple[Any, ...] = (int(author_id),)
        if limit is not None:
            page_limit = int(limit)
            if not 1 <= page_limit <= 100:
                raise ValueError("limit должен быть от 1 до 100")
            query += " LIMIT ?"
            parameters = (*parameters, page_limit)
        for row in self.db.execute(
            query,
            parameters,
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
                (SELECT COUNT(*) FROM tg_profile_photos AS photos
                  WHERE photos.author_id=profiles.author_id) AS photo_count,
                (SELECT COUNT(*) FROM tg_profile_sources AS links
                  WHERE links.author_id=profiles.author_id) AS source_count,
                (SELECT GROUP_CONCAT(links.source_id) FROM tg_profile_sources AS links
                  WHERE links.author_id=profiles.author_id) AS source_ids_text
                FROM tg_profiles AS profiles
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
                COUNT(DISTINCT downloads.message_id) AS video_count,
                COUNT(DISTINCT CASE WHEN frames.status='saved'
                    THEN frames.source_id || ':' || frames.message_id END)
                    AS frame_set_count,
                COUNT(DISTINCT links.source_id) AS source_count
                FROM tg_profiles profiles {join}
                LEFT JOIN tg_profile_photos photos ON photos.author_id=profiles.author_id
                LEFT JOIN downloads ON downloads.author_id=profiles.author_id
                LEFT JOIN tg_video_frame_sets frames
                  ON frames.source_id=downloads.source_id
                 AND frames.message_id=downloads.message_id
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

    def enrichment_profile_photo_ids(self) -> list[int]:
        """Return due profiles allowed by at least one full source or legacy ownership."""
        return [
            int(row["author_id"])
            for row in self.db.execute(
                """SELECT profiles.author_id FROM tg_profiles profiles
                WHERE profiles.photos_retry_at<=? AND (
                  NOT EXISTS (
                    SELECT 1 FROM tg_profile_sources links
                    WHERE links.author_id=profiles.author_id
                  ) OR EXISTS (
                    SELECT 1 FROM tg_profile_sources links
                    JOIN sources ON sources.id=links.source_id
                    WHERE links.author_id=profiles.author_id
                      AND sources.collection_mode='full'
                  )
                ) ORDER BY profiles.photos_retry_at,profiles.author_id""",
                (time.time(),),
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
            "video_frame_sets": (
                "SELECT * FROM tg_video_frame_sets ORDER BY source_id,message_id"
            ),
            "video_frames": (
                "SELECT * FROM tg_video_frames ORDER BY source_id,message_id,position"
            ),
        }
        return {
            name: [dict(row) for row in self.db.execute(query)]
            for name, query in queries.items()
        }
