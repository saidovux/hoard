"""Telegram login state machine tests without network access or real credentials."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

import hoard.telegram as telegram_module
from hoard.telegram import AuthFlowError, AuthState, TelegramEngine


class AuthClient:
    def __init__(self, *, authorized: bool = False):
        self.authorized = authorized
        self.connected = True
        self.code_calls = 0
        self.code_error = None
        self.code_sign_in_errors = []
        self.password_errors = []
        self.authorization_error = None

    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def is_user_authorized(self):
        if self.authorization_error:
            raise self.authorization_error
        return self.authorized

    async def send_code_request(self, phone):
        self.code_calls += 1
        if self.code_error:
            raise self.code_error
        return SimpleNamespace(phone_code_hash="hash-for-test")

    async def sign_in(self, **kwargs):
        if "password" in kwargs:
            if self.password_errors:
                raise self.password_errors.pop(0)
        elif self.code_sign_in_errors:
            raise self.code_sign_in_errors.pop(0)
        self.authorized = True


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(telegram_module, "read_credentials", lambda: (12345, "hash"))


async def test_existing_session_is_reused(configured):
    client = AuthClient(authorized=True)
    engine = TelegramEngine(store=None)
    engine.client = client

    result = await engine.begin_authorization()

    assert result["authorized"] is True
    assert result["state"] == "authorized"
    assert client.code_calls == 0


async def test_code_is_not_sent_twice_without_force(configured):
    client = AuthClient()
    engine = TelegramEngine(store=None)
    engine.client = client

    first = await engine.send_code("+79990000000")
    second = await engine.send_code("+79990000000")

    assert first["state"] == second["state"] == "code_required"
    assert client.code_calls == 1


async def test_login_without_two_factor_auth(configured):
    client = AuthClient()
    engine = TelegramEngine(store=None)
    engine.client = client

    await engine.send_code("+79990000000")
    result = await engine.sign_in("12345")

    assert result["authorized"] is True
    assert engine.phone == engine.phone_code_hash == ""


async def test_login_with_two_factor_and_wrong_password(configured):
    client = AuthClient()
    client.code_sign_in_errors = [SessionPasswordNeededError(request=None)]
    client.password_errors = [PasswordHashInvalidError(request=None)]
    engine = TelegramEngine(store=None)
    engine.client = client

    await engine.send_code("+79990000000")
    result = await engine.sign_in("12345")
    assert result["state"] == "password_required"

    resumed = await engine.begin_authorization()
    assert resumed["state"] == "password_required"

    with pytest.raises(AuthFlowError) as wrong:
        await engine.sign_in_password("wrong")
    assert wrong.value.state == AuthState.PASSWORD_REQUIRED
    assert wrong.value.code == "password_invalid"

    result = await engine.sign_in_password("correct")
    assert result["authorized"] is True


async def test_invalid_code_can_be_retried_without_new_request(configured):
    client = AuthClient()
    client.code_sign_in_errors = [PhoneCodeInvalidError(request=None)]
    engine = TelegramEngine(store=None)
    engine.client = client

    await engine.send_code("+79990000000")
    with pytest.raises(AuthFlowError) as wrong:
        await engine.sign_in("00000")
    assert wrong.value.code == "code_invalid"
    assert engine.phone_code_hash == "hash-for-test"

    result = await engine.sign_in("12345")
    assert result["authorized"] is True
    assert client.code_calls == 1


async def test_expired_code_returns_to_phone_state(configured):
    client = AuthClient()
    client.code_sign_in_errors = [PhoneCodeExpiredError(request=None)]
    engine = TelegramEngine(store=None)
    engine.client = client

    await engine.send_code("+79990000000")
    with pytest.raises(AuthFlowError) as expired:
        await engine.sign_in("12345")

    assert expired.value.state == AuthState.PHONE_REQUIRED
    assert expired.value.code == "code_expired"
    assert engine.phone == "+79990000000"
    assert engine.phone_code_hash == ""


async def test_cancel_clears_challenge_but_not_credentials(configured):
    client = AuthClient()
    engine = TelegramEngine(store=None)
    engine.client = client
    await engine.send_code("+79990000000")

    result = await engine.cancel_authorization()

    assert result["state"] == "phone_required"
    assert engine.phone == engine.phone_code_hash == ""
    assert engine.client is None


async def test_flood_wait_is_actionable_and_does_not_create_challenge(configured):
    client = AuthClient()
    client.code_error = FloodWaitError(request=None, capture=9)
    engine = TelegramEngine(store=None)
    engine.client = client

    with pytest.raises(AuthFlowError) as limited:
        await engine.send_code("+79990000000")

    assert limited.value.code == "flood_wait"
    assert limited.value.retryable is False
    assert "9" in str(limited.value)
    assert engine.phone_code_hash == ""


async def test_network_error_while_checking_session_is_actionable(configured):
    client = AuthClient()
    client.authorization_error = OSError("private low-level detail")
    engine = TelegramEngine(store=None)
    engine.client = client

    with pytest.raises(AuthFlowError) as unavailable:
        await engine.begin_authorization()

    assert unavailable.value.code == "network"
    assert "private low-level detail" not in str(unavailable.value)
