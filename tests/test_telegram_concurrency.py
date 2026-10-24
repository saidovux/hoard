"""Deterministic bounded-concurrency tests without a Telegram session or network."""
from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from telethon.errors import FloodWaitError

from hoard.models import Post
from hoard.storage import Store
from hoard.telegram import TelegramEngine, telegram_download_limit
from hoard.telegram_download import VIDEO_NOTE_PART_SIZE_KB


class ConcurrentMessage:
    def __init__(self, message_id: int, *, video: bool = True):
        self.id = message_id
        self.video_note = object() if video else None
        self.document = (
            SimpleNamespace(
                id=message_id,
                message_id=message_id,
                size=64,
                payload=bytes([message_id % 251]) * 64,
            )
            if video
            else None
        )
        self.input_chat = object()
        self.sender_id = 1000 + message_id
        self.sender = SimpleNamespace(
            username=f"user{message_id}",
            first_name="Test",
            last_name=str(message_id),
        )
        self.date = datetime(2026, 1, 1, tzinfo=UTC)


class ConcurrentClient:
    """A controllable Telethon double implementing the Round 1 adapter contract."""

    def __init__(
        self,
        messages: list[ConcurrentMessage],
        *,
        blocked: set[int] | None = None,
        failures: dict[int, BaseException] | None = None,
    ):
        self.messages = messages
        self.blocked = set(blocked or ())
        self.failures = dict(failures or {})
        self.gates: dict[int, asyncio.Event] = {}
        self.started: list[int] = []
        self.finished: list[int] = []
        self.download_calls: Counter[int] = Counter()
        self.request_limits: list[int] = []
        self.active = 0
        self.max_active = 0
        self.yielded = 0
        self.release_all = False

    async def is_user_authorized(self):
        return True

    async def get_entity(self, _username):
        return SimpleNamespace(broadcast=True, title="Concurrent channel")

    async def get_messages(self, _entity, ids):
        return SimpleNamespace(id=ids)

    async def iter_messages(self, _entity, reply_to, min_id, reverse):
        del reply_to, reverse
        for message in self.messages:
            if message.id > min_id:
                self.yielded += 1
                yield message
                await asyncio.sleep(0)

    def gate(self, message_id: int) -> asyncio.Event:
        gate = self.gates.setdefault(message_id, asyncio.Event())
        if self.release_all or message_id not in self.blocked:
            gate.set()
        return gate

    def release(self, message_id: int) -> None:
        self.gate(message_id).set()

    def release_everything(self) -> None:
        self.release_all = True
        for gate in self.gates.values():
            gate.set()

    async def _download_file(
        self,
        input_location,
        file,
        *,
        part_size_kb=None,
        file_size=None,
        progress_callback=None,
        dc_id=None,
        key=None,
        iv=None,
        msg_data=None,
        cdn_redirect=None,
    ):
        del dc_id, key, iv, msg_data, cdn_redirect
        message_id = int(input_location.message_id)
        self.download_calls[message_id] += 1
        self.request_limits.append(int(part_size_kb * 1024))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.append(message_id)
        if progress_callback:
            progress_callback(1, file_size)
        try:
            await self.gate(message_id).wait()
            failure = self.failures.get(message_id)
            if failure is not None:
                raise failure
            file.write(input_location.payload)
            if progress_callback:
                progress_callback(file.tell(), file_size)
        finally:
            self.active -= 1
            self.finished.append(message_id)


def make_scan(tmp_path, messages, *, limit=3, blocked=None, failures=None, progress=None):
    store = Store(tmp_path / "hoard.sqlite3")
    store.add_source(Post("channelx", 7), str(tmp_path / "archive"), 120)
    client = ConcurrentClient(messages, blocked=blocked, failures=failures)
    engine = TelegramEngine(store, progress, download_limit=limit)
    engine.connect = lambda: asyncio.sleep(0, result=client)
    return store, engine, client


async def spin_until(predicate, *, turns: int = 500) -> None:
    for _ in range(turns):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition did not become true")


def test_download_limit_configuration(monkeypatch):
    monkeypatch.delenv("HOARD_TG_DOWNLOADS", raising=False)
    monkeypatch.setenv("HOARD_MAX_JOBS", "1")
    assert telegram_download_limit() == 3
    for limit in range(1, 5):
        monkeypatch.setenv("HOARD_TG_DOWNLOADS", str(limit))
        assert telegram_download_limit() == limit
    for invalid in ("0", "5", "many", "", "1.5"):
        monkeypatch.setenv("HOARD_TG_DOWNLOADS", invalid)
        with pytest.raises(ValueError, match="HOARD_TG_DOWNLOADS.*1.*4"):
            telegram_download_limit()


