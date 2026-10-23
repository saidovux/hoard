"""No Telegram credentials / network needed for these tests."""
import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon.errors import FloodWaitError

from hoard.archive_paths import video_note_path
from hoard.models import Post
from hoard.storage import Store
from hoard.telegram import TelegramEngine


class FakeClient:
    def __init__(self, messages):
        self.messages = messages
        self.requests = []

    async def is_user_authorized(self):
        return True

    async def get_entity(self, username):
        return SimpleNamespace(broadcast=True, title="Channel X")

    async def get_messages(self, entity, ids):
        return SimpleNamespace(id=ids)

    async def iter_messages(self, entity, reply_to, min_id, reverse):
        self.requests.append((reply_to, min_id, reverse))
        for m in self.messages:
            if m.id > min_id:
                yield m

    async def download_media(self, msg, file, progress_callback=None):
        file.write(b"mock-video")
        if progress_callback:
            progress_callback(10, 10)
        return file


class FakeMessage:
    def __init__(self, id, video):
        self.id, self.video_note = id, object() if video else None
        self.sender_id = 101
        self.sender = SimpleNamespace(username="example", first_name="Test", last_name="User")
        self.date = datetime(2026, 1, 1, tzinfo=UTC)


class SelectiveFailureClient(FakeClient):
    def __init__(self, messages, failures):
        super().__init__(messages)
        self.failures = dict(failures)

    async def download_media(self, msg, file, progress_callback=None):
        failure = self.failures.get(msg.id)
        if failure is not None:
            raise failure
        return await super().download_media(msg, file, progress_callback)


