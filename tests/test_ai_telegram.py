"""Round-five Telegram AI and PSL contracts without live Telegram or DeepSeek."""

from __future__ import annotations

import json
import stat

import pytest

from hoard.ai import AICore, AIResponse, AIUsage
from hoard.ai.errors import AIConnectionError
from hoard.ai.provider import AIProvider
from hoard.ai.psl import validate_psl_result
from hoard.service import Service
from hoard.storage import Store
from hoard.telegram import AuthState


class FakeEngine:
    authorization_step = AuthState.DISCONNECTED

    async def disconnect(self):
        return None


class SequenceProvider(AIProvider):
    name = "deepseek"

    def __init__(self, *results):
        self.results = list(results)
        self.requests = []

    @property
    def configured(self):
        return True

    async def complete(self, request):
        self.requests.append(request)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def psl_response(
    *,
    status="completed",
    presentation="female",
    age=25,
    raw_min=4.0,
    raw_max=5.0,
    boosted=6.0,
    usage=None,
):
    content = json.dumps(
        {
            "status": status,
            "presentation": presentation,
            "estimated_age": age,
            "raw_psl_min": raw_min,
            "raw_psl_max": raw_max,
            "boosted_psl": boosted,
        }
    )
    return AIResponse(
        "deepseek",
        "deepseek-chat",
        content,
        "stop",
        request_id="request-1",
        usage=usage,
    )


def telegram_service(tmp_path, provider):
    archive = tmp_path / "archive"
    archive.mkdir()
    first = archive / "first.png"
    second = archive / "second.png"
    first.write_bytes(b"\x89PNG\r\n\x1a\nfirst-image")
    second.write_bytes(b"\x89PNG\r\n\x1a\nsecond-image")
    store = Store(tmp_path / "hoard.sqlite3")
    store.telegram._persist_archive_root(archive)
    with store.db:
        store.db.execute(
            "INSERT INTO tg_profiles(author_id,display_name) VALUES(101,'Synthetic')"
        )
        store.db.execute(
            "INSERT INTO tg_profiles(author_id,display_name) VALUES(202,'Other')"
        )
        store.db.execute(
            """INSERT INTO tg_profile_photos(author_id,photo_id,file_path,size)
            VALUES(101,'first',?,?)""",
            (str(first), first.stat().st_size),
        )
        store.db.execute(
            """INSERT INTO tg_profile_photos(author_id,photo_id,file_path,size)
            VALUES(202,'second',?,?)""",
            (str(second), second.stat().st_size),
        )
    service = Service(
        store=store,
        engine=FakeEngine(),
        ai_core=AICore({"deepseek": provider}),
    )
    return service, store, first


async def start_analysis(service, **overrides):
    values = {
        "author_id": 101,
        "photo_ids": ["first"],
        "content": "Analyze the selected adult profile.",
        "operation_id": "operation-1",
        "adult_confirmed": True,
        "external_confirmed": True,
        **overrides,
    }
    return (await service.handle("ai_telegram_psl_start", values))["analysis"]


async def test_telegram_ai_automatically_admits_adult_and_requires_external_confirmation(tmp_path):
    service, store, _photo = telegram_service(tmp_path, SequenceProvider(psl_response()))
    analysis = await start_analysis(service, adult_confirmed=False)
    assert analysis["adult_confirmed"] == 1
    with pytest.raises(ValueError, match="внешнему"):
        await start_analysis(
            service, operation_id="external-not-confirmed", external_confirmed=False
        )
    with pytest.raises(ValueError, match="не принадлежит"):
        await start_analysis(service, operation_id="wrong-photo", photo_ids=["second"])
    assert store.db.execute("SELECT COUNT(*) FROM ai_telegram_analyses").fetchone()[0] == 1
    assert store.db.execute("SELECT COUNT(*) FROM jobs WHERE kind='ai.chat'").fetchone()[0] == 1
    store.close()


