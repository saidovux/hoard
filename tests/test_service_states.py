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


async def test_service_builds_report_while_collector_is_stopped(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.add_source(Post("channelx", 7), str(tmp_path / "archive"))
    service = Service(store=store, engine=FakeEngine())

    result = await service.handle("telegram_report", {})

    assert "HTML-каталог обновлён" in result["message"]
    assert (tmp_path / "archive" / "telegram_archive.html").is_file()
    assert service.running is False
    store.close()
