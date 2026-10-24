"""Narrow compatibility adapter for optimized Telethon document downloads."""
from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, BinaryIO

VIDEO_NOTE_PART_SIZE_KB = 512
_REQUIRED_INTERNAL_PARAMETERS = {
    "input_location",
    "file",
    "part_size_kb",
    "file_size",
    "progress_callback",
    "msg_data",
}
_KEYWORD_INTERNAL_PARAMETERS = _REQUIRED_INTERNAL_PARAMETERS - {"input_location"}


def _compatible_internal_download(client: Any) -> Callable[..., Any] | None:
    """Return Telethon's recovery-aware primitive only for its known 1.45 contract."""
    method = getattr(client, "_download_file", None)
    if not callable(method):
        return None
    try:
        parameters = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return None
    if not _REQUIRED_INTERNAL_PARAMETERS.issubset(parameters):
        return None
    if any(
        parameters[name].kind is inspect.Parameter.POSITIONAL_ONLY
        for name in _KEYWORD_INTERNAL_PARAMETERS
    ):
        return None
    return method


async def download_video_note(
    client: Any,
    message: Any,
    stream: BinaryIO,
    progress_callback: Callable[[int, int], Any],
) -> Any:
    """Download one video note with 512 KiB requests when Telethon supports it.

    The public ``download_file`` accepts a part size but cannot pass ``msg_data``.
    Telethon needs that message context to refresh expired document file references.
    Its internal ``_download_file`` is also the primitive used by ``download_media``
    and retains DC migration, CDN redirects, retries and sender cleanup. If that
    private contract or a document size is unavailable, use the original public
    path without attempting a partially compatible optimization.
    """
    document = getattr(message, "document", None)
    try:
        file_size = int(getattr(document, "size", 0) or 0)
    except (TypeError, ValueError):
        file_size = 0
    method = _compatible_internal_download(client)
    if document is None or file_size <= 0 or method is None:
        return await client.download_media(
            message,
            file=stream,
            progress_callback=progress_callback,
        )

    input_chat = getattr(message, "input_chat", None)
    message_id = getattr(message, "id", None)
    msg_data = (
        (input_chat, message_id)
        if input_chat is not None and message_id is not None
        else None
    )
    await method(
        document,
        file=stream,
        part_size_kb=VIDEO_NOTE_PART_SIZE_KB,
        file_size=file_size,
        progress_callback=progress_callback,
        msg_data=msg_data,
    )
    # Telethon returns None for caller-owned streams. Normalize this to the public
    # download_media contract; download_one still verifies the .part file and size.
    return stream
