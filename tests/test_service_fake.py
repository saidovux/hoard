"""No Telegram credentials / network needed for these tests."""
import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

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

    async def download_media(self, msg, file, progress_callback):
        file.write(b"mock-video")
        progress_callback(10, 10)
        return file


class FakeMessage:
    def __init__(self, id, video):
        self.id, self.video_note = id, object() if video else None
        self.sender_id = 101
        self.sender = SimpleNamespace(username="example", first_name="Test", last_name="User")
        self.date = datetime(2026, 1, 1, tzinfo=UTC)


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
    missing = tmp_path / "channelx" / "7" / "101" / "msg_22.mp4"
    missing.unlink()
    assert store.earliest_missing_download(sid) == 22
    assert await engine.scan(store.sources()[0]) == (2, 1)
    assert missing.read_bytes() == b"mock-video"
    assert store.stats()["downloads"] == 2
    store.close()

async def test_truncated_download_is_replaced(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.add_source(Post("channelx", 7), str(tmp_path), 120)
    engine = TelegramEngine(store)
    fake = FakeClient([FakeMessage(22, True)])
    engine.connect = lambda: asyncio.sleep(0, result=fake)
    assert await engine.scan(store.sources()[0]) == (1, 1)
    file = tmp_path / "channelx" / "7" / "101" / "msg_22.mp4"
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
