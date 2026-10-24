"""Single-file Telegram optimization without a live account or network."""
from __future__ import annotations

import asyncio
import io
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon import TelegramClient
from telethon.errors import FileReferenceExpiredError, FloodWaitError
from telethon.sessions import StringSession
from telethon.tl import types

from hoard.archive_paths import video_note_path
from hoard.models import Post
from hoard.storage import Store
from hoard.telegram import TelegramEngine
from hoard.telegram_download import VIDEO_NOTE_PART_SIZE_KB


class SyntheticMessage:
    def __init__(self, payload: bytes, message_id: int = 22):
        self.id = message_id
        self.video_note = object()
        self.document = SimpleNamespace(size=len(payload), payload=payload)
        self.input_chat = object()
        self.sender_id = 101
        self.sender = SimpleNamespace(
            username="example",
            first_name="Test",
            last_name="User",
        )
        self.date = datetime(2026, 1, 1, tzinfo=UTC)


class OptimizedClient:
    def __init__(self, message: SyntheticMessage, *, failure: BaseException | None = None):
        self.message = message
        self.failure = failure
        self.request_limits: list[int] = []
        self.msg_data = None
        self.download_media_calls = 0
        self.final_existed_during_download = False

    async def is_user_authorized(self):
        return True

    async def get_entity(self, _username):
        return SimpleNamespace(broadcast=True, title="Channel X")

    async def get_messages(self, _entity, ids):
        return SimpleNamespace(id=ids)

    async def iter_messages(self, _entity, reply_to, min_id, reverse):
        del reply_to, reverse
        if self.message.id > min_id:
            yield self.message

    async def download_media(self, message, file, progress_callback=None):
        self.download_media_calls += 1
        file.write(message.document.payload)
        if progress_callback:
            progress_callback(len(message.document.payload), len(message.document.payload))
        return file

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
        del dc_id, key, iv, cdn_redirect
        self.msg_data = msg_data
        final = Path(file.name).with_suffix("")
        self.final_existed_during_download = final.exists()
        part_size = int(part_size_kb * 1024)
        payload = input_location.payload
        for offset in range(0, len(payload), part_size):
            self.request_limits.append(part_size)
            chunk = payload[offset:offset + part_size]
            file.write(chunk)
            if progress_callback:
                progress_callback(file.tell(), file_size)
            if self.failure is not None:
                raise self.failure


class IncompatibleClient:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.download_media_calls = 0
        self.internal_calls = 0

    async def _download_file(self, input_location, file):
        del input_location, file
        self.internal_calls += 1
        raise AssertionError("incompatible private method must not be called")

    async def download_media(self, message, file, progress_callback=None):
        self.download_media_calls += 1
        file.write(self.payload)
        if progress_callback:
            progress_callback(len(self.payload), len(self.payload))
        return file


def make_engine(tmp_path, progress=None):
    store = Store(tmp_path / "hoard.sqlite3")
    store.add_source(Post("channelx", 7), str(tmp_path / "archive"), 120)
    return store, TelegramEngine(store, progress)


async def test_optimized_download_uses_512k_and_preserves_atomic_result(tmp_path):
    payload = bytes(range(251)) * 5000
    progress = []
    store, engine = make_engine(tmp_path, progress.append)
    message = SyntheticMessage(payload)
    client = OptimizedClient(message)

    result = await engine.download_one(
        client,
        Post("channelx", 7),
        store.sources()[0],
        message,
    )

    output = Path(result["file_path"])
    assert output.read_bytes() == payload
    assert result["size"] == len(payload)
    assert client.request_limits == [512 * 1024] * 3
    assert client.msg_data == (message.input_chat, message.id)
    assert client.download_media_calls == 0
    assert client.final_existed_during_download is False
    assert not output.with_suffix(".mp4.part").exists()
    assert json.loads(output.with_suffix(".json").read_text())["size"] == len(payload)
    assert progress[-1]["bytes"] == len(payload)
    assert progress[-1]["total"] == len(payload)
    assert progress[-1]["percent"] == 100
    store.close()


async def test_incompatible_client_uses_original_download_media_fallback(tmp_path):
    payload = b"fallback-video"
    store, engine = make_engine(tmp_path)
    message = SyntheticMessage(payload)
    client = IncompatibleClient(payload)

    result = await engine.download_one(
        client,
        Post("channelx", 7),
        store.sources()[0],
        message,
    )

    assert Path(result["file_path"]).read_bytes() == payload
    assert client.download_media_calls == 1
    assert client.internal_calls == 0
    store.close()