async def test_reverse_completion_uses_traversal_order_with_gaps_and_plain_comments(tmp_path):
    messages = [
        ConcurrentMessage(101),
        ConcurrentMessage(103, video=False),
        ConcurrentMessage(105),
        ConcurrentMessage(109),
    ]
    progress: list[dict] = []
    store, engine, client = make_scan(
        tmp_path,
        messages,
        blocked={101, 105, 109},
        progress=progress.append,
    )
    scan = asyncio.create_task(engine.scan(store.sources()[0]))
    await spin_until(lambda: set(client.started) == {101, 105, 109})

    assert client.max_active == 3
    assert store.sources()[0]["last_id"] == 0
    client.release(109)
    await spin_until(lambda: store.seen(store.sources()[0]["id"], 109))
    client.release(105)
    await spin_until(lambda: store.seen(store.sources()[0]["id"], 105))
    assert store.sources()[0]["last_id"] == 0

    client.release(101)
    assert await scan == (4, 3)
    assert store.sources()[0]["last_id"] == 109
    assert client.request_limits == [VIDEO_NOTE_PART_SIZE_KB * 1024] * 3
    assert any(item.get("active_downloads") == 3 for item in progress)
    assert progress[-1] == {}
    store.close()


@pytest.mark.parametrize("limit", [1, 2, 3, 4])
async def test_configured_transfer_limit_is_never_exceeded(tmp_path, limit):
    messages = [ConcurrentMessage(message_id) for message_id in range(101, 107)]
    ids = {message.id for message in messages}
    store, engine, client = make_scan(tmp_path, messages, limit=limit, blocked=ids)
    scan = asyncio.create_task(engine.scan(store.sources()[0]))

    await spin_until(lambda: len(client.started) == limit)
    for _ in range(10):
        await asyncio.sleep(0)
    assert len(client.started) == limit
    client.release_everything()

    assert await scan == (6, 6)
    assert client.max_active == limit
    store.close()


async def test_finished_transfer_refills_one_slot(tmp_path):
    messages = [ConcurrentMessage(message_id) for message_id in (101, 105, 109)]
    store, engine, client = make_scan(
        tmp_path, messages, limit=2, blocked={101, 105, 109}
    )
    scan = asyncio.create_task(engine.scan(store.sources()[0]))
    await spin_until(lambda: client.started == [101, 105])

    client.release(101)
    await spin_until(lambda: client.started == [101, 105, 109])
    assert client.active == 2
    assert 105 not in client.finished
    client.release_everything()

    assert await scan == (3, 3)
    assert client.max_active == 2
    store.close()


async def test_discovery_is_bounded_and_cancellation_cleans_children(tmp_path):
    messages = [ConcurrentMessage(1)] + [
        ConcurrentMessage(message_id, video=False) for message_id in range(2, 1001)
    ]
    store, engine, client = make_scan(tmp_path, messages, blocked={1})
    scan = asyncio.create_task(engine.scan(store.sources()[0]))
    await spin_until(lambda: client.yielded == 4 * engine.download_limit)

    assert client.yielded == 4 * engine.download_limit
    scan.cancel()
    with pytest.raises(asyncio.CancelledError):
        await scan

    assert client.active == 0
    assert not list((tmp_path / "archive").rglob("*.part"))
    assert store.db.execute("SELECT COUNT(*) FROM tg_media_failures").fetchone()[0] == 0
    current = asyncio.current_task()
    leaked = [
        task
        for task in asyncio.all_tasks()
        if task is not current
        and not task.done()
        and task.get_name().startswith("hoard-tg-download-")
    ]
    assert leaked == []
    assert engine.progress == {}
    store.close()


async def test_cancelled_scan_can_restart_without_partial_or_failure_rows(tmp_path):
    messages = [ConcurrentMessage(message_id) for message_id in (101, 105, 109, 113)]
    ids = {message.id for message in messages}
    store, engine, client = make_scan(tmp_path, messages, blocked=ids)
    scan = asyncio.create_task(engine.scan(store.sources()[0]))
    await spin_until(lambda: len(client.started) == 3)

    scan.cancel()
    with pytest.raises(asyncio.CancelledError):
        await scan
    assert store.sources()[0]["last_id"] == 0
    assert store.db.execute("SELECT COUNT(*) FROM downloads").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM tg_media_failures").fetchone()[0] == 0

    client.release_everything()
    assert await engine.scan(store.sources()[0]) == (4, 4)
    assert store.sources()[0]["last_id"] == 113
    assert store.db.execute("SELECT COUNT(*) FROM downloads").fetchone()[0] == 4
    assert not list((tmp_path / "archive").rglob("*.part"))
    store.close()


