from __future__ import annotations

import asyncio
import json
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


async def test_repeated_cancellation_keeps_report_lock_until_worker_finishes(
    tmp_path, monkeypatch
):
    store = Store(tmp_path / "db.sqlite", migrate_legacy=False)
    archive = tmp_path / "archive"
    store.telegram.archive_root(archive, persist=True)
    store.telegram.set_catalog_dirty(False)
    service = Service(store=store, engine=FakeEngine())

    first_started = threading.Event()
    second_started = threading.Event()
    release = threading.Event()
    calls = 0

    def blocking_build(_database: Path, *, archive_root=None):
        nonlocal calls
        del archive_root
        calls += 1
        (first_started if calls == 1 else second_started).set()
        release.wait(5)
        return archive / "telegram_archive.html"

    monkeypatch.setattr("hoard.service.build_catalog_from_db", blocking_build)

    first = asyncio.create_task(service._telegram_report({}))
    assert await asyncio.to_thread(first_started.wait, 5)
    first.cancel()
    await asyncio.sleep(0.01)
    assert service._report_lock.locked() is True

    # This is the regression: the second cancel used to interrupt ``await
    # worker`` and release the lock while the thread was still blocked.
    first.cancel()
    await asyncio.sleep(0.01)
    assert service._report_lock.locked() is True
    assert first.done() is False

    second = asyncio.create_task(service._telegram_report({}))
    await asyncio.sleep(0.05)
    assert second_started.is_set() is False

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second

    assert calls == 2
    assert second_started.is_set() is True
    store.close()


async def test_concurrent_service_stop_cancels_report_once(tmp_path, monkeypatch):
    store = Store(tmp_path / "db.sqlite", migrate_legacy=False)
    archive = tmp_path / "archive"
    store.telegram.archive_root(archive, persist=True)
    service = Service(store=store, engine=FakeEngine())
    started = threading.Event()
    release = threading.Event()

    def blocking_build(_database: Path, *, archive_root=None):
        del archive_root
        started.set()
        release.wait(5)
        return archive / "telegram_archive.html"

    monkeypatch.setattr("hoard.service.build_catalog_from_db", blocking_build)

    await service.start()
    job_id = store.enqueue("telegram.report", {}, unique_key="telegram-report")
    service.runner.wakeup()
    assert await asyncio.to_thread(started.wait, 5)

    first_stop = asyncio.create_task(service.stop())
    await asyncio.sleep(0.05)
    second_stop = asyncio.create_task(service.stop())
    await asyncio.sleep(0.05)

    # The worker is still inside to_thread; neither the lock nor the job state
    # may run ahead of the real writer.
    assert service._report_lock.locked() is True
    assert store.db.execute(
        "SELECT state FROM jobs WHERE id=?", (job_id,)
    ).fetchone()[0] == "running"

    release.set()
    await asyncio.gather(first_stop, second_stop)

    assert service.running is False
    assert service._report_lock.locked() is False
    assert store.db.execute(
        "SELECT state FROM jobs WHERE id=?", (job_id,)
    ).fetchone()[0] == "queued"
    store.close()


def test_stale_build_cannot_overwrite_a_newer_catalog(tmp_path, monkeypatch):
    import hoard.catalog as catalog_module

    store = Store(tmp_path / "db.sqlite", migrate_legacy=False)
    root = tmp_path / "archive"
    store.add_source(Post("channelx", 7), str(root))
    store.telegram.set_catalog_dirty(False)
    base_revision = store.telegram.catalog_revision()

    original_bundle = catalog_module._bundle_html
    first_bundled = threading.Event()
    release_first = threading.Event()
    guard = threading.Lock()
    bundles = 0

    def controlled_bundle(index, *, style, data, app):
        nonlocal bundles
        content = original_bundle(index, style=style, data=data, app=app)
        with guard:
            bundles += 1
            position = bundles
        if position == 1:
            first_bundled.set()
            release_first.wait(5)
        return content

    monkeypatch.setattr(catalog_module, "_bundle_html", controlled_bundle)
    results: dict[str, Path] = {}
    errors: list[BaseException] = []

    def build(label: str) -> None:
        try:
            results[label] = build_catalog_from_db(
                store.filename, archive_root=root
            )
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=build, args=("old",), daemon=True)
    first.start()
    assert first_bundled.wait(5)

    # The first builder already holds its snapshot.  Mutate only now, then let
    # a second builder queue behind the same catalog lock.
    store.telegram.observe_profile(
        author_id=901,
        username="newer",
        display_name="Newer State",
    )
    expected_revision = store.telegram.catalog_revision()
    second = threading.Thread(target=build, args=("new",), daemon=True)
    second.start()
    second.join(0.1)
    assert second.is_alive() is True
    with guard:
        assert bundles == 1

    release_first.set()
    first.join(5)
    second.join(5)
    assert first.is_alive() is False
    assert second.is_alive() is False
    assert errors == []

    final = results["new"].read_text(encoding="utf-8")
    marker = "window.HOARD_CATALOG="
    payload = json.loads(final.split(marker, 1)[1].split(";\n", 1)[0])
    assert base_revision < expected_revision
    assert payload["revision"] == expected_revision
    assert [item["author_id"] for item in payload["profiles"]] == [901]
    assert store.telegram.catalog_dirty() is False
    store.close()


def test_successful_report_clears_previous_error(tmp_path):
    store = Store(tmp_path / "db.sqlite", migrate_legacy=False)
    store.telegram.record_catalog_report_failure("OSError: disk full", now=1_000.0)
    assert store.telegram.get_setting("catalog_report_last_error") == (
        "OSError: disk full"
    )

    store.telegram.record_catalog_report_success(now=1_001.0)

    assert store.telegram.get_setting("catalog_report_failures") == "0"
    assert store.telegram.get_setting("catalog_report_last_error") == ""
    assert float(store.telegram.get_setting("catalog_report_retry_at")) == 1_003.0
    store.close()


async def test_explicit_report_retry_bypasses_backoff(tmp_path):
    store = Store(tmp_path / "db.sqlite", migrate_legacy=False)
    archive = tmp_path / "archive"
    store.telegram.archive_root(archive, persist=True)
    service = Service(store=store, engine=FakeEngine())
    job_id = store.enqueue("telegram.report", {}, unique_key="telegram-report")
    store.mark_running(job_id)
    store.finish_job(job_id, "failed", "OSError: disk full")
    store.telegram.record_catalog_report_failure("OSError: disk full")
    assert store.telegram.catalog_report_due() is False

    result = await service.handle("job_retry", {"id": job_id})

    assert result["job_id"] == job_id
    assert store.telegram.catalog_report_due() is True
    assert store.db.execute(
        "SELECT state FROM jobs WHERE id=?", (job_id,)
    ).fetchone()[0] == "queued"
    store.close()