async def test_optimized_download_cancellation_removes_partial_file(tmp_path):
    payload = b"x" * (600 * 1024)
    store, engine = make_engine(tmp_path)
    message = SyntheticMessage(payload)
    client = OptimizedClient(message, failure=asyncio.CancelledError())
    output = video_note_path(
        tmp_path / "archive",
        store.sources()[0]["id"],
        message.sender_id,
        message.id,
    )

    with pytest.raises(asyncio.CancelledError):
        await engine.download_one(
            client,
            Post("channelx", 7),
            store.sources()[0],
            message,
        )

    assert not output.exists()
    assert not output.with_suffix(".mp4.part").exists()
    assert not output.with_suffix(".json").exists()
    store.close()


async def test_optimized_network_failure_does_not_advance_checkpoint(tmp_path):
    payload = b"x" * (600 * 1024)
    store, engine = make_engine(tmp_path)
    message = SyntheticMessage(payload)
    client = OptimizedClient(message, failure=OSError("synthetic network failure"))
    engine.connect = lambda: asyncio.sleep(0, result=client)

    assert await engine.scan(store.sources()[0]) == (1, 0)

    source = store.sources()[0]
    output = video_note_path(
        tmp_path / "archive", source["id"], message.sender_id, message.id
    )
    assert source["last_id"] == 0
    assert store.seen(source["id"], message.id) is False
    assert store.db.execute("SELECT COUNT(*) FROM downloads").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM tg_media_failures").fetchone()[0] == 1
    assert not output.exists()
    assert not output.with_suffix(".mp4.part").exists()
    store.close()


async def test_optimized_flood_wait_still_stops_scan(tmp_path):
    payload = b"x" * (600 * 1024)
    store, engine = make_engine(tmp_path)
    message = SyntheticMessage(payload)
    client = OptimizedClient(
        message,
        failure=FloodWaitError(request=None, capture=9),
    )
    engine.connect = lambda: asyncio.sleep(0, result=client)

    with pytest.raises(FloodWaitError):
        await engine.scan(store.sources()[0])

    assert store.sources()[0]["last_id"] == 0
    assert store.db.execute("SELECT COUNT(*) FROM tg_media_failures").fetchone()[0] == 0
    store.close()


async def _telethon_transfer(payload: bytes, part_size_kb: int):
    client = TelegramClient(StringSession(), 1, "test")
    request_limits = []

    async def fake_call(_sender, request):
        request_limits.append(request.limit)
        await asyncio.sleep(0.004)
        chunk = payload[request.offset:request.offset + request.limit]
        return types.upload.File(types.storage.FileUnknown(), 0, chunk)

    client._call = fake_call
    stream = io.BytesIO()
    progress = []
    location = types.InputDocumentFileLocation(1, 2, b"reference", "")
    started = time.perf_counter()
    await client._download_file(
        location,
        stream,
        part_size_kb=part_size_kb,
        file_size=len(payload),
        progress_callback=lambda current, total: progress.append((current, total)),
        msg_data=("chat", 7),
    )
    elapsed = time.perf_counter() - started
    return stream.getvalue(), request_limits, progress, elapsed


async def test_real_telethon_512k_path_reduces_sequential_requests_and_latency():
    payload = bytes(range(256)) * 8192 + b"last"

    baseline = await _telethon_transfer(payload, 128)
    optimized = await _telethon_transfer(payload, VIDEO_NOTE_PART_SIZE_KB)

    assert baseline[0] == optimized[0] == payload
    assert baseline[1] == [128 * 1024] * 17
    assert optimized[1] == [512 * 1024] * 5
    assert baseline[2][-1] == optimized[2][-1] == (len(payload), len(payload))
    assert optimized[3] < baseline[3]


async def test_real_telethon_path_refreshes_expired_document_reference():
    payload = b"recovered-video"
    client = TelegramClient(StringSession(), 1, "test")
    references = []
    location = types.InputDocumentFileLocation(71, 72, b"old-reference", "")
    document = types.Document(
        id=71,
        access_hash=72,
        file_reference=b"new-reference",
        date=datetime(2026, 1, 1, tzinfo=UTC),
        mime_type="video/mp4",
        size=len(payload),
        dc_id=2,
        attributes=[],
    )

    async def fake_call(_sender, request):
        references.append(request.location.file_reference)
        if len(references) == 1:
            raise FileReferenceExpiredError(request)
        return types.upload.File(types.storage.FileUnknown(), 0, payload)

    async def fake_get_messages(chat, ids):
        assert (chat, ids) == ("chat", 7)
        return SimpleNamespace(media=types.MessageMediaDocument(document=document))

    client._call = fake_call
    client.get_messages = fake_get_messages
    stream = io.BytesIO()
    await client._download_file(
        location,
        stream,
        part_size_kb=VIDEO_NOTE_PART_SIZE_KB,
        file_size=len(payload),
        msg_data=("chat", 7),
    )

    assert stream.getvalue() == payload
    assert references == [b"old-reference", b"new-reference"]