async def test_early_failure_keeps_later_successes_and_restart_deduplicates(tmp_path):
    messages = [ConcurrentMessage(message_id) for message_id in (101, 105, 109)]
    store, engine, client = make_scan(
        tmp_path,
        messages,
        failures={101: OSError("synthetic failure")},
    )

    assert await engine.scan(store.sources()[0]) == (3, 2)
    source = store.sources()[0]
    assert source["last_id"] == 0
    assert store.seen(source["id"], 105)
    assert store.seen(source["id"], 109)

    calls = client.download_calls.copy()
    assert await engine.scan(source) == (3, 0)
    assert client.download_calls[105] == calls[105]
    assert client.download_calls[109] == calls[109]

    store.db.execute("UPDATE tg_media_failures SET retry_at=0")
    store.db.commit()
    client.failures.clear()
    assert await engine.scan(store.sources()[0]) == (3, 1)
    assert store.sources()[0]["last_id"] == 109
    assert client.download_calls[105] == calls[105]
    assert client.download_calls[109] == calls[109]
    store.close()


async def test_flood_wait_stops_new_transfers_and_settles_started_ones(tmp_path):
    messages = [ConcurrentMessage(message_id) for message_id in (101, 105, 109)]
    store, engine, client = make_scan(
        tmp_path,
        messages,
        limit=2,
        blocked={101, 105, 109},
        failures={101: FloodWaitError(request=None, capture=9)},
    )
    scan = asyncio.create_task(engine.scan(store.sources()[0]))
    await spin_until(lambda: client.started == [101, 105])

    client.release(101)
    await spin_until(lambda: 101 in client.finished)
    for _ in range(10):
        await asyncio.sleep(0)
    assert 109 not in client.started
    assert not scan.done()

    client.release(105)
    with pytest.raises(FloodWaitError):
        await scan
    source = store.sources()[0]
    assert source["last_id"] == 0
    assert store.seen(source["id"], 105)
    assert store.db.execute("SELECT COUNT(*) FROM tg_media_failures").fetchone()[0] == 0
    assert client.active == 0
    store.close()


async def test_iteration_flood_wait_also_settles_in_flight_transfers(tmp_path):
    class IterationFloodClient(ConcurrentClient):
        async def iter_messages(self, _entity, reply_to, min_id, reverse):
            del reply_to, reverse
            for message in self.messages[:2]:
                if message.id > min_id:
                    self.yielded += 1
                    yield message
                    await asyncio.sleep(0)
            raise FloodWaitError(request=None, capture=9)

    messages = [ConcurrentMessage(message_id) for message_id in (101, 105, 109)]
    store = Store(tmp_path / "hoard.sqlite3")
    store.add_source(Post("channelx", 7), str(tmp_path / "archive"), 120)
    client = IterationFloodClient(messages, blocked={101, 105, 109})
    engine = TelegramEngine(store, download_limit=3)
    engine.connect = lambda: asyncio.sleep(0, result=client)
    scan = asyncio.create_task(engine.scan(store.sources()[0]))
    await spin_until(lambda: client.started == [101, 105])

    assert not scan.done()
    assert 109 not in client.started
    client.release_everything()
    with pytest.raises(FloodWaitError):
        await scan

    source = store.sources()[0]
    assert source["last_id"] == 105
    assert store.seen(source["id"], 101)
    assert store.seen(source["id"], 105)
    assert store.db.execute("SELECT COUNT(*) FROM tg_media_failures").fetchone()[0] == 0
    assert client.active == 0
    store.close()


def test_progress_tracks_individual_and_aggregate_transfers():
    reports: list[dict] = []
    engine = TelegramEngine(store=None, report_progress=reports.append, download_limit=2)
    first = (1, 101)
    second = (1, 105)

    engine._begin_download_progress(first, message_id=101, filename="101.mp4")
    engine._update_download_progress(first, current=10, total=100, speed_bps=100.0)
    engine._begin_download_progress(second, message_id=105, filename="105.mp4")
    engine._update_download_progress(second, current=20, total=100, speed_bps=250.0)

    assert reports[-1]["active_downloads"] == 2
    assert reports[-1]["file"] == "105.mp4"
    assert reports[-1]["speed_bps"] == 250.0
    assert reports[-1]["aggregate_speed_bps"] == 350.0

    engine._finish_download_progress(second)
    assert reports[-1]["active_downloads"] == 1
    assert reports[-1]["file"] == "101.mp4"
    assert reports[-1]["speed_bps"] == 100.0
    assert reports[-1]["aggregate_speed_bps"] == 100.0
    engine._finish_download_progress(first)
    assert reports[-1] == {}
