from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from hoard.catalog import build_catalog, build_catalog_from_db
from hoard.models import Post
from hoard.service import Service
from hoard.storage import Store
from hoard.telegram import AuthState


class FakeEngine:
    authorization_step = AuthState.DISCONNECTED

    async def disconnect(self) -> None:
        return None


async def wait_until(predicate, *, timeout: float = 5) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


async def test_dirty_after_three_builds_is_reconciled_without_duplicate_active_job(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("hoard.telegram_storage.CATALOG_REPORT_COOLDOWN", 0)
    store = Store(tmp_path / "db.sqlite", migrate_legacy=False)
    archive = tmp_path / "archive"
    store.telegram.archive_root(archive, persist=True)
    service = Service(store=store, engine=FakeEngine())
    calls = 0
    active_counts: list[int] = []
    fourth_finished = threading.Event()

    def changing_build(database: Path, *, archive_root=None):
        nonlocal calls
        report = build_catalog_from_db(database, archive_root=archive_root)
        calls += 1
        concurrent = Store(database, migrate_legacy=False)
        active_counts.append(
            concurrent.db.execute(
                """SELECT COUNT(*) FROM jobs WHERE unique_key='telegram-report'
                AND state IN ('queued','running')"""
            ).fetchone()[0]
        )
        if calls <= 3:
            concurrent.telegram.observe_profile(
                author_id=700 + calls,
                username=f"change{calls}",
                display_name=f"Change {calls}",
            )
        else:
            fourth_finished.set()
        concurrent.close()
        return report

    monkeypatch.setattr("hoard.service.build_catalog_from_db", changing_build)
    await service.start()
    assert await asyncio.to_thread(fourth_finished.wait, 5)
    await wait_until(
        lambda: not store.telegram.catalog_dirty()
        and store.db.execute(
            "SELECT COUNT(*) FROM jobs WHERE kind='telegram.report' AND state='done'"
        ).fetchone()[0]
        == 2
    )

    assert calls == 4
    assert active_counts == [1, 1, 1, 1]
    assert store.db.execute(
        "SELECT COUNT(*) FROM jobs WHERE kind='telegram.report'"
    ).fetchone()[0] == 2
    await service.stop()
    store.close()


async def test_dirty_catalog_is_recovered_after_store_and_runner_restart(tmp_path):
    filename = tmp_path / "db.sqlite"
    archive = tmp_path / "archive"
    first = Store(filename, migrate_legacy=False)
    first.telegram.archive_root(archive, persist=True)
    first.close()

    store = Store(filename, migrate_legacy=False)
    assert store.telegram.catalog_dirty() is True
    service = Service(store=store, engine=FakeEngine())
    await service.start()
    await wait_until(
        lambda: (archive / "telegram_archive.html").is_file()
        and not store.telegram.catalog_dirty()
        and store.db.execute(
            "SELECT COUNT(*) FROM jobs WHERE kind='telegram.report' AND state='done'"
        ).fetchone()[0]
        == 1
    )

    assert store.db.execute(
        "SELECT COUNT(*) FROM jobs WHERE kind='telegram.report' AND state='done'"
    ).fetchone()[0] == 1
    await service.stop()
    store.close()


async def test_report_failures_use_durable_backoff_but_manual_build_bypasses_it(
    tmp_path, monkeypatch
):
    clock = [1_000.0]
    monkeypatch.setattr("hoard.telegram_storage.time.time", lambda: clock[0])
    store = Store(tmp_path / "db.sqlite", migrate_legacy=False)
    archive = tmp_path / "archive"
    store.telegram.archive_root(archive, persist=True)
    service = Service(store=store, engine=FakeEngine())

    def failed_build(_database: Path, *, archive_root=None):
        del archive_root
        raise OSError("disk full")

    monkeypatch.setattr("hoard.service.build_catalog_from_db", failed_build)
    await service.start()
    await wait_until(
        lambda: store.telegram.get_setting("catalog_report_failures") == "1"
        and store.db.execute(
            "SELECT COUNT(*) FROM jobs WHERE kind='telegram.report' AND state='failed'"
        ).fetchone()[0]
        == 1
    )
    assert float(store.telegram.get_setting("catalog_report_retry_at")) == 1_030.0
    assert "OSError: disk full" in store.telegram.get_setting("catalog_report_last_error")

    service.runner._enqueue_due_jobs()
    assert store.db.execute(
        "SELECT COUNT(*) FROM jobs WHERE kind='telegram.report'"
    ).fetchone()[0] == 1

    clock[0] = 1_031.0
    service.runner.wakeup()
    await wait_until(
        lambda: store.telegram.get_setting("catalog_report_failures") == "2"
        and store.db.execute(
            "SELECT COUNT(*) FROM jobs WHERE kind='telegram.report' AND state='failed'"
        ).fetchone()[0]
        == 2
    )
    assert float(store.telegram.get_setting("catalog_report_retry_at")) == 1_151.0
    await service.stop()

    monkeypatch.setattr("hoard.service.build_catalog_from_db", build_catalog_from_db)
    result = await service.handle("telegram_report", {"archive_root": str(archive)})
    assert "HTML-каталог обновлён" in result["message"]
    assert (archive / "telegram_archive.html").is_file()
    assert store.telegram.get_setting("catalog_report_failures") == "0"
    assert store.telegram.catalog_dirty() is False
    store.close()


async def test_cancelled_report_holds_lock_until_worker_really_finishes(tmp_path, monkeypatch):
    store = Store(tmp_path / "db.sqlite", migrate_legacy=False)
    archive = tmp_path / "archive"
    store.telegram.archive_root(archive, persist=True)
    store.telegram.set_catalog_dirty(False)
    service = Service(store=store, engine=FakeEngine())
    first_started = threading.Event()
    first_finished = threading.Event()
    second_started = threading.Event()
    release = threading.Event()
    guard = threading.Lock()
    active = 0
    maximum_active = 0
    calls = 0

    def blocking_build(_database: Path, *, archive_root=None):
        nonlocal active, maximum_active, calls
        del archive_root
        with guard:
            calls += 1
            call = calls
            active += 1
            maximum_active = max(maximum_active, active)
        (first_started if call == 1 else second_started).set()
        release.wait(5)
        with guard:
            active -= 1
        if call == 1:
            first_finished.set()
        return archive / "telegram_archive.html"

    monkeypatch.setattr("hoard.service.build_catalog_from_db", blocking_build)
    first = asyncio.create_task(service._telegram_report({}))
    assert await asyncio.to_thread(first_started.wait, 5)
    first.cancel()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert service._report_lock.locked() is True

    second = asyncio.create_task(service._telegram_report({}))
    await asyncio.sleep(0)
    assert second_started.is_set() is False
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert await asyncio.to_thread(second_started.wait, 5)
    await second

    assert first_finished.is_set() is True
    assert maximum_active == 1
    store.close()


@pytest.mark.parametrize("runner_started", [False, True])
async def test_ai_catalog_change_is_delivered_without_starting_stopped_runner(
    tmp_path, monkeypatch, runner_started
):
    store = Store(tmp_path / f"db-{runner_started}.sqlite", migrate_legacy=False)
    archive = tmp_path / f"archive-{runner_started}"
    store.telegram.archive_root(archive, persist=True)
    store.telegram.set_catalog_dirty(False)
    service = Service(store=store, engine=FakeEngine())
    built = threading.Event()

    async def finish_analysis(_attempt_id: str) -> str:
        store.telegram.observe_profile(
            author_id=701,
            username="ai_result",
            display_name="AI Result",
        )
        return "completed"

    def observed_build(database: Path, *, archive_root=None):
        report = build_catalog_from_db(database, archive_root=archive_root)
        built.set()
        return report

    monkeypatch.setattr(service.ai_service, "execute_attempt", finish_analysis)
    monkeypatch.setattr("hoard.service.build_catalog_from_db", observed_build)
    if runner_started:
        await service.start()

    assert await service._ai_chat({"attempt_id": "synthetic"}) == "completed"
    assert store.db.execute(
        """SELECT COUNT(*) FROM jobs WHERE kind='telegram.report'
        AND state IN ('queued','running')"""
    ).fetchone()[0] == 1
    if not runner_started:
        assert service.running is False
        assert built.is_set() is False
        await service.start()

    assert await asyncio.to_thread(built.wait, 5)
    await wait_until(lambda: not store.telegram.catalog_dirty())
    html = (archive / "telegram_archive.html").read_text(encoding="utf-8")
    assert '"author_id":701' in html
    await service.stop()
    store.close()


def test_archive_root_invalidation_only_tracks_normalized_root_changes(tmp_path):
    store = Store(tmp_path / "db.sqlite", migrate_legacy=False)
    first = tmp_path / "first"
    second = tmp_path / "second"
    store.telegram.archive_root(first, persist=True)
    store.telegram.set_catalog_dirty(False)
    revision = store.telegram.catalog_revision()

    equivalent = first / ".." / first.name
    assert store.telegram.archive_root(equivalent, persist=True) == first.resolve()
    assert store.telegram.catalog_revision() == revision
    assert store.telegram.catalog_dirty() is False

    store.telegram.archive_root(second, persist=True)
    assert store.telegram.catalog_revision() == revision + 1
    assert store.telegram.catalog_dirty() is True
    assert first.resolve() in store.telegram.allowed_media_roots(second)
    assert second.resolve() in store.telegram.allowed_media_roots(second)

    build_catalog(store, archive_root=second)
    assert store.telegram.catalog_dirty() is False
    rendered_revision = store.telegram.catalog_revision()
    build_catalog(store, archive_root=second)
    assert store.telegram.catalog_revision() == rendered_revision
    assert store.telegram.catalog_dirty() is False
    store.close()


def test_post_metadata_fetched_at_only_does_not_invalidate_catalog(tmp_path, monkeypatch):
    store = Store(tmp_path / "db.sqlite", migrate_legacy=False)
    source_id = store.add_source(
        Post("channelx", 7),
        str(tmp_path / "archive"),
    )
    timestamps = iter(("2026-01-01", "2026-01-02", "2026-01-03"))
    monkeypatch.setattr("hoard.telegram_storage.utc_now", lambda: next(timestamps))
    metadata = {
        "channel_title": "Channel X",
        "post_text": "Stable text",
        "published_at": "2025-01-01",
    }

    store.telegram.record_post_metadata(source_id, **metadata)
    store.telegram.set_catalog_dirty(False)
    revision = store.telegram.catalog_revision()
    store.telegram.record_post_metadata(source_id, **metadata)

    assert store.telegram.catalog_dirty() is False
    assert store.telegram.catalog_revision() == revision
    assert store.db.execute(
        "SELECT fetched_at FROM tg_post_metadata WHERE source_id=?", (source_id,)
    ).fetchone()[0] == "2026-01-02"

    store.telegram.record_post_metadata(
        source_id,
        **{**metadata, "post_text": "Changed text"},
    )
    assert store.telegram.catalog_dirty() is True
    assert store.telegram.catalog_revision() == revision + 1
    store.close()
