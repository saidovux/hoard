"""Typed UI-facing operations over Hoard's existing private IPC."""

from __future__ import annotations

from typing import Any, Protocol, TypedDict

from hoard.ipc import ensure_daemon, request


class AuthResult(TypedDict, total=False):
    state: str
    authorized: bool
    message: str
    error: str
    error_code: str
    retryable: bool


class SourceValidation(TypedDict):
    url: str
    username: str
    post_id: int
    title: str


class HoardAPI(Protocol):
    """Replaceable boundary used by every Textual workflow."""

    async def ensure_started(self) -> None: ...

    async def status(self) -> dict[str, Any]: ...

    async def auth_begin(self) -> AuthResult: ...

    async def save_credentials(self, api_id: int, api_hash: str) -> dict[str, Any]: ...

    async def auth_send_code(self, phone: str, *, force: bool = False) -> AuthResult: ...

    async def auth_submit_code(self, code: str) -> AuthResult: ...

    async def auth_submit_password(self, password: str) -> AuthResult: ...

    async def auth_cancel(self) -> AuthResult: ...

    async def validate_source(self, url: str) -> SourceValidation: ...

    async def add_source(
        self, url: str, output: str, interval: int, *, title: str = ""
    ) -> dict[str, Any]: ...

    async def set_source_enabled(self, source_id: int, enabled: bool) -> dict[str, Any]: ...

    async def scan_source(self, source_id: int) -> dict[str, Any]: ...

    async def scan_all(self) -> dict[str, Any]: ...

    async def start_collector(self) -> dict[str, Any]: ...

    async def pause_collector(self) -> dict[str, Any]: ...

    async def stop_collector(self) -> dict[str, Any]: ...

    async def rebuild_catalog(self) -> dict[str, Any]: ...

    async def enrich_telegram(self) -> dict[str, Any]: ...

    async def retry_job(self, job_id: int) -> dict[str, Any]: ...

    async def ai_call(
        self, method: str, args: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...


# Compatibility for round-two tests and third-party imports of the protocol name.
StatusAPI = HoardAPI


class IPCStatusAPI:
    """Production adapter; endpoint and authentication stay owned by ``hoard.ipc``."""

    STATUS_TIMEOUT = 4
    CONTROL_TIMEOUT = 15
    NETWORK_TIMEOUT = 90
    REPORT_TIMEOUT = 180

    async def ensure_started(self) -> None:
        await ensure_daemon()

    async def status(self) -> dict[str, Any]:
        return await request("status", timeout=self.STATUS_TIMEOUT)

    async def auth_begin(self) -> AuthResult:
        return await request("auth_begin", timeout=self.NETWORK_TIMEOUT)

    async def save_credentials(self, api_id: int, api_hash: str) -> dict[str, Any]:
        return await request(
            "credentials",
            {"api_id": api_id, "api_hash": api_hash},
            timeout=self.CONTROL_TIMEOUT,
        )

    async def auth_send_code(self, phone: str, *, force: bool = False) -> AuthResult:
        return await request(
            "auth_send_code",
            {"phone": phone, "force": force},
            timeout=self.NETWORK_TIMEOUT,
        )

    async def auth_submit_code(self, code: str) -> AuthResult:
        return await request("auth_submit_code", {"code": code}, timeout=self.NETWORK_TIMEOUT)

    async def auth_submit_password(self, password: str) -> AuthResult:
        return await request(
            "auth_submit_password", {"password": password}, timeout=self.NETWORK_TIMEOUT
        )

    async def auth_cancel(self) -> AuthResult:
        return await request("auth_cancel", timeout=self.NETWORK_TIMEOUT)

    async def validate_source(self, url: str) -> SourceValidation:
        return await request("source_validate", {"url": url}, timeout=self.NETWORK_TIMEOUT)

    async def add_source(
        self, url: str, output: str, interval: int, *, title: str = ""
    ) -> dict[str, Any]:
        return await request(
            "source_add",
            {"url": url, "output": output, "interval": interval, "title": title},
            timeout=self.CONTROL_TIMEOUT,
        )

    async def set_source_enabled(self, source_id: int, enabled: bool) -> dict[str, Any]:
        return await request(
            "source_enable",
            {"id": source_id, "enabled": enabled},
            timeout=self.CONTROL_TIMEOUT,
        )

    async def scan_source(self, source_id: int) -> dict[str, Any]:
        return await request("scan_source", {"id": source_id}, timeout=self.CONTROL_TIMEOUT)

    async def scan_all(self) -> dict[str, Any]:
        return await request("scan", timeout=self.CONTROL_TIMEOUT)

    async def start_collector(self) -> dict[str, Any]:
        return await request("start", timeout=self.CONTROL_TIMEOUT)

    async def pause_collector(self) -> dict[str, Any]:
        return await request("pause", timeout=self.CONTROL_TIMEOUT)

    async def stop_collector(self) -> dict[str, Any]:
        return await request("stop", timeout=self.CONTROL_TIMEOUT)

    async def rebuild_catalog(self) -> dict[str, Any]:
        return await request("telegram_report", timeout=self.REPORT_TIMEOUT)

    async def enrich_telegram(self) -> dict[str, Any]:
        return await request("telegram_enrich", timeout=self.CONTROL_TIMEOUT)

    async def retry_job(self, job_id: int) -> dict[str, Any]:
        return await request("job_retry", {"id": job_id}, timeout=self.CONTROL_TIMEOUT)

    async def ai_call(
        self, method: str, args: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not method.startswith("ai_"):
            raise ValueError("AI IPC method must start with ai_")
        timeout = self.NETWORK_TIMEOUT if method in {
            "ai_chat_send",
            "ai_attempt_retry",
            "ai_telegram_psl_start",
        } else self.CONTROL_TIMEOUT
        return await request(method, args or {}, timeout=timeout)
