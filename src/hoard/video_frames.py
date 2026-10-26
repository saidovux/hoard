"""Bounded local FFmpeg extraction for Telegram video-note JPEG frames.

The module deliberately contains no Telegram or AI imports.  It can therefore be
used by the daemon's media runner and tested with synthetic MP4 files alone.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FRAME_ALGORITHM_VERSION = 1
FRAME_POSITIONS = (0.20, 0.50, 0.80)
DEFAULT_FRAME_TIMEOUT = 60.0
_MAX_DETAIL = 500
_PROCESS_STOP_TIMEOUT = 3.0
_SCALE_FILTER = (
    "scale=w='min(1280,iw)':h='min(1280,ih)':force_original_aspect_ratio=decrease"
)


class FrameExtractionError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        detail: str = "",
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.retryable = bool(retryable)
        self.detail = str(detail)[:_MAX_DETAIL]


@dataclass(frozen=True)
class ExtractedFrame:
    position: int
    timestamp_seconds: float
    path: Path
    size: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "timestamp_seconds": self.timestamp_seconds,
            "file_path": str(self.path),
            "size": self.size,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class ExtractedFrames:
    duration: float
    frames: tuple[ExtractedFrame, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "duration": self.duration,
            "frames": [frame.to_dict() for frame in self.frames],
        }


def _binary(name: str) -> str:
    value = shutil.which(name)
    if value is None:
        raise FrameExtractionError(
            f"{name}_missing",
            f"{name} не найден; установи FFmpeg и повтори обработку",
            retryable=True,
        )
    return value


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        if os.name == "nt":
            process.terminate()
        else:
            os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=_PROCESS_STOP_TIMEOUT)
        return
    except TimeoutError:
        pass
    try:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=_PROCESS_STOP_TIMEOUT)
    except TimeoutError:
        # There is nothing else the daemon can safely do cross-platform.  The
        # process handle remains reaped by asyncio if the OS reports exit later.
        pass


async def _run(
    arguments: list[str],
    *,
    timeout: float,
    code: str,
) -> tuple[int, bytes, bytes]:
    options: dict[str, object] = {
        "stdin": asyncio.subprocess.DEVNULL,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if os.name != "nt":
        options["start_new_session"] = True
    else:
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        process = await asyncio.create_subprocess_exec(*arguments, **options)
    except FileNotFoundError as exc:
        raise FrameExtractionError(
            f"{code}_missing",
            f"{Path(arguments[0]).name} не найден; установи FFmpeg и повтори обработку",
            retryable=True,
        ) from exc
    if sys.platform.startswith("linux") and hasattr(os, "setpriority"):
        try:
            os.setpriority(os.PRIO_PROCESS, process.pid, 10)
        except (OSError, PermissionError):
            # Nice priority is a best-effort resource guard, never a reason to
            # make optional local frame extraction unavailable.
            pass
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.CancelledError:
        await _terminate_process(process)
        raise
    except TimeoutError:
        await _terminate_process(process)
        raise FrameExtractionError(
            f"{code}_timeout",
            "FFmpeg не завершил обработку вовремя",
            retryable=True,
        ) from None
    return int(process.returncode or 0), stdout, stderr


async def probe_video(path: str | Path, *, timeout: float = 30.0) -> dict[str, Any]:
    """Return actual duration and the first video stream dimensions."""
    selected = Path(path)
    try:
        stat = selected.stat()
        if not selected.is_file() or stat.st_size <= 0:
            raise OSError
    except OSError as exc:
        raise FrameExtractionError(
            "source_missing", "Исходный MP4 недоступен локально", retryable=True
        ) from exc
    ffprobe = _binary("ffprobe")
    code, stdout, stderr = await _run(
        [
            ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(selected),
        ],
        timeout=timeout,
        code="ffprobe",
    )
    if code != 0:
        raise FrameExtractionError(
            "invalid_video",
            "Не удалось прочитать видеоконтейнер или видеопоток",
            detail=stderr.decode("utf-8", "replace"),
        )
    try:
        data = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FrameExtractionError(
            "invalid_video", "FFprobe вернул некорректные сведения о видео"
        ) from exc
    if not isinstance(data, dict):
        raise FrameExtractionError("invalid_video", "В файле нет видеопотока")
    streams = data.get("streams")
    video_stream = next(
        (
            item
            for item in (streams or [])
            if isinstance(item, dict) and item.get("codec_type") == "video"
        ),
        None,
    )
    if video_stream is None:
        raise FrameExtractionError("no_video_stream", "В файле нет видеопотока")
    format_info = data.get("format") if isinstance(data.get("format"), dict) else {}
    duration_value = format_info.get("duration") or video_stream.get("duration")
    try:
        duration = float(duration_value)
    except (TypeError, ValueError) as exc:
        raise FrameExtractionError(
            "invalid_duration", "Не удалось определить фактическую длительность видео"
        ) from exc
    if not (duration > 0.0) or duration != duration or duration == float("inf"):
        raise FrameExtractionError(
            "invalid_duration", "Фактическая длительность видео некорректна"
        )
    return {
        "duration": duration,
        "width": int(video_stream.get("width") or 0),
        "height": int(video_stream.get("height") or 0),
        "codec": str(video_stream.get("codec_name") or ""),
    }


def _candidate_times(duration: float, position: float) -> list[float]:
    target = max(0.0, min(duration * position, max(0.0, duration - 0.001)))
    step = max(0.005, min(0.25, duration * 0.05))
    offsets = (0.0, step, -step, 2 * step, -2 * step, 4 * step, -4 * step)
    values: list[float] = []
    for offset in offsets:
        value = round(max(0.0, min(duration - 0.001, target + offset)), 4)
        if value >= 0.0 and value not in values:
            values.append(value)
    return values


async def _extract_one(
    ffmpeg: str,
    video: Path,
    destination: Path,
    timestamp: float,
    *,
    timeout: float,
) -> None:
    code, _stdout, stderr = await _run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-threads",
            "1",
            "-filter_threads",
            "1",
            "-i",
            str(video),
            "-ss",
            f"{timestamp:.4f}",
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-threads",
            "1",
            "-an",
            "-sn",
            "-vf",
            _SCALE_FILTER,
            "-q:v",
            "2",
            "-f",
            "image2",
            str(destination),
        ],
        timeout=timeout,
        code="ffmpeg",
    )
    if code != 0:
        raise FrameExtractionError(
            "decode_failed",
            "Не удалось декодировать кадр видео",
            detail=stderr.decode("utf-8", "replace"),
        )


async def extract_frames(
    video_path: str | Path,
    destination: str | Path,
    *,
    timeout: float = DEFAULT_FRAME_TIMEOUT,
) -> ExtractedFrames:
    """Extract three distinct, real JPEG frames at 20/50/80 percent.

    ``destination`` is a private temporary directory owned by the caller.  This
    function never touches the source MP4 and never renames the final directory.
    """
    video = Path(video_path)
    output = Path(destination)
    output.mkdir(parents=True, exist_ok=True)
    probe = await probe_video(video)
    duration = float(probe["duration"])
    if duration < 0.06:
        raise FrameExtractionError(
            "too_short",
            "Видео слишком короткое для трёх разных кадров",
        )
    ffmpeg = _binary("ffmpeg")
    used_digests: set[str] = set()
    frames: list[ExtractedFrame] = []
    for position, factor in enumerate(FRAME_POSITIONS, start=1):
        selected: ExtractedFrame | None = None
        last_error: FrameExtractionError | None = None
        destination_file = output / f"frame_{position:02d}.jpg"
        for timestamp in _candidate_times(duration, factor):
            destination_file.unlink(missing_ok=True)
            try:
                await _extract_one(
                    ffmpeg,
                    video,
                    destination_file,
                    timestamp,
                    timeout=timeout,
                )
            except FrameExtractionError as exc:
                last_error = exc
                continue
            try:
                data = destination_file.read_bytes()
            except OSError:
                continue
            digest = hashlib.sha256(data).hexdigest()
            if (
                len(data) < 128
                or not data.startswith(b"\xff\xd8\xff")
                or digest in used_digests
            ):
                destination_file.unlink(missing_ok=True)
                continue
            used_digests.add(digest)
            selected = ExtractedFrame(
                position=position,
                timestamp_seconds=float(timestamp),
                path=destination_file,
                size=len(data),
                sha256=digest,
            )
            break
        if selected is None:
            if last_error is not None and last_error.code in {
                "decode_failed",
                "ffmpeg_timeout",
                "ffmpeg_missing",
            }:
                raise last_error
            raise FrameExtractionError(
                "insufficient_distinct_frames",
                "Не удалось извлечь три разных реальных кадра",
            )
        frames.append(selected)
    return ExtractedFrames(duration=duration, frames=tuple(frames))