async def test_psl_snapshot_idempotency_and_atomic_result(tmp_path):
    usage = AIUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    provider = SequenceProvider(psl_response(usage=usage))
    service, store, source_photo = telegram_service(tmp_path, provider)
    first = await start_analysis(service)
    repeated = await start_analysis(service)
    assert repeated["id"] == first["id"]
    assert store.db.execute("SELECT COUNT(*) FROM ai_telegram_analyses").fetchone()[0] == 1
    assert store.db.execute("SELECT COUNT(*) FROM jobs WHERE kind='ai.chat'").fetchone()[0] == 1

    source_photo.write_bytes(b"\x89PNG\r\n\x1a\nchanged-after-queue")
    await service.ai_service.execute_attempt(first["attempt_id"])
    saved = store.ai.telegram_analysis(first["id"])
    assert saved["result_status"] == "completed"
    assert saved["presentation"] == "female"
    assert saved["raw_psl_min"] == 4.0
    assert saved["actual_model"] == "deepseek-chat"
    assert saved["attempt"]["state"] == "completed"
    assert saved["attempt"]["prompt_tokens"] == 10
    assert provider.requests[0].messages[-1].images[0].data.endswith(b"first-image")

    reopened = await service.handle("ai_telegram_analysis_get", {"id": first["id"]})
    assert reopened["analysis"]["id"] == first["id"]
    assert len(provider.requests) == 1
    store.close()


async def test_invalid_minor_result_is_terminal_and_never_scores(tmp_path):
    provider = SequenceProvider(psl_response(age=16))
    service, store, _photo = telegram_service(tmp_path, provider)
    analysis = await start_analysis(service)
    with pytest.raises(Exception, match="PSL"):
        await service.ai_service.execute_attempt(analysis["attempt_id"])
    saved = store.ai.telegram_analysis(analysis["id"])
    assert saved["result_status"] == "invalid_result"
    assert saved["raw_psl_min"] is None
    assert saved["attempt"]["state"] == "failed"
    assert saved["attempt"]["error_code"] == "invalid_psl_result"
    store.close()


async def test_unknown_outcome_needs_linked_manual_retry(tmp_path):
    provider = SequenceProvider(AIConnectionError("lost"), psl_response())
    service, store, _photo = telegram_service(tmp_path, provider)
    original = await start_analysis(service)
    with pytest.raises(AIConnectionError):
        await service.ai_service.execute_attempt(original["attempt_id"])
    failed = store.ai.telegram_analysis(original["id"])
    assert failed["attempt"]["state"] == "outcome_unknown"
    assert len(provider.requests) == 1

    retry = (
        await service.handle(
            "ai_attempt_retry",
            {"id": original["attempt_id"], "operation_id": "manual-retry"},
        )
    )["attempt"]
    repeated = (
        await service.handle(
            "ai_attempt_retry",
            {"id": original["attempt_id"], "operation_id": "manual-retry"},
        )
    )["attempt"]
    assert retry["id"] == repeated["id"]
    retried_analysis = store.ai.telegram_analysis(retry["entity_id"])
    assert retried_analysis["root_analysis_id"] == original["id"]
    assert retried_analysis["photos"][0]["photo_id"] == "first"
    await service.ai_service.execute_attempt(retry["id"])
    assert store.ai.telegram_analysis(retry["entity_id"])["result_status"] == "completed"
    assert len(provider.requests) == 2
    store.close()


async def test_ai_config_ipc_never_returns_key(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    service, store, _photo = telegram_service(tmp_path, SequenceProvider(psl_response()))
    saved = await service.handle(
        "ai_config_set",
        {
            "provider": "deepseek",
            "api_key": "round-five-secret",
            "model": "deepseek-chat",
            "timeout": 90,
        },
    )
    visible = await service.handle("ai_config_get", {})
    assert saved["configured"] is True
    assert visible["configured"] is True
    assert "api_key" not in visible
    assert "round-five-secret" not in repr(visible)
    config_path = tmp_path / "config" / "hoard" / "ai.json"
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    store.close()


@pytest.mark.parametrize(
    "value",
    [
        {
            "status": "completed",
            "presentation": "male",
            "estimated_age": 30,
            "raw_psl_min": 1,
            "raw_psl_max": 2,
            "boosted_psl": 3,
        },
        {
            "status": "completed",
            "presentation": "female",
            "estimated_age": None,
            "raw_psl_min": 1,
            "raw_psl_max": 2,
            "boosted_psl": 3,
        },
        {
            "status": "completed",
            "presentation": "female",
            "estimated_age": 30,
            "raw_psl_min": 7,
            "raw_psl_max": 3,
            "boosted_psl": 4,
        },
    ],
)
def test_psl_domain_rejects_ineligible_or_inconsistent_scores(value):
    with pytest.raises(ValueError):
        validate_psl_result(value)