async def test_scan_filters_and_resumes(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.add_source(Post("channelx", 7), str(tmp_path), 120)
    progress = []
    engine = TelegramEngine(store, progress.append)
    fake = FakeClient([FakeMessage(21, False), FakeMessage(22, True), FakeMessage(23, True)])
    engine.connect = lambda: asyncio.sleep(0, result=fake)
    src = store.sources()[0]
    assert await engine.scan(src) == (3, 2)
    assert store.stats()["downloads"] == 2
    assert fake.requests[0] == (7, 0, True)
    assert store.sources()[0]["last_id"] == 23
    photo_jobs = [
        job for job in store.queued_jobs(limit=20)
        if job["kind"] == "telegram.profile_photos"
    ]
    assert len(photo_jobs) == 1
    assert '"author_id": 101' in photo_jobs[0]["payload"]
    assert await engine.scan(store.sources()[0]) == (0, 0)
    assert fake.requests[1] == (7, 23, True)
    files = list(tmp_path.rglob("*.mp4"))
    assert len(files) == 2
    assert all(p.read_bytes() == b"mock-video" for p in files)
    store.close()


async def test_missing_download_is_recovered(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    sid = store.add_source(Post("channelx", 7), str(tmp_path), 120)
    engine = TelegramEngine(store)
    fake = FakeClient([FakeMessage(21, False), FakeMessage(22, True), FakeMessage(23, True)])
    engine.connect = lambda: asyncio.sleep(0, result=fake)
    assert await engine.scan(store.sources()[0]) == (3, 2)
    missing = video_note_path(tmp_path, sid, 101, 22)
    missing.unlink()
    assert store.earliest_missing_download(sid) == 22
    assert await engine.scan(store.sources()[0]) == (2, 1)
    assert missing.read_bytes() == b"mock-video"
    assert store.stats()["downloads"] == 2
    store.close()


async def test_one_failed_media_does_not_poison_source_and_is_retried(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.add_source(Post("channelx", 7), str(tmp_path / "archive"), 120)
    messages = [FakeMessage(21, False), FakeMessage(22, True), FakeMessage(23, True)]
    fake = SelectiveFailureClient(messages, {22: OSError("synthetic failure")})
    engine = TelegramEngine(store)
    engine.connect = lambda: asyncio.sleep(0, result=fake)

    assert await engine.scan(store.sources()[0]) == (3, 1)
    assert store.sources()[0]["last_id"] == 21
    assert store.seen(store.sources()[0]["id"], 23) is True
    failure = store.db.execute("SELECT * FROM tg_media_failures").fetchone()
    assert failure["message_id"] == 22
    assert failure["attempts"] == 1
    assert any("повтор через" in event["text"] for event in store.recent_events())

    assert await engine.scan(store.sources()[0]) == (2, 0)
    store.db.execute("UPDATE tg_media_failures SET retry_at=0")
    store.db.commit()
    fake.failures.clear()
    assert await engine.scan(store.sources()[0]) == (2, 1)
    assert store.sources()[0]["last_id"] == 23
    assert store.db.execute("SELECT COUNT(*) FROM tg_media_failures").fetchone()[0] == 0
    store.close()


async def test_flood_wait_still_stops_source_scan(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.add_source(Post("channelx", 7), str(tmp_path / "archive"), 120)
    fake = SelectiveFailureClient(
        [FakeMessage(22, True)],
        {22: FloodWaitError(request=None, capture=9)},
    )
    engine = TelegramEngine(store)
    engine.connect = lambda: asyncio.sleep(0, result=fake)

    with pytest.raises(FloodWaitError):
        await engine.scan(store.sources()[0])
    assert store.db.execute("SELECT COUNT(*) FROM tg_media_failures").fetchone()[0] == 0
    assert store.sources()[0]["last_id"] == 0
    store.close()


async def test_each_source_uses_its_own_archive_root(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    store.add_source(Post("channelx", 7), str(first_root), 120)
    store.add_source(Post("channely", 8), str(second_root), 120)
    engine = TelegramEngine(store)
    fake = FakeClient([FakeMessage(22, True)])
    engine.connect = lambda: asyncio.sleep(0, result=fake)

    first, second = store.sources()
    await engine.scan(first)
    await engine.scan(second)

    rows = list(store.db.execute("SELECT source_id,file_path FROM downloads ORDER BY source_id"))
    assert Path(rows[0]["file_path"]).is_relative_to(first_root)
    assert Path(rows[1]["file_path"]).is_relative_to(second_root)
    store.close()

async def test_truncated_download_is_replaced(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.add_source(Post("channelx", 7), str(tmp_path), 120)
    engine = TelegramEngine(store)
    fake = FakeClient([FakeMessage(22, True)])
    engine.connect = lambda: asyncio.sleep(0, result=fake)
    assert await engine.scan(store.sources()[0]) == (1, 1)
    file = video_note_path(tmp_path, store.sources()[0]["id"], 101, 22)
    file.write_bytes(b"x")
    assert await engine.scan(store.sources()[0]) == (1, 1)
    assert file.read_bytes() == b"mock-video"
    store.close()


async def test_source_is_resolved_before_being_saved(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    engine = TelegramEngine(store)
    fake = FakeClient([])
    engine.connect = lambda: asyncio.sleep(0, result=fake)

    result = await engine.validate_post(Post("channelx", 7))

    assert result == {
        "url": "https://t.me/channelx/7",
        "username": "channelx",
        "post_id": 7,
        "title": "Channel X",
    }
    store.close()


class ProfileClient(FakeClient):
    def __init__(self, photos, *, unavailable=False, error=None):
        super().__init__([])
        self.photos = photos
        self.unavailable = unavailable
        self.error = error
        self.photo_downloads = 0

    async def get_entity(self, username):
        return SimpleNamespace(
            broadcast=True,
            title="Channel X",
            username="current_name",
            first_name="Profile",
            last_name="User",
        )

    async def iter_profile_photos(self, entity, limit):
        if self.error is not None:
            raise self.error
        if self.unavailable:
            raise RuntimeError("private")
        for photo in self.photos[:limit]:
            yield photo

    async def download_media(self, msg, file, progress_callback=None):
        self.photo_downloads += 1
        file.write(f"photo-{msg.id}".encode())
        return file


async def test_profile_photos_are_limited_deduplicated_and_independent(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.telegram.observe_profile(author_id=101, username="old", display_name="Old")
    photos = [
        SimpleNamespace(id=index, date=datetime(2026, 1, 1, tzinfo=UTC))
        for index in range(35)
    ]
    fake = ProfileClient(photos)
    engine = TelegramEngine(store)
    engine.connect = lambda: asyncio.sleep(0, result=fake)

    assert "новых: 30" in await engine.refresh_profile_photos(101)
    assert fake.photo_downloads == 30
    assert store.db.execute("SELECT COUNT(*) FROM tg_profile_photos").fetchone()[0] == 30
    store.db.execute("UPDATE tg_profiles SET photos_retry_at=0 WHERE author_id=101")
    store.db.commit()
    assert "новых: 0" in await engine.refresh_profile_photos(101)
    assert fake.photo_downloads == 30
    profile = store.db.execute("SELECT * FROM tg_profiles WHERE author_id=101").fetchone()
    assert profile["current_username"] == "current_name"
    store.close()


async def test_unavailable_profile_photo_does_not_raise(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.telegram.observe_profile(author_id=101, username=None, display_name="Private")
    fake = ProfileClient([], unavailable=True)
    engine = TelegramEngine(store)
    engine.connect = lambda: asyncio.sleep(0, result=fake)

    result = await engine.refresh_profile_photos(101)

    assert "недоступны" in result
    row = store.db.execute(
        "SELECT photo_error,photo_error_code FROM tg_profiles"
    ).fetchone()
    assert row["photo_error"]
    assert row["photo_error_code"] == "unavailable"
    store.close()


async def test_profile_photo_privacy_error_is_classified(tmp_path):
    from telethon.errors import UserpicPrivacyRequiredError

    store = Store(tmp_path / "db.sqlite")
    store.telegram.observe_profile(author_id=101, username=None, display_name="Private")
    fake = ProfileClient([], error=UserpicPrivacyRequiredError(request=None))
    engine = TelegramEngine(store)
    engine.connect = lambda: asyncio.sleep(0, result=fake)

    result = await engine.refresh_profile_photos(101)

    assert "приватности" in result
    row = store.db.execute(
        "SELECT photo_error,photo_error_code FROM tg_profiles"
    ).fetchone()
    assert row["photo_error_code"] == "privacy"
    assert "приватности" in row["photo_error"]
    store.close()


class PostMediaClient(FakeClient):
    def __init__(self):
        super().__init__([])
        self.original = SimpleNamespace(
            id=7,
            message="Post text",
            date=datetime(2026, 2, 1, tzinfo=UTC),
            photo=SimpleNamespace(id=555),
            document=None,
        )
        self.media_downloads = 0

    async def get_messages(self, entity, ids):
        return self.original

    async def download_media(self, msg, file, progress_callback=None):
        self.media_downloads += 1
        file.write(b"post-photo")
        return file


async def test_post_metadata_and_media_are_enriched_without_duplicates(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.add_source(Post("channelx", 7), str(tmp_path / "archive"), 120)
    source = store.sources()[0]
    fake = PostMediaClient()
    engine = TelegramEngine(store)
    engine.connect = lambda: asyncio.sleep(0, result=fake)

    assert "обновлены" in await engine.enrich_source(source)
    assert store.db.execute("SELECT post_text FROM tg_post_metadata").fetchone()[0] == "Post text"
    assert "сохранено" in await engine.download_post_media(source)
    assert "уже сохранено" in await engine.download_post_media(source)
    assert fake.media_downloads == 1
    media = store.db.execute("SELECT * FROM tg_post_media").fetchone()
    assert media["media_id"] == "555"
    assert Path(media["file_path"]).read_bytes() == b"post-photo"
    store.close()
