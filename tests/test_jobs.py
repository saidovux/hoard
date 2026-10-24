import asyncio
import sqlite3

from hoard.jobs import JobRunner
from hoard.storage import Store


async def wait_for(predicate, seconds=3):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0.02)


async def test_queue_executes_generic_tasks_and_persists(tmp_path):
    store = Store(tmp_path / "hoard.sqlite3")
    calls = []

    async def example(payload):
        calls.append(payload)
        return "ok"

    jid = store.enqueue("demo", {"hello": "world"})
    runner = JobRunner(store, {"demo": example})
    await runner.start()
    await wait_for(lambda: store.recent_jobs()[0]["state"] == "done")
    await runner.stop()
    assert calls == [{"hello": "world"}]
    assert store.recent_jobs()[0]["attempts"] == 1
    store.close()
    again = Store(tmp_path / "hoard.sqlite3")
    assert again.recent_jobs()[0]["id"] == jid
    again.close()


async def test_interrupted_job_is_retried_not_lost(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    started = asyncio.Event()

    async def hanging(payload):
        started.set()
        await asyncio.Future()

    store.enqueue("sleep", {})
    runner = JobRunner(store, {"sleep": hanging})
    await runner.start()
    await wait_for(started.is_set)
    await runner.stop()
    assert store.recent_jobs()[0]["state"] == "queued"
    calls = []

    async def ok(payload):
        calls.append(1)
        return "done"

    runner2 = JobRunner(store, {"sleep": ok})
    await runner2.start()
    await wait_for(lambda: store.recent_jobs()[0]["state"] == "done")
    await runner2.stop()
    assert calls == [1] and store.recent_jobs()[0]["attempts"] == 2
    store.close()


async def test_deduplicates_same_source_while_queued(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    a = store.enqueue("telegram.scan", {"source_id": 4}, unique_key="telegram:4")
    b = store.enqueue("telegram.scan", {"source_id": 4}, unique_key="telegram:4")
    assert a == b
    store.mark_running(a)
    assert store.enqueue("telegram.scan", {"source_id": 4}, unique_key="telegram:4") == a
    store.finish_job(a, "done", "ok")
    assert store.enqueue("telegram.scan", {"source_id": 4}, unique_key="telegram:4") != a
    store.close()


def test_existing_profiles_get_photo_jobs_without_rescanning_sources(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    store.telegram.observe_profile(
        author_id=101,
        username="example",
        display_name="Example User",
        observed_at="2026-01-01T00:00:00+00:00",
    )
    async def photos(_payload):
        return "ok"

    runner = JobRunner(store, {"telegram.profile_photos": photos})
    runner._enqueue_due_jobs()

    assert store.db.execute(
        "SELECT COUNT(*) FROM jobs WHERE kind='telegram.scan'"
    ).fetchone()[0] == 0
    assert store.db.execute(
        "SELECT COUNT(*) FROM jobs WHERE kind='telegram.profile_photos'"
    ).fetchone()[0] == 1
    job = store.recent_jobs()[0]
    assert job["state"] == "queued"
    payload = store.db.execute(
        "SELECT payload FROM jobs WHERE kind='telegram.profile_photos'"
    ).fetchone()["payload"]
    assert payload == '{"author_id": 101}'
    store.close()


async def test_failed_job_requires_explicit_retry(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    async def bad(_):
        raise ValueError("intentional")
    jid = store.enqueue("fake", {})
    runner = JobRunner(store, {"fake": bad})
    await runner.start()
    await wait_for(lambda: store.recent_jobs()[0]["state"] == "failed")
    await runner.stop()
    store.retry_job(jid)
    assert store.recent_jobs()[0]["state"] == "queued"
    store.close()


async def test_deferred_catalog_report_does_not_block_other_queued_work(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("hoard.telegram_storage.time.time", lambda: 1_000.0)
    store = Store(tmp_path / "test.sqlite3", migrate_legacy=False)
    store.telegram.set_catalog_dirty()
    store.telegram.record_catalog_report_failure("OSError: disk full", now=1_000.0)
    report_id = store.enqueue("telegram.report", {}, unique_key="telegram-report")
    demo_id = store.enqueue("demo", {})
    demo_finished = asyncio.Event()
    report_calls = 0

    async def report(_payload):
        nonlocal report_calls
        report_calls += 1
        return "unexpected"

    async def demo(_payload):
        demo_finished.set()
        return "done"

    runner = JobRunner(
        store,
        {"telegram.report": report, "demo": demo},
        parallel=1,
    )
    await runner.start()
    await asyncio.wait_for(demo_finished.wait(), timeout=2)
    await wait_for(
        lambda: store.db.execute("SELECT state FROM jobs WHERE id=?", (demo_id,)).fetchone()[0]
        == "done"
    )
    await runner.stop()

    assert report_calls == 0
    assert store.db.execute(
        "SELECT state FROM jobs WHERE id=?", (report_id,)
    ).fetchone()[0] == "queued"
    store.close()


async def test_mark_running_error_recovers_and_runner_still_stops(tmp_path):
    store = Store(tmp_path / "test.sqlite3", migrate_legacy=False)
    finished = asyncio.Event()

    async def demo(_payload):
        finished.set()
        return "done"

    job_id = store.enqueue("demo", {})
    runner = JobRunner(store, {"demo": demo}, parallel=1)
    original_mark = store.mark_running
    calls = 0

    def flaky_mark(jid):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("database is locked")
        return original_mark(jid)

    store.mark_running = flaky_mark
    runner._idle_timeout = 0.05

    await runner.start()
    await asyncio.wait_for(finished.wait(), 3)
    await wait_for(
        lambda: store.db.execute(
            "SELECT state FROM jobs WHERE id=?", (job_id,)
        ).fetchone()[0] == "done"
    )
    assert runner.running is True
    assert calls >= 2

    await runner.stop()

    assert runner.running is False
    store.close()


async def test_scheduler_error_recovers_and_runner_still_stops(tmp_path):
    store = Store(tmp_path / "test.sqlite3", migrate_legacy=False)
    finished = asyncio.Event()

    async def demo(_payload):
        finished.set()
        return "done"

    job_id = store.enqueue("demo", {})
    runner = JobRunner(store, {"demo": demo}, parallel=1)
    original = runner._enqueue_due_jobs
    calls = 0

    def flaky_scheduler():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("database is locked")
        original()

    runner._enqueue_due_jobs = flaky_scheduler
    runner._scheduler_error_delay = 0.01
    runner._idle_timeout = 0.05

    await runner.start()
    await asyncio.wait_for(finished.wait(), 3)
    await wait_for(
        lambda: store.db.execute(
            "SELECT state FROM jobs WHERE id=?", (job_id,)
        ).fetchone()[0] == "done"
    )
    assert runner.running is True
    assert any(
        "scheduler" in event["text"]
        for event in store.recent_events()
    )

    await runner.stop()

    assert runner.running is False
    assert store.db.execute(
        "SELECT state FROM jobs WHERE id=?", (job_id,)
    ).fetchone()[0] == "done"
    store.close()


def test_retry_returns_existing_active_job_instead_of_integrity_error(tmp_path):
    store = Store(tmp_path / "test.sqlite3", migrate_legacy=False)
    old = store.enqueue("telegram.report", {}, unique_key="telegram-report")
    store.mark_running(old)
    store.finish_job(old, "failed", "OSError: disk full")
    active = store.enqueue("telegram.report", {}, unique_key="telegram-report")

    assert store.retry_job(old) == active

    assert store.db.execute(
        "SELECT state FROM jobs WHERE id=?", (old,)
    ).fetchone()[0] == "failed"
    assert store.db.execute(
        "SELECT state FROM jobs WHERE id=?", (active,)
    ).fetchone()[0] == "queued"
    store.close()
