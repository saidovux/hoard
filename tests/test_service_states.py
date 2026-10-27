import asyncio

import pytest

from hoard.models import Post
from hoard.service import Service
from hoard.storage import Store
from hoard.telegram import AuthFlowError, AuthState


class FakeEngine:
    authorization_step = AuthState.DISCONNECTED

    def __init__(self):
        self.disconnects = 0

    async def disconnect(self):
        self.disconnects += 1

    async def begin_authorization(self):
        raise AuthFlowError(
            "Неверный код",
            AuthState.CODE_REQUIRED,
            code="code_invalid",
        )


class AccountEngine(FakeEngine):
    authorization_step = AuthState.AUTHORIZED

    def __init__(self):
        super().__init__()
        self.connected = True

    def account_snapshot(self):
        return {
            "state": "connected" if self.connected else "saved",
            "connected": self.connected,
            "session_present": True,
            "verified": True,
            "profile": {"id": 101, "name": "Test User", "username": "tester"},
        }

    async def begin_authorization(self):
        self.connected = True
        self.authorization_step = AuthState.AUTHORIZED
        return {
            "state": "authorized",
            "authorized": True,
            "message": "Сессия подтверждена",
        }

    async def disconnect(self):
        await super().disconnect()
        self.connected = False
        self.authorization_step = AuthState.DISCONNECTED


async def test_pause_resume_and_stop_are_distinct_states(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    engine = FakeEngine()
    service = Service(store=store, engine=engine)

    await service.handle("start", {})
    assert (await service.handle("status", {}))["collector_state"] == "running"

    await service.handle("pause", {})
    paused = await service.handle("status", {})
    assert paused["collector_state"] == "paused"
    assert paused["running"] is False

    await service.handle("start", {})
    await service.handle("stop", {})
    stopped = await service.handle("status", {})
    assert stopped["collector_state"] == "stopped"
    assert stopped["running"] is False
    store.close()


async def test_expected_auth_errors_are_structured_for_ui(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    service = Service(store=store, engine=FakeEngine())

    result = await service.handle("auth_begin", {})

    assert result == {
        "state": "code_required",
        "authorized": False,
        "error": "Неверный код",
        "error_code": "code_invalid",
        "retryable": True,
    }
    store.close()


async def test_auth_timeout_is_bounded_and_retryable(monkeypatch, tmp_path):
    class SlowEngine(FakeEngine):
        async def begin_authorization(self):
            await asyncio.sleep(1)

    monkeypatch.setattr("hoard.service.AUTH_OPERATION_TIMEOUT", 0.01)
    store = Store(tmp_path / "db.sqlite")
    service = Service(store=store, engine=SlowEngine())

    result = await service.handle("auth_begin", {})

    assert result["state"] == "unknown"
    assert result["error_code"] == "timeout"
    assert result["retryable"] is True
    assert "VPN" in result["error"]
    store.close()


async def test_account_status_check_and_safe_disconnect_contract(monkeypatch, tmp_path):
    monkeypatch.setattr("hoard.service.read_credentials", lambda: (12345, "secret"))
    store = Store(tmp_path / "db.sqlite")
    engine = AccountEngine()
    service = Service(store=store, engine=engine)

    status = await service.handle("status_brief", {})
    assert status["telegram_account"] == {
        "state": "connected",
        "connected": True,
        "session_present": True,
        "verified": True,
        "profile": {"id": 101, "name": "Test User", "username": "tester"},
    }
    assert "secret" not in repr(status)

    checked = await service.handle("telegram_account_check", {})
    assert checked["authorized"] is True
    assert checked["account"]["state"] == "connected"

    await service.handle("start", {})
    with pytest.raises(RuntimeError, match="Сначала останови"):
        await service.handle("telegram_account_disconnect", {})
    await service.handle("pause", {})
    disconnected = await service.handle("telegram_account_disconnect", {})
    assert disconnected["account"]["state"] == "saved"
    assert service.collector_state == "paused"
    store.close()


async def test_service_builds_report_while_collector_is_stopped(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.add_source(Post("channelx", 7), str(tmp_path / "archive"))
    service = Service(store=store, engine=FakeEngine())

    result = await service.handle("telegram_report", {})

    assert "HTML-каталог обновлён" in result["message"]
    assert (tmp_path / "archive" / "telegram_archive.html").is_file()
    assert service.running is False
    store.close()


async def test_invalid_optional_ai_config_does_not_break_telegram(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOARD_AI_MAX_JOBS", "invalid")
    store = Store(tmp_path / "db.sqlite")
    service = Service(store=store, engine=FakeEngine())

    assert "запущен" in (await service.handle("start", {}))["message"].lower()
    assert (await service.handle("status", {}))["integrations"]["ai"] is False

    await service.handle("stop", {})
    store.close()


async def test_targeted_scan_queues_only_selected_source_and_deduplicates(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    first = store.add_source(Post("channelx", 7), str(tmp_path / "archive"))
    second = store.add_source(Post("channely", 8), str(tmp_path / "archive"))
    service = Service(store=store, engine=FakeEngine())

    one = await service.handle("scan_source", {"id": first})
    repeated = await service.handle("scan_source", {"id": first})

    assert one["job_id"] == repeated["job_id"]
    jobs = list(store.db.execute("SELECT kind,payload FROM jobs ORDER BY id"))
    assert len(jobs) == 1
    assert jobs[0]["kind"] == "telegram.scan"
    assert f'"source_id": {first}' in jobs[0]["payload"]
    assert str(second) not in jobs[0]["payload"]

    store.set_enabled(first, False)
    with pytest.raises(ValueError, match="включи"):
        await service.handle("scan_source", {"id": first})
    with pytest.raises(ValueError, match="не найден"):
        await service.handle("scan_source", {"id": 999})
    store.close()


async def test_status_exposes_profile_summaries_without_ui_database_access(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    source_id = store.add_source(Post("channelx", 7), str(tmp_path / "archive"))
    store.telegram.observe_profile(
        author_id=101,
        username="alice",
        display_name="Alice",
        source_id=source_id,
    )
    store.telegram.record_profile_photo(
        101,
        "photo-1",
        file_path=str(tmp_path / "archive" / "photo.jpg"),
        size=12,
        photo_date="2026-10-21",
    )
    service = Service(store=store, engine=FakeEngine())

    profiles = (await service.handle("status", {}))["profiles"]

    assert profiles[0]["author_id"] == 101
    assert profiles[0]["photo_count"] == 1
    assert profiles[0]["source_count"] == 1
    store.close()
